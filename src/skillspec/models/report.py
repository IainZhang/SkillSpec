"""Minimal local checkpoint stored in status.json."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, StrictBool, model_validator

StageName = Literal["build", "reason", "verify"]
StageStatus = Literal["pending", "running", "failed", "completed"]
STAGES: tuple[StageName, ...] = ("build", "reason", "verify")
NodeStatus = Literal["pending", "spec", "reason", "completed"]


class NodeProgress(BaseModel):
    """One phase marker plus the completed judgment; details live in defects.jsonl."""

    status: NodeStatus
    defect: StrictBool | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def valid_defect(self) -> "NodeProgress":
        if (self.status == "completed") != (self.defect is not None):
            raise ValueError("Only completed nodes must carry a boolean defect judgment")
        return self


class Report(BaseModel):
    """Only the state needed to resume a run; not an experiment result."""

    stage_status: dict[StageName, StageStatus] = Field(default_factory=dict)
    reason_nodes: dict[str, NodeProgress] | None = None  # None: not initialized; values name the next unfinished phase
