from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from skillspec.config import CONFIG, SkillNamespace, SpecConfig, load_config
from skillspec.models import View

SAMPLE_YAML = """
llm:
  model: test/model
  url: https://example.test/v1
  key_from_env: TEST_KEY
  concurrency: 3
output:
  dir: ./out
verify:
  sandbox:
    image: test-sandbox:latest
    unknown_sandbox_key: ignored
    model: prov/test-model
unknown_key: ignored
"""


def test_load_config_overlays_ignores_extra_and_builds_paths(tmp_path):
    yaml_path = tmp_path / "c.yaml"
    yaml_path.write_text(SAMPLE_YAML, encoding="utf-8")
    load_config(yaml_path)

    assert (CONFIG.llm.model, CONFIG.llm.concurrency) == ("test/model", 3)
    assert CONFIG.verify.sandbox.model == "prov/test-model"
    assert CONFIG.output.dir == Path("./out")

    ns = SkillNamespace(skill_id="skA", skill_name="my-skill", snapshot_dir=CONFIG.output.snapshot_dir("skA"))
    assert CONFIG.output.snapshot_dir("skA") == CONFIG.output.dir / "skA" / "snapshot"
    assert ns.out_dir() == CONFIG.output.dir / "skA"
    assert ns.artifact("record.jsonl") == CONFIG.output.dir / "skA" / "record.jsonl"
    assert ns.artifact("audit/workflow_0/result.json") == CONFIG.output.dir / "skA" / "audit" / "workflow_0" / "result.json"


def test_api_key_present_and_missing_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "secret-123")
    assert CONFIG.llm.api_key() == "secret-123"
    monkeypatch.delenv("TEST_KEY")
    with pytest.raises(RuntimeError):
        CONFIG.llm.api_key()
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yaml")


def test_fact_views_validator():
    assert SpecConfig(fact_views=[View.SELF, View.SELF, View.LINEAGE]).fact_views == [View.SELF, View.LINEAGE]
    with pytest.raises(ValidationError):
        SpecConfig(fact_views=[View.FULL])
    with pytest.raises(ValidationError):
        SpecConfig(fact_views=[])


@pytest.mark.parametrize("mode", ["direct", "spec"])
def test_removed_mode_setting_is_rejected(mode):
    with pytest.raises(ValidationError):
        SpecConfig.model_validate({"mode": mode})
