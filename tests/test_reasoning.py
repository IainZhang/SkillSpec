from __future__ import annotations

import pytest

from skillspec.models import (
    CodeGraph,
    CodeNode,
    DefectGroup,
    DefKind,
    Metadata,
    NodeKind,
    RetryableError,
    SkillManifest,
    Step,
    StepContext,
    UnitSpec,
    Verdict,
    View,
    WorkflowGraph,
)
from skillspec.pipelines import Node, UnifiedGraph
from skillspec.pipelines.node import _resolve_location_unit, _visible_unit_ids
from skillspec.prompts import SPEC
from skillspec.prompts.render import expect_context, fact_context, reason_context


def test_parse_spec_extracts_json_fields():
    text = (
        "Here is the spec.\n"
        "```json\n"
        '{"unit": "U1 search", "pre": ["query is a non-empty string"], '
        '"post": ["results is a JSON array of records"], "effects": ["logs query"]}\n'
        "```\n"
        "trailing chatter\n"
    )
    spec = UnitSpec.parse(text)
    spec.unit_id = "U1"
    assert spec.unit_id == "U1"
    assert spec.name == "U1 search"
    assert spec.pre == ["query is a non-empty string"]
    assert spec.post == ["results is a JSON array of records"]
    assert spec.effects == ["logs query"]


def test_parse_spec_no_json_raises():
    text = "No JSON here, just prose about the unit."
    with pytest.raises(RetryableError):
        UnitSpec.parse(text)


def test_parse_verdict():
    v = Verdict.parse(
        '{"verdict": "Yes", "candidate_bug": "q=", '
        '"reason": "post breaks", "location": "ctx", "location_unit_id": "step1"}'
    )
    assert v.decision is True
    assert v.candidate_bug == "q="
    assert v.reason == "post breaks"
    assert v.location == "ctx"
    assert v.location_unit_id == "step1"
    properties = Verdict.tool()["function"]["parameters"]["properties"]
    assert "location_unit_id" in properties

    with pytest.raises(RetryableError):
        Verdict.parse(
            '{"verdict": "Yes", "location": "ctx", "location_unit_id": "step1"}'
        )

    with pytest.raises(RetryableError):
        Verdict.parse(
            '{"verdict": "Yes", "candidate_bug": "q=", '
            '"reason": "post breaks", "location": "ctx"}'
        )

    v = Verdict.parse('{"verdict": "No"}')
    assert v.decision is False
    assert v.candidate_bug == ""


def test_view_context_masks_each_layer():
    ctx = StepContext(
        unit_id="u", skill_name="demo", overview="OVERVIEW_TXT", language="python",
        entity=Step(name="u", instruction="do U"),
        holistic_ctx=[Step(name="root", instruction="ROOTDOC", kind=NodeKind.ROOT)],
        lineage=[Step(name="p", instruction="do P")],
        neighbors=[Step(name="sib", instruction="do SIB")],
    )
    full = expect_context(ctx)
    self_only = fact_context(ctx, View.SELF)
    lineage = fact_context(ctx, View.LINEAGE)
    neighbors = fact_context(ctx, View.NEIGHBORS)

    for token in ("demo", "OVERVIEW_TXT", "root"):
        assert token in full
        assert token not in self_only and token not in lineage and token not in neighbors
    assert "do P" in full and "do P" in lineage
    assert "do P" not in self_only and "do P" not in neighbors
    assert "do SIB" in full and "do SIB" in neighbors
    assert "do SIB" not in self_only and "do SIB" not in lineage
    assert all("do U" in body for body in (full, self_only, lineage, neighbors))


def test_self_reveals_downstream_code_only_when_requested():
    code = CodeNode(
        id="app.py::run", name="run", kind=DefKind.FUNCTION, file="app.py",
        start_line=1, end_line=2, snippet="def run(): return 1", code_lines=1,
    )
    ctx = StepContext(
        unit_id="u", skill_name="demo", language="python",
        entity=Step(name="u", instruction="do U"), linked=[code],
    )
    hidden = expect_context(ctx)
    revealed = fact_context(ctx, View.SELF)
    assert "def run" not in hidden
    assert "def run" in revealed


