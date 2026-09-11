"""Build-stage product: the skill's workflow graph definition."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field

from .errors import WorkflowParseError
from .utils import clean_xml, strip_line_numbers, transitive_closure


class NodeType(StrEnum):
    """Top-level node type: STRUCTURAL is skipped in analysis, OPERATION is analyzed."""

    STRUCTURAL = "structural"
    OPERATION  = "operation"


class NodeKind(StrEnum):
    """Node kind (the artifact a node represents); the kind determines its NodeType."""

    ROOT        = "root"          # STRUCTURAL - skill root intro
    STAGE       = "stage"         # STRUCTURAL - independent functional stage
    CONTEXT     = "context"       # STRUCTURAL - supporting general non-execution node: examples, constraints, notes
    PLAIN       = "plain"         # OPERATION - prose step relying on LLM capability, no runnable code
    INLINE_CODE = "inline_code"   # OPERATION - code/command lines embedded directly in the markdown
    REF_CODE    = "ref_code"      # OPERATION - invokes/imports a repo script; drives code-linking


_STRUCTURAL_KINDS = frozenset({NodeKind.ROOT, NodeKind.STAGE, NodeKind.CONTEXT})


class EdgeKind(StrEnum):
    """Edge relation: CONTAIN organizes structure, DEPENDENCY is a true execution dependency."""

    CONTAIN    = "contain"      # structural grouping (parent -> child); no execution order
    DEPENDENCY = "dependency"   # target runs after / needs the source; serial steps chain these


def _default_edge_kind(source_structural: bool) -> EdgeKind:
    """Edge relation when none is annotated: structural sources contain, operations depend."""
    return EdgeKind.CONTAIN if source_structural else EdgeKind.DEPENDENCY


class Step(BaseModel):
    """One workflow step; prev/next link step names. script_path/entry_point set only for REF_CODE."""

    name: str
    instruction: str = ""
    prev: list[str] = Field(default_factory=list)
    next: list[str] = Field(default_factory=list)
    next_kind: dict[str, EdgeKind] = Field(default_factory=dict)  # successor name -> edge relation
    kind: NodeKind = NodeKind.PLAIN
    script_path: str | None = None
    entry_point: str | None = None  # REF_CODE entry fn as named by the model; resolved to a CodeNode in link

    @computed_field
    @property
    def type(self) -> NodeType:
        """Top-level node type, derived from the kind."""
        return NodeType.STRUCTURAL if self.kind in _STRUCTURAL_KINDS else NodeType.OPERATION

    @property
    def is_structural(self) -> bool:
        """Structural node (root/stage/context) — skipped during analysis."""
        return self.type is NodeType.STRUCTURAL

    @property
    def is_ref_code(self) -> bool:
        """Invokes a repo script/module — the code-linking gate."""
        return self.kind is NodeKind.REF_CODE

    def edge_kind(self, target: str) -> EdgeKind:
        """Relation of the edge to `target`; defaults by source type when unannotated."""
        return self.next_kind.get(target) or _default_edge_kind(self.is_structural)


def _parse_xml(text: str) -> tuple[str, list[dict]]:
    """Extract the <workflow> block and parse into (entry, nodes)."""
    root = ET.fromstring(clean_xml(text))
    if root.tag != 'workflow':
        raise WorkflowParseError(f"root element must be <workflow>, got <{root.tag}>")
    entry = root.get('root', '')  # <workflow root="..."> names the graph entry unit
    nodes = []
    for unit in root.findall('unit'):
        name = unit.get('name')
        if not name:
            raise WorkflowParseError("unit missing required 'name' attribute")
        nxt: list[str] = []
        rels: dict[str, str] = {}
        for n in unit.findall('next'):
            target = (n.text or '').strip()
            if not target:
                continue
            nxt.append(target)
            rels[target] = (n.get('rel') or '').strip().lower()
        content_el = unit.find('content')
        # itertext, not .text: nested markup inside <content> must not truncate the instruction
        content = strip_line_numbers(''.join(content_el.itertext()) if content_el is not None else '')
        ntype = (unit.get('type') or '').strip().lower()
        script = (unit.get('script') or '').strip() or None
        entry_point = (unit.get('entry') or '').strip() or None
        nodes.append({
            'name': name, 'next': nxt, 'next_rel': rels, 'content': content,
            'type': ntype, 'script': script, 'entry': entry_point,
        })
    return entry, nodes


def _coerce_kind(type_str: str) -> NodeKind:
    """Map ``<unit type=...>`` to a NodeKind; unknown -> PLAIN so it's analyzed, not dropped."""
    try:
        return NodeKind(type_str)
    except ValueError:
        return NodeKind.PLAIN


