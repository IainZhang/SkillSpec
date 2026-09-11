"""The Link/UnifiedGraph types plus the deterministic graph ops that assemble them."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from pydantic import BaseModel, Field

from typing import NamedTuple

from skillspec.models import (
    CodeContext,
    CodeGraph,
    CodeNode,
    DefectGroup,
    EdgeKind,
    Metadata,
    NodeKind,
    Step,
    StepContext,
    VerifyKind,
    WorkflowGraph,
    empty_by_kind,
)
from skillspec.models.utils import upward_paths

from .node import Node

logger = logging.getLogger(__name__)

_MAX_PATHS = 8  # a node invoked by several steps would otherwise emit one chain set per step


def _dedupe_paths(paths: Iterable[list[str]]) -> list[list[str]]:
    """Distinct chains in first-seen order, capped."""
    seen: dict[tuple[str, ...], list[str]] = {}
    for path in paths:
        seen.setdefault(tuple(path), path)
    return list(seen.values())[:_MAX_PATHS]


def _scoped(code_nodes: Iterable[CodeNode], resources: dict[str, str]) -> list[str]:
    """The resource keys of the files a node's code lives in (content stays on UnifiedGraph.resources)."""
    return [f for f in sorted({n.file for n in code_nodes}) if f in resources]


def _step_lineage(workflow: WorkflowGraph, ancestors: dict[str, list[str]], name: str) -> list[Step]:
    """Return the step's ancestors in topological order, including structural nodes."""
    return [workflow.nodes[a] for a in ancestors[name]]


def _step_neighbors(workflow: WorkflowGraph, name: str, step: Step) -> list[Step]:
    """Neighbors layer for a step: the parent stage(s) + all their other children of any kind (the
    local task family); dependency sequences live in the lineage."""
    out: list[Step] = []
    seen: set[str] = set()
    for p in step.prev:
        parent = workflow.nodes[p]
        if parent.kind is not NodeKind.STAGE:  # only a stage anchors the family; sequences are lineage
            continue
        if p not in seen:
            seen.add(p)
            out.append(parent)
        for c in parent.next:
            if c == name or c in seen:  # any sibling kind joins the family (a stage child is never root)
                continue
            seen.add(c)
            out.append(workflow.nodes[c])
    return out


def _step_downstream(workflow: WorkflowGraph, step: Step) -> list[Step]:
    """Return all direct dependency successors in declared order; never traverse beyond one hop."""
    out: list[Step] = []
    seen: set[str] = set()
    for name in step.next:
        if name == step.name or name in seen or name not in workflow.nodes:
            continue
        seen.add(name)
        if step.edge_kind(name) is EdgeKind.DEPENDENCY:
            out.append(workflow.nodes[name])
    return out


class _Maps(NamedTuple):
    """Derived adjacency, computed once per build_mask and shared by both context builders."""

    ancestors: dict[str, list[str]]   # step -> root->node ancestor names
    topo: dict[str, int]              # step -> topological index
    preds: dict[str, set[str]]        # step -> predecessor names
    callers: dict[str, set[str]]      # code id -> caller ids
    callees: dict[str, set[str]]      # code id -> callee ids
    entry_by_step: dict[str, str]     # linked step -> the code entry it invokes


def _resolve_entry(entry_point: str | None, file: str, callgraph: CodeGraph) -> str | None:
    if not entry_point:
        return None
    # bare fn name; removesuffix, not rstrip — "f(x)" must not lose its closing paren
    want = entry_point.strip().removesuffix("()")
    in_file = callgraph.defs_in_file(file)

    # Match the qualified name, e.g. "Cls.method" in "module::Cls.method".
    by_qual = [n.id for n in in_file if n.id.split("::")[-1] == want]
    if len(by_qual) == 1:
        return by_qual[0]

    # fall back to name match; require a unique hit
    by_name = [n.id for n in in_file if n.name == want]
    if len(by_name) == 1:
        return by_name[0]
    logger.warning("Entry resolution failed: %r", entry_point)
    return None


