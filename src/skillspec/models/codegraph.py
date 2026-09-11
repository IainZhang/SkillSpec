"""Build-stage products: the skill's internal code graph."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from enum import StrEnum

from pydantic import BaseModel, Field

from .utils import transitive_closure, upward_paths


class DefKind(StrEnum):
    FUNCTION = "function"
    METHOD = "method"
    MODULE = "module"  # synthetic caller for module-level call sites


class CodeNode(BaseModel):
    """One resolved definition, addressed by id."""

    id: str  # "<module_path>::<qualname>"
    name: str
    kind: DefKind
    file: str  # resources key, e.g. "scripts/util.py"
    start_line: int  # 1-based
    end_line: int
    snippet: str = ""
    code_lines: int = 0  # pure code lines, no comments and blanks

    @property
    def is_real(self) -> bool:
        """A real def with a body, not a synthetic module node — worth a spec."""
        return self.kind is not DefKind.MODULE and bool(self.snippet.strip())


class CodeEdge(BaseModel):
    """A resolved internal call: caller id -> callee id."""

    caller: str
    callee: str
    call_line: int  # 1-based


class CodeGraph(BaseModel):
    """Internal callgraph for one skill: id->node, caller->callee edges."""

    language: str = "python"
    nodes: dict[str, CodeNode] = Field(default_factory=dict)
    edges: list[CodeEdge] = Field(default_factory=list)
    unparsed: list[str] = Field(default_factory=list)  # files with parse errors

    def _internal_edges(self) -> Iterator[tuple[str, str]]:
        """(caller, callee) pairs where both endpoints are known nodes."""
        for e in self.edges:
            if e.caller in self.nodes and e.callee in self.nodes:
                yield e.caller, e.callee

    def _real_sorted(self, ids: Iterable[str]) -> list[CodeNode]:
        """The real CodeNodes among `ids`, sorted by (file, start_line)."""
        nodes = (self.nodes[i] for i in ids if i in self.nodes)
        return sorted((n for n in nodes if n.is_real), key=lambda n: (n.file, n.start_line))

    def defs_in_file(self, file: str) -> list[CodeNode]:
        """Non-module definitions declared in `file`."""
        return [n for n in self.nodes.values() if n.file == file and n.kind is not DefKind.MODULE]

    def has_nodes_in_file(self, file: str) -> bool:
        """Whether any code node (incl. the synthetic module node) lives in `file`."""
        return any(n.file == file for n in self.nodes.values())

    def module_node(self, file: str) -> CodeNode | None:
        """The synthetic <module> node for `file`, if parsed."""
        return next((n for n in self.nodes.values() if n.file == file and n.kind is DefKind.MODULE), None)

    def reachable_subgraph(self, roots: set[str], callees: dict[str, set[str]] | None = None) -> set[str]:
        """Forward caller->callee closure from `roots` (inclusive); pass a callees_map to reuse it."""
        roots = {r for r in roots if r in self.nodes}
        if not roots:
            return set()
        return transitive_closure(roots, self.callees_map() if callees is None else callees)

    def callers_map(self) -> dict[str, set[str]]:
        """Reverse adjacency callee -> {callers}, internal nodes only."""
        callers: dict[str, set[str]] = {}
        for caller, callee in self._internal_edges():
            callers.setdefault(callee, set()).add(caller)
        return callers

    def real_reachable(self, entry: str, callees: dict[str, set[str]] | None = None) -> list[CodeNode]:
        """Forward-reachable real CodeNodes from `entry`, sorted by (file, start_line)."""
        return self._real_sorted(self.reachable_subgraph({entry}, callees))

    def real_callers(self, node_id: str, callers: dict[str, set[str]]) -> list[CodeNode]:
        """Transitive caller real CodeNodes for `node_id`, sorted by (file, start_line)."""
        acc = transitive_closure(callers.get(node_id, ()), callers)
        acc.discard(node_id)  # a node is not its own caller
        return self._real_sorted(acc)

    def caller_paths(self, node_id: str, callers: dict[str, set[str]]) -> list[list[str]]:
        """Real caller chains entry->...->`node_id`, each ordered upstream-first (`node_id` last)."""

        def is_real(i: str) -> bool:  # synthetic <module> frames must not appear on a chain
            return i in self.nodes and self.nodes[i].is_real

        # a lone [node_id] means no real caller — nothing to say about upstream propagation
        return [c for c in upward_paths(node_id, callers, keep=is_real) if len(c) > 1]

    def callees_map(self) -> dict[str, set[str]]:
        """Forward adjacency caller -> {callees}, internal nodes only (mirror of callers_map)."""
        callees: dict[str, set[str]] = {}
        for caller, callee in self._internal_edges():
            callees.setdefault(caller, set()).add(callee)
        return callees

    def immediate_callers(self, node_id: str, callers: dict[str, set[str]]) -> list[CodeNode]:
        """Direct (one-hop) caller real CodeNodes for `node_id` (excluding itself), sorted by (file, start_line)."""
        return self._real_sorted(c for c in callers.get(node_id, ()) if c != node_id)

    def sibling_callees(
        self, node_id: str, callers: dict[str, set[str]], callees: dict[str, set[str]]
    ) -> list[CodeNode]:
        """Real CodeNodes sharing an immediate caller with `node_id` (excluding itself), sorted by (file, start_line)."""
        siblings = {
            sib
            for caller in callers.get(node_id, ())
            for sib in callees.get(caller, ())
            if sib != node_id
        }
        return self._real_sorted(siblings)


class Resolution(BaseModel):
    """One call site's resolution outcome."""

    file: str  # resources key
    caller: str
    name: str  # callee simple name as written at the call site
    kind: str  # "name" | "self" | "attr" | "self_field"
    recv_var: str | None = None  # "attr" receiver var; else None
    recv_field: str | None = None  # "self_field" field name; else None
    line: int  # 1-based
    outcome: str  # "resolved" | "dropped"
    callee: str | None = None  # node id; None when dropped
    reason: str = ""  # "" when resolved; a DropReason value otherwise