def _coerce_edge_kind(raw: str, source_structural: bool) -> EdgeKind:
    """Map a ``<next rel=...>`` string to an EdgeKind; unknown/missing defaults by source type."""
    try:
        return EdgeKind(raw)
    except ValueError:
        return _default_edge_kind(source_structural)


def _rebuild_edges(nodes: dict[str, Step]) -> None:
    """Invariant-enforcer for the next/next_kind/prev triple: dedupe, drop self/dangling next, prune next_kind, mirror prev."""
    names = set(nodes)
    for step in nodes.values():
        step.next = [n for n in dict.fromkeys(step.next) if n in names and n != step.name]
        step.next_kind = {t: k for t, k in step.next_kind.items() if t in step.next}
        step.prev = []
    for name, step in nodes.items():
        for nxt in step.next:
            nodes[nxt].prev.append(name)


def _ground(entry: str, nodes: list[dict]) -> "WorkflowGraph":
    """Validate parsed nodes into a WorkflowGraph; enforce uniqueness and entry resolution."""
    result: dict[str, Step] = {}
    for node in nodes:
        if node['name'] in result:  # rule: unit names must be unique
            raise WorkflowParseError(f"duplicate unit name {node['name']!r}; unit names must be unique")
        kind = _coerce_kind(node['type'])
        structural = kind in _STRUCTURAL_KINDS
        next_rel = node.get('next_rel', {})
        result[node['name']] = Step(
            name=node['name'],
            next=node['next'],
            next_kind={t: _coerce_edge_kind(next_rel.get(t, ''), structural) for t in node['next']},
            instruction=node['content'],
            kind=kind,
            script_path=node['script'],
            entry_point=node['entry'],
        )
    if entry and entry not in result:  # rule: entry must resolve to a unit
        raise WorkflowParseError(f"entry {entry!r} is not the name of any unit")
    _rebuild_edges(result)
    return WorkflowGraph(entry=entry, nodes=result)


class WorkflowGraph(BaseModel):
    """Workflow graph: name -> Step adjacency list with a single entry node."""

    entry: str = ""
    nodes: dict[str, Step] = Field(default_factory=dict)

    @classmethod
    def parse(cls, text: str) -> "WorkflowGraph":
        try:
            entry, nodes = _parse_xml(text)
        except ET.ParseError as err:  # normalize to WorkflowParseError so callers recognise a parse miss
            raise WorkflowParseError(f"malformed workflow XML: {err}") from err
        return _ground(entry, nodes)

    @staticmethod
    def correction(error: str) -> str:
        kinds = ", ".join(NodeKind)
        rels = ", ".join(EdgeKind)
        return (
            f"That result failed to validate as the workflow XML:\n{error}\n\n"
            f"Return a corrected xml code block that fixes the error above. "
            f"Each <unit> must carry a type attribute (one of {kinds}); {NodeKind.REF_CODE} "
            f"units must also carry script (an exact available file path) and entry (a bare function name). "
            f"Each <next> should carry rel (one of {rels}): {EdgeKind.CONTAIN} for structural "
            f"grouping, {EdgeKind.DEPENDENCY} for execution order; a missing rel defaults by source type."
        )

    @classmethod
    def tool(cls) -> dict | None:
        return None  # reply is XML, not a JSON object; no tool is sent

    def predecessors(self) -> dict[str, set[str]]:
        """Reverse adjacency name -> {predecessor names}, known nodes only."""
        return {n: {p for p in s.prev if p in self.nodes} for n, s in self.nodes.items()}

    def ancestors(self) -> dict[str, list[str]]:
        """Transitive predecessors per node, each list ordered root->node (topological)."""
        names = set(self.nodes)
        preds = self.predecessors()

        topo = self.topo_order()
        out: dict[str, list[str]] = {}
        for start in names:
            acc = transitive_closure(preds[start], preds)
            acc.discard(start)  # a node is not its own ancestor
            out[start] = sorted(acc, key=lambda n: topo[n])
        return out

    def topo_order(self) -> dict[str, int]:
        """Deterministic root->leaf topological index per node (Kahn's, ties by name)."""
        names = sorted(self.nodes)
        indeg = {n: sum(1 for p in self.nodes[n].prev if p in self.nodes) for n in names}
        ready = sorted(n for n in names if indeg[n] == 0)
        order: dict[str, int] = {}
        while ready:
            n = ready.pop(0)
            order[n] = len(order)
            for nxt in sorted(self.nodes[n].next):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
            ready.sort()
        for n in names:  # cycle leftovers (if any): append deterministically
            order.setdefault(n, len(order))
        return order
