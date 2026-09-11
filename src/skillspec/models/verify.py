"""verify-stage products: the agent's per-bug verdict, and the progress snapshotted for checkpoint resume."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .defect import VerifyKind

VerificationDefectStatus = Literal["True", "False", "Skip", "Inconclusive"]

BugStatus = Literal["pending", "passed", "failed"]  # run lifecycle, not a reproduce/refute verdict


class VerifyResult(BaseModel):
    """The sandbox agent's result.json. Agent-authored, so parsed leniently; code-kind extras are ignored."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    defect_status: VerificationDefectStatus | None = None
    trig_instruction: str | None = None
    trig_precondition: str | None = None
    reason: str = ""

    @classmethod
    def load(cls, path: Path) -> VerifyResult | None:
        """Parse a retained result.json; None when missing, unreadable, or off-schema."""
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(obj, dict):
            return None
        try:
            return cls.model_validate(obj)
        except ValidationError:
            return None


class BugProgress(BaseModel):
    """One defect group's verification state + where its retained evidence landed (one sandbox run per group)."""

    index: int  # position in the kind's ordered defect list — the stable resume key
    unit_ids: list[str] = Field(default_factory=list)  # the group's owner, then its other reporters
    status: BugStatus = "pending"
    returncode: int = 0
    trajectory_path: str = ""  # out-dir-relative retained trajectory.json ('' if none)
    result_path: str = ""  # out-dir-relative audit/<tag>/result.json ('' if none)
    raw_log_path: str = ""  # out-dir-relative retained opencode.txt (raw event stream; '' if none)
    instruction: str = ""


class VerificationProgress(BaseModel):
    """Checkpointable verify-stage progress, snapshotted to audit/status_<kind>.json after each group."""

    # A resume attempts pending and failed bugs once; passed bugs retain their evidence.

    skill_id: str = ""
    kind: VerifyKind = "code"  # defect origin this snapshot tracks
    bugs: list[BugProgress] = Field(default_factory=list)

    def pending(self) -> list[BugProgress]:
        """Bugs still needing a successful run, including previous execution failures."""
        return [b for b in self.bugs if b.status != "passed"]