def test_downstream_is_all_direct_dependency_successors_only():
    xml = (
        '<workflow root="producer">'
        '<unit name="producer" type="plain">'
        '<next rel="dependency">consumer-a</next>'
        '<next rel="dependency">consumer-b</next>'
        '<next rel="contain">grouped</next>'
        '<content>produce the intermediate artifact</content></unit>'
        '<unit name="consumer-a" type="plain">'
        '<next rel="dependency">two-hops-away</next>'
        '<content>consume the artifact as JSON</content></unit>'
        '<unit name="consumer-b" type="plain"><content>consume the artifact as text</content></unit>'
        '<unit name="grouped" type="plain"><content>structurally contained node</content></unit>'
        '<unit name="two-hops-away" type="plain"><content>must stay two hops away</content></unit>'
        '</workflow>'
    )
    metadata = Metadata(manifest=SkillManifest(name="demo", description="demo skill"))
    unified = UnifiedGraph(workflow=WorkflowGraph.parse(xml), callgraph=CodeGraph())
    unified.build_mask(metadata)

    ctx = unified.nodes["producer"].context
    assert [s.name for s in ctx.downstream] == ["consumer-a", "consumer-b"]
    assert ctx.neighbors == []

    expected = expect_context(ctx)
    adjudication = reason_context(ctx, resources={})
    assert "<Downstream>consumer-a, consumer-b</Downstream>" in expected
    assert "<Downstream>consumer-a, consumer-b</Downstream>" in adjudication
    assert "consume the artifact as JSON" in expected
    assert "consume the artifact as text" in expected
    assert "structurally contained node" not in expected
    assert "must stay two hops away" not in expected
    for view in (View.SELF, View.NEIGHBORS, View.LINEAGE, View.HOLISTIC):
        factual = fact_context(ctx, view)
        assert "<Downstream>" not in factual
        assert "consume the artifact as JSON" not in factual
        assert "consume the artifact as text" not in factual

    visible = _visible_unit_ids(ctx)
    assert {"consumer-a", "consumer-b"} <= visible
    assert _resolve_location_unit("consumer-a", "producer", visible) == "consumer-a"


def test_build_mask_materializes_layers_and_spec_gen_masks_them():
    xml = (
        '<workflow root="root">'
        '<unit name="root" type="root"><next rel="contain">stage</next>'
        '<next rel="contain">ctx</next><content>intro</content></unit>'
        '<unit name="ctx" type="context"><content>notes</content></unit>'
        '<unit name="stage" type="stage"><next rel="contain">s1</next>'
        '<next rel="contain">s2</next><content>Stage A</content></unit>'
        '<unit name="s1" type="plain"><next rel="dependency">s2</next><content>do one</content></unit>'
        '<unit name="s2" type="plain"><content>do two</content></unit>'
        "</workflow>"
    )
    metadata = Metadata(manifest=SkillManifest(name="demo", description="demo skill"))
    unified = UnifiedGraph(workflow=WorkflowGraph.parse(xml), callgraph=CodeGraph())
    unified.build_mask(metadata)

    ctx = unified.nodes["s2"].context
    assert [s.name for s in ctx.holistic_ctx] == ["root", "ctx"]
    assert [s.name for s in ctx.lineage] == ["root", "stage", "s1"]
    assert [s.name for s in ctx.neighbors] == ["stage", "s1"]

    full = SPEC.expect(ctx)
    self_only = SPEC.fact(ctx, View.SELF)
    lineage = SPEC.fact(ctx, View.LINEAGE)
    assert "demo skill" in full and "demo skill" not in self_only and "demo skill" not in lineage
    assert "do one" in full and "do one" in lineage and "do one" not in self_only
    assert all("do two" in user for user in (full, self_only, lineage))


def test_candidate_defects_collects_only_the_yes_verdict():
    node = Node(
        unit_id="step1",
        context=StepContext(
            unit_id="step1", language="python", entity=Step(name="step1", instruction="do it")
        ),
    )
    assert node.defects == []

    node.verdict = Verdict(
        unit_id="step1", decision=True,
        candidate_bug="q=", reason="post breaks", location="ctx", location_unit_id="step1",
    )
    unified = UnifiedGraph(workflow=WorkflowGraph(), callgraph=CodeGraph(), nodes={"step1": node})
    defects = unified.candidate_defects()
    assert len(defects) == 1
    d = defects[0]
    assert isinstance(d, DefectGroup)
    assert d.unit_id == "step1" and d.kind == "workflow"
    assert len(d.verdicts) == 1
    v = d.verdicts[0]
    assert v.unit_id == "step1" and v.decision is True
    assert v.candidate_bug == "q=" and v.reason == "post breaks" and v.location == "ctx"
    assert v.location_unit_id == "step1"

    node.verdict = Verdict(unit_id="step1", decision=False)
    assert unified.candidate_defects() == []
