"""VerifyStage — sandbox verification of candidate defects, one agent run per defect group.

Each group (code and workflow) — every finding bound to one unit — is run as its own test inside the
shared warm container; its status and evidence pointers live in a per-kind VerificationProgress snapshot
(audit/status_<kind>.json) rewritten after each bug. Resumable: a re-entry reloads the snapshots and
re-runs pending and failed bugs once per invocation. Passed bugs keep their evidence.
"""

from __future__ import annotations

import json
import shutil
from contextlib import aclosing
from typing import TYPE_CHECKING

from skillspec.config import CONFIG
from skillspec.models import BugProgress, DefectGroup, VerificationProgress, VerifyKind, VerifyResult, empty_by_kind
from skillspec.prompts.render import candidate_bug, render_verify_instruction, unit_block
from skillspec.sandbox import Directory, SandboxRun, run_sandbox

from .utils import load_model, status_artifact

if TYPE_CHECKING:
    from skillspec.config import SkillNamespace
    from skillspec.models import Metadata, StageStats
    from .graph import UnifiedGraph

# Verify code groups before workflow groups in the shared container.
_VERIFY_KINDS: tuple[VerifyKind, ...] = ("code", "workflow")


def _group_unit_ids(group: DefectGroup) -> list[str]:
    """Record the owner followed by its other reporters in the saved bug state."""
    others = {v.unit_id for v in group.verdicts if v.unit_id and v.unit_id != group.unit_id}
    return [group.unit_id, *sorted(others)]


