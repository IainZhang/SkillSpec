"""Observability via local Phoenix + OpenInference."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from skillspec.models import StageStats
    from skillspec.llm import LLMResponse

logger = logging.getLogger(__name__)


class Tracer:
    """Phoenix tracing lifecycle: register -> span -> annotate."""

    def __init__(self) -> None:
        self._enabled = False

    def register(self) -> None:
        from skillspec.config import CONFIG

        if not CONFIG.telemetry.enabled:
            logger.info("Telemetry export disabled.")
            return

        try:
            from phoenix.otel import register
            from openinference.instrumentation.openai import OpenAIInstrumentor
        except ImportError:
            logger.warning("Phoenix/OpenInference not installed — skipping tracing setup.")
            return

        try:  # telemetry is optional — a bad endpoint/config must not kill the analysis run
            provider = register(
                project_name=CONFIG.telemetry.project,
                endpoint=CONFIG.telemetry.endpoint,
                api_key=os.environ.get("PHOENIX_API_KEY") or CONFIG.telemetry.api_key or None,
            )
            OpenAIInstrumentor().instrument(tracer_provider=provider)
        except Exception:
            logger.warning("Phoenix tracing setup failed — continuing untraced.", exc_info=True)
            return
        self._enabled = True

    @contextmanager
    def span(self, name: str) -> Iterator[object | None]:
        """Open a Phoenix CHAIN span named `name`; yield None when tracing is off."""
        if not self._enabled:
            yield None
            return

        from opentelemetry import trace

        with trace.get_tracer("skillspec").start_as_current_span(name) as span:
            span.set_attribute("openinference.span.kind", "CHAIN")
            yield span

    def annotate(self, span: object | None, stats: "StageStats") -> None:
        if span is None:
            return

        attrs = {
            "llm.token_count.prompt": stats.input_tokens,
            "llm.token_count.completion": stats.output_tokens,
            "llm.token_count.total": stats.total_tokens,
            "llm.token_count.prompt_details.cache_read": stats.cached_tokens,
            "skillspec.model": stats.model,
            "skillspec.calls": stats.calls,
            "skillspec.failures": stats.failures,
        }
        for key, value in attrs.items():
            span.set_attribute(key, value)

    def annotate_node(
        self, span: object | None, unit_id: str, responses: "list[LLMResponse]"
    ) -> None:
        """Stamp one unit CHAIN span's token/call counts by transient StageStats."""
        if span is None:
            return
        from skillspec.models import StageStats

        stats = StageStats(stage=unit_id)
        stats.record_responses(responses)
        self.annotate(span, stats)


tracer = Tracer()
