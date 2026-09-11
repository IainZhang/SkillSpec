"""BuildStage — load or construct the unified graph (codegraph + workflow -> link -> mask)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from skillspec.codegraph import build_codegraph
from skillspec.config import CONFIG
from skillspec.llm import LLMResponse, run
from skillspec.models import CodeGraph, StageStats, WorkflowGraph, WorkflowParseError
from skillspec.prompts import WORKFLOW

from .graph import UnifiedGraph
from .utils import CODEGRAPH_PNG, GRAPH, WORKFLOW_JSONL, WORKFLOW_PNG, load_model
from .visualize import draw_codegraph, draw_workflow

if TYPE_CHECKING:
    from skillspec.config import SkillNamespace
    from skillspec.models import Metadata


logger = logging.getLogger(__name__)


def _resolve_scripts(workflow: WorkflowGraph, resources: dict[str, str]) -> None:
    """Resolve each REF_CODE step's path to a real resource key; clear path/entry on other steps."""
    for step in workflow.nodes.values():
        if step.is_ref_code:
            step.script_path = _resolve_script_path(step.script_path, resources)
        else:
            if step.script_path or step.entry_point:
                logger.warning(
                    "Step %r carries a script path/entry but is classified %s, not ref_code — "
                    "ignoring the stray script/entry attribute(s).", step.name, step.kind.value,
                )
            step.script_path = None
            step.entry_point = None


def _resolve_script_path(candidate: str | None, resources: dict[str, str]) -> str | None:
    """Match a model-named path to a real resource key: exact then unique basename."""
    if not candidate:
        return None

    norm = candidate.strip()
    while norm.startswith("./"):  # prefix-strip, not lstrip's char-set strip (".hidden/…" must survive)
        norm = norm[2:]
    norm = norm.lstrip("/")
    if norm in resources:
        return norm
    base = norm.rsplit("/", 1)[-1]
    hits = [k for k in resources if k.rsplit("/", 1)[-1] == base]
    if len(hits) == 1:
        return hits[0]
    logger.warning("Script path resolution failed: %r", candidate)
    return None


async def build_workflow(metadata: Metadata) -> tuple[WorkflowGraph, list[LLMResponse]]:
    """Derive the workflow DAG from the skill via the LLM, then resolve each step's script path."""
    user = WORKFLOW.build(metadata)
    logger.info("Workflow: querying %s …", CONFIG.llm.model)
    workflow, rounds = await run(WORKFLOW, user)
    if workflow is None:
        raise WorkflowParseError(rounds[-1].error if rounds else "workflow derivation failed")

    _resolve_scripts(workflow, metadata.resources)
    logger.info("Workflow: ok — %d nodes, entry=%r.", len(workflow.nodes), workflow.entry)
    return workflow, [r.resp for r in rounds if r.resp]


class BuildStage:
    """Build stage methods (mixed into Pipeline); owns graph construction and persistence."""

    if TYPE_CHECKING:  # shared state declared on Pipeline; here for type checkers only
        ns: SkillNamespace
        metadata: Metadata
        unified: UnifiedGraph | None

    async def build(self) -> None:
        """Build -> link -> mask and persist; the runner handles completed stages."""
        cg, workflow = await self._build_graph()
        self._build_link(cg, workflow)
        self._build_mask()

    def _load_graph(self) -> None:
        """Load the graph of a completed stage; missing or invalid data is an error."""
        self.unified = load_model(self.ns.artifact(GRAPH), UnifiedGraph)
        self.unified.resources = self.metadata.resources
        self.unified.build_mask(self.metadata)
        self._skip(None, "Build: restored completed stage.")

    async def _build_graph(self) -> tuple[CodeGraph, WorkflowGraph | None]:
        """build_graph span: codegraph (AST) then workflow (DAG)."""
        cg: CodeGraph | None = None
        workflow: WorkflowGraph | None = None
        with self._stage("build_graph") as (_, stats):
            with self._substage(stats, "Build: codegraph construction failed."):
                cg = build_codegraph(self.metadata.resources)
                self._info(
                    stats,
                    f"Codegraph: {len(cg.nodes)} node(s), {len(cg.edges)} edge(s), "
                    f"{len(cg.unparsed)} unparsed [{cg.language}].",
                )
            with self._substage(stats, "Build: workflow derivation failed."):
                workflow, responses = await build_workflow(self.metadata)
                stats.record_responses(responses)
        if cg is None:  # codegraph failed — an empty graph keeps the (LLM-paid) workflow side alive
            cg = CodeGraph()
        return cg, workflow

    def _build_link(self, cg: CodeGraph, workflow: WorkflowGraph | None) -> None:
        """build_link span: bind REF_CODE workflow nodes to the code callgraph, then export the DAGs."""
        with self._stage("build_link") as (_, stats):
            if workflow and workflow.nodes:
                with self._substage(stats, "Build: callgraph link failed."):
                    self.unified = UnifiedGraph.build_link(workflow, cg)
            else:
                self._skip(stats, "Build: no workflow graph — skipping link.")

            self._export_build_artifacts()

    def _export_build_artifacts(self) -> None:
        """Render graph PNGs and export workflow.jsonl when workflow nodes are available."""
        if not (self.unified and self.unified.workflow.nodes):
            return
        wf = self.unified.workflow
        draw_workflow(wf, self.ns.artifact(WORKFLOW_PNG))
        draw_codegraph(self.unified.callgraph, self.ns.artifact(CODEGRAPH_PNG))
        self.ns.write_jsonl(
            WORKFLOW_JSONL,
            [{"entry": wf.entry},
             *(step.model_dump(mode="json") for step in wf.nodes.values())],
        )

    def _build_mask(self) -> None:
        """build_mask span: materialize each node's intent-visibility context, then persist graph.json."""
        with self._stage("build_mask") as (_, stats):
            if self.unified and self.unified.workflow.nodes:
                with self._substage(stats, "Build: mask materialization failed."):
                    self.unified.build_mask(self.metadata)
                    self._info(stats, f"Mask: materialized {len(self.unified.nodes)} node(s).")
                    self._persist_graph(stats)
            else:
                self._skip(stats, "Build: no unified graph — skipping mask.")

    def _persist_graph(self, stats: StageStats) -> None:
        """Write the post-mask unified graph as graph.json (resources excluded; re-attached on load)."""
        if not (self.unified and self.unified.nodes):
            return
        self.ns.write_text_artifact(GRAPH, self.unified.model_dump_json(exclude={"resources"}))
        self._info(stats, "Build: wrote graph.json.")
