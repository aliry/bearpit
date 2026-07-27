#!/usr/bin/env bash
# Start the AgentRealm control plane (Gatekeeper API + UI) with the runtime secrets it needs.
#
# The app reads four secrets from the environment (never from a package):
#   LITELLM_MASTER_KEY, REALMTOOLS_SECRET  — kept in deploy/.env (gitignored), sourced below.
#   AGENTREALM_KEYSTORE_KEY                — Fernet key that decrypts ~/.agentrealm/keystore.json.
#   AGENTREALM_SYSTEM_PASSWORD            — the Matrix system user's password (must be stable).
#
# The last two are yours and must stay constant across restarts (a different keystore key can't
# decrypt your stored API keys; a different system password won't match the existing Matrix user).
# Put them in deploy/.env alongside the others, or export them before running this script.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f deploy/.env ] && set -a && . deploy/.env && set +a

# The Docker SDK needs DOCKER_HOST when Docker isn't at the default socket (e.g. colima). Auto-fill
# it from the active docker context so a restart doesn't fail with "cannot connect to Docker".
if [ -z "${DOCKER_HOST:-}" ] && command -v docker >/dev/null 2>&1; then
  DOCKER_HOST=$(docker context inspect 2>/dev/null \
    | python3 -c 'import sys,json;print(json.load(sys.stdin)[0]["Endpoints"]["docker"]["Host"])' 2>/dev/null)
  [ -n "$DOCKER_HOST" ] && export DOCKER_HOST
fi

missing=()
for v in LITELLM_MASTER_KEY REALMTOOLS_SECRET AGENTREALM_KEYSTORE_KEY AGENTREALM_SYSTEM_PASSWORD; do
  [ -n "${!v:-}" ] || missing+=("$v")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing required env: ${missing[*]}" >&2
  echo "Add them to deploy/.env (gitignored) or export them, then re-run." >&2
  exit 1
fi

exec uv run arealm serve --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
