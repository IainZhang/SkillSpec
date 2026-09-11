"""Sandbox paths: container-side constants + `Directory`, the host<->container model.

The skill snapshot is docker-cp'd (not bound); `Directory` owns host dirs, binds, lifecycle, and harvest.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .models import SandboxResult

if TYPE_CHECKING:
    from skillspec.config import SkillNamespace

logger = logging.getLogger(__name__)

SKILLS_ROOT = "/harbor/skills"  # uploaded-skills root (docker cp, not a bind)
WORKSPACE_ROOT = "/workspace"   # per-run workspace bind; cwd == WORKSPACE_ROOT/<tag>
AGENT_LOGS = "/logs/agent"      # Harbor/OpenCode logs bind

# Workspace-relative names the agent reads/writes (cwd == WORKSPACE_ROOT/<tag>).
RESULT = "result.json"  # the agent's structured verdict (what the sandbox prompt writes)
PROMPT = "prompt.md"  # the run's rendered instruction (report + target unit), written to audit/<tag>/ after the run
TRAJECTORY = "trajectory.json"  # harness-harvested per run (not agent-facing)
RAW_LOG = "opencode.txt"  # opencode's raw JSONL event stream (tee'd into /logs/agent; harvested verbatim)


def workspace(tag: str) -> str:
    return f"{WORKSPACE_ROOT}/{tag}"


def skill_dir(skill_name: str) -> str:
    return f"{SKILLS_ROOT}/{skill_name}"


def prompt_placeholders(skill_name: str, tag: str) -> dict[str, str]:
    """{{Key}} -> path map for the verify prompts; {{CandidateDefect}} is left for render to inline."""
    return {"SkillDir": skill_dir(skill_name), "OutputDir": workspace(tag)}


@dataclass(frozen=True)
class Directory:
    """Host<->container directory model: scratch under .sandbox/ (wiped each run), evidence under audit/."""

    skill_id: str
    skill_name: str
    out_dir: Path  # artifacts/<skill_id>/ — anchors every host path
    snapshot_dir: Path  # artifacts/<skill_id>/snapshot — upload source for the skill

    @classmethod
    def from_namespace(cls, ns: SkillNamespace) -> Directory:
        return cls(ns.skill_id, ns.skill_name, ns.out_dir(), ns.snapshot_dir)

    # --- host scratch (.sandbox/, wiped each run) ---
    @property
    def sandbox_dir(self) -> Path:
        return self.out_dir / ".sandbox"

    @property
    def env_dir(self) -> Path:
        return self.sandbox_dir / "env"  # kept empty: Harbor would upload its contents into the workdir

    @property
    def trial_dir(self) -> Path:
        return self.sandbox_dir / "trial"  # Harbor TrialPaths root; trial/agent == host side of /logs/agent

    @property
    def workspace_dir(self) -> Path:
        return self.sandbox_dir / "workspace"

    def run_workspace(self, tag: str) -> Path:
        return self.workspace_dir / tag

    # --- host evidence (audit/, retained) ---
    @property
    def audit_dir(self) -> Path:
        return self.out_dir / "audit"

    def run_dir(self, tag: str) -> Path:
        return self.audit_dir / tag

    # --- host<->container mapping ---
    def mounts(self, agent_dir: Path) -> list[dict]:
        return [
            {"type": "bind", "source": str(agent_dir.resolve()), "target": AGENT_LOGS},
            {"type": "bind", "source": str(self.workspace_dir.resolve()), "target": WORKSPACE_ROOT},
        ]

    # --- directory lifecycle ---
    def prepare(self) -> None:
        """Wipe scratch and recreate bind roots; audit/ is owned by the stage."""
        if self.sandbox_dir.exists():
            shutil.rmtree(self.sandbox_dir)
        self.sandbox_dir.mkdir(parents=True, exist_ok=True)
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

    def discard_run(self, tag: str) -> None:
        shutil.rmtree(self.run_workspace(tag), ignore_errors=True)

    # --- per-run harvest: scratch -> audit/<tag>/ ---
    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.out_dir))

    def _copy_out(self, src: Path, dest: Path) -> str:
        """Copy a harvested file; drop a stale dest on absent source (re-run safety)."""
        if not src.exists():
            dest.unlink(missing_ok=True)
            return ""
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return self._rel(dest)

    def _harvest_workspace(self, tag: str) -> None:
        src = self.run_workspace(tag)
        if not src.exists():
            return
        try:
            shutil.copytree(src, self.run_dir(tag), dirs_exist_ok=True)
        except OSError as exc:
            logger.warning("sandbox[%s/%s]: workspace harvest failed: %s", self.skill_id, tag, exc)

    def harvest(self, tag: str, agent_dir: Path, returncode: int, prompt: str = "") -> SandboxResult:
        run_dir = self.run_dir(tag)
        shutil.rmtree(run_dir, ignore_errors=True)  # a retry must not inherit the prior attempt's result.json/probes
        traj_src = agent_dir / TRAJECTORY
        readable = False
        if traj_src.exists():
            try:
                json.loads(traj_src.read_text(encoding="utf-8"))
                readable = True
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("sandbox[%s]: trajectory unreadable: %s", self.skill_id, exc)
        self._harvest_workspace(tag)  # creates audit/<tag>
        if prompt:  # evidence: exactly what this run was asked, beside what it produced
            run_dir.mkdir(parents=True, exist_ok=True)  # absent when the run left no workspace
            (run_dir / PROMPT).write_text(prompt, encoding="utf-8")
        trajectory_path = self._copy_out(traj_src, run_dir / TRAJECTORY)
        # Retain the raw log even when trajectory recovery fails.
        raw_log_path = self._copy_out(agent_dir / RAW_LOG, run_dir / RAW_LOG)
        if not trajectory_path and raw_log_path:
            logger.info("sandbox[%s/%s]: no trajectory; raw log retained at %s", self.skill_id, tag, raw_log_path)
        result = run_dir / RESULT  # brought in by _harvest_workspace
        result_path = self._rel(result) if result.exists() else ""
        return SandboxResult(
            ok=returncode == 0 and readable,
            returncode=returncode,
            trajectory_path=trajectory_path,
            result_path=result_path,
            raw_log_path=raw_log_path,
        )
