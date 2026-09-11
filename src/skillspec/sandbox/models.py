"""I/O models for the Harbor/OpenCode verification sandbox driver."""

from __future__ import annotations

from pydantic import BaseModel


class SandboxRun(BaseModel):
    """One agent run inside the warm container: an instruction graded via its harvested evidence."""

    tag: str  # per-run namespace, e.g. "code_0"
    instruction: str  # NL task handed straight to agent.run (caller-supplied); harvested as prompt.md


class SandboxResult(BaseModel):
    """Outcome of one run."""

    ok: bool
    returncode: int
    trajectory_path: str = ""  # out-dir-relative
    result_path: str = ""  # out-dir-relative retained result.json ('' if none)
    raw_log_path: str = ""  # out-dir-relative retained opencode.txt (raw event stream; '' if none)
