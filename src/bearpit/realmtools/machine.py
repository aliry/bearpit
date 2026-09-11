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

from bearpit.core.machine import MachineDef


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
    """Member of the pointer's role and in none of its skip sets."""
    p = defn.actor
    if p is None:
        return False
    return not any(agent in state.sets.get(s, ()) for s in p.skip)


def _rotation(defn: MachineDef, bindings: Bindings) -> tuple[str, ...]:
    assert defn.actor is not None
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
