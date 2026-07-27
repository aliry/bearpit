"""Stateless bearer tokens for the Realmtools MCP server (#39).

Each agent gets a token with its identity (realm, agent, role) and the realm's participant
roster baked in and HMAC-signed with a platform secret. The MCP server verifies the signature
and trusts the embedded identity — so an agent cannot submit *as* a peer by passing a different
id (the id isn't a tool argument, it's in the signed token). The roster travels in the token so
the (stateless, container-only) server can report who is still pending a sealed submission
without any extra registration call. Stateless: no shared DB between the minting side and the
server, just the shared secret.
"""

from __future__ import annotations

import base64
import hmac
from collections.abc import Sequence
from hashlib import sha256

_SEP = ":"


def _sign(payload: str, secret: str) -> str:
    mac = hmac.new(secret.encode(), payload.encode(), sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def mint_token(
    realm_id: str, agent_id: str, *, is_referee: bool, secret: str, roster: Sequence[str] = ()
) -> str:
    """Mint a signed token embedding (realm, agent, role, roster). realm_id/agent_id/roster ids
    are lowercase alnum/dash (schema-enforced), so `:` (fields) and `,` (roster) are safe
    delimiters. `roster` is the participant (non-referee) agent ids expected to submit."""
    role = "referee" if is_referee else "player"
    payload = _SEP.join((realm_id, agent_id, role, ",".join(roster)))
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{body}.{_sign(payload, secret)}"


def verify_token(token: str, secret: str) -> tuple[str, str, bool, tuple[str, ...]] | None:
    """Return (realm_id, agent_id, is_referee, roster) if the token is valid, else None."""
    try:
        body, sig = token.split(".", 1)
        padded = body + "=" * (-len(body) % 4)
        payload = base64.urlsafe_b64decode(padded).decode()
        realm_id, agent_id, role, roster_csv = payload.split(_SEP, 3)
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None
    if not hmac.compare_digest(sig, _sign(payload, secret)):
        return None  # tampered or wrong secret
    roster = tuple(r for r in roster_csv.split(",") if r)
    return realm_id, agent_id, role == "referee", roster
