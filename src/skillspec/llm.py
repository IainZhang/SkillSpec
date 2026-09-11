"""LLM client: provider calls, concurrency, and the bounded parse/correction retry loop."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, Iterator, TypeVar
from urllib.parse import urlparse

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from skillspec.config import CONFIG
from skillspec.models import RetryableError
from skillspec.trace import tracer

if TYPE_CHECKING:  # Prompt/Parsable are annotation-only
    from skillspec.prompts.prompt import Parsable, Prompt

logger = logging.getLogger(__name__)
M = TypeVar("M", bound="Parsable")
_MAX_PARSE = 3  # parse/correct attempts incl. the first
_next_timestamp = 0.0
# Loop-scoped shared state: asyncio primitives and the httpx connection pool bind to the running
# event loop, so a fresh asyncio.run() in the same process must get fresh instances.
_loop_state: tuple[asyncio.AbstractEventLoop, asyncio.Lock, asyncio.Semaphore, AsyncOpenAI] | None = None


def _loop_llm() -> tuple[asyncio.Lock, asyncio.Semaphore, AsyncOpenAI]:
    """The running loop's (pacer lock, concurrency semaphore, client), built on first use per loop."""
    global _loop_state
    loop = asyncio.get_running_loop()
    if _loop_state is None or _loop_state[0] is not loop:
        _loop_state = (loop, asyncio.Lock(), asyncio.Semaphore(CONFIG.llm.concurrency), CONFIG.llm.client())
    return _loop_state[1], _loop_state[2], _loop_state[3]


async def _pace() -> None:
    """Space consecutive sends ≥ min_request_interval apart."""
    global _next_timestamp
    interval = CONFIG.llm.min_request_interval
    if interval <= 0:
        return
    lock, _, _ = _loop_llm()
    async with lock:
        now = time.monotonic()
        wait = max(0.0, _next_timestamp - now)
        _next_timestamp = max(now, _next_timestamp) + interval
    if wait > 0:
        await asyncio.sleep(wait)


def _reply(message) -> tuple[str, str]:
    """(reply text, tool name): tool-call arguments when present, else plain content (XML/no-tool path)."""
    if message is None:
        return "", ""
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls and tool_calls[0].function.arguments:
        return tool_calls[0].function.arguments, tool_calls[0].function.name or ""
    return message.content or "", ""


class LLMResponse(BaseModel):
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    finish_reason: str = ""  # "length" truncation is a real failure mode on these long JSON replies
    tool_name: str = ""      # the tool the reply came back through; "" on the content fallback

    @classmethod
    def from_completion(cls, resp) -> "LLMResponse":
        """Extract text + token usage from a chat-completion response (defensive on missing choices/usage)."""
        choice = resp.choices[0] if resp.choices else None  # empty choices -> empty text -> parse-miss retry
        message = choice.message if (choice and choice.message) else None
        usage = resp.usage
        details = getattr(usage, "prompt_tokens_details", None) if usage else None
        text, tool_name = _reply(message)
        return cls(
            text=text,
            tool_name=tool_name,
            finish_reason=(choice.finish_reason or "") if choice else "",
            input_tokens=(usage.prompt_tokens or 0) if usage else 0,
            output_tokens=(usage.completion_tokens or 0) if usage else 0,
            cached_tokens=(details.cached_tokens or 0) if details is not None else 0,
        )


@dataclass
class Round:
    resp: LLMResponse | None = None
    error: str = ""


@dataclass
class BatchResult(Generic[M]):
    parsed: list[M | None] = field(default_factory=list)
    responses: list[LLMResponse] = field(default_factory=list)
    failures: int = 0


