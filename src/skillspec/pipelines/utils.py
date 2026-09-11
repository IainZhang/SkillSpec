"""Shared pipeline plumbing: artifact names and strict reads."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from skillspec.models import VerifyKind
from skillspec.models.report import StageName

M = TypeVar("M", bound=BaseModel)

# Artifact filenames; completion is recorded separately in status.json.
GRAPH = "graph.json"
SPECS = "specs.jsonl"
DEFECTS = "defects.jsonl"
CALLS = "llm_calls.jsonl"  # local LLM transcript; never used for recovery
STATUS = "status.json"
WORKFLOW_JSONL = "workflow.jsonl"
WORKFLOW_PNG = "workflow.png"
CODEGRAPH_PNG = "codegraph.png"


def status_artifact(kind: VerifyKind) -> str:
    """The checkpoint snapshot path for one verify kind (audit/status_code.json | audit/status_workflow.json)."""
    return f"audit/status_{kind}.json"


def load_model(path: Path, model: type[M]) -> M:
    """Read a required saved product or checkpoint. Never fall back to recomputation."""
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"Cannot read {path}; restore the saved file or use a new output.dir. No automatic rerun.") from exc


def read_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file into a list of records (blank lines skipped)."""
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def clear_products(out_dir: Path, stage: StageName) -> None:
    """Remove a stage's products and downstream outputs after invalidation is committed."""
    if stage in ("build", "reason"):
        for name in (SPECS, DEFECTS, CALLS):
            (out_dir / name).unlink(missing_ok=True)
    if stage == "build":
        for name in (GRAPH, WORKFLOW_JSONL, WORKFLOW_PNG, CODEGRAPH_PNG, "workflow", "codegraph"):
            (out_dir / name).unlink(missing_ok=True)
    for name in ("audit", ".sandbox"):
        path = out_dir / name
        if path.exists():
            shutil.rmtree(path)
