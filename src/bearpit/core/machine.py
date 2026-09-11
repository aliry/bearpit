"""The game-state-machine declaration.

Spec: docs/superpowers/specs/2026-09-10-game-state-machine-design.md

A scenario declares states, transitions, roles, guards, effects and a data document with
per-key visibility. This module is the declaration's SCHEMA and every launch-time refusal.
The engine that runs it lives in `bearpit.realmtools.machine`; nothing here executes.

Refuse rather than guess: a declaration that references an unknown state, role or key — or
gives a participant an effect it may not have — fails at launch with a message naming the
problem.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The closed vocabulary. Adding to either list is a spec change.
GUARDS = frozenset({
    "caller_is_actor", "caller_in", "caller_not_in", "data_equals", "data_present",
    "data_set_empty", "data_set_full", "members_count", "escrow_complete",
})
EFFECTS = frozenset({
    "advance_actor", "add_to", "remove_from", "reset", "set", "unset", "set_actor", "reveal",
})
# What a participant role may carry without an opt-in: mark yourself, show your own hand, pass.
SAFE_EFFECTS = frozenset({"advance_actor", "add_to", "reveal"})
# Guards that read hidden data and so must not appear on a public-log transition (the rejection
# text would leak what they read).
_HIDDEN_READING = frozenset({"caller_in", "caller_not_in", "data_equals", "data_set_empty",
                             "data_set_full"})
AFTER_S_FLOOR = 240  # scenario-contract §13: a resolver run_code may block 90s on a real pipeline


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleDef(_Base):
    members: Literal["referee", "participants"] | list[str]
    visibility: Literal["public", "hidden"] = "public"


class DataDef(_Base):
    visibility: Literal["public", "owner", "referee"]
    type: Literal["value", "set"] = "value"


class PointerDef(_Base):
    over: str
    skip: list[str] = Field(default_factory=list)


class _Clause(_Base):
    """A guard or effect: `name` (bare string form) or `{name: arg}` (single-key dict form)."""

    name: str
    arg: Any = None

    @model_validator(mode="before")
    @classmethod
    def _normalise(cls, v: Any) -> Any:
        if isinstance(v, str):
            return {"name": v}
        if isinstance(v, dict) and "name" not in v and len(v) == 1:
            (k, a), = v.items()
            return {"name": k, "arg": a}
        return v


class Guard(_Clause):
    pass


class Effect(_Clause):
    pass


class TransitionDef(_Base):
    from_: list[str] = Field(alias="from")
    to: str
    by: str
    guard: list[Guard] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    log: Literal["public", "referee"] = "public"

    @model_validator(mode="before")
    @classmethod
    def _from_list(cls, v: Any) -> Any:
        if isinstance(v, dict) and isinstance(v.get("from"), str):
            v = dict(v)
            v["from"] = [v["from"]]
        return v


class WakeRule(_Base):
    role: str  # a declared role, or the reserved word "actor"
    when: list[Guard] = Field(default_factory=list)
    unless: list[Guard] = Field(default_factory=list)
    after_s: int | None = None


class MachineDef(_Base):
    roles: dict[str, RoleDef]
    states: list[str]
    initial: str
    terminal: list[str] = Field(default_factory=list)
    actor: PointerDef | None = None
    participant_effects: list[str] = Field(default_factory=list)
    referee_reads_commons: bool = False
    data: dict[str, DataDef] = Field(default_factory=dict)
    transitions: dict[str, TransitionDef]
    wake: list[WakeRule] = Field(default_factory=list)

    # --- helpers the validators and the engine share -------------------------------------
    def is_set(self, key: str) -> bool:
        d = self.data.get(key)
        return d is not None and d.type == "set"

    def is_referee_role(self, role: str) -> bool:
        r = self.roles.get(role)
        return r is not None and r.members == "referee"

    @model_validator(mode="after")
    def _structure(self) -> MachineDef:
        states = set(self.states)
        if self.initial not in states:
            raise ValueError(f"initial {self.initial!r} is not a declared state")
        for t in self.terminal:
            if t not in states:
                raise ValueError(f"terminal {t!r} is not a declared state")
        if self.actor is not None:
            if self.actor.over not in self.roles:
                raise ValueError(f"actor.over {self.actor.over!r} is not a declared role")
            for s in self.actor.skip:
                if not self.is_set(s):
                    raise ValueError(f"actor.skip {s!r} is not a declared set key")
        for name, trans in self.transitions.items():
            for s in trans.from_:
                if s != "any" and s not in states:
                    raise ValueError(f"transition {name!r}: {s!r} is not a declared state")
            if trans.to != "same" and trans.to not in states:
                raise ValueError(f"transition {name!r}: {trans.to!r} is not a declared state")
            if trans.by not in self.roles:
                raise ValueError(f"transition {name!r}: {trans.by!r} is not a declared role")
            for g in trans.guard:
                if g.name not in GUARDS:
                    raise ValueError(f"transition {name!r}: unknown guard {g.name!r}")
            for e in trans.effects:
                if e.name not in EFFECTS:
                    raise ValueError(f"transition {name!r}: unknown effect {e.name!r}")
                self._check_effect_keys(name, e)
        return self

    def _check_effect_keys(self, tname: str, e: Effect) -> None:
        if e.name in {"reset"} and not self.is_set(str(e.arg)):
            raise ValueError(
                f"transition {tname!r}: 'reset' needs a set key, {e.arg!r} is not one")
        if e.name in {"add_to", "remove_from"}:
            key = (e.arg or {}).get("key") if isinstance(e.arg, dict) else None
            if not self.is_set(str(key)):
                raise ValueError(
                    f"transition {tname!r}: {e.name!r} needs a set key, {key!r} is not one")
        if e.name == "unset" and self.is_set(str(e.arg)):
            raise ValueError(
                f"transition {tname!r}: 'unset' on a set key {e.arg!r} — use 'reset'")
        if e.name == "set":
            key = (e.arg or {}).get("key") if isinstance(e.arg, dict) else None
            if key not in self.data:
                raise ValueError(f"transition {tname!r}: 'set' on undeclared key {key!r}")
        if e.name == "reveal":
            key = (e.arg or {}).get("key") if isinstance(e.arg, dict) else None
            d = self.data.get(str(key))
            if d is None or d.visibility != "owner":
                raise ValueError(
                    f"transition {tname!r}: 'reveal' needs an owner-visibility key, got {key!r}")
