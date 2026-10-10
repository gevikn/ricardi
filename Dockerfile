# syntax=docker/dockerfile:1
#
# Ricardi: a Slack proxy to Claude Code agents.
#
#   docker compose up -d --build        # see compose.yaml
#
# The image contains Claude Code, because the Agent SDK does not talk to an
# API -- it spawns the `claude` CLI as a child process and drives it over
# stdio. No credentials are baked in: those arrive as a mounted config
# directory at run time, which keeps them out of every layer and every
# registry.
#
# Nothing listens. Socket Mode dials out to Slack, so this image publishes no
# ports and needs none.

FROM python:3.11-slim-bookworm

# git: the agents work in repositories and reach for it constantly.
# ca-certificates + curl: fetching the Claude Code installer below.
# ripgrep: Claude Code uses it for search when present.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl ripgrep \
 && rm -rf /var/lib/apt/lists/*

# A non-root user, because the whole point of this project is that an agent
# should have the reach someone decided on and no more. uid 1000 by default so
# bind-mounted files from a typical Linux host are writable; override at build
# time to match another host (docker build --build-arg UID=$(id -u)).
ARG UID=1000
ARG GID=1000
RUN groupadd -g "$GID" ricardi \
 && useradd -m -u "$UID" -g "$GID" -s /bin/bash ricardi

# Claude Code itself. Installed as the user that will run it, since the
# installer puts it under ~/.local/bin.
USER ricardi
ENV PATH="/home/ricardi/.local/bin:${PATH}"
RUN curl -fsSL https://claude.ai/install.sh | bash \
 && claude --version

WORKDIR /app
COPY --chown=ricardi:ricardi pyproject.toml README.md ./
COPY --chown=ricardi:ricardi src ./src

# --no-cache-dir: the layer is the artefact, a pip cache in it is dead weight.
RUN pip install --no-cache-dir --user -e ".[slack,sdk]"

# Credentials live here and are mounted, never copied. Pointing Claude Code at
# a directory of its own -- rather than mounting a whole host ~/.claude -- means
# the container holds one account's credentials and this project's transcripts,
# not everything you have ever done with Claude Code.
ENV CLAUDE_CONFIG_DIR=/config/claude
VOLUME ["/config/claude", "/app/state", "/workspace"]

# Unbuffered, so `docker logs` shows what happened while it was happening
# rather than when the buffer filled.
ENV PYTHONUNBUFFERED=1

# No HEALTHCHECK: the process holds an outbound WebSocket and exposes nothing
# to probe. A health check would have to ask Slack, which is a cost on a timer
# for no information the logs do not already carry.

ENTRYPOINT ["python", "-m", "ricardi.cli"]
CMD ["--transport", "slack", "--backend", "sdk", \
     "--agents", "/app/agents.toml", "--policy", "/app/policy.toml"]
