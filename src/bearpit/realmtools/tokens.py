"""Stateless bearer tokens for the Realmtools MCP server (#39).

Each agent gets a token with its identity (realm, agent, role), the realm's participant roster
and its tool grants baked in and HMAC-signed with a platform secret. The MCP server verifies the
signature and trusts the embedded identity — so an agent cannot submit *as* a peer by passing a
different id (the id isn't a tool argument, it's in the signed token), and by the same reasoning
cannot award itself a tool it was not granted (ADR-004 §4). The roster travels in the token so
the (stateless, container-only) server can report who is still pending a sealed submission
without any extra registration call. Stateless: no shared DB between the minting side and the
server, just the shared secret.

**This module is deployed twice** — in the host Forge that mints, and in the Realmtools container
that verifies — so a change here is only safe if the two can be out of step for a moment. Hence
the field count is tolerated rather than fixed: a token from before grants existed verifies as a
grantless one. Skew then costs nothing instead of reading as an auth failure, which is the least
debuggable symptom this system has.
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
    realm_id: str,
    agent_id: str,
    *,
    is_referee: bool,
    secret: str,
    roster: Sequence[str] = (),
    grants: Sequence[str] = (),
) -> str:
    """Mint a signed token embedding (realm, agent, role, roster, grants).

    realm_id/agent_id/roster ids are lowercase alnum/dash and tool names are `family.verb`
    (both schema-enforced), so `:` (fields) and `,` (lists) are safe delimiters — no value can
    contain either. `roster` is the participant (non-referee) ids expected to submit; `grants`
    is this agent's tool loadout.

    Grants are sorted, so one set of grants always mints one token. An identical loadout
    producing different tokens depending on dict order would make two runs impossible to compare
    and a cached token impossible to recognise.
    """
    role = "referee" if is_referee else "player"
    payload = _SEP.join((realm_id, agent_id, role, ",".join(roster), ",".join(sorted(grants))))
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    return f"{body}.{_sign(payload, secret)}"


def verify_token(
    token: str, secret: str
) -> tuple[str, str, bool, tuple[str, ...], tuple[str, ...]] | None:
    """Return (realm_id, agent_id, is_referee, roster, grants) if the token is valid, else None.

    A four-field payload — minted before grants existed — verifies as a grantless token rather
    than failing, so the host and the Realmtools container can be deployed a moment apart.
    """
    try:
        body, sig = token.split(".", 1)
        padded = body + "=" * (-len(body) % 4)
        payload = base64.urlsafe_b64decode(padded).decode()
        fields = payload.split(_SEP, 4)
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None
    if len(fields) == 4:
        fields = [*fields, ""]
    elif len(fields) != 5:
        return None
    realm_id, agent_id, role, roster_csv, grants_csv = fields
    # AFTER the shape check, and over the payload exactly as it arrived: signing a normalised
    # form would let two different payloads share a signature.
    if not hmac.compare_digest(sig, _sign(payload, secret)):
        return None  # tampered or wrong secret
    roster = tuple(r for r in roster_csv.split(",") if r)
    grants = tuple(g for g in grants_csv.split(",") if g)
    return realm_id, agent_id, role == "referee", roster, grants
