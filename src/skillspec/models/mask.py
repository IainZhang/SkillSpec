"""Intent mask: the composable visibility layers/views, the per-stage reveal policy, and the
per-unit node context they project.

Visibility is decided by two independent gates, both defined here so one file answers
"what does a unit of kind K see at stage S under view V?":

  View  -> which neighbourhood *fields* are filled       (`_VIEW_LAYERS`)
  Stage -> what each filled field may *show*             (`_STAGE_POLICY`)

`prompts.render` applies these tables to the node context.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .codegraph import CodeNode
from .workflow import Step


class Layer(StrEnum):
    """One atomic source of contextual information; a View is a composition of layers."""

    HOLISTIC = "holistic"          # global skill context: skill name + overview + root/context nodes
    LINEAGE = "lineage"            # the root-to-node path: upstream intent and dependencies
    NEIGHBORS = "neighbors"        # the local task family: parent stage + siblings / caller + sibling callees
    SELF = "self"                  # target unit plus its directly linked cross-artifact counterpart


class View(StrEnum):
    """A composition of context layers; stage policy independently controls executable evidence."""

    SELF = "self"
    NEIGHBORS = "neighbors"
    LINEAGE = "lineage"
    HOLISTIC = "holistic"
    FULL = "full"

    @property
    def layers(self) -> frozenset[Layer]:
        """The atomic layers this view composes."""
        return _VIEW_LAYERS[self]


_VIEW_LAYERS: dict[View, frozenset[Layer]] = {
    View.SELF: frozenset({Layer.SELF}),
    View.NEIGHBORS: frozenset({Layer.NEIGHBORS, Layer.SELF}),
    View.LINEAGE: frozenset({Layer.LINEAGE, Layer.SELF}),
    View.HOLISTIC: frozenset({Layer.HOLISTIC, Layer.SELF}),
    View.FULL: frozenset({Layer.HOLISTIC, Layer.LINEAGE, Layer.NEIGHBORS, Layer.SELF}),
}

# Every view registers Self for target identity; registration is distinct from content visibility.
if any(Layer.SELF not in layers for layers in _VIEW_LAYERS.values()):
    raise ValueError("every View must include Layer.SELF: the renderer treats Self as unmaskable")


class Stage(StrEnum):
    """Which specification is being produced; a stage fixes what the mask reveals beyond its View."""

    EXPECT = "expect"  # workflow text retained; CodeNode source withheld
    FACT = "fact"      # actual contract from the visible implementation, under one masked view
    REASON = "reason"  # ExpectSpec/FactSpec adjudication: never masked, raw source attached


class Policy(BaseModel):
    """What one stage reveals once a View has decided which fields are filled."""

    model_config = ConfigDict(frozen=True)

    code_body: bool         # CodeNodes entries carry their source (else a withheld placeholder)
    step_shows_code: bool   # a step's Self names the code it invokes
    code_shows_steps: bool  # a code unit's Self names the steps that invoke it
    show_paths: bool         # Lineage carries ordered workflow/call paths (code units only)
    show_downstream: bool    # direct workflow dependency successors, never transitive
    show_skill: bool        # append <source>: skill manifest + relevant files for the current node


_STAGE_POLICY: dict[Stage, Policy] = {
    # EXPECT retains mixed intent/procedure text in workflow steps while masking CodeNode implementation.
    Stage.EXPECT: Policy(code_body=False, step_shows_code=False, code_shows_steps=True, show_paths=True, show_downstream=True, show_skill=False),
    Stage.FACT: Policy(code_body=True, step_shows_code=True, code_shows_steps=True, show_paths=False, show_downstream=False, show_skill=False),
    Stage.REASON: Policy(code_body=True, step_shows_code=True, code_shows_steps=True, show_paths=False, show_downstream=True, show_skill=True),
}


def policy(stage: Stage) -> Policy:
    """The reveal policy for one stage."""
    return _STAGE_POLICY[stage]


class NodeContext(BaseModel):
    """One unit's global context plus identity; subclasses add the typed neighbourhood."""

    unit_id: str
    skill_name: str = ""
    overview: str = ""
    manifest_body: str = ""  # SKILL.md body, rendered into <source> at prompt time
    relevant_resources: list[str] = Field(default_factory=list)  # node's scoped resource keys; content lives on UnifiedGraph
    language: str
    holistic_ctx: list[Step] = Field(default_factory=list)  # Holistic layer: root + context nodes (shared global)


class StepContext(NodeContext):
    """A workflow step's lineage, local family, direct successors, and linked code."""

    ctx_kind: Literal["step"] = "step"  # union discriminator for graph.json round-trip
    entity: Step
    lineage: list[Step] = Field(default_factory=list)      # Lineage: full root->node path (structural included), ordered root->node
    neighbors: list[Step] = Field(default_factory=list)    # Neighbors: parent stage + all its sibling steps (any kind)
    downstream: list[Step] = Field(default_factory=list)   # Direct dependency successors only (1-hop, any count)
    # Reachable code definitions, visible during factual extraction and reasoning.
    linked: list[CodeNode] = Field(default_factory=list)


class CodeContext(NodeContext):
    """A code definition's neighbourhood across visibility layers: caller lineage, local family, linked steps."""

    ctx_kind: Literal["code"] = "code"  # union discriminator for graph.json round-trip
    entity: CodeNode
    lineage: list[CodeNode] = Field(default_factory=list)    # Lineage: ancestor functions along the call path
    neighbors: list[CodeNode] = Field(default_factory=list)  # Neighbors: immediate caller + sibling callees
    linked: list[Step] = Field(default_factory=list)         # Self: the linked workflow step intent
    # The unified root->node intent path, rendered for ExpectSpec only (masked FactSpec views never
    # see it). The two path fields carry ordering as refs; their nodes are indexed once, as elsewhere.
    step_lineage: list[Step] = Field(default_factory=list)         # root->linked-step steps, topo-ordered
    workflow_paths: list[list[str]] = Field(default_factory=list)  # step-name chains; names resolve in `step_lineage`
    call_paths: list[list[str]] = Field(default_factory=list)      # code-id chains; ids resolve in `lineage` + `entity`
