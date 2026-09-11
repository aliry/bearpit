"""MachineService — the only place the game state machine meets IO.

Holds one live state per realm, rebuilt on demand from the chronicle: the latest MACHINE event
(declaration + host-resolved bindings) followed by the GAME events after it. Every act/set is
serialised per realm and APPENDED BEFORE it is applied, so memory never leads the chronicle.
Escrow completion is looked up through an injected callable so this module needs no escrow
of its own and the engine stays pure.
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from bearpit.chronicle import Chronicle, EventKind
from bearpit.core.machine import Guard, MachineDef
from bearpit.realmtools import machine as eng
from bearpit.realmtools.machine import ReplayError
from bearpit.realmtools.service import Identity

EscrowLookup = Callable[[str, str], Awaitable[set[str]]]
MACHINE_VERSION = 1


@dataclass
class _Live:
    defn: MachineDef
    bindings: eng.Bindings
    state: eng.MachineState
    machine_event_id: int
    last_event_id: int  # the newest GAME event folded into `state`


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
        defn = MachineDef.model_validate(p["declaration"])
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
        return _Live(defn, bindings, state, head.id, games[-1][2] if games else head.id)

    async def _get(self, realm_id: str) -> _Live | None:
        live = self._live.get(realm_id)
        if live is None:
            live = await self._load(realm_id)
            if live is not None:
                self._live[realm_id] = live
        return live

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
                rid = str(eng.resolve(g.arg.get("round"), ctx, live.state))
                if rid not in out:
                    out[rid] = await self._escrow_lookup(realm_id, rid)
        return out

    # --- the four operations ---------------------------------------------------------------
    async def state(
        self, who: Identity, since: int | None = None, log_limit: int = 100
    ) -> dict[str, Any]:
        live = await self._get(who.realm_id)
        if live is None or self._chron is None:
            return {"error": "no machine declared"}
        v = eng.view(live.defn, live.bindings, live.state, who.agent_id)
        rows: list[dict[str, Any]] = []
        last = since if since is not None else live.machine_event_id
        for e in await self._chron.events(who.realm_id, kind=EventKind.GAME):
            if e.id <= last or e.id <= live.machine_event_id:
                continue
            last = e.id
            row = eng.log_row(live.defn, live.bindings, e.payload, who.agent_id)
            if row is not None:
                rows.append({**row, "id": e.id})
        rows = rows[-log_limit:]
        return {**v, "log": rows, "next_since": last}

    async def act(
        self, who: Identity, transition: str, args: dict[str, Any] | None
    ) -> dict[str, Any]:
        args = dict(args or {})
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
            eid = await self._chron.append_event(who.realm_id, EventKind.GAME, out.payload)
            live.state, live.last_event_id = out.state, eid  # apply AFTER the append landed
        return eng.view(live.defn, live.bindings, live.state, who.agent_id)

    async def set(
        self, who: Identity, key: str, value: Any, owner: str | None = None
    ) -> dict[str, Any]:
        async with self._lock(who.realm_id):
            live = await self._get(who.realm_id)
            if live is None or self._chron is None:
                return {"error": "no machine declared"}
            # A `set` has no transition guards of its own — only the wake rules it might trip.
            escrow = await self._escrow_for(live, who.realm_id, None, who, {})
            out = eng.set_value(live.defn, live.bindings, live.state, who.agent_id, key, value,
                                owner, escrow, self._clock())
            if isinstance(out, eng.Rejection):
                return {"error": "rejected", "check": out.check, "detail": out.detail}
            eid = await self._chron.append_event(who.realm_id, EventKind.GAME, out.payload)
            live.state, live.last_event_id = out.state, eid
        return eng.view(live.defn, live.bindings, live.state, who.agent_id)

    async def declaration(self, who: Identity) -> dict[str, Any]:
        live = await self._get(who.realm_id)
        if live is None:
            return {"error": "no machine declared"}
        return eng.declaration_view(live.defn, live.bindings, who.agent_id)


__all__ = ["EscrowLookup", "MachineService"]
