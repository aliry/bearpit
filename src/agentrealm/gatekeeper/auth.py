"""A local access token for the control-plane API.

This origin can start realms, spend real money, read every transcript, private note and revealed
submission, and take an arbitrary host filesystem path as a scenario package. It binds to loopback,
which stops the network but not everything else on the machine — nor a page in the operator's own
browser, which is why the cross-site write guard in `api.py` exists alongside this.

The shape is the one Jupyter uses, because it solves the browser problem without a login form:

  * a token is generated once and kept in `~/.agentrealm/api-token` (0600);
  * `arealm serve` prints the URL with `?token=…` in it;
  * opening that URL exchanges the token for an HttpOnly, SameSite=Strict cookie, so the console
    keeps working with no further ceremony;
  * scripts and the CLI send `Authorization: Bearer <token>` instead.

This is a *local* control, not multi-user auth. There are no accounts, no roles, and no revocation
beyond deleting the file. It raises the bar from "anything that can reach the port" to "anything
that has been told the token", which is the honest description and the one in SECURITY.md.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path

COOKIE_NAME = "agentrealm_token"
QUERY_PARAM = "token"
HEADER = "authorization"
ENV_VAR = "AGENTREALM_API_TOKEN"

# Paths served without a token: the console shell and its assets (which contain nothing sensitive
# and must load before the cookie exchange can happen), plus the OpenAPI docs.
PUBLIC_PREFIXES = ("/static/", "/docs", "/openapi.json", "/redoc", "/favicon")


def token_path() -> Path:
    return Path.home() / ".agentrealm" / "api-token"


def load_or_create_token() -> str:
    """The API token, generated on first use.

    `AGENTREALM_API_TOKEN` wins if set — that is how you pin a known value across restarts, or
    share one between the platform and something fronting it.
    """
    from_env = os.environ.get(ENV_VAR, "").strip()
    if from_env:
        return from_env
    path = token_path()
    if path.is_file():
        existing = path.read_text().strip()
        if existing:
            return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return token


def presented_token(
    *, header: str | None, cookie: str | None, query: str | None
) -> str | None:
    """The token the caller presented, from whichever of the three channels carried one."""
    if header:
        scheme, _, value = header.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return (cookie or query or "").strip() or None


def token_matches(expected: str, presented: str | None) -> bool:
    """Constant-time comparison — a timing oracle on a local token is still a timing oracle."""
    if not presented:
        return False
    return hmac.compare_digest(expected, presented)


def is_public_path(path: str) -> bool:
    """Whether `path` is served without a token.

    `/` is public deliberately: it is the page that performs the cookie exchange, and it exposes
    nothing but the console shell. Everything under `/api/` is not.
    """
    return path == "/" or path.startswith(PUBLIC_PREFIXES)
