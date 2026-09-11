"""SkillSpec — run-lifecycle orchestrator"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from pydantic import BaseModel

from skillspec.config import CONFIG, SkillNamespace
from skillspec.models import Metadata, Report
from skillspec.models.report import STAGES, StageName
from skillspec.pipelines import Pipeline
from skillspec.pipelines.utils import STATUS, clear_products, load_model
from skillspec.trace import tracer

logger = logging.getLogger(__name__)

_run_log_handler: logging.FileHandler | None = None


class SkillSpec(BaseModel):

    repo_dir: Path = Path()
    resume: bool = False
    rerun: StageName | None = None
    ns: SkillNamespace | None = None
    checkpoint: Report | None = None

    @classmethod
    def init(cls, repo: str | Path | None = None, *, resume: bool = False, rerun: StageName | None = None) -> "SkillSpec":
        repo_dir = Path(repo) if repo else Path("skillrepos")
        return cls(repo_dir=repo_dir, resume=resume, rerun=rerun)

    def status(self) -> dict:
        """Show reconciled progress without checkpoint writes or evidence cleanup."""
        metadata = self._parse()
        path = self.ns.artifact(STATUS)
        self.checkpoint = self._load_report() if path.exists() else None
        base = {"output_dir": str(self.ns.out_dir())}
        if self.checkpoint is None:
            return {**base, "checkpoint": "missing", "stage_status": {}, "reason_nodes": None, "verification": {}}
        if self.checkpoint.stage_status["build"] == "completed":
            metadata = self._saved_metadata()
        pipeline = self._pipeline(metadata)
        progresses = pipeline.reconcile()
        return {
            **base,
            "checkpoint": "available",
            "stage_status": pipeline.stage_status,
            "reason_nodes": self.checkpoint.model_dump(mode="json")["reason_nodes"],
            "verification": {
                progress.kind: {state: sum(b.status == state for b in progress.bugs)
                                for state in ("pending", "passed", "failed")}
                for progress in progresses
            },
        }

    async def run(self) -> None:
        """Execute one fresh run, recovery pass, or explicitly requested stage rerun."""
        metadata = self.prepare()
        pipeline = self._pipeline(metadata)

        logger.info("Run — skill=%s, model=%s.", self.ns.skill_id, CONFIG.llm.model)
        span_name = f"{CONFIG.llm.model}@{metadata.manifest.name}"
        with tracer.span(span_name) as span:
            try:
                await pipeline.run()
            finally:
                tracer.annotate(span, pipeline.telemetry.aggregate("pipelines"))

    def prepare(self) -> Metadata:
        """Validate reuse, or commit invalidation before replacing products for a rerun."""
        metadata = self._parse()

        # guards
        if self.resume and self.rerun is not None:
            raise RuntimeError("--resume and --rerun are mutually exclusive.")
        enabled = {"build": True, "reason": CONFIG.spec.enable, "verify": CONFIG.verify.enable}
        if self.rerun is not None and not enabled[self.rerun]:
            raise RuntimeError(f"Enable {self.rerun} in config before requesting --rerun {self.rerun}.")
        out = self.ns.out_dir()
        if not self.resume and self.rerun is None and out.exists() and any(out.iterdir()):
            raise RuntimeError(f"Output already exists: {out}. Use --resume, --rerun, or choose a new output.dir.")

        # dispatch
        if self.resume:
            metadata = self._begin_resume()
        elif self.rerun not in (None, "build"):
            metadata = self._begin_rerun()
        else:
            self._begin_fresh()

        self._attach_log()
        return metadata

    def _begin_fresh(self) -> None:
        """Commit a pending checkpoint, then replace the snapshot and build products."""
        self.checkpoint = Report(stage_status=dict.fromkeys(STAGES, "pending"))
        self._commit()
        clear_products(self.ns.out_dir(), "build")
        self._snapshot()

    def _begin_resume(self) -> Metadata:
        self.checkpoint = self._load_report()
        return self._saved_metadata()

    def _begin_rerun(self) -> Metadata:
        self.checkpoint = self._reset_report(self._load_report())
        metadata = self._saved_metadata()
        upstream = self._pipeline(metadata)
        upstream.reconcile()
        if any(upstream.stage_status[name] != "completed" for name in STAGES[:STAGES.index(self.rerun)]):
            raise RuntimeError(f"Cannot rerun {self.rerun}: its upstream stages have unfinished work.")
        self._commit()
        clear_products(self.ns.out_dir(), self.rerun)
        return metadata

    def _commit(self) -> None:
        self.ns.write_text_artifact(STATUS, self.checkpoint.model_dump_json(indent=2))

    def _load_report(self) -> Report:
        report = load_model(self.ns.artifact(STATUS), Report)
        if set(report.stage_status) != set(STAGES):
            raise RuntimeError("status.json has no explicit run checkpoint; use --rerun build.")
        return report

    def _reset_report(self, report: Report) -> Report:
        """A rerun discards only the target state; its upstream checkpoints remain required."""
        start = STAGES.index(self.rerun)
        for name in STAGES[:start]:
            if report.stage_status[name] != "completed":
                raise RuntimeError(
                    f"Cannot rerun {self.rerun}: upstream {name} has not completed; use --rerun build."
                )
        return report.model_copy(update={
            "stage_status": {**report.stage_status, **dict.fromkeys(STAGES[start:], "pending")},
            "reason_nodes": None if self.rerun == "reason" else report.reason_nodes,
        })

    def _parse(self) -> Metadata:
        """Resolve the output namespace without creating files (also used by --status)."""
        metadata, message = Metadata.parse(self.repo_dir)
        if metadata.manifest is None:
            raise RuntimeError(f"skill failed structural compliance: {message}")
        skill_id = metadata.id() or self.repo_dir.name
        snapshot_dir = CONFIG.output.snapshot_dir(skill_id)
        self.ns = SkillNamespace(
            skill_id=skill_id,
            skill_name=metadata.manifest.name,
            snapshot_dir=snapshot_dir,
        )
        if message:
            logger.warning("Parse: skill tolerated non-fatal compliance issue: %s", message)
        return metadata

    def _saved_metadata(self) -> Metadata:
        metadata, message = Metadata.parse(self.ns.snapshot_dir)
        if metadata.manifest is None:
            raise RuntimeError(f"Cannot read saved snapshot ({message}); use --rerun build.")
        return metadata

    def _snapshot(self) -> None:
        """Replace the saved source snapshot; called only after the pending STATUS commit."""
        if self.ns.snapshot_dir.exists():
            shutil.rmtree(self.ns.snapshot_dir)
        shutil.copytree(self.repo_dir, self.ns.snapshot_dir, ignore=shutil.ignore_patterns(".*"))
        logger.info("Parse: snapshotted skill into %s.", self.ns.snapshot_dir)

    def _pipeline(self, metadata: Metadata) -> Pipeline:
        pipeline = Pipeline(ns=self.ns, metadata=metadata)
        if self.checkpoint is not None:
            pipeline.stage_status = self.checkpoint.stage_status.copy()
            pipeline.reason_nodes = self.checkpoint.reason_nodes.copy() if self.checkpoint.reason_nodes is not None else None
        return pipeline

    def _attach_log(self) -> None:
        global _run_log_handler
        if _run_log_handler is not None:  # swap, don't stack: a prior skill's log must not collect this run
            logging.getLogger().removeHandler(_run_log_handler)
            _run_log_handler.close()
        log_path = self.ns.log_file_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        _run_log_handler = handler
