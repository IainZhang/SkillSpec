"""StageRunner — telemetry accumulation and structured stage logging."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from pydantic import BaseModel, Field

from skillspec.config import CONFIG
from skillspec.models import EventCategory, EventLevel, StageStats, Telemetry
from skillspec.trace import tracer

logger = logging.getLogger("skillspec.pipelines.pipeline")


class StageRunner(BaseModel):
    """Per-stage telemetry and structured logging."""

    telemetry: Telemetry = Field(default_factory=Telemetry)

    @contextmanager
    def _stage(self, name: str) -> Iterator[tuple[object | None, StageStats]]:
        with tracer.span(name) as span:
            stats = self.telemetry.stage(name, CONFIG.llm.model)
            try:
                yield span, stats
            finally:
                tracer.annotate(span, stats)

    @contextmanager
    def _substage(self, stats: StageStats, fail_msg: str) -> Iterator[None]:
        """Record substage exceptions without propagating them."""
        try:
            yield
        except Exception:
            self._fail(stats, fail_msg, exc=True)

    def _emit(
        self,
        stats: StageStats | None,
        level: EventLevel,
        msg: str,
        *,
        category: EventCategory = "",
        exc: bool = False,
    ) -> None:
        """Unified log+event sink: emit to logger and (if stats given) record_event."""
        if exc:
            logger.exception(msg)
        else:
            getattr(logger, level)(msg)
        if stats is not None:
            stats.record_event(level, msg, category)

    def _info(self, stats: StageStats | None, msg: str) -> None:
        self._emit(stats, "info", msg, category="ok")

    def _skip(self, stats: StageStats | None, msg: str) -> None:
        self._emit(stats, "info", msg, category="skip")

    def _warn(self, stats: StageStats | None, msg: str) -> None:
        self._emit(stats, "warning", msg, category="skip")

    def _fail(self, stats: StageStats, msg: str, *, exc: bool = False, n: int = 1) -> None:
        stats.record_failures(n)
        self._emit(stats, "error", msg, category="fail", exc=exc)