class Link(BaseModel):
    """One binding: a REF_CODE workflow step, its script file, and the resolved code entry point."""

    step: str   # WorkflowGraph.nodes key
    file: str   # resources key
    entry: str  # CodeNode.id (already resolved, with <module> fallback applied)


class UnifiedGraph(BaseModel):
    """Unified view: workflow DAG + code callgraph + the link table, plus per-node analysis state."""

    workflow: WorkflowGraph
    callgraph: CodeGraph
    links: list[Link] = Field(default_factory=list)

    nodes: dict[str, Node] = Field(default_factory=dict)  # unit id → node (context + specs + verdicts)
    resources: dict[str, str] = Field(default_factory=dict)  # skill files path -> content

    @classmethod
    def build_link(cls, workflow: WorkflowGraph, callgraph: CodeGraph) -> "UnifiedGraph":
        """Build a UnifiedGraph with one Link per resolvable REF_CODE node bound to its file and code entry point."""
        links: list[Link] = []
        for name, step in workflow.nodes.items():
            if not step.is_ref_code or not step.script_path:
                continue
            file = step.script_path
            # skip non-code / unparsed resources
            if not callgraph.has_nodes_in_file(file):
                continue

            # resolve the entry function the unit invokes; fall back to the file's <module> node
            entry = _resolve_entry(step.entry_point, file, callgraph)
            if entry is None:
                mod = callgraph.module_node(file)
                entry = mod.id if mod else None
            if entry is None:
                continue
            links.append(Link(step=name, file=file, entry=entry))

        logger.info("Link: %d REF_CODE node(s) linked.", len(links))
        for link in links:
            logger.info("  %s -> %s::%s", link.step, link.file, link.entry)
        return cls(workflow=workflow, callgraph=callgraph, links=links)

    def _entry_by_step(self) -> dict[str, str]:
        """Each linked step mapped to the first code entry it invokes (first link wins)."""
        out: dict[str, str] = {}
        for link in self.links:
            out.setdefault(link.step, link.entry)
        return out

    def entry_of(self, step: str) -> str | None:
        """The resolved CodeNode.id a workflow step invokes, or None if unlinked."""
        return self._entry_by_step().get(step)

    def linked_code_units(self, callees: dict[str, set[str]] | None = None) -> dict[str, list[str]]:
        """Real code nodes reachable from any linked entry, each mapped to its invoking step names."""
        linked: dict[str, set[str]] = {}
        for link in self.links:
            for node in self.callgraph.real_reachable(link.entry, callees):
                linked.setdefault(node.id, set()).add(link.step)
        return {nid: sorted(names) for nid, names in linked.items()}

    def build_mask(self, metadata: Metadata) -> None:
        """Materialize each unit's context onto self.nodes (workflow steps, then linked code defs).

        Context fields by visibility layer:

        | Layer     | StepContext                        | CodeContext                          |
        |-----------|------------------------------------|--------------------------------------|
        | Holistic  | holistic_ctx (root/context steps, shared)                                 |
        | Lineage   | lineage: root->step path           | lineage: transitive callers          |
        | Neighbors | neighbors: parent stage + siblings | neighbors: callers + sibling callees |
        | Self      | entity + linked (invoked code)     | entity + linked (invoking steps)     |

        StepContext.downstream separately holds every direct dependency successor. It is not a
        visibility layer and is rendered only where stage policy requests downstream obligations.
        """
        manifest = metadata.manifest
        resources = metadata.resources
        maps = _Maps(
            ancestors=self.workflow.ancestors(),
            topo=self.workflow.topo_order(),
            preds=self.workflow.predecessors(),
            callers=self.callgraph.callers_map(),
            callees=self.callgraph.callees_map(),
            entry_by_step=self._entry_by_step(),
        )
        common = {
            "skill_name": manifest.name,
            "overview": manifest.description.strip(),
            "manifest_body": manifest.content,
            "language": self.callgraph.language,
            # Holistic layer: descriptive structural nodes (root/context); stage nodes are excluded.
            "holistic_ctx": [
                s for s in self.workflow.nodes.values()
                if s.kind in (NodeKind.ROOT, NodeKind.CONTEXT)
            ],
        }

        nodes: dict[str, Node] = {}
        for name, step in self.workflow.nodes.items():
            ctx = self._step_context(name, step, maps, common, resources)
            nodes[name] = Node(unit_id=name, context=ctx)
        code_to_steps = self.linked_code_units(maps.callees)
        for cid in sorted(code_to_steps):
            ctx = self._code_context(cid, code_to_steps[cid], maps, common, resources)
            nodes[cid] = Node(unit_id=cid, context=ctx)

        self.nodes = nodes
        self.resources = resources

    def _step_context(
        self, name: str, step: Step, maps: _Maps, common: dict, resources: dict[str, str]
    ) -> StepContext:
        """One workflow step's context; Self carries the whole code branch the step invokes."""
        entry = maps.entry_by_step.get(name)
        # Reachable internal codes from the entry function, sorted by file and line.
        relevant_codes = self.callgraph.real_reachable(entry, maps.callees) if entry else []
        return StepContext(
            unit_id=name,
            **common,
            relevant_resources=_scoped(relevant_codes, resources),
            entity=step,
            lineage=_step_lineage(self.workflow, maps.ancestors, name),
            neighbors=_step_neighbors(self.workflow, name, step),
            downstream=_step_downstream(self.workflow, step),
            linked=relevant_codes,
        )

    def _code_context(
        self, cid: str, steps: list[str], maps: _Maps, common: dict, resources: dict[str, str]
    ) -> CodeContext:
        """One code definition's context; adds the ordered root-to-node chains an ExpectSpec renders."""
        code = self.callgraph.nodes[cid]
        lineage_code = self.callgraph.real_callers(cid, maps.callers)
        # Unified root-to-node intent path: workflow root->invoking step, then entry->this code.
        # The invoking steps stay on the path (they are also Self) — dropping them would leave a
        # hole whenever one invoking step is an ancestor of another.
        path_steps = {a for s in steps for a in maps.ancestors[s]} | set(steps)
        # immediate callers + sibling callees, deduped by id (a node can be both on a cycle)
        neighbor_code = list({
            n.id: n
            for n in (
                *self.callgraph.immediate_callers(cid, maps.callers),
                *self.callgraph.sibling_callees(cid, maps.callers, maps.callees),
            )
        }.values())
        return CodeContext(
            unit_id=cid,
            **common,
            relevant_resources=_scoped([code, *lineage_code, *neighbor_code], resources),
            entity=code,
            lineage=lineage_code,
            neighbors=neighbor_code,
            linked=[self.workflow.nodes[s] for s in steps],
            step_lineage=[self.workflow.nodes[n] for n in sorted(path_steps, key=lambda n: maps.topo[n])],
            workflow_paths=_dedupe_paths(c for s in steps for c in upward_paths(s, maps.preds)),
            call_paths=self.callgraph.caller_paths(cid, maps.callers),
        )

    def bind_defects(self) -> None:
        """Bind each Yes-verdict to its owner unit (location_unit_id, else self); idempotent."""
        for node in self.nodes.values():
            node.defects = []
        for uid, node in self.nodes.items():
            verdict = node.verdict
            if verdict and verdict.decision:
                owner = verdict.location_unit_id if verdict.location_unit_id in self.nodes else uid
                self.nodes[owner].defects.append(verdict)

    def defects_by_kind(self) -> dict[VerifyKind, list[DefectGroup]]:
        """Defect groups bucketed by owner kind; one group per unit with findings."""
        self.bind_defects()
        out = empty_by_kind()
        for uid, node in self.nodes.items():
            if node.defects:
                out[node.kind].append(DefectGroup(unit_id=uid, kind=node.kind, verdicts=node.defects))
        return out

    def candidate_defects(self) -> list[DefectGroup]:
        """All defect groups flattened across kinds."""
        return [g for gs in self.defects_by_kind().values() for g in gs]
