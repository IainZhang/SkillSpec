from __future__ import annotations

from skillspec.codegraph import build_codegraph, lang_parse
from skillspec.models import DefKind


def _ids(graph) -> set[str]:
    return set(graph.nodes)


def _edges(graph) -> set[tuple[str, str]]:
    return {(e.caller, e.callee) for e in graph.edges}


def test_python_callgraph():
    src = '''
MODEL = "gpt-4"
MAX: int = 5


class Base:
    def shared(self):
        pass


class Worker(Base):
    def run(self):
        self.shared()
        helper()


def helper():
    pass


def use(w: Worker):
    w.run()
'''
    g = build_codegraph({"py_mod.py": src})
    assert g.language == "python"
    assert {"py_mod::Base.shared", "py_mod::Worker.run", "py_mod::helper", "py_mod::use"} <= _ids(g)
    assert g.nodes["py_mod::Worker.run"].kind == DefKind.METHOD
    assert g.nodes["py_mod::helper"].kind == DefKind.FUNCTION
    edges = _edges(g)
    assert ("py_mod::Worker.run", "py_mod::Base.shared") in edges
    assert ("py_mod::Worker.run", "py_mod::helper") in edges
    assert ("py_mod::use", "py_mod::Worker.run") in edges
    _, facts = lang_parse("py_mod.py", src)
    assert {g.name: g.type_name for g in facts.globals} == {"MODEL": None, "MAX": "int"}


def test_javascript_callgraph():
    src = '''
function g() {}

class Mixin {
  shared() { g(); }
}

const ns = { Mixin };

class Derived extends ns.Mixin {
  run() { this.shared(); }
  h = () => { g(); };
}

const f = () => { g(); };
'''
    g = build_codegraph({"js_mod.js": src})
    assert g.language == "javascript"
    assert {"js_mod::g", "js_mod::Mixin.shared", "js_mod::Derived.run",
            "js_mod::Derived.h", "js_mod::f"} <= _ids(g)
    assert g.nodes["js_mod::f"].kind == DefKind.FUNCTION
    assert g.nodes["js_mod::Derived.h"].kind == DefKind.METHOD
    edges = _edges(g)
    assert ("js_mod::f", "js_mod::g") in edges
    assert ("js_mod::Derived.h", "js_mod::g") in edges
    assert ("js_mod::Derived.run", "js_mod::Mixin.shared") in edges
    _, facts = lang_parse("js_mod.js", src)
    globs = {g.name for g in facts.globals}
    assert "ns" in globs and "f" not in globs


def test_typescript_callgraph():
    src = '''
const M: Foo = new Foo();

class Foo {
  greet(): void {}
}

class Bar {
  x: Foo = new Foo();
  run(): void {
    this.x.greet();
  }
}

function make(f: Foo): Foo {
  f.greet();
  return f;
}

function pick(v: Foo | Bar) {
  v.greet();
}
'''
    g = build_codegraph({"ts_mod.ts": src})
    assert g.language == "typescript"
    assert {"ts_mod::Foo.greet", "ts_mod::Bar.run", "ts_mod::make", "ts_mod::pick"} <= _ids(g)
    edges = _edges(g)
    assert ("ts_mod::Bar.run", "ts_mod::Foo.greet") in edges
    assert ("ts_mod::make", "ts_mod::Foo.greet") in edges
    assert ("ts_mod::pick", "ts_mod::Foo.greet") in edges
    _, facts = lang_parse("ts_mod.ts", src)
    assert {g.name: g.type_name for g in facts.globals} == {"M": "Foo"}


def test_shell_callgraph():
    src = '''
API=value

bar() {
  echo hi
}

foo() {
  bar
  git status
}

foo
'''
    g = build_codegraph({"sh_mod.sh": src})
    assert g.language == "shell"
    assert {"sh_mod::bar", "sh_mod::foo"} <= _ids(g)
    edges = _edges(g)
    assert ("sh_mod::foo", "sh_mod::bar") in edges
    assert ("sh_mod::<module>", "sh_mod::foo") in edges
    assert not any("git" in nid or "echo" in nid for nid in _ids(g))
    _, facts = lang_parse("sh_mod.sh", src)
    assert {g.name for g in facts.globals} == {"API"}


def test_assignment_form_function_expression():
    src = '''
function k() {}
let h;
h = () => { k(); };
'''
    g = build_codegraph({"assign_mod.js": src})
    assert "assign_mod::h" in _ids(g)
    assert g.nodes["assign_mod::h"].kind == DefKind.FUNCTION
    assert ("assign_mod::h", "assign_mod::k") in _edges(g)
