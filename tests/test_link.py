from __future__ import annotations

from skillspec.codegraph import build_codegraph
from skillspec.models import CodeContext, Metadata, NodeKind, SkillManifest, Step, WorkflowGraph
from skillspec.pipelines import UnifiedGraph

_APP = '''
def main():
    helper()

def helper():
    pass

if __name__ == "__main__":
    main()
'''

_LIB = '''
def run(x):
    return _helper(x)

def _helper(x):
    return x
'''

_AMBIG = '''
class A:
    def save(self):
        pass

class B:
    def save(self):
        pass
'''


def _script_step(name: str, path: str, entry: str | None = None) -> Step:
    return Step(
        name=name, instruction=f"run {path}", kind=NodeKind.REF_CODE,
        script_path=path, entry_point=entry,
    )


def test_link_graph_produces_link_table():
    resources = {"scripts/app.py": _APP, "scripts/lib.py": _LIB}
    callgraph = build_codegraph(resources)
    workflow = WorkflowGraph(entry="app", nodes={
        "app": _script_step("app", "scripts/app.py"),
        "lib": _script_step("lib", "scripts/lib.py"),
        "prose": Step(name="prose", instruction="just text", kind=NodeKind.PLAIN),
        "ghost": _script_step("ghost", "scripts/missing.py"),
    })

    unified = UnifiedGraph.build_link(workflow, callgraph)

    assert unified.workflow is workflow
    assert unified.callgraph is callgraph

    assert {link.step for link in unified.links} == {"app"}

    assert unified.entry_of("app") == "scripts.app::<module>"

    assert unified.entry_of("lib") is None

    assert unified.entry_of("prose") is None
    assert unified.entry_of("ghost") is None


def test_resolve_entry_contract():
    resources = {"scripts/app.py": _APP, "scripts/lib.py": _LIB, "scripts/ambig.py": _AMBIG}
    callgraph = build_codegraph(resources)
    workflow = WorkflowGraph(entry="bare", nodes={
        "bare":   _script_step("bare",   "scripts/app.py",   "main"),
        "parens": _script_step("parens", "scripts/app.py",   "main()"),
        "lib":    _script_step("lib",    "scripts/lib.py",   "run"),
        "ambig":  _script_step("ambig",  "scripts/ambig.py", "save"),
    })

    unified = UnifiedGraph.build_link(workflow, callgraph)

    assert unified.entry_of("bare") == "scripts.app::main"
    assert unified.entry_of("parens") == "scripts.app::main"
    assert unified.entry_of("lib") == "scripts.lib::run"
    assert unified.entry_of("ambig") is None
    assert {link.step for link in unified.links} == {"bare", "parens", "lib"}


def test_build_mask_code_node_and_link_symmetry():
    resources = {"scripts/app.py": _APP}
    callgraph = build_codegraph(resources)
    workflow = WorkflowGraph(entry="app", nodes={"app": _script_step("app", "scripts/app.py", "main")})
    unified = UnifiedGraph.build_link(workflow, callgraph)

    code_to_steps = unified.linked_code_units()
    assert set(code_to_steps) == {"scripts.app::main", "scripts.app::helper"}
    assert all(steps == ["app"] for steps in code_to_steps.values())

    unified.build_mask(Metadata(
        manifest=SkillManifest(name="demo", description="demo skill"), resources=resources,
    ))
    ctx = unified.nodes["scripts.app::helper"].context
    assert isinstance(ctx, CodeContext)
    assert [n.id for n in ctx.lineage] == ["scripts.app::main"]
    assert [n.id for n in ctx.neighbors] == ["scripts.app::main"]
    assert [s.name for s in ctx.linked] == ["app"]
    assert ctx.relevant_resources == ["scripts/app.py"]
