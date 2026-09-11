# SPDX-License-Identifier: Apache-2.0
# Trimmed from the NVIDIA OpenShell Community base sandbox image.

# SkillSpec verification sandbox — skill-agnostic, built once and reused across runs.
#
# Provides the env the OpenCode agent runs in: JS/TS + Python toolchains to run and test
# a skill's resource scripts, plus a pre-baked OpenCode CLI. The skill under test is NOT
# baked in — the harness bind-mounts it read-only at /skill per run (no opencode skill
# discovery). OpenCode is pre-installed so the patched Harbor (which prechecks for an
# existing opencode) skips its NVM install.
#
# Build:  make image   (docker build -t skillspec-sandbox:latest .)

FROM nvcr.io/nvidia/base/ubuntu:noble-20251013 AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /sandbox

# Core prereqs: curl (toolchain installs), git + build-essential (agent builds/tests + native npm addons).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged agent user with a writable HOME and login bash (Harbor needs ~/.nvm and ~/.config/opencode).
RUN groupadd -r sandbox && useradd -r -g sandbox -d /sandbox -s /bin/bash sandbox

# JS/TS env + agent CLI: Node.js 22 + TypeScript toolchain + pre-baked OpenCode.
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g typescript tsx opencode-ai@1.18.15

# Python env: uv-managed Python 3.14 + a writable venv the agent can `uv pip install` into.
COPY --from=ghcr.io/astral-sh/uv:0.10.8 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR="/sandbox/.uv/python" \
    PATH="/sandbox/.venv/bin:/usr/local/bin:/usr/bin:/bin" \
    VIRTUAL_ENV="/sandbox/.venv"
RUN uv python install 3.14.3 \
    && ln -s "$(uv python find 3.14.3)" /usr/local/bin/python3 \
    && ln -s "$(uv python find 3.14.3)" /usr/local/bin/python \
    && uv venv --python 3.14.3 --seed /sandbox/.venv \
    && uv cache clean \
    # Minimal shell init so login shells (Harbor sources ~/.nvm/nvm.sh from one) get a sane PATH.
    && printf 'export PATH="/sandbox/.venv/bin:/usr/local/bin:/usr/bin:/bin"\nexport VIRTUAL_ENV="/sandbox/.venv"\nexport UV_PYTHON_INSTALL_DIR="/sandbox/.uv/python"\nexport PS1="\\u@\\h:\\w\\$ "\n' \
        > /sandbox/.bashrc \
    && printf '[ -f ~/.bashrc ] && . ~/.bashrc\n' > /sandbox/.profile \
    && chown -R sandbox:sandbox /sandbox

USER sandbox

# No ENTRYPOINT: Harbor's prebuilt compose keeps the container alive with its own
# CMD (`sh -c "sleep infinity"`) and execs agent commands into it. A bash entrypoint
# would prepend to that CMD (`/bin/bash sh -c …`), so the keep-alive fails with exit
# 126 and the trial dies before the agent starts. Reset any inherited entrypoint.
ENTRYPOINT []
