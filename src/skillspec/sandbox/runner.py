"""Harbor/OpenCode driver: one warm container per skill, one agent run per request."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from skillspec.config import CONFIG, SandboxConfig, SkillNamespace

from . import layout
from .models import SandboxRun, SandboxResult

logger = logging.getLogger(__name__)

# Shared across runs — clear before each run so a failing run can't harvest a prior trajectory.
_AGENT_FILES = (layout.RAW_LOG, layout.TRAJECTORY)
_TEARDOWN_TIMEOUT = 60.0  # bound on pkill / env.stop so a wedged docker compose can't hang the run

# One shared bridge network (created once via `make net`): per-project compose networks each claim
# a subnet from Docker's default pool, which parallel batch runs exhaust.
_NETWORK = "skillspec-net"
# Compose overlay attaching Harbor's `main` service to the shared network; `external: true` stops
# compose from creating/removing the shared one on up/down.
_COMPOSE_NET_YAML = f"""\
services:
  main:
    networks: [skillspec]
networks:
  skillspec:
    name: {_NETWORK}
    external: true
"""


def _sanitize(name: str) -> str:
    """Lowercase to the [a-z0-9_-] charset Harbor requires for compose project names."""
    return re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-") or "skillspec"


def _stage_workspace(env, directory: layout.Directory, run: SandboxRun) -> None:
    host_ws = directory.run_workspace(run.tag)
    host_ws.mkdir(parents=True, exist_ok=True)
    host_ws.chmod(0o777)  # Harbor chmod-777s only mount roots, not this subdir; container runs non-root
    env.task_env_config.workdir = layout.workspace(run.tag)


async def _run_one(directory: layout.Directory, env, agent, agent_dir: Path, run: SandboxRun) -> SandboxResult:
    """Run one instruction and harvest its evidence, recording execution failures."""
    from harbor.models.agent.context import AgentContext

    timeout = CONFIG.verify.sandbox.timeout
    for name in _AGENT_FILES:
        (agent_dir / name).unlink(missing_ok=True)

    ctx = AgentContext()
    returncode = 0
    try:
        _stage_workspace(env, directory, run)
        await asyncio.wait_for(agent.run(run.instruction, env, ctx), timeout=timeout)
        agent.populate_context_post_run(ctx)  # opencode.txt -> trajectory.json
    except asyncio.TimeoutError:
        returncode = -1
        logger.warning("sandbox[%s/%s]: agent.run timed out after %.0fs", directory.skill_id, run.tag, timeout)
        try:  # kill the orphaned opencode so the salvaged trajectory is final
            await asyncio.wait_for(env.exec("pkill -f opencode || true"), timeout=_TEARDOWN_TIMEOUT)
        except Exception:
            logger.debug("sandbox[%s/%s]: pkill opencode failed", directory.skill_id, run.tag)
        _salvage(agent, ctx)
    except Exception:  # incl. NonZeroAgentExitCodeError
        returncode = -1
        logger.exception("sandbox[%s/%s]: agent.run failed", directory.skill_id, run.tag)
        _salvage(agent, ctx)

    try:
        return directory.harvest(run.tag, agent_dir, returncode, run.instruction)
    except Exception:
        logger.exception("sandbox[%s/%s]: harvest failed", directory.skill_id, run.tag)
        return SandboxResult(ok=False, returncode=returncode or -1)
    finally:
        directory.discard_run(run.tag)


def _salvage(agent, ctx) -> None:
    """Best-effort trajectory from partial opencode.txt after a failed/timed-out run."""
    try:
        agent.populate_context_post_run(ctx)
    except Exception:
        pass


def _provider_key(sb: SandboxConfig) -> tuple[str, str]:
    """Env-var name + value opencode reads for the sandbox provider; fails loud when unset."""
    key_var = sb.key_from_env or f"{sb.model.split('/', 1)[0].upper()}_API_KEY"
    value = os.environ.get(key_var)
    if not value:
        raise RuntimeError(
            f"{key_var} is not set — the sandbox provider ({sb.model}) needs its own API key. "
            f"Export {key_var}, or point verify.sandbox.key_from_env at the var that holds it."
        )
    return key_var, value


def _create_env_and_agent(directory: layout.Directory, sb: SandboxConfig, key_var: str, key: str):
    """Build the warm Harbor env + OpenCode agent; return (env, agent, host agent-log dir)."""
    from harbor.agents.factory import AgentFactory
    from harbor.environments.factory import EnvironmentFactory
    from harbor.models.agent.name import AgentName
    from harbor.models.environment_type import EnvironmentType
    from harbor.models.task.config import (
        EnvironmentConfig as TaskEnvironmentConfig,
        NetworkMode,
        NetworkPolicy,
        TaskOS,
    )
    from harbor.models.trial.paths import TrialPaths

    trial_paths = TrialPaths(trial_dir=directory.trial_dir)
    trial_paths.mkdir()
    agent_dir = trial_paths.agent_dir  # host side of the /logs/agent bind
    # extra_docker_compose needs an existing file; trial dir: Harbor uploads env_dir contents
    compose_net = trial_paths.trial_dir / "compose_net.yaml"
    compose_net.write_text(_COMPOSE_NET_YAML, encoding="utf-8")

    task_cfg = TaskEnvironmentConfig(
        docker_image=sb.image,
        os=TaskOS.LINUX,
        network_mode=NetworkMode.PUBLIC,  # the agent must reach its LLM
    )
    safe_id = _sanitize(directory.skill_id)
    # the uuid suffix uniquifies the compose project name only; host paths are shared (prepare()
    # wipes them), so concurrent runs of one skill are not safe
    session = f"skillspec-{safe_id}-{uuid.uuid4().hex[:8]}"
    env = EnvironmentFactory.create_environment(
        EnvironmentType.DOCKER,
        environment_dir=directory.env_dir,
        environment_name=safe_id,
        session_id=f"{session}__env",
        trial_paths=trial_paths,
        task_env_config=task_cfg,
        keep_containers=sb.keep_containers,
        network_policy=NetworkPolicy(network_mode=NetworkMode.PUBLIC),
        persistent_env={key_var: key, "PYTHONDONTWRITEBYTECODE": "1"},
        mounts=directory.mounts(agent_dir),
        extra_docker_compose=[compose_net],
    )
    agent = AgentFactory.create_agent_from_name(
        AgentName.OPENCODE,
        logs_dir=agent_dir,
        model_name=sb.model,
        version=sb.opencode_version,  # pin opencode-ai to the pre-baked image
    )
    return env, agent, agent_dir


async def _upload_skill(env, directory: layout.Directory) -> None:
    """docker cp the skill snapshot once; chmod a+rX so the non-root agent can read it."""
    skill_target = layout.skill_dir(directory.skill_name)
    await env.exec(f"mkdir -p {shlex.quote(layout.SKILLS_ROOT)}", user="root")
    await env.upload_dir(source_dir=str(directory.snapshot_dir.resolve()), target_dir=skill_target)
    await env.exec(f"chmod -R a+rX {shlex.quote(skill_target)}", user="root")


async def _teardown(env, sb: SandboxConfig, skill_id: str) -> None:
    """Attempt bounded container cleanup, shielded from cancellation."""
    stop = asyncio.ensure_future(
        asyncio.wait_for(env.stop(delete=not sb.keep_containers), timeout=_TEARDOWN_TIMEOUT)
    )
    try:
        await asyncio.shield(stop)
    except asyncio.CancelledError:
        await asyncio.shield(stop)  # finish teardown before the cancel propagates
        raise
    except Exception:
        logger.exception("sandbox[%s]: env.stop failed", skill_id)


async def run_sandbox(
    ns: SkillNamespace, runs: list[SandboxRun]
) -> AsyncIterator[tuple[SandboxRun, SandboxResult]]:
    """Run every request into ONE warm container; yield (run, result) as each lands (order-aligned)."""
    # Setup failures leave requests pending; cleanup runs in the finally block.
    sb = CONFIG.verify.sandbox
    directory = layout.Directory.from_namespace(ns)
    if not directory.snapshot_dir.exists():  # fail loud here, not as an opaque Docker bind error later
        raise FileNotFoundError(f"sandbox: snapshot dir not found: {directory.snapshot_dir}")

    key_var, key = _provider_key(sb)  # derive before prepare(): a missing key fails before touching the fs
    directory.prepare()
    env, agent, agent_dir = _create_env_and_agent(directory, sb, key_var, key)

    try:
        try:
            await env.start(force_build=False)  # prebuilt image; empty env_dir -> no upload
            await _upload_skill(env, directory)
            await agent.setup(environment=env)
        except Exception:
            logger.exception("sandbox[%s]: env start/setup failed; %d run(s) skipped", directory.skill_id, len(runs))
            return
        for run in runs:
            yield run, await _run_one(directory, env, agent, agent_dir, run)
    finally:
        await _teardown(env, sb, directory.skill_id)
