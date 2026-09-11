"""Spec-reason prompt: compare FactSpecs with ExpectSpec, then validate mismatches."""

from __future__ import annotations

from skillspec.models import NodeContext, UnitSpec, Verdict, View
from skillspec.prompts.prompt import Prompt, read_md
from skillspec.prompts.render import reason_context, spec_block


class ReasonPrompt(Prompt[Verdict]):
    system = read_md("reason_sys")
    template = read_md("reason_user")
    schema = Verdict

    def build(
        self,
        ctx: NodeContext,
        *,
        expect: UnitSpec,
        facts: dict[View, UnitSpec],
        resources: dict[str, str],
    ) -> str:
        # validation is never masked: full view, implementation, and scoped source visible
        context = reason_context(ctx, resources)
        expect_block = spec_block(expect, "ExpectSpec")
        # one block per distinct spec, labeled with every view it stands for; group by
        # value — a reloaded specs.jsonl loses object identity
        merged: dict[tuple, tuple[UnitSpec, list[View]]] = {}
        for view, spec in facts.items():
            key = (spec.name, tuple(spec.pre), tuple(spec.post), tuple(spec.effects))
            merged.setdefault(key, (spec, []))[1].append(view)
        fact_blocks = "\n".join(spec_block(s, "FactSpec", views=vs) for s, vs in merged.values())
        return self.render(
            self.template, Context=context, ExpectSpec=expect_block, FactSpec=fact_blocks
        )


REASON = ReasonPrompt()
