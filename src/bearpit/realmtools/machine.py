"""The game state machine ENGINE — pure functions over a declaration and a state document.

(declaration, bindings, state, caller, transition, args) -> new state | Rejection

No IO and no clock: `now_ms` is always a parameter, the escrow's submitted set is passed in, and
every function is deterministic — which is what makes replay-as-recovery correct (spec §4). The
engine has no arithmetic: numbers are opaque values the referee writes. Adding either a timer or
a `+` here is the line the design says never to cross (ADR-002).

The service in `machine_service.py` is the only place this meets the chronicle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from bearpit.core.machine import Guard, MachineDef


@dataclass(frozen=True)
class Bindings:
    """Host-resolved at launch and persisted in the MACHINE event: who is in each role, and the
    roster order the pointer rotates over. Frozen so a replay sees exactly what the run saw."""

    members: dict[str, tuple[str, ...]]
    roster: tuple[str, ...]
    referee: str | None


@dataclass
class MachineState:
    state: str
    actor: str | None
    actor_since: int
    data: dict[str, Any] = field(default_factory=dict)  # public + referee scalar values
    owner_data: dict[str, dict[str, Any]] = field(default_factory=dict)  # key -> owner -> value
    sets: dict[str, set[str]] = field(default_factory=dict)
    revealed: dict[str, set[str]] = field(default_factory=dict)  # owner-key -> owners now public

    def copy(self) -> MachineState:
        return MachineState(
            state=self.state, actor=self.actor, actor_since=self.actor_since,
            data=dict(self.data),
            owner_data={k: dict(v) for k, v in self.owner_data.items()},
            sets={k: set(v) for k, v in self.sets.items()},
            revealed={k: set(v) for k, v in self.revealed.items()},
        )


def initial_state(defn: MachineDef, bindings: Bindings, now_ms: int) -> MachineState:
    return MachineState(
        state=defn.initial, actor=None, actor_since=now_ms,
        owner_data={k: {} for k, d in defn.data.items() if d.visibility == "owner"},
        sets={k: set() for k, d in defn.data.items() if d.type == "set"},
        revealed={k: set() for k, d in defn.data.items() if d.visibility == "owner"},
    )


def roles_of(defn: MachineDef, bindings: Bindings, agent: str) -> set[str]:
    return {r for r, ids in bindings.members.items() if agent in ids}


def eligible(defn: MachineDef, state: MachineState, agent: str) -> bool:
    """In none of the pointer's skip sets. Assumes `agent` is already a member of the pointer's
    role — callers filter by membership first (see `_rotation`)."""
    p = defn.actor
    if p is None:
        return False
    return not any(agent in state.sets.get(s, ()) for s in p.skip)


def _rotation(defn: MachineDef, bindings: Bindings) -> tuple[str, ...]:
    assert defn.actor is not None
    # Host invariant, refused at launch otherwise: every role member is on the roster.
    members = set(bindings.members.get(defn.actor.over, ()))
    return tuple(a for a in bindings.roster if a in members)


def advance_actor(defn: MachineDef, bindings: Bindings, state: MachineState, now_ms: int) -> None:
    """Next eligible member after the current actor, wrapping; PARK at None if there is none.
    From a parked pointer, start at the top of the rotation."""
    if defn.actor is None:
        return
    order = _rotation(defn, bindings)
    if not order:
        state.actor, state.actor_since = None, now_ms
        return
    start = order.index(state.actor) + 1 if state.actor in order else 0
    for i in range(len(order)):
        cand = order[(start + i) % len(order)]
        if eligible(defn, state, cand):
            state.actor, state.actor_since = cand, now_ms
            return
    state.actor, state.actor_since = None, now_ms


def set_actor(
    defn: MachineDef, bindings: Bindings, state: MachineState, who: str | None, now_ms: int
) -> str | None:
    """Returns a rejection reason, or None on success. Refuses a stranger and a skipped member —
    a referee typo must not hand the floor to a folded player."""
    if defn.actor is None:
        return "this machine has no pointer"
    if who is not None:
        if who not in bindings.members.get(defn.actor.over, ()):
            return f"{who!r} is not a member of {defn.actor.over!r}"
        for s in defn.actor.skip:
            if who in state.sets.get(s, ()):
                return f"{who!r} is in skip set {s!r}"
    state.actor, state.actor_since = who, now_ms
    return None


@dataclass(frozen=True)
class Ctx:
    """Per-call inputs. `escrow` maps round id -> agents who have sealed it; the service fetches
    it before calling in, so the engine stays pure. `caller` is None when evaluating wake rules."""

    caller: str | None
    args: dict[str, Any]
    escrow: dict[str, set[str]]


def resolve(value: Any, ctx: Ctx, state: MachineState) -> Any:
    if isinstance(value, str):
        if value == "$caller":
            return ctx.caller
        if value.startswith("$args."):
            return ctx.args.get(value[6:])
        if value.startswith("$data."):
            key = value[6:]
            if key == "actor":
                return state.actor
            return state.data.get(key)
    return value


def _expected(
    defn: MachineDef, bindings: Bindings, state: MachineState, over: str, minus: list[str]
) -> set[str]:
    members = set(bindings.members.get(over, ()))
    for s in minus:
        members -= state.sets.get(s, set())
    return members


def check_guard(
    g: Guard, defn: MachineDef, bindings: Bindings, state: MachineState, ctx: Ctx
) -> bool:
    a = g.arg if isinstance(g.arg, dict) else {}
    if g.name == "caller_is_actor":
        return ctx.caller is not None and state.actor == ctx.caller
    if g.name == "caller_in":
        return ctx.caller in state.sets.get(str(g.arg), set())
    if g.name == "caller_not_in":
        return ctx.caller not in state.sets.get(str(g.arg), set())
    if g.name == "data_present":
        return str(g.arg) in state.data
    if g.name == "data_equals":
        key = a.get("key")
        current = state.actor if key == "actor" else state.data.get(str(key))
        return bool(current == resolve(a.get("value"), ctx, state))
    if g.name == "data_set_empty":
        return not state.sets.get(str(g.arg), set())
    if g.name == "members_count":
        n = len(_expected(defn, bindings, state, a["over"], a.get("minus", [])))
        if "equals" in a:
            return bool(n == a["equals"])
        if "at_most" in a:
            return bool(n <= a["at_most"])
        return bool(n >= a["at_least"])
    if g.name == "data_set_full":
        expected = _expected(defn, bindings, state, a["over"], a.get("minus", []))
        return expected <= state.sets.get(str(a["key"]), set())
    if g.name == "escrow_complete":
        round_id = resolve(a.get("round"), ctx, state)
        expected = _expected(defn, bindings, state, a["over"], a.get("minus", []))
        return expected <= ctx.escrow.get(str(round_id), set())
    raise ValueError(f"unknown guard {g.name!r}")  # unreachable: MachineDef refused it at launch


def guards_hold(
    guards: list[Guard], defn: MachineDef, bindings: Bindings, state: MachineState, ctx: Ctx
) -> tuple[bool, str | None]:
    for g in guards:
        if not check_guard(g, defn, bindings, state, ctx):
            return False, g.name
    return True, None