class VerifyStage:
    """Verify stage methods (mixed into Pipeline); one warm container per skill, one agent run per bug."""

    if TYPE_CHECKING:  # shared state declared on Pipeline; here for type checkers only
        ns: SkillNamespace
        metadata: Metadata
        unified: UnifiedGraph | None
        loaded_defects: dict[VerifyKind, list[DefectGroup]] | None

    async def verify(self) -> None:
        """Run each pending bug of both kinds as its own test in one warm container, checkpointing per-kind after each."""
        with self._stage("verify") as (_, stats):
            if not CONFIG.verify.enable:
                self._skip(stats, "Verify: disabled by config (verify.enable=false) — skipping sandbox.")
                return
            by_kind = self._candidate_defects_by_kind()
            progresses = self._verification_progress(by_kind)
            runs, by_tag = self._plan_runs(progresses, by_kind)
            if runs:
                self._info(stats, f"Verify: {len(runs)} bug(s) to run …")
                landed = await self._run_pending(stats, runs, by_tag)
                if landed < len(runs):
                    self._fail(stats, "Verify: sandbox did not finish — progress saved; use --resume to continue.")
            else:
                self._skip(stats, "Verify: no pending bugs — skipping sandbox.")
            # Check all saved work, including failures from earlier invocations.
            unfinished = sum(b.status != "passed" for p in progresses for b in p.bugs)
            if unfinished:
                self._fail(stats, f"Verify: {unfinished} bug run(s) remain unsuccessful; use --resume to retry.", n=unfinished)

    async def _run_pending(
        self,
        stats: "StageStats",
        runs: list[SandboxRun],
        by_tag: dict[str, tuple[VerificationProgress, BugProgress]],
    ) -> int:
        """Run each pending bug, recording its outcome and re-snapshotting its kind after each; return #landed."""
        landed = 0
        try:
            # aclosing: a loop-body failure must run the generator's teardown (container stop) now,
            # not at interpreter shutdown.
            async with aclosing(run_sandbox(self.ns, runs)) as results:
                async for run, result in results:
                    progress, bug = by_tag[run.tag]
                    bug.returncode = result.returncode
                    bug.trajectory_path = result.trajectory_path
                    bug.result_path = result.result_path
                    bug.raw_log_path = result.raw_log_path
                    bug.status = "passed" if result.ok and self._valid_evidence(bug) else "failed"
                    self._snapshot(progress)
                    landed += 1
        except Exception:
            self._fail(stats, "Verify: sandbox driver failed — progress saved; use --resume to continue.", exc=True)
        return landed

    def _valid_evidence(self, bug: BugProgress) -> bool:
        """Execution success is separate from the valid True/False/Skip/Inconclusive conclusion."""
        if bug.returncode != 0 or not bug.result_path or not bug.trajectory_path:
            return False
        result = VerifyResult.load(self.ns.artifact(bug.result_path))
        if result is None or result.defect_status is None:
            return False
        try:
            json.loads(self.ns.artifact(bug.trajectory_path).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return False
        return True

    def _plan_runs(
        self,
        progresses: list[VerificationProgress],
        by_kind: dict[VerifyKind, list[DefectGroup]],
    ) -> tuple[list[SandboxRun], dict[str, tuple[VerificationProgress, BugProgress]]]:
        """Render one verify instruction per pending group of each kind; return the runs and a tag -> (progress, bug) map."""
        runs: list[SandboxRun] = []
        by_tag: dict[str, tuple[VerificationProgress, BugProgress]] = {}
        for progress in progresses:
            kind = progress.kind
            groups = by_kind[kind]
            for bug in progress.pending():
                tag = f"{kind}_{bug.index}"
                group = groups[bug.index]
                node = self.unified.nodes.get(group.unit_id) if self.unified else None
                bug.instruction = render_verify_instruction(
                    self.ns.skill_name, tag, kind,
                    report=candidate_bug(group),
                    unit=unit_block(node.context if node else None, group.unit_id),
                )
                runs.append(SandboxRun(tag=tag, instruction=bug.instruction))
                by_tag[tag] = (progress, bug)
        return runs, by_tag

    def _candidate_defects_by_kind(self) -> dict[VerifyKind, list[DefectGroup]]:
        """Defect groups to verify, keyed by kind — loaded from defects.jsonl when reason was skipped."""
        if self.loaded_defects is not None:
            return self.loaded_defects
        if self.unified and self.unified.nodes:
            return self.unified.defects_by_kind()
        return empty_by_kind()

    def _verification_progress(
        self, by_kind: dict[VerifyKind, list[DefectGroup]], *, readonly: bool = False
    ) -> list[VerificationProgress]:
        """Derive kinds from candidates; stage state decides initialization vs strict resume."""
        resume = self.stage_status["verify"] in ("running", "failed", "completed")
        if not resume and readonly:
            return []
        if not resume:
            self._wipe_audit()
        progresses = []
        for kind in _VERIFY_KINDS:
            if not by_kind[kind]:
                continue
            expected = [(i, _group_unit_ids(g)) for i, g in enumerate(by_kind[kind])]
            if resume:
                progress = load_model(self.ns.artifact(status_artifact(kind)), VerificationProgress)
                if (progress.skill_id != self.ns.skill_id or progress.kind != kind
                        or [(b.index, b.unit_ids) for b in progress.bugs] != expected):
                    raise RuntimeError(f"Verification checkpoint does not match {kind} candidates. No automatic rerun.")
            else:
                progress = VerificationProgress(
                    skill_id=self.ns.skill_id, kind=kind,
                    bugs=[BugProgress(index=i, unit_ids=ids) for i, ids in expected],
                )
            for bug in progress.bugs:
                if bug.status == "passed" and not self._valid_evidence(bug):
                    bug.status = "failed"
            progresses.append(progress)
        if not resume:
            # Save all pending snapshots before Pipeline commits verify=running.
            for progress in progresses:
                self._snapshot(progress)
        return progresses

    def _snapshot(self, progress: VerificationProgress) -> None:
        """Persist one kind's progress as its checkpoint (rewritten after each of its bugs)."""
        self.ns.write_text_artifact(status_artifact(progress.kind), progress.model_dump_json(indent=2))

    def _wipe_audit(self) -> None:
        """Drop the audit/ tree (per-kind status snapshots + per-run evidence) for a fresh run."""
        audit_dir = Directory.from_namespace(self.ns).audit_dir
        if audit_dir.exists():
            shutil.rmtree(audit_dir)
