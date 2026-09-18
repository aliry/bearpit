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
# What a participant role may carry without an opt-in: mark yourself, show what is yours, pass.
SAFE_EFFECTS = frozenset({"advance_actor", "add_to", "reveal"})
# Guards that read hidden data and so must not appear on a public-log transition (the rejection
# text would leak what they read).
_HIDDEN_READING = frozenset({"caller_in", "caller_not_in", "data_equals", "data_set_empty",
                             "data_set_full"})
# Guards that name a data key directly — the key must be declared, except the reserved pointer
# "actor" for data_equals/data_present (a comparison against the current actor, not a data key).
# `data_set_full` was missing here: a keyless one launched fine and raised KeyError('key') inside
# `check_guard` on the first act, mid-realm and uncaught (review I2).
_KEY_READING = frozenset({"data_equals", "data_present", "data_set_empty", "data_set_full",
                          "caller_in", "caller_not_in"})
# ...of those, the ones whose key must be a declared SET. A value key makes them permanently
# false (`data_set_full`, `caller_in`) or permanently true (`data_set_empty`, `caller_not_in`):
# a rule that never fires, or one that always does, with nothing to show for it at launch.
_SET_READING = frozenset({"caller_in", "caller_not_in", "data_set_empty", "data_set_full"})
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
    # `from` is a Python keyword, so the field is `from_` with the JSON key as its alias. The
    # model must round-trip through a plain `model_dump()` → `model_validate()` on its own:
    # `bind_params` does exactly that on EVERY launch, and the first live machine realm was a 500
    # before a single container existed because the dump emitted `from_` and `extra="forbid"`
    # rejected it. Serialising by alias keeps every dump on the JSON key; validating by name keeps
    # any dump that was already stored the old way loadable.
    model_config = ConfigDict(
        extra="forbid", validate_by_name=True, validate_by_alias=True, serialize_by_alias=True
    )

    from_: list[str] = Field(alias="from")
    to: str
    by: str
    guard: list[Guard] = Field(default_factory=list)
    effects: list[Effect] = Field(default_factory=list)
    log: Literal["public", "referee"] = "public"

    @model_validator(mode="before")
    @classmethod
    def _from_list(cls, v: Any) -> Any:
        if isinstance(v, dict):
            for key in ("from", "from_"):
                if isinstance(v.get(key), str):
                    v = dict(v)
                    v[key] = [v[key]]
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
    # Grants the referee `table_talk` — a PULL of the commons, on a wake it already has. It does
    # NOT put the referee in the message feed: doing that was the old meaning, and it cost two
    # live poker runs where the dealer hit ~50% of all messages and ~90% of spend narrating its
    # own inaction. See `runconfig.referee_sees_all`.
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
        for key, d in self.data.items():
            if d.visibility == "owner" and d.type == "set":
                raise ValueError(f"data key {key!r}: an owner-visibility key cannot be a set")
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
        # Reachability (review M6). A machine nobody can start, and a declared ending nothing
        # leads to, are both dead on arrival — and both look like a realm that simply sits there.
        if not any("any" in t.from_ or self.initial in t.from_
                   for t in self.transitions.values()):
            raise ValueError(
                f"initial state {self.initial!r} has no outgoing transition — nothing could ever"
                f" fire")
        # Only when `terminal` is DECLARED: the spec makes the list optional (§2), and a machine
        # that ends by verdict or duration instead is a legitimate scenario.
        if self.terminal and not any(t.to in self.terminal for t in self.transitions.values()):
            raise ValueError(
                "no transition reaches a terminal state — machine_terminal could never fire")
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
            elif self.data[key].visibility == "owner":
                raise ValueError(
                    f"transition {tname!r}: 'set' on owner-visibility key {key!r} — owner values"
                    f" are written with game_set(owner=)")
            value = (e.arg or {}).get("value") if isinstance(e.arg, dict) else None
            src = value[6:] if isinstance(value, str) and value.startswith("$data.") else None
            source = self.data.get(str(src)) if src is not None else None
            if (source is not None and source.visibility != "public"
                    and self.data[str(key)].visibility == "public"):
                # Each half is legal on its own; together they are a copy machine pointed out of
                # the referee's view. The engine resolves $data at act time and would publish it
                # without a word (review M3).
                raise ValueError(
                    f"transition {tname!r}: 'set' copies {src!r} ({source.visibility}-visibility)"
                    f" into public key {key!r} — declare the destination {source.visibility}, or"
                    f" write the value the referee meant to publish")
        if e.name == "reveal":
            key = (e.arg or {}).get("key") if isinstance(e.arg, dict) else None
            d = self.data.get(str(key))
            if d is None or d.visibility != "owner":
                raise ValueError(
                    f"transition {tname!r}: 'reveal' needs an owner-visibility key, got {key!r}")

    @model_validator(mode="after")
    def _authority_and_visibility(self) -> MachineDef:
        opt_in = set(self.participant_effects)
        for name, t in self.transitions.items():
            role = self.roles[t.by]
            if role.members != "referee":
                for e in t.effects:
                    if e.name not in SAFE_EFFECTS and e.name not in opt_in:
                        raise ValueError(
                            f"transition {name!r}: role {t.by!r} may not use {e.name!r} — add it"
                            f" to participant_effects")
                    if e.name == "add_to" and "add_to" not in opt_in and (
                            (e.arg or {}).get("value") != "$caller"):
                        raise ValueError(
                            f"transition {name!r}: role {t.by!r} may only add_to with value"
                            f" $caller")
                    if e.name == "reveal" and "reveal" not in opt_in and (
                            (e.arg or {}).get("owners") != "$caller"):
                        raise ValueError(
                            f"transition {name!r}: role {t.by!r} may only reveal owners $caller")
            if role.visibility == "hidden" and t.log != "referee":
                raise ValueError(
                    f"transition {name!r}: by hidden role {t.by!r} requires log: referee")
            for g in t.guard:
                self._check_guard(name, g, public_log=(t.log == "public"))
        for i, w in enumerate(self.wake):
            if w.role != "actor" and w.role not in self.roles:
                raise ValueError(f"wake rule: {w.role!r} is not a declared role")
            if w.role != "actor" and self.roles[w.role].visibility == "hidden":
                raise ValueError(
                    f"wake rule targets hidden role {w.role!r} — deferred until per-agent wake"
                    f" rooms exist")
            if w.role != "actor" and not w.when and w.after_s is None:
                raise ValueError(
                    f"wake rule: role {w.role!r} needs `when` or `after_s` — only `actor` may"
                    f" be bare")
            if w.role == "actor" and w.after_s is not None:
                # `actor` is the pointer, not a role with members: members['actor'] is always
                # empty, so the nudge would be delivered to nobody and the stall never surface.
                raise ValueError(
                    "wake rule: after_s needs a declared role — 'actor' has no members to "
                    "nudge; nudge the referee instead")
            if w.after_s is not None and w.after_s < AFTER_S_FLOOR:
                raise ValueError(
                    f"wake rule: after_s {w.after_s} is below the floor of {AFTER_S_FLOOR}")
            for g in [*w.when, *w.unless]:
                if g.name in {"caller_is_actor", "caller_in", "caller_not_in"}:
                    raise ValueError(f"wake rule: {g.name!r} has no caller in a wake rule")
                self._check_guard(f"wake[{i}]", g, public_log=False)
        return self

    def _check_guard(self, where: str, g: Guard, *, public_log: bool) -> None:
        arg = g.arg if isinstance(g.arg, dict) else {}
        key = arg.get("key") if isinstance(g.arg, dict) else g.arg
        if g.name in _KEY_READING:
            if key is None:
                raise ValueError(f"transition {where!r}: guard {g.name!r} needs a key")
            if (isinstance(key, str) and key not in self.data
                    and not (key == "actor" and g.name in {"data_equals", "data_present"})):
                raise ValueError(
                    f"transition {where!r}: guard {g.name!r} reads undeclared key {key!r}")
            if g.name in _SET_READING and not self.is_set(str(key)):
                raise ValueError(
                    f"transition {where!r}: guard {g.name!r} needs a set key, {key!r} is not one")
        if g.name == "escrow_complete":
            # `round` may be a literal, `$args.<n>` or `$data.<key>`. An undeclared $data key
            # resolves to None every time, and the guard then asks the escrow about a round
            # literally named "None" — fails closed forever, and says nothing about why.
            rnd = arg.get("round")
            if isinstance(rnd, str) and rnd.startswith("$data."):
                rkey = rnd[6:]
                if rkey != "actor" and rkey not in self.data:
                    raise ValueError(
                        f"transition {where!r}: escrow_complete.round reads undeclared key"
                        f" {rkey!r}")
        if g.name == "members_count":
            n = next((arg[k] for k in ("equals", "at_most", "at_least") if k in arg), None)
            if not isinstance(n, int) or isinstance(n, bool):
                raise ValueError(f"transition {where!r}: members_count N must be a literal")
            if arg.get("over") not in self.roles:
                raise ValueError(
                    f"transition {where!r}: members_count.over {arg.get('over')!r} is not a"
                    f" declared role")
        if g.name in {"data_set_full", "escrow_complete"} and arg.get("over") not in self.roles:
            raise ValueError(
                f"transition {where!r}: {g.name}.over {arg.get('over')!r} is not a declared role")
        for s in arg.get("minus", []) if isinstance(arg, dict) else []:
            if not self.is_set(s):
                raise ValueError(
                    f"transition {where!r}: {g.name}.minus {s!r} is not a declared set key")
        if public_log and g.name in _HIDDEN_READING and isinstance(key, str):
            d = self.data.get(key)
            if d is not None and d.visibility == "referee":
                raise ValueError(
                    f"transition {where!r}: public log but guard {g.name!r} reads"
                    f" referee-visibility key {key!r}")