class GenAICall(BaseModel):
    """One inference attempt, keyed by OpenTelemetry GenAI semantic-convention attribute names."""

    model_config = ConfigDict(populate_by_name=True)

    operation: str = Field("chat", alias="gen_ai.operation.name")
    provider: str = Field("", alias="gen_ai.provider.name")  # gen_ai.system is deprecated in favor of this
    request_model: str = Field("", alias="gen_ai.request.model")
    server_address: str = Field("", alias="server.address")
    input_messages: list[dict] = Field(default_factory=list, alias="gen_ai.input.messages")
    output_messages: list[dict] = Field(default_factory=list, alias="gen_ai.output.messages")
    finish_reasons: list[str] = Field(default_factory=list, alias="gen_ai.response.finish_reasons")
    input_tokens: int = Field(0, alias="gen_ai.usage.input_tokens")
    output_tokens: int = Field(0, alias="gen_ai.usage.output_tokens")
    # everything the conventions don't cover (cached tokens, our labels) stays namespaced
    meta: dict = Field(default_factory=dict, alias="skillspec")


# Collect inference records only while a recorder is installed.
_recorder: ContextVar[list[GenAICall] | None] = ContextVar("llm_recorder", default=None)


@contextmanager
def record_calls() -> Iterator[list[GenAICall]]:
    """Collect every inference attempt made inside this block, including inside tasks it spawns."""
    # gather() copies the ambient context into each task, and the list is mutated in place
    # (never rebound), so batched attempts land here too.
    calls: list[GenAICall] = []
    token = _recorder.set(calls)
    try:
        yield calls
    finally:
        _recorder.reset(token)


def _provider() -> str:
    """The GenAI provider name: the model's family when recognizable, else the endpoint's host label."""
    model = CONFIG.llm.model.lower()
    if "gpt" in model:
        return "openai"
    family = next((f for f in _EFFORT if f in model), None)
    if family:
        return family
    host = urlparse(CONFIG.llm.url).hostname or ""
    parts = [p for p in host.split(".") if p not in ("www", "api", "com", "ai", "io", "cn")]
    return parts[-1] if parts else host


def _text_part(content: str) -> dict:
    return {"type": "text", "content": content}


def _output_message(resp: LLMResponse) -> dict:
    """The reply as a GenAI output message; a tool-call reply keeps its structure instead of stringifying."""
    if resp.tool_name:
        try:
            arguments = json.loads(resp.text)
        except json.JSONDecodeError:  # a malformed tool call is exactly what a parse miss looks like
            arguments = resp.text
        part = {"type": "tool_call", "name": resp.tool_name, "arguments": arguments}
    else:
        part = _text_part(resp.text)
    return {"role": "assistant", "parts": [part], "finish_reason": resp.finish_reason}


def _record(messages: list[dict], resp: LLMResponse | None, *, label: str, attempt: int, error: str) -> None:
    """Append one attempt to the active recorder (no-op when none is installed)."""
    sink = _recorder.get()
    if sink is None:
        return
    sink.append(
        GenAICall(
            provider=_provider(),
            request_model=CONFIG.llm.model,
            server_address=urlparse(CONFIG.llm.url).hostname or "",
            input_messages=[{"role": m["role"], "parts": [_text_part(m["content"])]} for m in messages],
            output_messages=[_output_message(resp)] if resp else [],
            finish_reasons=[resp.finish_reason] if resp and resp.finish_reason else [],
            input_tokens=resp.input_tokens if resp else 0,
            output_tokens=resp.output_tokens if resp else 0,
            meta={
                "label": label,
                "attempt": attempt,
                "cached_tokens": resp.cached_tokens if resp else 0,
                "error": error,
            },
        )
    )


# Model-family effort mappings; unrecognized values pass through unchanged.
_EFFORT = {
    "deepseek": {"low": "high", "medium": "high", "high": "high", "max": "max"},
    "gpt":      {"low": "low",  "medium": "medium", "high": "high", "max": "xhigh"},
}

_TOOL_CHOICE = "auto"


def _reasoning_effort() -> str | None:
    """Map the canonical effort to the active model family's accepted token."""
    effort = CONFIG.llm.reasoning_effort
    if not effort:
        return None
    model = CONFIG.llm.model.lower()
    family = next((f for f in _EFFORT if f in model), None)
    return _EFFORT[family].get(effort, effort) if family else effort


def _messages(system: str, user: str, replay: list[dict] | None = None) -> list[dict]:
    """The message array as sent: system, the replayed miss/correction turns, then the current user."""
    return [{"role": "system", "content": system}, *(replay or []), {"role": "user", "content": user}]


