"""Resolve per-file FileFacts into the repo-wide, internal-only CodeGraph."""
# Calls are resolved using imports, type hints, and unique-name matching.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from skillspec.models.codegraph import CodeEdge, CodeGraph, CodeNode, DefKind, Resolution

from .facts import Call, CallKind, ClassDef, Definition, FileFacts

_CTOR_NAMES = ("__init__", "constructor")
Imports = dict[str, tuple[str, str | None]]  # local name -> (module path, imported attr | None)


class DropReason(StrEnum):
    """Why resolve_call dropped a call."""

    EXTERNAL = "external-or-unknown"       # bare name matches no internal def/class
    AMBIGUOUS = "ambiguous"                # name matches multiple internal defs/classes
    RECEIVER_UNTYPED = "receiver-untyped"  # attr/self_field receiver has no inferred type
    METHOD_NOT_FOUND = "method-not-found"  # method absent on class and its bases
    NO_ENCLOSING_CLASS = "no-enclosing-class"  # self/self_field call without enclosing class


def build_graph(
    facts_by_path: dict[str, FileFacts], unparsed: list[str], trace: list[Resolution] | None = None
) -> CodeGraph:
    """Assemble the callgraph: every definition is a node, every resolved internal call an edge."""
    graph = CodeGraph(unparsed=unparsed)
    resolver = Resolver()

    for path, facts in facts_by_path.items():
        resolver.index_definitions(path, facts, graph)
    # Bind var/field type annotations to internal class ids. Must follow all-file indexing.
    for facts in facts_by_path.values():
        resolver.bind_types(facts)
    _emit_edges(graph, resolver, facts_by_path, trace)

    return graph


def _emit_edges(
    graph: CodeGraph, resolver: Resolver, facts_by_path: dict[str, FileFacts], trace: list[Resolution] | None
) -> None:
    """Resolve every call site to an internal callee and add a deduplicated edge."""
    seen: set[tuple[str, str, int]] = set()
    for path, facts in facts_by_path.items():
        mod = facts.module_path
        for call in facts.calls:
            callee, reason = resolver.resolve_call(call, mod, facts.imports)
            caller = _caller_id(mod, call)
            if trace is not None:  # record every call site outcome before dropping
                trace.append(_trace_call(path, caller, call, callee, reason))
            if callee is None:
                continue
            if not _ensure_caller_node(graph, caller, call, path):
                continue
            key = (caller, callee, call.line)
            if key in seen:  # collapse repeated identical call sites into one edge
                continue
            seen.add(key)
            graph.edges.append(CodeEdge(caller=caller, callee=callee, call_line=call.line))


def _caller_id(mod: str, call: Call) -> str:
    """Caller node id: enclosing function, or synthetic <module> for top-level calls."""
    return f"{mod}::{call.caller}" if call.caller else f"{mod}::<module>"


def _ensure_caller_node(graph: CodeGraph, caller: str, call: Call, path: str) -> bool:
    """Ensure a node exists for `caller`, synthesizing a <module> node for top-level calls."""
    if caller in graph.nodes:
        return True
    if call.caller:  # named caller should already be indexed; reaching here is defensive
        return False
    graph.nodes[caller] = CodeNode(
        id=caller, name="<module>", kind=DefKind.MODULE, file=path, start_line=1, end_line=1,
    )
    return True


def _trace_call(path: str, caller: str, call: Call, callee: str | None, reason: str) -> Resolution:
    """Build the per-call-site verification record."""
    return Resolution(
        file=path, caller=caller, name=call.name, kind=call.kind.value,
        recv_var=call.recv_var, recv_field=call.recv_field, line=call.line,
        outcome="resolved" if callee else "dropped", callee=callee, reason=reason,
    )


