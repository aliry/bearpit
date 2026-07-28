"""Arbiter — referee scoring + verdicts backed by a platform-maintained scoreboard (#18, §10).

The load-bearing lesson (proven twice in the RPS POCs): an LLM referee judges reliably per
decision but CANNOT hold a cumulative score in its context — the running tally drifts and even
resets. So the referee *decides* (score this round, penalize this violation, rule the winner)
and the *platform* keeps the authoritative running total. State lives here + in the Chronicle,
never in the model. Every change is a SCORE/VIOLATION/VERDICT event, so the scoreboard is
auditable and flows into the final report.

All mutating tools are referee-only (identity from the verified token, not a claim). The
referee can judge and record; it explicitly CANNOT stop or steer agents — that stays with the
platform (influence by message, control by kill).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.service import Identity


class ArbiterService:
    def __init__(self, chronicle: Chronicle | None = None) -> None:
        self._chron = chronicle
        self._scores: dict[str, dict[str, float]] = {}  # realm -> {agent: cumulative}
        self._fills: dict[str, asyncio.Lock] = {}  # realm -> guards its one-time cache fill

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle

    def _c(self) -> Chronicle:
        if self._chron is None:
            raise RuntimeError("ArbiterService has no chronicle connected")
        return self._chron

    async def _board(self, realm_id: str) -> dict[str, float]:
        """The running board for a realm, rebuilt from the chronicle on first access.

        realmtools runs as a container the operator restarts (deploys), and this board was held in
        process memory ONLY — a restart mid-realm silently reset every score to zero, so the referee
        would rule on a blank scoreboard. The SCORE events are the durable record; sum them once,
        then keep serving from memory.

        The fill is locked per realm because its chronicle read is an await, and callers mutate the
        dict this returns. Two scores landing inside that window each built their own dict and the
        later store discarded the earlier one's increment — while both still appended their SCORE
        event, so the chronicle ended up one point ahead of the board the referee ruled on (#17,
        realm rps-rv1: a 3-2 ledger under a 2-2 ruling, silently).

        Per realm, not one global lock: the read is held across a database round-trip, and in the
        wild that stalled for 97 seconds. A shared lock would have blocked every other realm's
        first score behind it."""
        cached = self._scores.get(realm_id)
        if cached is not None:
            return cached
        # setdefault is atomic here — no await between the lookup and the store
        async with self._fills.setdefault(realm_id, asyncio.Lock()):
            # re-check: whoever held the lock before us has already filled it, and we must return
            # THAT dict, not a second one, or their increments are lost exactly as before
            cached = self._scores.get(realm_id)
            if cached is not None:
                return cached
            board: dict[str, float] = {}
            for e in await self._c().events(realm_id, kind=EventKind.SCORE):
                a = str(e.payload.get("agent", ""))
                board[a] = round(board.get(a, 0.0) + float(e.payload.get("delta", 0.0)), 6)
            self._scores[realm_id] = board
            return board

    @staticmethod
    def _require_referee(who: Identity, action: str) -> None:
        if not who.is_referee:
            raise PermissionError(f"only the referee may {action}")

    async def score(self, who: Identity, agent: str, delta: float, reason: str) -> dict[str, float]:
        """Award (or deduct, if delta<0) points to an agent. Returns the running scoreboard."""
        self._require_referee(who, "score")
        board = await self._board(who.realm_id)
        board[agent] = round(board.get(agent, 0.0) + float(delta), 6)
        await self._c().append_event(
            who.realm_id, EventKind.SCORE,
            {"agent": agent, "delta": float(delta), "reason": reason, "issued_by": who.agent_id},
        )
        return dict(board)

    async def flag(self, who: Identity, agent: str, reason: str) -> dict[str, str]:
        """Record a rule violation against an agent (no score change on its own)."""
        self._require_referee(who, "flag")
        await self._c().append_event(
            who.realm_id, EventKind.VIOLATION,
            {"agent": agent, "reason": reason, "issued_by": who.agent_id},
        )
        return {"flagged": agent, "reason": reason}

    async def penalize(
        self, who: Identity, agent: str, amount: float, reason: str
    ) -> dict[str, float]:
        """Flag a violation AND deduct points for it. Returns the running scoreboard."""
        self._require_referee(who, "penalize")
        await self.flag(who, agent, reason)
        return await self.score(who, agent, -abs(float(amount)), f"penalty: {reason}")

    async def scoreboard(self, who: Identity) -> dict[str, float]:
        """The authoritative running score — read this instead of tracking it yourself."""
        return dict(await self._board(who.realm_id))

    # tokens meaning "this round ends with nobody ejected" — a tie is a first-class resolution
    _NO_ELIMINATION = {"none", "nobody", "no one", "no-one", "n/a", "tie", "-", ""}

    async def eliminate(self, who: Identity, agent: str, reason: str = "") -> dict[str, Any]:
        """Eject a player from the game (or close a round with no ejection). Chronicled as an
        ELIMINATION event; the host enforces it as physics — the named player is dropped from the
        turn rotation and can no longer act. Tool-based on purpose: parsing an `ELIMINATED: <name>`
        control line out of referee prose silently failed whenever the model dressed the line in
        markdown (among-us-tele3/4) — a session mechanic must never hinge on message parsing."""
        self._require_referee(who, "eliminate")
        name = re.sub(r"^\W+|\W+$", "", (agent or "").strip()).lower()
        target = name if name not in self._NO_ELIMINATION else None
        # NEVER claim a removal we cannot make. The tool used to return "the system removes them
        # from the turn rotation now" for ANY string — so a referee that named an id the roster does
        # not contain (a typo, a display name, a hyphenated id the platform had mangled) got a
        # confident success, an ELIMINATION event was written, and the participant carried on taking
        # turns. That is scenario-contract §1 in reverse: the PLATFORM lying to the referee. The
        # roster rides in the caller's verified token, so we can simply check.
        if target is not None and who.roster and target not in {r.lower() for r in who.roster}:
            roster = ", ".join(sorted(who.roster))
            return {"error": f"no participant named {target!r} — the roster is: {roster}. "
                             f"Nobody was removed. Use an exact id, or 'none' to close the round."}
        await self._c().append_event(
            who.realm_id, EventKind.ELIMINATION,
            {"agent": target, "reason": reason, "issued_by": who.agent_id},
        )
        if target is None:
            return {"eliminated": None, "note": "round closed — nobody removed"}
        return {"eliminated": target,
                "note": "the system removes them from the turn rotation now"}

    async def rule(
        self, who: Identity, outcome: str, reasons: str = ""
    ) -> dict[str, Any]:
        """Issue the final verdict (ends the realm if the referee's powers allow). Records the
        outcome + the final scoreboard for the report. A verdict is FINAL: repeat calls are
        no-ops (they would otherwise flip the outcome between poll ticks)."""
        self._require_referee(who, "rule")
        prior = await self._c().events(who.realm_id, kind=EventKind.VERDICT)
        already = next((e.payload for e in prior if e.payload.get("final")), None)
        if already is not None:  # a final verdict stands — don't record another
            return {"outcome": already.get("outcome"), "scoreboard": already.get("scoreboard", {}),
                    "note": "the realm already has a final verdict — it cannot be changed"}
        # Some turns scenarios want participants to act before any verdict (e.g. a debate). That is
        # a per-project policy (Turns.min_rounds_before_verdict), NOT a hardcoded rule: the value
        # rides along in each TURN event. Default 0 => no guard (a sudden-death game can rule on
        # move 1). round <= min_rounds means fewer than `min_rounds` full rounds have completed.
        turns = await self._c().events(who.realm_id, kind=EventKind.TURN)
        if turns:
            latest = turns[-1].payload
            min_rounds = int(latest.get("min_rounds", 0))
            if int(latest.get("round", 1)) <= min_rounds:
                return {
                    "error": f"too early to rule — this scenario requires {min_rounds} full "
                    "round(s) of turns before a verdict. Wait for the round-complete cue."
                }
        board = dict(await self._board(who.realm_id))
        payload = {"outcome": outcome, "reasons": reasons, "scoreboard": board,
                   "issued_by": who.agent_id, "final": True}
        await self._c().append_event(who.realm_id, EventKind.VERDICT, payload)
        return {"outcome": outcome, "scoreboard": board}