async def _call(
    *,
    system: str,
    user: str,
    replay: list[dict] | None = None,
    tool: dict | None = None,
) -> LLMResponse:
    messages = _messages(system, user, replay)

    kwargs: dict = {"model": CONFIG.llm.model, "messages": messages}
    effort = _reasoning_effort()
    if effort:
        kwargs["reasoning_effort"] = effort
    if tool:  # offer the schema's tool (tool_choice=auto); reply JSON arrives in tool args or content
        kwargs["tools"] = [tool]
        kwargs["tool_choice"] = _TOOL_CHOICE

    _, semaphore, client = _loop_llm()
    async with semaphore:
        await _pace()
        resp = await client.chat.completions.create(**kwargs)
    return LLMResponse.from_completion(resp)


async def run(
    prompt: "Prompt[M]",
    user: str,
    *,
    label: str | None = None,
) -> tuple[M | None, list[Round]]:
    """Call and parse with bounded correction retries; retain responses and request errors."""
    # On RetryableError the failed attempt is replayed with schema.correction(); provider errors
    # become a failed Round so callers keep the usage already spent on earlier rounds.
    tool = prompt.schema.tool()
    replay: list[dict] = []  # failed attempts replayed back on each correction round
    current = user
    rounds: list[Round] = []
    result: M | None = None
    # label opens a per-unit CHAIN span so retries + ChatCompletion nest under it
    with (tracer.span(label) if label else nullcontext(None)) as node_span:
        for attempt in range(_MAX_PARSE):
            sent = _messages(prompt.system, current, replay)  # exactly what _call will send
            try:
                resp = await _call(
                    system=prompt.system, user=current, replay=replay, tool=tool
                )
            except Exception as err:  # provider/network failure after the client's own retries
                logger.warning("request failed (attempt %d/%d): %s", attempt + 1, _MAX_PARSE, err)
                rounds.append(Round(error=str(err)))
                _record(sent, None, label=label or "", attempt=attempt, error=str(err))
                break
            try:
                result = prompt.schema.parse(resp.text)
                rounds.append(Round(resp=resp))
                _record(sent, resp, label=label or "", attempt=attempt, error="")
                break
            except RetryableError as err:
                logger.warning("parse miss (attempt %d/%d): %s", attempt + 1, _MAX_PARSE, err)
                rounds.append(Round(resp=resp, error=str(err)))
                _record(sent, resp, label=label or "", attempt=attempt, error=str(err))
                replay = [
                    *replay,
                    {"role": "user", "content": current},
                    {"role": "assistant", "content": resp.text},
                ]
                current = prompt.schema.correction(str(err))
        if result is None:
            logger.warning("request unresolved after %d attempt(s) — giving up.", len(rounds))
        tracer.annotate_node(node_span, label or "", [r.resp for r in rounds if r.resp])
        return result, rounds


def _collect(results: list["tuple[M | None, list[Round]] | BaseException"]) -> "BatchResult[M]":
    """Pool per-request outcomes, counting raises and unresolved parses as failures."""
    out: BatchResult[M] = BatchResult()
    for i, res in enumerate(results):
        if isinstance(res, BaseException):
            logger.warning("run_serial: request %d raised — %s", i, res)
            out.parsed.append(None)
            out.failures += 1
            continue
        product, rounds = res
        out.responses.extend(r.resp for r in rounds if r.resp is not None)
        out.parsed.append(product)
        if product is None:
            out.failures += 1
            if rounds:
                logger.warning("run_serial: request %d unresolved — %s", i, rounds[-1].error)
    logger.debug(
        "run_serial: %d request(s), %d failure(s), %d tokens.",
        len(results), out.failures, sum(r.input_tokens + r.output_tokens for r in out.responses),
    )
    return out


async def run_serial(
    prompt: "Prompt[M]",
    users: list[str],
    *,
    labels: list[str] | None = None,
) -> "BatchResult[M]":
    """Run prompts sequentially to allow prefix-cache reuse."""
    spans = labels if labels is not None else [None] * len(users)
    results: list = []
    for user, label in zip(users, spans):
        try:
            results.append(await run(prompt, user, label=label))
        except Exception as err:
            results.append(err)
    return _collect(results)
