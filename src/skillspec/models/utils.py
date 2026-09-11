"""Helpers that lift structured payloads out of raw LLM replies."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping

from .errors import RetryableError


def transitive_closure(seeds: Iterable[str], neighbors: Mapping[str, Iterable[str]]) -> set[str]:
    """All nodes reachable from `seeds` via the `neighbors` adjacency (seeds included); cycle-safe."""
    acc: set[str] = set()
    frontier = list(seeds)
    while frontier:
        node = frontier.pop()
        if node in acc:
            continue
        acc.add(node)
        frontier.extend(neighbors.get(node, ()))
    return acc


def upward_paths(
    start: str,
    preds: Mapping[str, Iterable[str]],
    *,
    keep: Callable[[str], bool] | None = None,
    max_paths: int = 6,
    max_depth: int = 16,
) -> list[list[str]]:
    """Enumerate cycle-free predecessor chains, bounded by count and depth.

    Chains are ordered upstream-first, with `start` last. `keep` filters predecessors.
    """
    out: list[list[str]] = []

    def walk(chain: list[str], seen: frozenset[str]) -> None:
        if len(out) >= max_paths:
            return
        ups = sorted(p for p in preds.get(chain[0], ()) if p not in seen and (keep is None or keep(p)))
        if not ups or len(chain) >= max_depth:
            out.append(chain)
            return
        for up in ups:
            walk([up, *chain], seen | {up})

    walk([start], frozenset({start}))
    return out[:max_paths]


_THINK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> str:
    """First complete JSON object in an LLM reply; '' on miss.

    Strips <think> reasoning, then walks each '{' with json.raw_decode — this skips stray braces in
    surrounding prose and stops at the object's true end, unlike a greedy first-{…-last-} regex.
    """
    text = _THINK.sub("", text)
    decoder = json.JSONDecoder()
    idx = text.find("{")
    while idx != -1:
        try:
            _, end = decoder.raw_decode(text, idx)
            return text[idx:end]
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
    return ""


def json_object(text: str, what: str) -> dict:
    """Extract one JSON object from an LLM reply; RetryableError if absent or not an object."""
    candidate = extract_json(text)
    if not candidate:
        raise RetryableError("no JSON object found in the reply")
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as err:
        raise RetryableError(f"invalid {what} JSON: {err}") from err
    if not isinstance(obj, dict):
        raise RetryableError(f"{what} JSON is not an object")
    return obj


def clean_xml(text: str) -> str:
    """Lift the <workflow>…</workflow> block from a response, or the stripped text if absent."""
    m = re.search(r'(<workflow\b.*?</workflow>)', text, re.DOTALL)
    return m.group(1) if m else text.strip()


_LINE_NO = re.compile(r"^\d+:[ ]?", re.MULTILINE)


def strip_line_numbers(content: str) -> str:
    """Remove the `<n>: ` prefix the manifest body is rendered with (render._number_lines)."""
    return _LINE_NO.sub("", content)
