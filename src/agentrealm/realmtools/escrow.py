"""Sealed-submission escrow (M10, §9.5).

The platform is the trusted escrow: an agent submits a payload for a labeled round; peers
cannot see it, and it cannot be changed after sealing (both by construction — physics, not a
protocol the model must run). The escrow reveals atomically once everyone has submitted (or
on an external deadline), then a ruleset tallies the result. This is what let weak models
play fair hidden-move games in the POC without the commit-reveal gymnastics that broke them.

Submissions are chronicled as *markers* (who sealed, not what); the reveal chronicles the
payloads. So the sealed invariant holds while the outcome stays fully auditable.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agentrealm.chronicle import Chronicle

# Domain separation. The same REALMTOOLS_SECRET is the HMAC key for agent identity tokens, so the
# sealing key must be derived along a distinct path — otherwise the two uses share key material and
# a weakness in either reaches the other. The label is versioned so it can be rotated deliberately.
_SEAL_INFO = b"agentrealm/escrow/seal/v1"


def _fernet(secret: str) -> Fernet:
    """A Fernet keyed off the realmtools secret. The escrow already holds that secret to verify
    tokens, so deriving the sealing key from it needs no new key management — but it derives it
    through HKDF with a distinct label rather than reusing the secret's own digest."""
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_SEAL_INFO).derive(
        secret.encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


class SealedError(Exception):
    """A submission/reveal violated the escrow's invariants."""


class SealedEscrow:
    def __init__(
        self, chronicle: Chronicle, realm_id: str, participants: set[str],
        secret: str | None = None,
    ) -> None:
        self._chron = chronicle
        self._realm = realm_id
        self._participants = set(participants)
        self._sealed: dict[str, dict[str, str]] = {}  # round_id -> {agent: payload}
        self._revealed: set[str] = set()
        self._fernet = _fernet(secret) if secret else None
        self._loaded = False  # have we replayed the chronicle into memory yet?

    async def _load(self) -> None:
        """Rebuild sealed/revealed state from the chronicle. realmtools runs as a container the
        operator restarts (deploys); the escrow was in-memory ONLY, so a restart mid-round LOST
        sealed submission — a round could then never be revealed, or worse, be re-sealed. The submit
        marker now carries the ENCRYPTED payload (opaque to a chronicle/event-stream reader; only
        realmtools' secret decrypts it), so a restart recovers exactly where it left off."""
        if self._loaded or self._fernet is None:
            self._loaded = True
            return
        for e in await self._chron.events(self._realm, kind="sealed_submit"):
            rnd, agent, blob = (str(e.payload.get(k, "")) for k in ("round", "agent", "sealed"))
            if not (rnd and agent and blob):
                continue
            try:
                payload = self._fernet.decrypt(blob.encode()).decode()
            except InvalidToken:
                continue  # a marker from before encryption, or a foreign secret — skip it
            self._sealed.setdefault(rnd, {}).setdefault(agent, payload)
        for e in await self._chron.events(self._realm, kind="reveal"):
            self._revealed.add(str(e.payload.get("round", "")))
        self._loaded = True

    async def submit(self, round_id: str, agent: str, payload: str) -> None:
        await self._load()
        if agent not in self._participants:
            raise SealedError(f"{agent!r} is not a participant in this realm")
        if round_id in self._revealed:
            raise SealedError(f"round {round_id!r} already revealed — cannot submit")
        sealed = self._sealed.setdefault(round_id, {})
        if agent in sealed:
            raise SealedError(f"{agent!r} already sealed round {round_id!r} (immutable)")
        sealed[agent] = payload
        # The marker carries the payload ENCRYPTED (opaque to any chronicle/event-stream reader;
        # only realmtools decrypts it), so it survives a realmtools restart. When no secret
        # is configured (tests), fall back to a marker-only event and in-memory state.
        extra = {"sealed": self._fernet.encrypt(payload.encode()).decode()} if self._fernet else {}
        await self._chron.append_event(
            self._realm, "sealed_submit", {"round": round_id, "agent": agent, **extra}
        )

    async def status_async(self, round_id: str) -> dict[str, list[str]]:
        await self._load()
        return self.status(round_id)

    def status(self, round_id: str) -> dict[str, list[str]]:
        """Who has sealed and who is pending — never *what* they sealed."""
        submitted = set(self._sealed.get(round_id, {}))
        return {
            "submitted": sorted(submitted),
            "pending": sorted(self._participants - submitted),
        }

    def all_in(self, round_id: str) -> bool:
        submitted = set(self._sealed.get(round_id, {}))
        return bool(self._participants) and submitted >= self._participants

    async def reveal(self, round_id: str) -> dict[str, str]:
        """Atomically reveal every payload for the round and chronicle them. Idempotency: a
        second reveal raises (the round is closed after the first)."""
        await self._load()
        if round_id in self._revealed:
            raise SealedError(f"round {round_id!r} already revealed")
        payloads = dict(self._sealed.get(round_id, {}))
        self._revealed.add(round_id)
        await self._chron.append_event(
            self._realm, "reveal", {"round": round_id, "submissions": payloads}
        )
        return payloads
