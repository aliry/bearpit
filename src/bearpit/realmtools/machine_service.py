"""MachineService — the only place the game state machine meets IO.

Holds one live state per realm, rebuilt on demand from the chronicle: the latest MACHINE event
(declaration + host-resolved bindings) followed by the GAME events after it. Every act/set is
serialised per realm and APPENDED BEFORE it is applied, so memory never leads the chronicle.
Escrow completion is looked up through an injected callable so this module needs no escrow
of its own and the engine stays pure.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from bearpit.chronicle import Chronicle, EventKind
from bearpit.chronicle.safety import oversized, unstorable
from bearpit.core.machine import Guard, MachineDef
from bearpit.realmtools import machine as eng
from bearpit.realmtools.machine import ReplayError
from bearpit.realmtools.service import Identity

# Must not hang: it is awaited while the realm lock is held, so a stuck lookup blocks every act
# on the realm. The injection site owns the timeout.
EscrowLookup = Callable[[str, str], Awaitable[set[str]]]

def _refuse(label: str, value: Any) -> str | None:
    """Why this write must be refused before it reaches the chronicle, or None.

    Refused HERE, and not left to the chronicle, for one reason: the chronicle is append-only. A
    value that stores but can never be serialised back out takes the realm's whole console view
    with it, permanently, and the agent that wrote it is never told. Answering the agent costs one
    walk of a small dict.
    """
    if (why := unstorable(value)) is not None:
        return f"{label} {why}"
    try:  # depth is already bounded above, so dumps cannot recurse away
        n = len(json.dumps(value, ensure_ascii=True, default=str))
    except (TypeError, ValueError):
        return f"{label} is not serialisable"
    return None if (why := oversized(n)) is None else f"{label} {why}"

MACHINE_VERSION = 1
# `log_limit` arrives from an agent's tool call. CLAMPED, never rejected: a caller that asks for
# too much gets the maximum rather than an error it has to learn to handle — and one agent can no
# longer make this process build (and ship) an arbitrarily long list (review M7).
LOG_LIMIT_MAX = 500


@dataclass
class _Live:
    defn: MachineDef
    bindings: eng.Bindings
    state: eng.MachineState
    machine_event_id: int


class MachineService:
    def __init__(
        self, chronicle: Chronicle | None = None, *, escrow_lookup: EscrowLookup | None = None,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self._chron = chronicle
        self._escrow_lookup = escrow_lookup
        self._clock = clock_ms
        self._live: dict[str, _Live] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle
        self._live.clear()
        # The locks go with the state they guard. An asyncio.Lock belongs to the loop it was
        # created on, so one left over from a previous wiring is a RuntimeError on the next act,
        # not merely a stale entry.
        self._locks.clear()

    # --- loading -----------------------------------------------------------------------
    async def _load(self, realm_id: str) -> _Live | None:
        if self._chron is None:
            return None
        machines = await self._chron.events(realm_id, kind=EventKind.MACHINE)
        if not machines:
            return None
        head = machines[-1]  # LAST wins: a realm id can be reused
        p = head.payload
        if p.get("version") != MACHINE_VERSION:
            return None
        try:
            defn = MachineDef.model_validate(p["declaration"])
        except ValidationError as exc:
            # Launch validation grows stricter over time, so a declaration that was legal when it
            # was chronicled can be refused by the schema that reloads it. To the service that is
            # chronicle damage: fail the realm loudly, and never let pydantic's text (which names
            # transitions and keys) reach a caller.
            raise ReplayError(f"realm {realm_id!r}: stored declaration is no longer valid") from exc
        bindings = eng.Bindings(
            members={r: tuple(ids) for r, ids in p["members"].items()},
            roster=tuple(p["roster"]), referee=p.get("referee"),
        )
        raw = await self._chron.events(realm_id, kind=EventKind.GAME)
        games = [(e.ts_ms, e.payload, e.id) for e in raw if e.id > head.id]
        try:
            state = eng.replay(
                defn, bindings, [(ts, pl) for ts, pl, _ in games], start_ms=head.ts_ms
            )
        except (ReplayError, KeyError) as exc:
            # A corrupt chronicle must fail the realm loudly with a clear message, not a stack
            # trace: `eng.replay` raises a bare KeyError on a row missing caller/key/transition.
            raise ReplayError(f"realm {realm_id!r}: {exc}") from exc
        return _Live(defn, bindings, state, head.id)

    async def _get(self, realm_id: str) -> _Live | None:
        live = self._live.get(realm_id)
        if live is not None:
            return live
        loaded = await self._load(realm_id)
        if loaded is None:
            return None
        # A writer caches BEFORE it appends, so if an entry appeared while we were loading it is
        # at least as new as our snapshot — never overwrite it with an older one. `act`/`set`
        # cannot take the realm lock here (it is not reentrant and they already hold it), so a
        # cold `state()`/`declaration()` load racing a concurrent `act` must lose gracefully
        # instead of clobbering the applied state with a pre-append snapshot.
        return self._live.setdefault(realm_id, loaded)

    def _lock(self, realm_id: str) -> asyncio.Lock:
        return self._locks.setdefault(realm_id, asyncio.Lock())

    async def _escrow_for(
        self, live: _Live, realm_id: str, transition: str | None, who: Identity,
        args: dict[str, Any],
    ) -> dict[str, set[str]]:
        """Pre-fetch every escrow round the transition's guards (if any — `set` has none) or the
        wake rules reference."""
        if self._escrow_lookup is None:
            return {}
        guards: list[Guard] = []
        if transition is not None:
            t = live.defn.transitions.get(transition)
            if t is not None:
                guards += list(t.guard)
        for w in live.defn.wake:
            guards += [*w.when, *w.unless]
        ctx = eng.Ctx(caller=who.agent_id, args=args, escrow={})
        out: dict[str, set[str]] = {}
        for g in guards:
            if g.name == "escrow_complete" and isinstance(g.arg, dict):
                # Resolved with the CALLER's ctx; compute_wakes evaluates wake guards with an
                # empty ctx against the new state, so a wake round keyed on $caller/$args/a
                # rewritten $data key may be pre-fetched under a different id. Fails closed (a
                # missed wake, never a spurious one).
                rid = str(eng.resolve(g.arg.get("round"), ctx, live.state))
                if rid not in out:
                    out[rid] = await self._escrow_lookup(realm_id, rid)
        return out

    # --- the four operations ---------------------------------------------------------------
    async def state(
        self, who: Identity, since: int | None = None, log_limit: int = 100
    ) -> dict[str, Any]:
        log_limit = max(1, min(log_limit, LOG_LIMIT_MAX))
        live = await self._get(who.realm_id)
        if live is None or self._chron is None:
            return {"error": "no machine declared"}
        # Fetch the log BEFORE the view: the view must be at least as new as the log, never
        # older — reading `live.state` after this await has returned can only see a state at
        # least this fresh (it may have advanced further while the query was in flight, but it
        # can never be behind what the log below already reflects).
        events = await self._chron.events(who.realm_id, kind=EventKind.GAME)
        v = eng.view(live.defn, live.bindings, live.state, who.agent_id)
        rows: list[dict[str, Any]] = []
        last = since if since is not None else live.machine_event_id
        for e in events:
            if e.id <= last or e.id <= live.machine_event_id:
                continue
            last = e.id
            row = eng.log_row(live.defn, live.bindings, e.payload, who.agent_id)
            if row is not None:
                rows.append({**row, "id": e.id})
                # Paging forward: cap the SCAN, not the result, so `last` stops at the id of
                # the event that filled this page and the next call resumes exactly there —
                # otherwise a page boundary silently drops the rows between it and the next
                # visible one. The first (unpaged) call keeps scanning everything and returns
                # only the tail, matching the "give me the recent log" use.
                if since is not None and len(rows) == log_limit:
                    break
        if since is None:
            rows = rows[-log_limit:]
        return {**v, "log": rows, "next_since": last}

    async def act(
        self, who: Identity, transition: str, args: dict[str, Any] | None
    ) -> dict[str, Any]:
        args = dict(args or {})
        if (why := _refuse("args", args)) is not None:
            return {"error": why}
        async with self._lock(who.realm_id):
            live = await self._get(who.realm_id)
            if live is None or self._chron is None:
                return {"error": "no machine declared"}
            escrow = await self._escrow_for(live, who.realm_id, transition, who, args)
            out = eng.act(live.defn, live.bindings, live.state, who.agent_id, transition, args,
                          escrow, self._clock())
            if isinstance(out, eng.Rejection):
                t = live.defn.transitions.get(transition)
                payload = eng.reject_payload(who.agent_id, transition, args, out.check, out.detail,
                                             t.log if t else "public")
                await self._chron.append_event(who.realm_id, EventKind.GAME, payload)
                return {"error": "rejected", "check": out.check, "detail": out.detail}
            await self._chron.append_event(who.realm_id, EventKind.GAME, out.payload)
            live.state = out.state  # apply AFTER the append landed
            return eng.view(live.defn, live.bindings, live.state, who.agent_id)

    async def set(
        self, who: Identity, key: str, value: Any, owner: str | None = None
    ) -> dict[str, Any]:
        for label, v in (("key", key), ("owner", owner), ("value", value)):
            if (why := _refuse(label, v)) is not None:
                return {"error": why}
        async with self._lock(who.realm_id):
            live = await self._get(who.realm_id)
            if live is None or self._chron is None:
                return {"error": "no machine declared"}
            # A `set` has no transition guards of its own — only the wake rules it might trip.
            escrow = await self._escrow_for(live, who.realm_id, None, who, {})
            out = eng.set_value(live.defn, live.bindings, live.state, who.agent_id, key, value,
                                owner, escrow, self._clock())
            if isinstance(out, eng.Rejection):
                # A refused write is still an event — chronicled referee-only so a participant's
                # attempted (and refused) write never leaks into their own log.
                reject: dict[str, Any] = {
                    "op": "reject", "key": key, "owner": owner, "caller": who.agent_id,
                    "check": out.check, "detail": out.detail, "log": "referee", "wake": [],
                }
                await self._chron.append_event(who.realm_id, EventKind.GAME, reject)
                return {"error": "rejected", "check": out.check, "detail": out.detail}
            await self._chron.append_event(who.realm_id, EventKind.GAME, out.payload)
            live.state = out.state
            return eng.view(live.defn, live.bindings, live.state, who.agent_id)

    async def declaration(self, who: Identity) -> dict[str, Any]:
        live = await self._get(who.realm_id)
        if live is None:
            return {"error": "no machine declared"}
        return eng.declaration_view(live.defn, live.bindings, who.agent_id)


__all__ = ["EscrowLookup", "MachineService"]
