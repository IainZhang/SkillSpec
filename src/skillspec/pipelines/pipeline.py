"""Pipeline — owns the unified graph and drives build -> reason -> verify over it.

Stage logic lives in the per-stage mixins (build/reason/verify); this module
holds the explicit stage state, the execution order, and the status.json checkpoint.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from pydantic import Field

from skillspec.config import CONFIG, SkillNamespace
from skillspec.models import DefectGroup, Metadata, Report, VerificationProgress, VerifyKind
from skillspec.models.report import STAGES, NodeProgress, StageName, StageStatus

from .graph import UnifiedGraph
from .stages import StageRunner
from .build import BuildStage
from .reason import ReasonStage
from .verify import VerifyStage
from .utils import GRAPH, STATUS, clear_products, load_model

logger = logging.getLogger(__name__)


class Pipeline(BuildStage, ReasonStage, VerifyStage, StageRunner):
    """Owns the unified graph and runs build/reason/verify over it; owns the run's telemetry accumulator."""

    ns: SkillNamespace
    metadata: Metadata
    unified: UnifiedGraph | None = None
    loaded_defects: dict[VerifyKind, list[DefectGroup]] | None = None  # defect groups loaded from defects.jsonl
    stage_status: dict[StageName, StageStatus] = Field(
        default_factory=lambda: dict.fromkeys(STAGES, "pending")
    )
    reason_nodes: dict[str, NodeProgress] | None = None

    def reconcile(self) -> list[VerificationProgress]:
        """Read saved products and derive truthful states without writing any files."""
        # build
        if self.stage_status["build"] == "completed":
            self._load_graph()

        # reason
        if self.stage_status["reason"] == "completed":
            if self.stage_status["build"] != "completed":
                raise RuntimeError("Reason requires a completed build; use --rerun build.")
            if self.reason_nodes is not None and any(p.status != "completed" for p in self.reason_nodes.values()):
                self.stage_status["reason"] = "failed"
                self.stage_status["verify"] = "pending"
            else:
                self._load_defects_for_verify()

        # verify
        if self.stage_status["reason"] != "completed":
            return []
        progresses = self._verification_progress(self._candidate_defects_by_kind(), readonly=True)
        if self.stage_status["verify"] == "completed" and any(p.pending() for p in progresses):
            self.stage_status["verify"] = "failed"
        return progresses

    async def run(self) -> None:
        """Resume each unfinished phase once, preserving successful work and disabled states."""
        self._resync()
        for name, enabled, operation in (
            ("build", True, self.build),
            ("reason", CONFIG.spec.enable, self.reason),
            ("verify", CONFIG.verify.enable, self.verify),
        ):
            if self.stage_status[name] == "completed" or not enabled:
                continue
            if any(self.stage_status[stage] != "completed" for stage in STAGES[:STAGES.index(name)]):
                raise RuntimeError(f"Cannot run {name}: enable and complete its upstream stages first.")
            await self._execute(name, operation)

    def _resync(self) -> None:
        """Reconcile with saved products and persist any corrected states."""
        before = self.stage_status.copy()
        progresses = self.reconcile()
        if self.stage_status != before:
            self.report()
            for progress in progresses:
                self._snapshot(progress)

    async def _execute(self, name: StageName, operation: Callable[[], Awaitable[None]]) -> None:
        """Commit running, reset stale products, run once, check, commit the outcome."""
        self._prepare(name)
        self.stage_status[name] = "running"
        self.report()
        failures = self.telemetry.aggregate().failures
        try:
            self._reset(name)
            await operation()
            self._check(name)
            if self.telemetry.aggregate().failures > failures:
                raise RuntimeError(f"{name} did not complete cleanly; progress saved. Use --resume to continue.")
        except BaseException:
            self.stage_status[name] = "failed"
            raise
        else:
            self.stage_status[name] = "completed"
            logger.info("%s: stage completed.", name.capitalize())
        finally:
            self.report()

    def _prepare(self, name: StageName) -> None:
        """Validate resume inputs and invalidate downstream state before marking running."""
        if name == "build":
            self.stage_status["reason"] = "pending"
            self.reason_nodes = None
            self.loaded_defects = None
        elif name == "reason":
            self._restore_reason_nodes()
            self.loaded_defects = None
        elif name == "verify":
            # All kind snapshots must exist before verify=running is committed.
            for progress in self._verification_progress(self._candidate_defects_by_kind()):
                self._snapshot(progress)
        if name in ("build", "reason"):
            self.stage_status["verify"] = "pending"

    def _reset(self, name: StageName) -> None:
        """Drop products a fresh run must not inherit — only after running is committed to disk."""
        if name == "build" or (name == "reason" and self.reason_nodes is None):
            clear_products(self.ns.out_dir(), name)
        elif name == "reason":
            self._wipe_audit()

    def _check(self, name: StageName) -> None:
        """Re-validate committed products; reason additionally loads defects for verify."""
        if name == "build":
            graph = load_model(self.ns.artifact(GRAPH), UnifiedGraph)
            if not graph.nodes or self.unified is None or not self.unified.nodes:
                raise RuntimeError("Build produced no graph")
        elif name == "reason":
            self._load_defects_for_verify()

    def report(self) -> Report:
        """Write the run checkpoint (stage state and node judgments) to status.json."""
        report = Report(
            stage_status=self.stage_status,
            reason_nodes=self.reason_nodes,
        )
        self.ns.write_text_artifact(STATUS, report.model_dump_json(indent=2))
        return report
