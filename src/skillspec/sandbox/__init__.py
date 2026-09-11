"""Verification sandbox substrate: run a coding agent against a skill via Harbor/OpenCode."""

from __future__ import annotations

from . import layout
from .layout import Directory
from .models import SandboxRun, SandboxResult
from .runner import run_sandbox

__all__ = ["Directory", "SandboxResult", "SandboxRun", "layout", "run_sandbox"]
