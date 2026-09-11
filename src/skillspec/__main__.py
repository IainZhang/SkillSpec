"""CLI entry point: `python -m skillspec`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

import rich.traceback
from rich.console import Console
from rich.logging import RichHandler

from skillspec.config import CONFIG, load_config
from skillspec.skill import SkillSpec
from skillspec.trace import tracer


def init_console_logging(level: str) -> None:
    """Boot-time console logging; the per-skill file handler is added later in prepare."""
    console = RichHandler(
        console=Console(stderr=True),
        rich_tracebacks=True,
        markup=False,  # %r ids contain brackets
        show_path=False,
    )
    console.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    logging.basicConfig(level=level.upper(), handlers=[console])


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="skillspec",
        description="Analyze a Claude Agent Skill repository for workflow- and code-level defects.",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Path to the skill repo to analyze (default: skillrepos/).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to the provider config YAML (default: config.yaml in the cwd).",
    )
    
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="Resume unfinished work and retry failed tasks once.")
    mode.add_argument("--rerun", choices=("build", "reason", "verify"), help="Rerun a stage and invalidate its downstream results.")
    mode.add_argument("--status", action="store_true", help="Show saved progress as JSON without running analysis.")
    args = parser.parse_args()

    load_config(args.config)
    rich.traceback.install(show_locals=False)
    init_console_logging(CONFIG.output.log_level)
    skill = SkillSpec.init(args.repo, resume=args.resume, rerun=args.rerun)
    try:
        if args.status:
            print(json.dumps(skill.status(), indent=2))
            return
        tracer.register()
        asyncio.run(skill.run())
    except RuntimeError as exc:
        parser.exit(1, f"skillspec: {exc}\n")


if __name__ == "__main__":
    main()
