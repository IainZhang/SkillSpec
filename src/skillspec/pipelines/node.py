"""Per-unit analysis node: holds context + specs/verdict and runs its own spec gen/reason."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated

import tiktoken
from pydantic import BaseModel, Field

from skillspec.config import CONFIG
from skillspec.llm import LLMResponse, run, run_serial
from skillspec.models import (
    CodeContext,
    StepContext,
    UnitSpec,
    Verdict,
    VerifyKind,
    View,
)
from skillspec.prompts import REASON, SPEC

logger = logging.getLogger(__name__)

MIN_REASON_LINES = 8  # too short for func
MIN_REASON_TOKENS = 30  # too short for workflow instruction


@lru_cache(maxsize=1)
def _encoder() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def _token_count(text: str) -> int:
    return len(_encoder().encode(text))


def _fact_views() -> list[View]:
    return list(CONFIG.spec.fact_views)


def _visible_unit_ids(ctx: StepContext | CodeContext) -> set[str]:
    """Every unit id this node's rendered context actually names — the candidate set an
    LLM-reported location_unit_id is allowed to resolve against (see _resolve_location_unit)."""
    ids = {ctx.unit_id, *(s.name for s in ctx.holistic_ctx)}
    if isinstance(ctx, StepContext):
        ids.update(s.name for s in ctx.lineage)
        ids.update(s.name for s in ctx.neighbors)
        ids.update(s.name for s in ctx.downstream)
        ids.update(n.id for n in ctx.linked)
    else:
        ids.update(n.id for n in ctx.lineage)
        ids.update(n.id for n in ctx.neighbors)
        ids.update(s.name for s in ctx.linked)
        ids.update(s.name for s in ctx.step_lineage)
    return ids


def _resolve_location_unit(raw: str, unit_id: str, candidates: set[str]) -> str:
    """Resolve a reported location against visible candidates, defaulting to the current unit."""
    if raw in candidates:
        return raw
    norm = raw.strip().casefold()
    for cand in candidates:
        if cand.strip().casefold() == norm:
            return cand
    suffix_matches = [c for c in candidates if c.rsplit("::", 1)[-1].strip().casefold() == norm]
    if len(suffix_matches) == 1:
        return suffix_matches[0]
    return unit_id


class Node(BaseModel):
    """One unified-graph unit: context (input) + specs/verdict (output)."""

    unit_id: str
    context: Annotated[StepContext | CodeContext, Field(discriminator="ctx_kind")]
    expect: UnitSpec | None = None  # ExpectSpec (Full View)
    facts: dict[View, UnitSpec] = Field(default_factory=dict)  # FactSpec per masked view
    verdict: Verdict | None = None  # this node's own judgment
    # Findings whose location_unit_id binds them here — this node's own plus any other node's;
    # filled by UnifiedGraph.bind_defects, never by the node itself.
    defects: list[Verdict] = Field(default_factory=list)

    @property
    def in_scope(self) -> bool:
        """False for structural or trivially-short nodes (skipped by spec-gen/reason)."""
        ctx = self.context
        if isinstance(ctx, StepContext):
            if ctx.entity.is_structural:
                return False
            # ref_code steps bind workflow to code — always analyzed
            return ctx.entity.is_ref_code or _token_count(ctx.entity.instruction) >= MIN_REASON_TOKENS
        return ctx.entity.code_lines >= MIN_REASON_LINES

    @property
    def kind(self) -> VerifyKind:
        """Defect origin: workflow steps vs linked code definitions."""
        return "workflow" if isinstance(self.context, StepContext) else "code"

    async def analyze(self, resources: dict[str, str], *, gen: bool) -> tuple[list[LLMResponse], int]:
        """Generate specs when requested, then reason over them; pooled responses/failures."""
        responses, failures = await self.spec_gen() if gen else ([], 0)
        if failures or not self.spec_complete:
            return responses, failures or 1
        reason_responses, reason_failures = await self.spec_reason(resources=resources)
        return responses + reason_responses, failures + reason_failures

    @property
    def spec_complete(self) -> bool:
        """All configured views and the expectation must exist before reasoning."""
        return self.expect is not None and all(v in self.facts for v in _fact_views())

    async def spec_gen(self) -> tuple[list[LLMResponse], int]:
        """Generate the node's ExpectSpec (Full View) plus a FactSpec per masked view."""
        # The entire spec is one retry unit; never retain products from a previous attempt.
        self.expect = None
        self.facts = {}
        self.verdict = None
        views = _fact_views()
        # Share one factual extraction across views with identical prompts.
        groups: dict[str, list[View]] = {}
        for view in views:
            groups.setdefault(SPEC.fact(self.context, view), []).append(view)

        # Run factual prompts first, then the expectation prompt.
        out = await run_serial(
            SPEC,
            [*groups, SPEC.expect(self.context)],
            labels=[*(f"gen:{'+'.join(v.value for v in vs)}" for vs in groups.values()), "gen:expect"],
        )
        for product in out.parsed:
            if product is not None:
                product.unit_id = self.unit_id  # stamp the real id (parse left it "")
        *facts, expect = out.parsed
        if expect is not None:
            self.expect = expect
        for vs, product in zip(groups.values(), facts):
            if product is not None:
                self.facts.update(dict.fromkeys(vs, product))
        logger.debug(
            "spec_gen[%s]: expect=%s, facts=%d, shared=%d.",
            self.unit_id, self.expect is not None, len(self.facts), len(views) - len(groups),
        )
        return out.responses, out.failures

    async def spec_reason(self, *, resources: dict[str, str]) -> tuple[list[LLMResponse], int]:
        """Compare FactSpecs with ExpectSpec, then validate mismatches against raw source."""
        facts = {v: self.facts[v] for v in _fact_views() if v in self.facts}
        if self.expect is None:
            logger.debug("spec_reason[%s]: missing ExpectSpec — skipping.", self.unit_id)
            return [], 0
        verdict, rounds = await run(
            REASON, REASON.build(self.context, expect=self.expect, facts=facts, resources=resources),
            label=f"judge:{self.unit_id}",
        )
        responses = [r.resp for r in rounds if r.resp]
        if verdict is None:
            return responses, 1
        verdict.unit_id = self.unit_id
        if verdict.decision:
            verdict.location_unit_id = _resolve_location_unit(
                verdict.location_unit_id, self.unit_id, _visible_unit_ids(self.context)
            )
        self.verdict = verdict
        logger.debug("spec_reason[%s]: decision=%s.", self.unit_id, verdict.decision)
        return responses, 0
