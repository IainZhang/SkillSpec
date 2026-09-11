from __future__ import annotations

import asyncio

import pytest

from skillspec.models import CodeContext, CodeNode, DefKind, NodeKind, Step, StepContext
from skillspec.pipelines import Node


@pytest.fixture
def no_llm(monkeypatch):

    async def _boom(*args, **kwargs):
        raise AssertionError("spec_reason reached the LLM; a skip-gate regressed")

    monkeypatch.setattr("skillspec.pipelines.node.run", _boom)
    monkeypatch.setattr("skillspec.pipelines.node.run_serial", _boom)


def test_spec_reason_skips_without_expect(no_llm):
    node = Node(
        unit_id="s1",
        context=StepContext(unit_id="s1", language="python", entity=Step(name="s1", instruction="do it")),
    )
    assert asyncio.run(node.spec_reason(resources={})) == ([], 0)
    assert node.verdict is None


LONG_EN = (
    "Read the configuration file, validate that every required field is present, "
    "then write the normalized result to the output directory and log a summary of what changed."
)
LONG_ZH = (
    "读取配置文件并逐一校验每个必填字段是否存在，然后把规范化之后的结果写入输出目录，并记录一份说明本次改动内容的摘要报告。"
)


def _step_node(unit_id: str, **step: object) -> Node:
    return Node(unit_id=unit_id, context=StepContext(unit_id=unit_id, language="python",
                                                     entity=Step(name=unit_id, **step)))


def test_in_scope():
    structural = _step_node("root", kind=NodeKind.ROOT)
    assert structural.in_scope is False

    assert _step_node("s1", instruction=LONG_EN).in_scope is True

    assert _step_node("s2", instruction="do it").in_scope is False

    assert _step_node("s3", instruction="Run it.", kind=NodeKind.REF_CODE,
                      script_path="scripts/build.py").in_scope is True

    assert _step_node("z1", instruction=LONG_ZH).in_scope is True
    assert _step_node("z2", instruction="保存输出结果。").in_scope is False

    short_code = CodeNode(
        id="m::f", name="f", kind=DefKind.FUNCTION, file="m.py",
        start_line=1, end_line=3, snippet="def f(): return 1", code_lines=3,
    )
    short = Node(unit_id="m::f", context=CodeContext(unit_id="m::f", language="python", entity=short_code))
    assert short.in_scope is False

    long_code = CodeNode(
        id="m::g", name="g", kind=DefKind.FUNCTION, file="m.py",
        start_line=1, end_line=20, snippet="def g():\n    ...\n", code_lines=20,
    )
    long = Node(unit_id="m::g", context=CodeContext(unit_id="m::g", language="python", entity=long_code))
    assert long.in_scope is True
