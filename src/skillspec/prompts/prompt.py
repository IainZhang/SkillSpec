"""Prompt base class: system text + output schema for an LLM session."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Generic, Protocol, Self, TypeVar

_MD_DIR = Path(__file__).resolve().parent / "md"
_TOKEN = re.compile(r"\{\{(\w+)\}\}")


def read_md(stem: str) -> str:
    """Read `md/{stem}.md` (stripped)."""
    path = _MD_DIR / f"{stem}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"prompt template not found: {path}. Each Prompt reads its own md/<stem>.md; "
            f"create the missing template before using this stage."
        )
    return path.read_text(encoding="utf-8").strip()


class Parsable(Protocol):
    """An output model that validates and self-corrects its own LLM reply text."""

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse the reply into a validated model; raise RetryableError on a parse miss."""
        ...

    @staticmethod
    def correction(error: str) -> str:
        """Retry feedback on a parse miss."""
        ...

    @classmethod
    def tool(cls) -> dict | None:
        """OpenAI tool spec for the reply; None for non-JSON (XML) replies."""
        ...


M = TypeVar("M", bound=Parsable)


class Prompt(Generic[M]):
    """Stage prompt: system text, user template, output schema (subclasses build the user message)."""

    system: str = ""
    template: str = ""
    schema: type[M]  # required: output model that owns parse()/correction()

    @staticmethod
    def render(template: str, **fields: str) -> str:
        """Substitute each `{{Key}}` token in one pass; unknown tokens are left as-is."""
        # single pass: substituted values are never rescanned, so skill content cannot inject fields
        return _TOKEN.sub(lambda m: fields.get(m.group(1), m.group(0)), template)

    def build(self, *args, **kwargs) -> str:
        """Render the user message from stage input."""
        raise NotImplementedError
