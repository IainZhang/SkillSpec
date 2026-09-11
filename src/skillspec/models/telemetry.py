"""Per-stage token/cache accounting for one run."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from pydantic import BaseModel, Field

EventLevel = Literal["info", "warning", "error"]
EventCategory = Literal["", "skip", "fail", "ok"]


class StageEvent(BaseModel):
    """One structured log/error event emitted during a stage."""

    level: EventLevel = "info"
    category: EventCategory = ""
    message: str


class StageStats(BaseModel):
    """One stage's accumulator: LLM calls, token/cache totals, failures, and events."""

    stage: str  # "build_graph" | "build_link" | "build_mask" | "reason" | "verify" | <node label> | <aggregate label>
    model: str = ""
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0  # subset of input_tokens
    failures: int = 0  # agent calls that errored after retries
    events: list[StageEvent] = Field(default_factory=list)

    def record_responses(self, responses: Iterable[object]) -> None:
        """Fold a batch of LLM responses (duck-typed token fields) into totals."""
        for resp in responses:
            self.calls += 1
            self.input_tokens += resp.input_tokens
            self.output_tokens += resp.output_tokens
            self.cached_tokens += resp.cached_tokens

    def record_event(
        self, level: EventLevel, message: str, category: EventCategory = ""
    ) -> None:
        """Append a structured log/error event for this stage."""
        self.events.append(StageEvent(level=level, category=category, message=message))

    def record_failures(self, n: int = 1) -> None:
        """Note n agent calls that failed after retries."""
        self.failures += n

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of input tokens served from cache (0.0 when nothing was sent)."""
        return self.cached_tokens / self.input_tokens if self.input_tokens else 0.0


class Telemetry(BaseModel):
    """Per-run collection of stage accumulators."""

    stats: dict[str, StageStats] = Field(default_factory=dict)

    def stage(self, name: str, model: str = "") -> StageStats:
        """Get-or-create the stats accumulator for stage `name`."""
        st = self.stats.get(name)
        if st is None:
            st = StageStats(stage=name, model=model)
            self.stats[name] = st
        elif model and not st.model:
            st.model = model
        return st

    def aggregate(self, stage: str = "run") -> StageStats:
        """Roll up every per-stage accumulator into a total (default: whole run)."""
        agg = StageStats(stage=stage)
        for st in self.stats.values():
            agg.calls += st.calls
            agg.input_tokens += st.input_tokens
            agg.output_tokens += st.output_tokens
            agg.cached_tokens += st.cached_tokens
            agg.failures += st.failures
        return agg
