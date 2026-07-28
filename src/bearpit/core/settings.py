"""Runtime configuration for the control plane.

Loaded from the environment (12-factor). Secrets (LiteLLM master key, provider keys) live
here at run time — never in a project package (§13.5). BYOK provider keys are resolved by
handle against a keystore the Ledger owns; this holds only platform-level config.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

# Matrix localpart of the human operator, unless BEARPIT_OPERATOR_USER says otherwise.
DEFAULT_OPERATOR = "operator"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="BEARPIT_", extra="ignore")

    # Control-plane database (Chronicle + platform state). The default is a convenience for a
    # throwaway local Postgres only — the deploy stack requires a generated POSTGRES_PASSWORD, so
    # set BEARPIT_DATABASE_URL to match it (see deploy/.env.example). A wrong password here
    # surfaces as a connection error at startup, not as silent data loss.
    database_url: str = "postgresql+asyncpg://pit:pit@localhost:5432/bearpit"

    # Matrix bus (Herald). The control plane reaches Conduit at `matrix_homeserver` (host);
    # AGENT CONTAINERS reach it at `matrix_homeserver_internal` (Docker DNS on the realm network).
    matrix_homeserver: str = "http://localhost:6167"
    matrix_homeserver_internal: str = "http://pit-conduit:6167"
    matrix_server_name: str = "realm.local"
    conduit_container: str = "pit-conduit"

    # model proxy (Ledger / LiteLLM). Same split: host URL for the control plane, in-cluster
    # URL for agents (baked into their model config).
    litellm_url: str = "http://localhost:4000"
    litellm_url_internal: str = "http://pit-litellm:4000"
    litellm_container: str = "pit-litellm"

    # Realmtools MCP server (deterministic mechanics; agents reach the in-cluster URL). The
    # secret must match the server's REALMTOOLS_SECRET; without it, mechanics aren't wired.
    realmtools_url_internal: str = "http://pit-realmtools:9100/mcp"
    realmtools_container: str = "pit-realmtools"

    # The human operator's Matrix localpart. Every agent is allowed to hear this user, so an
    # operator can inject a message into a live realm (influence by message, never mid-run
    # control). Set BEARPIT_OPERATOR_USER to use your own account.
    operator_user: str = DEFAULT_OPERATOR

    # docker
    docker_host: str | None = None  # None = default local socket


def load_settings() -> Settings:
    return Settings()