@dataclass
class Namespace:
    """One resolvable name scope (defs or classes): module-top names plus a global by-name index."""

    top_by_module: dict[str, dict[str, str]] = field(default_factory=dict)  # mod -> {top name: id}
    by_name: dict[str, list[str]] = field(default_factory=dict)             # simple name -> ids

    def add(self, mod: str, name: str, node_id: str, *, top_level: bool) -> None:
        """Index `node_id` under its simple name (always), and as a module-top name when top_level."""
        self.by_name.setdefault(name, []).append(node_id)
        if top_level:
            self.top_by_module.setdefault(mod, {})[name] = node_id

    def top_level(self, mod: str, name: str) -> str | None:
        """Id bound to a top-level `name` in module `mod`, if any."""
        return self.top_by_module.get(mod, {}).get(name)

    def resolve(self, name: str, mod: str, imports: Imports, modules: set[str]) -> str | None:
        """Resolve by precedence: same-module top-level, then `from`-import, then a unique global."""
        same = self.top_level(mod, name)
        if same:
            return same
        imp = imports.get(name)
        if imp:
            module, attr = imp
            if attr is not None:  # `from <module> import attr`
                m = _internal_module(module, modules)
                hit = self.top_level(m, attr) if m else None
                if hit:
                    return hit
        ids = self.by_name.get(name, [])  # unique internal def of that name
        return ids[0] if len(ids) == 1 else None


