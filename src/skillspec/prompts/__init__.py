"""Stage prompts: the `Prompt` base class and one Prompt per stage."""

from __future__ import annotations

from skillspec.prompts.prompt import Prompt
from skillspec.prompts.reason import REASON
from skillspec.prompts.spec import SPEC
from skillspec.prompts.workflow import WORKFLOW

__all__ = [
    "Prompt",
    "WORKFLOW",
    "SPEC",
    "REASON",
]
