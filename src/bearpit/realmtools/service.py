"""EscrowService — the realm-tools logic behind the MCP server (#39, §9.5).

Holds one SealedEscrow per realm and adjudicates via the deterministic rulesets. Identity
comes from the verified token (never a tool argument), so submissions are correctly attributed
and role gates (referee-only reveal/tally) are enforced here, not in the model. Escrow events
land in the Chronicle, so a run's hidden-move history is auditable and the referee's tally
emits a verdict the Warden can terminate on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.escrow import SealedError, SealedEscrow
from bearpit.realmtools.tally import TallyError, tally


@dataclass(frozen=True)
class Identity:
    realm_id: str
    agent_id: str
    is_referee: bool
    roster: tuple[str, ...] = field(default_factory=tuple)  # participant ids expected to submit


class EscrowService:
    def __init__(self, chronicle: Chronicle | None = None, secret: str | None = None) -> None:
        # chronicle may be deferred (the server connects it inside the serving event loop via a
        # lifespan, so the async engine binds to the right loop) — tests pass it directly.
        self._chron = chronicle
        self._secret = secret  # keys the escrow's at-rest encryption (durable submissions)
        self._escrows: dict[str, SealedEscrow] = {}

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle

    def _chronicle(self) -> Chronicle:
        if self._chron is None:
            raise RuntimeError("EscrowService has no chronicle connected")
        return self._chron

    def _escrow(self, realm_id: str, roster: Sequence[str] = ()) -> SealedEscrow:
        # The roster (from the caller's signed token) is the authoritative expected-submitter set,
        # so `reveal_status` can report who is still pending before anyone has submitted.
        escrow = self._escrows.get(realm_id)
        if escrow is None:
            escrow = SealedEscrow(self._chronicle(), realm_id, set(roster), self._secret)
            self._escrows[realm_id] = escrow
        elif roster:
            escrow._participants.update(roster)  # noqa: SLF001 - service owns the escrow
        return escrow

    async def submit(self, who: Identity, round_id: str, payload: str) -> str:
        if who.is_referee:
            raise PermissionError("referees adjudicate; they do not submit")
        escrow = self._escrow(who.realm_id, who.roster)
        escrow._participants.add(who.agent_id)  # noqa: SLF001 - tolerate a roster-less token
        await escrow.submit(round_id, who.agent_id, payload)
        return f"sealed your move for round {round_id!r} (hidden until the referee reveals)"

    async def status(self, who: Identity, round_id: str) -> dict[str, list[str]]:
        """Who has sealed this round and who is still pending — never *what*. Everyone may call."""
        return await self._escrow(who.realm_id, who.roster).status_async(round_id)

    async def reveal(self, who: Identity, round_id: str) -> dict[str, str]:
        if not who.is_referee:
            raise PermissionError("only the referee may reveal")
        return await self._reveal(who.realm_id, round_id, who.roster)

    async def tally(
        self, who: Identity, round_id: str, ruleset: str,
        config: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if not who.is_referee:
            raise PermissionError("only the referee may tally")
        payloads = await self._reveal(who.realm_id, round_id, who.roster)
        # `config` carries the SCENARIO's rules for a parameterized ruleset (e.g. dominance's beat
        # map) — supplied by the referee at call time, so the platform holds no game-specific logic.
        result = tally(ruleset, payloads, config)
        # A TALLY, never a VERDICT. `referee_verdict` termination fires on ANY verdict event, so
        # recording a round tally as a verdict ended the realm the instant a referee scored its
        # FIRST round — before it could reveal the rest, announce a winner, or call rule(). Every
        # sealed-submit scenario that scores per round (auctions, votes, juries, best-of-N games)
        # was quietly decapitated by its own scorekeeping. Only rule() ends a realm.
        await self._chronicle().append_event(
            who.realm_id, EventKind.TALLY,
            {"round": round_id, "ruleset": ruleset, "outcome": result.result,
             "kind": result.kind, "detail": result.detail},
        )
        return {"result": result.result, "kind": result.kind, "detail": result.detail}

    async def _reveal(
        self, realm_id: str, round_id: str, roster: Sequence[str] = ()
    ) -> dict[str, str]:
        escrow = self._escrow(realm_id, roster)
        if round_id in escrow._revealed:  # noqa: SLF001 - idempotent reveal for tally-after-reveal
            return dict(escrow._sealed.get(round_id, {}))  # noqa: SLF001
        return await escrow.reveal(round_id)


def _short(mxid: str, realm_id: str) -> str:
    """Turn a realm-scoped mxid (@<realm>-<agent>:server) back into the bare agent id, so the
    referee reads friendly ids in turn_status rather than raw Matrix addresses."""
    local = mxid.lstrip("@").split(":", 1)[0]
    prefix = f"{realm_id}-"
    return local[len(prefix):] if local.startswith(prefix) else local


async def read_turn_status(chronicle: Chronicle, realm_id: str) -> dict[str, object]:
    """The current turn state from the latest TURN event: who holds the floor, the order, and
    who is still to come this round. `active` is False when the realm isn't running turns."""
    events = await chronicle.events(realm_id, kind=EventKind.TURN)
    if not events:
        return {"active": False, "detail": "turns are not enabled for this realm"}
    p = events[-1].payload
    order = [_short(str(m), realm_id) for m in p.get("order", [])]
    position = int(p.get("position", 0))
    # THE ROUND A REFEREE RESOLVES IS THE ONE THAT JUST COMPLETED, NOT THE ONE NOW OPEN.
    # At a boundary the manager chronicles the INCREMENTED round (the Arbiter's min_rounds guard
    # reads it), so `round` already says N+1 while the referee is still resolving N. A referee that
    # keyed its reveal off `round` would call reveal('R2') for moves the players sealed as 'R1' and
    # get an empty round back. Both numbers are now explicit, so neither side has to guess.
    completed = next(
        (int(e.payload.get("completed", 0)) for e in reversed(events)
         if e.payload.get("event") == "round_complete"),
        0,
    )
    return {
        "active": True,
        "round": p.get("round"),  # the round now OPEN
        "last_completed_round": completed,  # the round to RESOLVE (0 = none finished yet)
        "current": _short(str(p.get("current", "")), realm_id),
        "order": order,
        "done_this_round": order[:position],
        "upcoming_this_round": order[position + 1 :],
    }


class TurnReader:
    """Read-only view of the turn state for the realmtools server (mirrors ArbiterService's
    chronicle wiring). The floor is DRIVEN by the host-side TurnManager; agents only read here."""

    def __init__(self, chronicle: Chronicle | None = None) -> None:
        self._chron = chronicle

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle

    async def status(self, realm_id: str) -> dict[str, object]:
        if self._chron is None:
            raise RuntimeError("TurnReader has no chronicle connected")
        return await read_turn_status(self._chron, realm_id)


__all__ = ["EscrowService", "Identity", "SealedError", "TallyError", "TurnReader"]
