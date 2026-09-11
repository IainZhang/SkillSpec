from __future__ import annotations

from pathlib import Path

import pytest

from skillspec.config import CONFIG, SkillNamespace
from skillspec.sandbox import Directory, SandboxResult
from skillspec.sandbox.runner import _provider_key, _sanitize


def _dir(tmp_path: Path) -> Directory:
    CONFIG.output.dir = tmp_path
    ns = SkillNamespace(skill_id="skA", skill_name="my-skill", snapshot_dir=tmp_path / "snap")
    return Directory.from_namespace(ns)


def _stage_harvest_inputs(tmp_path: Path, *, result: bool = True, trajectory: str | None = '{"steps": []}'):
    directory = _dir(tmp_path)
    ws = directory.run_workspace("code_0")
    ws.mkdir(parents=True, exist_ok=True)
    if result:
        (ws / "result.json").write_text('{"defect_status":"True"}', encoding="utf-8")
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    if trajectory is not None:
        (agent_dir / "trajectory.json").write_text(trajectory, encoding="utf-8")
    return directory, agent_dir



def test_sanitize_lowercases_and_drops_unsafe_chars():
    assert _sanitize("My Skill!") == "my-skill"
    assert _sanitize("UPPER/Case") == "upper-case"
    assert _sanitize("a..b  c") == "a-b-c"
    assert _sanitize("") == "skillspec"



def test_provider_key_resolution(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("TEST_KEY", "sk-analysis")
    with pytest.raises(RuntimeError):
        _provider_key(CONFIG.verify.sandbox)

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-provider")
    assert _provider_key(CONFIG.verify.sandbox) == ("DEEPSEEK_API_KEY", "sk-provider")

    CONFIG.verify.sandbox.key_from_env = "MY_KEY"
    monkeypatch.setenv("MY_KEY", "sk-explicit")
    assert _provider_key(CONFIG.verify.sandbox) == ("MY_KEY", "sk-explicit")



def test_harvest_ok_when_returncode_zero_and_trajectory_readable(tmp_path):
    directory, agent_dir = _stage_harvest_inputs(tmp_path)

    r = directory.harvest("code_0", agent_dir, returncode=0)

    assert isinstance(r, SandboxResult)
    assert r.ok is True and r.returncode == 0
    assert r.trajectory_path == str(Path("audit/code_0/trajectory.json"))
    assert r.result_path == str(Path("audit/code_0/result.json"))
    assert (directory.run_dir("code_0") / "result.json").read_text(encoding="utf-8") == '{"defect_status":"True"}'


def test_harvest_not_ok_when_returncode_nonzero(tmp_path):
    directory, agent_dir = _stage_harvest_inputs(tmp_path)
    assert directory.harvest("code_0", agent_dir, returncode=2).ok is False


def test_harvest_missing_trajectory_not_ok_and_empty_path(tmp_path):
    directory, agent_dir = _stage_harvest_inputs(tmp_path, trajectory=None)
    r = directory.harvest("code_0", agent_dir, returncode=0)
    assert r.ok is False and r.trajectory_path == ""


def test_harvest_unreadable_trajectory_not_ok_but_mirrored(tmp_path):
    directory, agent_dir = _stage_harvest_inputs(tmp_path, result=False, trajectory="not json")

    r = directory.harvest("code_0", agent_dir, returncode=0)

    assert r.ok is False and r.returncode == 0
    assert r.trajectory_path == str(Path("audit/code_0/trajectory.json"))
    assert r.result_path == ""
