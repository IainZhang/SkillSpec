"""Single source of config: paths, tunables, and the active LLM provider."""

from __future__ import annotations

import json
import os
from pathlib import Path
from openai import AsyncOpenAI

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from skillspec.models import View

DEFAULT_CONFIG_PATH = Path("config.yaml")  # cwd-relative; overridden by --config

# Strict validation for configuration sections that use this policy.
_STRICT = ConfigDict(extra="forbid")


class LLMConfig(BaseModel):
    model_config = _STRICT

    model: str  # required: no default provider/model
    url: str  # required: provider base URL
    key_from_env: str  # required: env var name holding the API key
    concurrency: int = 30  # max concurrent agent queries
    min_request_interval: float = 0.5   # avoid transient concurrency
    max_retries: int = 3
    timeout: float = 600.0
    reasoning_effort: str | None = "high"  # canonical effort; mapped per-provider in llm.py; None omits it

    def api_key(self) -> str:
        key = os.environ.get(self.key_from_env, "")
        if not key:
            raise RuntimeError(
                f"{self.key_from_env} is not set. Export it — API keys are read from the "
                f"environment only (the config file names the var via llm.key_from_env)."
            )
        return key

    def client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=self.api_key(),
            base_url=self.url,
            max_retries=self.max_retries,
            timeout=self.timeout,
        )


class TelemetryConfig(BaseModel):
    """Local Phoenix / OpenInference observability."""

    model_config = _STRICT

    enabled: bool = True
    endpoint: str = "http://localhost:6006/v1/traces"
    project: str = "SkillSpec"
    api_key: str | None = None


class SpecConfig(BaseModel):
    """Reason-stage tunables for specification-driven analysis."""

    model_config = _STRICT

    enable: bool = False  # master switch for node-level defect reasoning
    # Masked FactSpec views to generate; Full View (ExpectSpec) is always added.
    fact_views: list[View] = Field(
        default_factory=lambda: [View.SELF, View.NEIGHBORS, View.LINEAGE, View.HOLISTIC]
    )

    @field_validator("fact_views")
    @classmethod
    def _masked_only(cls, v: list[View]) -> list[View]:
        allowed = (View.SELF, View.NEIGHBORS, View.LINEAGE, View.HOLISTIC)
        views = list(dict.fromkeys(v))  # dedupe, preserve order
        if not views:
            raise ValueError("fact_views must not be empty")
        bad = [x.value for x in views if x not in allowed]
        if bad:
            raise ValueError(
                f"fact_views must be a subset of {[a.value for a in allowed]} (Full View is ExpectSpec); got {bad}"
            )
        return views


class SandboxConfig(BaseModel):
    """Harbor/OpenCode verification substrate (one warm Docker container per skill)."""

    model_config = ConfigDict(extra="ignore")

    image: str = "skillspec-sandbox:latest"
    opencode_version: str = "1.18.15"  # pin opencode-ai; keep in sync with Dockerfile's npm install
    model: str = "deepseek/deepseek-v4-pro"  # opencode provider/model, not llm.model
    key_from_env: str | None = None  # env var opencode reads for this provider (its models.dev `env`); None -> derive <PROVIDER>_API_KEY
    timeout: float = 600.0  # per-run agent.run wall-clock
    keep_containers: bool = False  # debugging: leave the container stopped (not removed) after the run

class VerifyConfig(BaseModel):
    """Verify-stage sandbox substrate."""

    model_config = _STRICT

    enable: bool = False  # master switch for the sandbox verify stage
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)


class OutputConfig(BaseModel):
    """Output root and log level; per-skill paths live on SkillNamespace."""

    model_config = _STRICT

    dir: Path = Path("./artifacts")  # output root
    log_level: str = "info"

    def snapshot_dir(self, skill_id: str) -> Path:
        return self.dir / skill_id / "snapshot"  # skill-scoped; reused across runs, resolved before a SkillNamespace exists


class SkillNamespace(BaseModel):
    """The per-skill output namespace — identity plus bound paths, threaded through the pipeline."""

    skill_id: str
    skill_name: str  # kebab SKILL.md `name`; the skill folder name for opencode discovery
    snapshot_dir: Path  # artifacts/<skill_id>/snapshot — source of the skill, uploaded to /harbor/skills/<skill_name>/

    def out_dir(self) -> Path:
        return CONFIG.output.dir / self.skill_id  # stable per-skill directory

    def artifact(self, name: str) -> Path:
        return self.out_dir() / name

    def write_text_artifact(self, name: str, text: str) -> Path:
        """Write `text` to a per-skill artifact atomically (parent dirs created); returns the path."""
        path = self.artifact(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        # tmp + replace: preserve the previous checkpoint or product until the write completes.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return path

    def write_jsonl(self, name: str, records: list[object]) -> Path:
        """Write `records` as JSON Lines to a per-skill artifact; returns the path."""
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
        return self.write_text_artifact(name, body)

    def log_file_path(self) -> Path:
        return self.out_dir() / "output.log"

    # Sandbox/verify host paths live on sandbox.layout.Directory, built from this namespace via from_namespace().


class Config(BaseModel):
    """Top-level config: nested groups plus run-wide tunables."""

    model_config = ConfigDict(extra="ignore")

    output: OutputConfig = Field(default_factory=OutputConfig)
    llm: LLMConfig
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    spec: SpecConfig = Field(default_factory=SpecConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def load_config(path: str | Path | None = None) -> None:
    """Build CONFIG from YAML; raise if a required field (e.g. llm.model) is missing."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if path is not None and not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    CONFIG._active = Config(**_read_yaml(p))


class _ConfigProxy:
    """Stable handle imported as CONFIG; forwards to the Config built by load_config()."""

    __slots__ = ("_active",)

    def __init__(self) -> None:
        self._active: Config | None = None

    def __getattr__(self, name: str):
        if self._active is None:
            raise RuntimeError("config not loaded — call load_config() first")
        return getattr(self._active, name)


CONFIG: Config = _ConfigProxy()  # type: ignore[assignment]  # annotated as Config for IDE help
