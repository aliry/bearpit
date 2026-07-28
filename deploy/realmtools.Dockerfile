# Realmtools MCP server — serves the deterministic mechanics (sealed submissions, scoring,
# verdicts) to agents. Built from the bearpit package; Forge attaches it to each realm network.
#
# Built with `context: ..` (the repo root), so the root .dockerignore decides what reaches the
# daemon. Keep that allowlist tight: this build needs only the package and its lockfile.
FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir uv

# Dependencies first so the layer caches, then the package itself.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
# --frozen installs exactly what uv.lock pins and fails if the lock is stale. The previous
# `uv pip install .` ignored the lockfile entirely and resolved fresh versions at build time,
# which is how a "pinned" image quietly drifts.
RUN uv sync --frozen --no-dev --no-cache

# Nothing here needs root. This server holds the HMAC secret behind every agent identity token, so
# a bug in it should not also be a root shell.
RUN useradd --system --uid 10001 --create-home realmtools
USER realmtools

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 9100
CMD ["python", "-m", "bearpit.realmtools.server"]
