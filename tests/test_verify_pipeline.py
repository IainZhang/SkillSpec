from __future__ import annotations

import asyncio
from pathlib import Path

from skillspec.config import CONFIG, SkillNamespace
from skillspec.models import Metadata, SkillManifest, StageStats
from skillspec.pipelines.pipeline import Pipeline


def _pipeline() -> Pipeline:
    ns = SkillNamespace(skill_id="skA", skill_name="my-skill", snapshot_dir=Path("/tmp/skA-snap"))
    return Pipeline(ns=ns, metadata=Metadata(manifest=SkillManifest(name="my-skill")))


def test_candidate_defects_empty_without_graph():
    by_kind = _pipeline()._candidate_defects_by_kind()
    assert set(by_kind) == {"code", "workflow"}
    assert all(v == [] for v in by_kind.values())


def test_run_pending_resilient_on_sandbox_error(tmp_path, monkeypatch):
    CONFIG.output.dir = tmp_path

    async def boom(ns, runs):
        raise OSError("disk full")
        yield

    monkeypatch.setattr("skillspec.pipelines.verify.run_sandbox", boom)
    stats = StageStats(stage="verify")

    landed = asyncio.run(_pipeline()._run_pending(stats, [], {}))

    assert landed == 0
    assert stats.failures == 1