@dataclass
class Resolver:
    """Repo-wide symbol tables: index_definitions/bind_types populate, resolve_* methods read."""

    modules: set[str] = field(default_factory=set)
    defs: Namespace = field(default_factory=Namespace)
    classes: Namespace = field(default_factory=Namespace)
    class_by_id: dict[str, ClassDef] = field(default_factory=dict)
    method_index: dict[tuple[str, str], str] = field(default_factory=dict)  # (class id, method) -> def id
    imports_by_module: dict[str, Imports] = field(default_factory=dict)
    var_type: dict[tuple[str, str | None, str], str] = field(default_factory=dict)  # (mod, func, var) -> class id
    field_type: dict[tuple[str, str], str] = field(default_factory=dict)  # (class id, field) -> class id

    def index_definitions(self, path: str, facts: FileFacts, graph: CodeGraph) -> None:
        """Register one file's defs as graph nodes and index them for lookup."""
        mod = facts.module_path
        self.modules.add(mod)
        self.imports_by_module[mod] = facts.imports
        for defn in facts.defs:
            self._index_def(mod, defn, path, graph)
        for cls in facts.classes:
            self._index_class(mod, cls)

    def _index_def(self, mod: str, defn: Definition, path: str, graph: CodeGraph) -> None:
        """Create the def's node, then index it under its enclosing scope."""
        did = f"{mod}::{defn.qualname}"
        parent, dot, simple = defn.qualname.rpartition(".")
        graph.nodes[did] = CodeNode(
            id=did, name=simple, kind=defn.kind, file=path,
            start_line=defn.start_line, end_line=defn.end_line, snippet=defn.snippet,
            code_lines=defn.code_lines,
        )
        self.defs.add(mod, simple, did, top_level=not dot)
        if dot:  # method/nested def: also index under enclosing scope id
            self.method_index[(f"{mod}::{parent}", simple)] = did

    def _index_class(self, mod: str, cls: ClassDef) -> None:
        """Index a class for receiver typing and constructor lookup."""
        cid = f"{mod}::{cls.qualname}"
        simple = cls.qualname.rpartition(".")[2]
        self.class_by_id[cid] = cls
        self.classes.add(mod, simple, cid, top_level="." not in cls.qualname)

    def bind_types(self, facts: FileFacts) -> None:
        """Resolve declared var/field type-names to internal class ids (skip unresolvable)."""
        mod = facts.module_path
        for tb in facts.var_types:  # function-scoped
            cid = self.resolve_class(tb.type_name, mod, facts.imports)
            if cid:
                self.var_type[(mod, tb.scope, tb.name)] = cid
        for tb in facts.field_types:  # class-scoped
            cid = self.resolve_class(tb.type_name, mod, facts.imports)
            if cid:
                self.field_type[(f"{mod}::{tb.scope}", tb.name)] = cid

    def resolve_call(self, call: Call, mod: str, imports: Imports) -> tuple[str | None, str]:
        """Resolve a call to an internal callee id, or (None, DropReason)."""
        match call.kind:
            case CallKind.SELF:
                if call.cls is None:
                    return None, DropReason.NO_ENCLOSING_CLASS
                return self._method_outcome(f"{mod}::{call.cls}", call.name)
            case CallKind.SELF_FIELD:
                if call.cls is None:
                    return None, DropReason.NO_ENCLOSING_CLASS
                cid = self.field_type.get((f"{mod}::{call.cls}", call.recv_field))
                return self._method_outcome(cid, call.name)
            case CallKind.ATTR:
                imp = imports.get(call.recv_var or "")
                if imp:
                    module, attr = imp
                    if attr is None:  # recv_var aliases an internal module -> module.name
                        m = _internal_module(module, self.modules)
                        hit = self.defs.top_level(m, call.name) if m else None
                        if hit:
                            return hit, ""
                cid = self.var_type.get((mod, call.caller, call.recv_var))  # locally-typed var
                return self._method_outcome(cid, call.name)
            case CallKind.NAME:
                fid = self.resolve_name(call.name, mod, imports)
                if fid:
                    return fid, ""
                cid = self.resolve_class(call.name, mod, imports)  # bare class name -> constructor
                if cid:
                    ctor = self._constructor_of(cid)
                    if ctor:
                        return ctor, ""
                ambiguous = (
                    len(self.defs.by_name.get(call.name, [])) > 1
                    or len(self.classes.by_name.get(call.name, [])) > 1
                )
                return None, (DropReason.AMBIGUOUS if ambiguous else DropReason.EXTERNAL)
            case _:
                return None, DropReason.EXTERNAL

    def _method_outcome(self, cid: str | None, method: str) -> tuple[str | None, str]:
        """Look up `method` on class id `cid`."""
        if cid is None:
            return None, DropReason.RECEIVER_UNTYPED
        hit = self.method_lookup(cid, method)
        return (hit, "") if hit else (None, DropReason.METHOD_NOT_FOUND)

    def _constructor_of(self, cid: str) -> str | None:
        """Class constructor def id (__init__/constructor), searching its bases."""
        for ctor in _CTOR_NAMES:
            hit = self.method_lookup(cid, ctor)
            if hit:
                return hit
        return None

    def resolve_name(self, name: str, mod: str, imports: Imports) -> str | None:
        """Resolve a bare name to a def id by import-aware precedence."""
        return self.defs.resolve(name, mod, imports, self.modules)

    def resolve_class(self, type_name: str, mod: str, imports: Imports) -> str | None:
        """Resolve a type-name to a class id by import-aware precedence."""
        return self.classes.resolve(type_name, mod, imports, self.modules)

    def method_lookup(self, class_id: str | None, method: str, _seen: set[str] | None = None) -> str | None:
        """Find a method by cycle-guarded depth-first search over declared bases."""
        if class_id is None:
            return None
        _seen = _seen if _seen is not None else set()
        if class_id in _seen:
            return None
        _seen.add(class_id)
        hit = self.method_index.get((class_id, method))
        if hit:
            return hit
        cd = self.class_by_id.get(class_id)
        if cd is None:
            return None
        cmod = class_id.split("::", 1)[0]
        cimports = self.imports_by_module.get(cmod, {})
        for base in cd.bases:
            bid = self.resolve_class(base, cmod, cimports)
            found = self.method_lookup(bid, method, _seen) if bid else None
            if found:
                return found
        return None


def _internal_module(target: str, modules: set[str]) -> str | None:
    """Match an import target to a skill module by exact path or unique suffix."""
    if target in modules:
        return target
    cands = [m for m in modules if m.endswith("." + target)]
    return cands[0] if len(cands) == 1 else None
