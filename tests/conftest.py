from __future__ import annotations

import pytest

from skillspec.config import CONFIG, Config, LLMConfig, SandboxConfig, VerifyConfig


def base_config() -> Config:
    return Config(
        llm=LLMConfig(model="test/model", url="https://example.test/v1", key_from_env="TEST_KEY"),
        verify=VerifyConfig(
            sandbox=SandboxConfig(
                image="skillspec-sandbox:latest", model="deepseek/deepseek-v4-pro"
            )
        ),
    )


@pytest.fixture(autouse=True)
def config_isolation():
    saved = CONFIG._active
    CONFIG._active = base_config()
    yield
    CONFIG._active = saved
