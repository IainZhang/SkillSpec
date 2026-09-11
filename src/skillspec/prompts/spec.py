"""Spec-inference prompt: derive a unit's Hoare spec from a masked context view."""

from __future__ import annotations

from skillspec.models import NodeContext, UnitSpec, View
from skillspec.prompts.prompt import Prompt, read_md
from skillspec.prompts.render import expect_context, fact_context

_EXPECT_STANCE = (
    "**ExpectSpec — intended specification.** The implementation is hidden; ground every predicate in what the context normatively requires the Unit to do. Examples, sample outputs, quick starts, tutorials, and recommendations are illustrative — they may clarify a requirement but never create, specialize, or narrow one. Promote their concrete details into a predicate only when normative text independently requires it."
)

_FACT_STANCE = (
    "**FactSpec — actual specification.** The implementation is visible under this view's mask; treat the visible fragment as evidence, not as something to interpret. Specify what it actually does, including behavior that looks wrong — do not import surrounding intent to explain it away, and do not repair a suspected defect. Where the mask withholds the source, fall back to the public metadata alone; do not reconstruct a body you cannot see."
)


class SpecPrompt(Prompt[UnitSpec]):
    system = read_md("spec_sys")
    template = read_md("spec_user")
    schema = UnitSpec

    def expect(self, ctx: NodeContext) -> str:
        """Build the expectation prompt from the node context."""
        return self.render(self.template, Context=expect_context(ctx), Stance=_EXPECT_STANCE)

    def fact(self, ctx: NodeContext, view: View) -> str:
        """FactSpec: actual behavior inferred from a partial implementation view."""
        return self.render(self.template, Context=fact_context(ctx, view), Stance=_FACT_STANCE)


SPEC = SpecPrompt()
