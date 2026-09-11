"""spec_reason product: the per-unit refinement Verdict, and the DefectGroup it binds into.

A Yes-verdict is a candidate finding; the unit its `location_unit_id` names owns it. All findings
bound to one unit form a DefectGroup — the atom of persistence (one defects.jsonl row) and of
verification (one sandbox run).
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field

from .errors import RetryableError
from .utils import json_object

VerifyKind = Literal["workflow", "code"]  # defect origin: workflow steps vs linked code definitions


class Verdict(BaseModel):
    """Refinement verdict for one unit; decision=True requires a concrete witness."""

    unit_id: str = ""
    decision: bool = False
    candidate_bug: str = ""
    reason: str = ""
    location: str = ""
    location_unit_id: str = ""

    @classmethod
    def parse(cls, text: str) -> "Verdict":
        obj = json_object(text, "verdict")
        verdict_text = str(obj.get("verdict", "")).strip()
        if not verdict_text:
            raise RetryableError("verdict JSON has no 'verdict' field")
        decision = verdict_text.lower().startswith("y")

        # if none is ""
        candidate_bug = reason = location = location_unit_id = ""
        if decision:
            candidate_bug = str(obj.get("candidate_bug", "")).strip()
            reason = str(obj.get("reason", "")).strip()
            location = str(obj.get("location", "")).strip()
            location_unit_id = str(obj.get("location_unit_id", "")).strip()
            if not candidate_bug and not reason:
                raise RetryableError("verdict=Yes but no 'candidate_bug'/'reason'")
            if not location_unit_id:
                raise RetryableError("verdict=Yes but no 'location_unit_id'")
        return cls(
            decision=decision,
            candidate_bug=candidate_bug,
            reason=reason,
            location=location,
            location_unit_id=location_unit_id,
        )

    @staticmethod
    def correction(error: str) -> str:
        return (
            f"That reply did not contain a usable verdict:\n{error}\n\n"
            f"Return a single JSON object with 'verdict' ('Yes' or 'No'); when 'Yes', include flat "
            f"fields 'candidate_bug', 'reason', 'location', and 'location_unit_id'."
        )

    @classmethod
    def tool(cls) -> dict | None:
        return {
            "type": "function",
            "function": {
                "name": "emit_verdict",
                "description": "Return the refinement verdict as specified in the system prompt.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "verdict": {"type": "string", "enum": ["Yes", "No"]},
                        "candidate_bug": {"type": "string"},
                        "reason": {"type": "string"},
                        "location": {"type": "string"},
                        "location_unit_id": {"type": "string"},
                    },
                    "required": ["verdict"],
                },
            },
        }


class DefectGroup(BaseModel):
    """Every Yes-verdict bound to one unit; one group = one defects.jsonl row = one verify run."""

    unit_id: str  # the unit the defect lives in (the owner), a UnifiedGraph.nodes key
    kind: VerifyKind  # the owner's origin
    verdicts: list[Verdict] = Field(default_factory=list)  # witnesses, ordered by producing unit

def empty_by_kind() -> dict[VerifyKind, list[DefectGroup]]:
    """Empty defect buckets, one per VerifyKind — the single source for the kind universe."""
    return {kind: [] for kind in get_args(VerifyKind)}
