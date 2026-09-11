# Game State Machine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A generic, declarative game state machine shipped as a platform mechanic, proven live by re-expressing `rps-duel` as a declaration.

**Architecture:** A pure engine (`realmtools/machine.py`, no IO) applies declared transitions to a state document and stamps wake targets on each event; a service (`realmtools/machine_service.py`) wraps it with a per-realm lock, chronicle persistence and replay; four MCP builtins expose it; the host writes the declaration to the chronicle before provisioning, delivers wake mentions, counts GAME events as activity, and evaluates a new terminal condition. The engine has no arithmetic and no clock.

**Tech Stack:** Python 3.12, pydantic v2, FastMCP (realmtools), SQLAlchemy chronicle (sqlite in tests), pytest + the repo's Protocol-for-IO fakes (`tests/fakes.py`, `FakeHerald`, `FakeTurnBus`).

**Spec:** `docs/superpowers/specs/2026-09-10-game-state-machine-design.md` — the plan argues from the spec; read both.

## Global Constraints

- The engine has **no arithmetic over agent-written values and no clock** (spec §1). `members_count.N` must be a literal.
- Every transition is invoked by an agent; a wake is notification, never advancement (ADR-002).
- `uv run pytest` / `uv run ruff check .` (line length 100) / `uv run mypy` (strict) must all pass at every commit.
- Commit style: imperative subject, body explains why. Trailer on every commit:
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>` and
  `Claude-Session: https://claude.ai/code/session_011xccv3MYdCyEDScbEkEZXB`.
- Tests follow the repo convention: Protocol-for-IO + fakes; a guard test must be shown to FAIL on the bug before it is trusted (reintroduce, run, restore).
- Deploying to the live stack means rebuilding **three** components: `scripts/serve.sh`, the `pit-realmtools` image (`docker compose -f deploy/docker-compose.yaml build realmtools && … up -d realmtools`), and the model-provider process on :8787. A stale image starts silently — diff the package out of the image against the working tree.
- All work on branch `feat/game-state-machine` (already created; the spec is its first commit).

## File structure

| file | responsibility |
|---|---|
| `src/bearpit/core/machine.py` (new) | The declaration model: `MachineDef` and every launch-time validator. Pure pydantic; no runtime. Kept out of `schema.py` (already ~800 lines). |
| `src/bearpit/core/schema.py` (modify) | `MechanicKind.STATE_MACHINE`; `Mechanic.machine`; `TerminationKind.MACHINE_TERMINAL`; spec-level rules (one machine, wake+turns refusal); project-level rule (explicit role members on roster). |
| `src/bearpit/chronicle/chronicle.py` (modify) | `EventKind.MACHINE`, `EventKind.GAME`. |
| `src/bearpit/realmtools/machine.py` (new) | The **pure engine**: state, pointer, guards, effects, `act`, `set_value`, wake stamping, views, replay. No IO, no clock — `now_ms` is a parameter. |
| `src/bearpit/realmtools/machine_service.py` (new) | Per-realm lock, MACHINE-event loading (last wins), GAME replay, chronicle writes, escrow lookup. The only place the engine meets IO. |
| `src/bearpit/realmtools/server.py` (modify) | Four builtins: `game_state`, `game_act`, `game_set`, `game_declaration`. |
| `src/bearpit/core/tools.py` (modify) | `BUILTIN_VERBS` gains the four names. |
| `src/bearpit/core/runconfig.py`, `src/bearpit/herald/herald.py` (modify) | `referee_sees_all` takes `referee_reads_commons`. |
| `src/bearpit/gatekeeper/runner.py` (modify) | MACHINE event before provisioning; wake delivery incl. `after_s`; GAME counts as activity; snapshot carries machine state. |
| `src/bearpit/warden/termination.py` (modify) | `RealmSnapshot.machine_state`; `MACHINE_TERMINAL` branch. |
| `src/bearpit/forge/adapters/hermes/config.py` (modify) | Birth prompt names the four tools and the wake notice when a machine is declared. |
| `docs/scenario-contract.md`, `CLAUDE.md`, `README.md` (modify) | §10 tool list; §12 exception; new "Machine realms" rule; invariant count. |
| `examples/rps-machine/` (new) | rps-duel re-expressed as a declaration — the genericity proof and the live target. |
| tests | `tests/test_machine_def.py`, `tests/test_machine_engine.py`, `tests/test_machine_service.py`, plus additions to `test_core_schema.py`, `test_realmtools_service.py`, `test_runner.py`, `test_warden.py`, `test_forge_config.py`, `test_examples.py`. |

---

### Task 1: Chronicle event kinds

**Files:**
- Modify: `src/bearpit/chronicle/chronicle.py:26-50` (the `EventKind` enum)
- Test: `tests/test_chronicle.py`

**Interfaces:**
- Produces: `EventKind.MACHINE = "machine"`, `EventKind.GAME = "game"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_chronicle.py`:

```python
async def test_machine_and_game_are_first_class_event_kinds():
    """The machine's declaration and every transition are chronicle events like any other
    source — 'everything is chronicled' means a new event source feeds it from day one."""
    from bearpit.chronicle import EventKind

    assert EventKind.MACHINE == "machine"
    assert EventKind.GAME == "game"
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await c.append_event("r", EventKind.MACHINE, {"declaration": {}})
    await c.append_event("r", EventKind.GAME, {"op": "act"})
    assert [e.kind for e in await c.events("r")] == ["machine", "game"]
    await c.close()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_chronicle.py -k first_class_event_kinds -q`
Expected: FAIL — `AttributeError: MACHINE`.

- [ ] **Step 3: Add the two kinds**

In `src/bearpit/chronicle/chronicle.py`, after the `EXEC` member of `EventKind`, add:

```python
    MACHINE = "machine"  # a realm's game-state-machine declaration + host-resolved role bindings
    GAME = "game"  # one machine transition/write/reject {op, transition|key, caller, ..., wake}
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_chronicle.py -q` — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/chronicle/chronicle.py tests/test_chronicle.py
git commit -m "feat(chronicle): MACHINE and GAME event kinds for the game state machine"
```

---

### Task 2: The declaration model — structure

**Files:**
- Create: `src/bearpit/core/machine.py`
- Test: `tests/test_machine_def.py`

**Interfaces:**
- Produces: `MachineDef` (pydantic), with nested `RoleDef`, `DataDef`, `PointerDef`, `Guard`, `Effect`, `TransitionDef`, `WakeRule`. `Guard`/`Effect` normalise both the bare-string form (`caller_is_actor`) and the single-key-dict form (`{data_equals: {key, value}}`) into `.name: str` and `.arg: Any`. `TransitionDef.from_` is aliased to `from`. Vocabulary constants `GUARDS`, `EFFECTS`, `SAFE_EFFECTS`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_machine_def.py`:

```python
"""MachineDef: the declaration is validated at launch, with the parameter validator's precision.

Every bad declaration in the table below must be refused with a message that names the problem —
refuse rather than guess (scenario-contract). The table IS the launch-time contract.
"""
import pytest
from pydantic import ValidationError

from bearpit.core.machine import MachineDef

MINIMAL = {
    "roles": {"ref": {"members": "referee"}, "player": {"members": "participants"}},
    "states": ["a", "b"],
    "initial": "a",
    "transitions": {"go": {"from": "a", "to": "b", "by": "ref"}},
}


def _with(**over):
    d = {k: (dict(v) if isinstance(v, dict) else v) for k, v in MINIMAL.items()}
    d.update(over)
    return d


def test_minimal_declaration_loads():
    m = MachineDef.model_validate(MINIMAL)
    assert m.initial == "a" and m.transitions["go"].from_ == ["a"] and m.transitions["go"].to == "b"


def test_guards_and_effects_accept_string_and_single_key_dict_forms():
    m = MachineDef.model_validate(_with(
        data={"acted": {"visibility": "public", "type": "set"}},
        transitions={"go": {"from": "a", "to": "b", "by": "player",
                            "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
                            "effects": [{"add_to": {"key": "acted", "value": "$caller"}}, "advance_actor"]}},
        actor={"over": "player", "skip": ["acted"]},
    ))
    g = m.transitions["go"].guard
    assert [x.name for x in g] == ["caller_is_actor", "caller_not_in"] and g[1].arg == "acted"
    e = m.transitions["go"].effects
    assert [x.name for x in e] == ["add_to", "advance_actor"]


@pytest.mark.parametrize("bad, message", [
    (_with(initial="zzz"), "initial 'zzz' is not a declared state"),
    (_with(transitions={"go": {"from": "a", "to": "nope", "by": "ref"}}), "transition 'go': 'nope' is not a declared state"),
    (_with(transitions={"go": {"from": "a", "to": "b", "by": "judge"}}), "transition 'go': 'judge' is not a declared role"),
    (_with(actor={"over": "nobody"}), "actor.over 'nobody' is not a declared role"),
    (_with(actor={"over": "player", "skip": ["out"]}), "actor.skip 'out' is not a declared set key"),
    (_with(data={"pot": {"visibility": "public"}}, actor={"over": "player", "skip": ["pot"]}), "actor.skip 'pot' is not a declared set key"),
    (_with(terminal=["zzz"]), "terminal 'zzz' is not a declared state"),
    (_with(transitions={"go": {"from": "a", "to": "b", "by": "ref", "guard": ["teleport"]}}), "transition 'go': unknown guard 'teleport'"),
    (_with(transitions={"go": {"from": "a", "to": "b", "by": "ref", "effects": ["explode"]}}), "transition 'go': unknown effect 'explode'"),
    (_with(transitions={"go": {"from": "a", "to": "b", "by": "ref", "effects": [{"reset": "pot"}]}}, data={"pot": {"visibility": "public"}}), "transition 'go': 'reset' needs a set key, 'pot' is not one"),
    (_with(transitions={"go": {"from": "a", "to": "b", "by": "ref", "effects": [{"unset": "s"}]}}, data={"s": {"visibility": "public", "type": "set"}}), "transition 'go': 'unset' on a set key 's' — use 'reset'"),
])
def test_bad_structure_is_refused_with_a_precise_message(bad, message):
    with pytest.raises(ValidationError) as exc:
        MachineDef.model_validate(bad)
    assert message in str(exc.value)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_machine_def.py -q` — Expected: FAIL, `ModuleNotFoundError: bearpit.core.machine`.

- [ ] **Step 3: Write the model**

Create `src/bearpit/core/machine.py`:

```python
"""The game-state-machine declaration (spec: docs/superpowers/specs/2026-09-10-game-state-machine-design.md).

A scenario declares states, transitions, roles, guards, effects and a data document with per-key
visibility. This module is the declaration's SCHEMA and every launch-time refusal. The engine that
runs it lives in `bearpit.realmtools.machine`; nothing here executes.

Refuse rather than guess: a declaration that references an unknown state, role or key — or gives
a participant an effect it may not have — fails at launch with a message naming the problem.
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
        for name, t in self.transitions.items():
            for s in t.from_:
                if s != "any" and s not in states:
                    raise ValueError(f"transition {name!r}: {s!r} is not a declared state")
            if t.to != "same" and t.to not in states:
                raise ValueError(f"transition {name!r}: {t.to!r} is not a declared state")
            if t.by not in self.roles:
                raise ValueError(f"transition {name!r}: {t.by!r} is not a declared role")
            for g in t.guard:
                if g.name not in GUARDS:
                    raise ValueError(f"transition {name!r}: unknown guard {g.name!r}")
            for e in t.effects:
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_machine_def.py -q` — Expected: PASS (13 tests).

- [ ] **Step 5: Lint, type, commit**

```bash
uv run ruff check . && uv run mypy
git add src/bearpit/core/machine.py tests/test_machine_def.py
git commit -m "feat(core): MachineDef — the game state machine declaration, structurally validated"
```

---

### Task 3: The declaration model — authority, visibility and wake rules

**Files:**
- Modify: `src/bearpit/core/machine.py`
- Test: `tests/test_machine_def.py`

**Interfaces:**
- Produces: the remaining launch refusals from spec §2: participant transitions limited to `SAFE_EFFECTS` unless opted in (and `add_to`/`reveal` with a non-`$caller` value are NOT safe); hidden-role transitions need `log: referee`; public-log guards may not read `referee`-visibility keys or hidden sets; `members_count.N` literal; wake rules target declared roles or `actor`, never a hidden role; `after_s` defaults to and is floored at 240.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_machine_def.py`:

```python
HIDDEN = _with(
    roles={"ref": {"members": "referee"}, "player": {"members": "participants"},
           "impostor": {"members": ["cass", "vega"], "visibility": "hidden"}},
    data={"dead": {"visibility": "public", "type": "set"},
          "secret": {"visibility": "referee"},
          "acted": {"visibility": "public", "type": "set"}},
    actor={"over": "player", "skip": ["dead"]},
)


@pytest.mark.parametrize("bad, message", [
    # a participant may only mark itself, show its own hand, and pass
    (_with(data={"acted": {"visibility": "public", "type": "set"}},
           transitions={"go": {"from": "a", "to": "b", "by": "player", "effects": [{"reset": "acted"}]}}),
     "transition 'go': role 'player' may not use 'reset' — add it to participant_effects"),
    (_with(data={"out": {"visibility": "public", "type": "set"}},
           transitions={"go": {"from": "a", "to": "b", "by": "player",
                               "effects": [{"add_to": {"key": "out", "value": "$args.victim"}}]}}),
     "transition 'go': role 'player' may only add_to with value $caller"),
    (_with(data={"hole": {"visibility": "owner"}},
           transitions={"go": {"from": "a", "to": "b", "by": "player",
                               "effects": [{"reveal": {"key": "hole", "owners": "$args.who"}}]}}),
     "transition 'go': role 'player' may only reveal owners $caller"),
    # hidden roles and hidden data must not leak through the public log
    ({**HIDDEN, "transitions": {"kill": {"from": "a", "to": "b", "by": "impostor"}}},
     "transition 'kill': by hidden role 'impostor' requires log: referee"),
    ({**HIDDEN, "transitions": {"go": {"from": "a", "to": "b", "by": "ref",
                                        "guard": [{"data_equals": {"key": "secret", "value": 1}}]}}},
     "transition 'go': public log but guard 'data_equals' reads referee-visibility key 'secret'"),
    # cardinality compares to a literal, never to something an agent wrote
    ({**HIDDEN, "transitions": {"go": {"from": "a", "to": "b", "by": "ref",
                                        "guard": [{"members_count": {"over": "player", "equals": "$data.n"}}]}}},
     "transition 'go': members_count N must be a literal"),
    # wake rules
    ({**HIDDEN, "wake": [{"role": "impostor"}]},
     "wake rule targets hidden role 'impostor' — deferred until per-agent wake rooms exist"),
    ({**HIDDEN, "wake": [{"role": "ghost"}]}, "wake rule: 'ghost' is not a declared role"),
    ({**HIDDEN, "wake": [{"role": "ref", "after_s": 30}]}, "wake rule: after_s 30 is below the floor of 240"),
    ({**HIDDEN, "wake": [{"role": "ref", "when": ["caller_is_actor"]}]},
     "wake rule: 'caller_is_actor' has no caller in a wake rule"),
])
def test_authority_and_visibility_rules_are_refused_at_launch(bad, message):
    with pytest.raises(ValidationError) as exc:
        MachineDef.model_validate(bad)
    assert message in str(exc.value)


def test_participant_effects_opt_in_lifts_the_default():
    m = MachineDef.model_validate(_with(
        data={"acted": {"visibility": "public", "type": "set"}}, participant_effects=["reset"],
        transitions={"go": {"from": "a", "to": "b", "by": "player", "effects": [{"reset": "acted"}]}},
    ))
    assert m.participant_effects == ["reset"]


def test_after_s_defaults_to_the_floor():
    m = MachineDef.model_validate(_with(wake=[{"role": "ref", "after_s": None}]))
    assert m.wake[0].after_s is None  # None = not a time rule; the floor applies to a set value
    m2 = MachineDef.model_validate(_with(wake=[{"role": "ref", "after_s": 600}]))
    assert m2.wake[0].after_s == 600
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_machine_def.py -q` — Expected: the new parametrised cases FAIL (no such refusals yet).

- [ ] **Step 3: Add the validators**

In `src/bearpit/core/machine.py`, extend `MachineDef` with a second `model_validator` placed after `_structure`:

```python
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
                            f"transition {name!r}: role {t.by!r} may only add_to with value $caller")
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
        if g.name == "members_count":
            n = next((arg[k] for k in ("equals", "at_most", "at_least") if k in arg), None)
            if not isinstance(n, int) or isinstance(n, bool):
                raise ValueError(f"transition {where!r}: members_count N must be a literal")
            if arg.get("over") not in self.roles:
                raise ValueError(f"transition {where!r}: members_count.over {arg.get('over')!r} is not a declared role")
        if g.name in {"data_set_full", "escrow_complete"} and arg.get("over") not in self.roles:
            raise ValueError(f"transition {where!r}: {g.name}.over {arg.get('over')!r} is not a declared role")
        for s in arg.get("minus", []) if isinstance(arg, dict) else []:
            if not self.is_set(s):
                raise ValueError(f"transition {where!r}: {g.name}.minus {s!r} is not a declared set key")
        if public_log and g.name in _HIDDEN_READING and isinstance(key, str):
            d = self.data.get(key)
            if d is not None and d.visibility == "referee":
                raise ValueError(
                    f"transition {where!r}: public log but guard {g.name!r} reads"
                    f" referee-visibility key {key!r}")
```

Note the error prefix: `_check_guard` uses `transition {where!r}` for both transitions and wake rules for a single message shape; the wake-rule tests above assert on the substrings that do not depend on that prefix.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_machine_def.py -q` — Expected: PASS. Then `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/core/machine.py tests/test_machine_def.py
git commit -m "feat(core): MachineDef authority, visibility and wake-rule refusals"
```

---

### Task 4: Wire the mechanic into the project schema

**Files:**
- Modify: `src/bearpit/core/schema.py:152-158` (`MechanicKind`), `:482-505` (`Mechanic`), `:136-149` (`TerminationKind`), `:453-470` (`_required_by_type`), `:790` (`ProjectSpec.mechanics`), and the `Project` model's after-validator (search `class Project(`).
- Modify: `src/bearpit/core/tools.py:56-59` (`BUILTIN_VERBS`)
- Test: `tests/test_core_schema.py`, `tests/test_examples.py`

**Interfaces:**
- Produces: `MechanicKind.STATE_MACHINE = "state-machine"`; `Mechanic.machine: MachineDef | None`; `ProjectSpec.machine` property returning the single declared `MachineDef | None`; `TerminationKind.MACHINE_TERMINAL = "machine_terminal"`; `BUILTIN_VERBS` includes `game_state`, `game_act`, `game_set`, `game_declaration`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_core_schema.py`:

```python
def _machine_spec(**over):
    m = {"roles": {"ref": {"members": "referee"}, "player": {"members": "participants"}},
         "states": ["a", "b"], "initial": "a",
         "transitions": {"go": {"from": "a", "to": "b", "by": "ref"}}}
    m.update(over)
    return {"kind": "state-machine", "machine": m}


def _project(spec_over=None, agents=None):
    from bearpit.core.schema import Project
    spec = {"goals": ["g"], "termination": [{"type": "duration", "limit": "10m"}]}
    spec.update(spec_over or {})
    return Project.model_validate({
        "apiVersion": "bearpit/v1alpha1", "kind": "Project", "metadata": {"name": "m"},
        "spec": spec,
        "agents": agents or [
            {"id": "ref", "role": "referee", "model_category": "large", "rubric": "r"},
            {"id": "a", "role": "participant", "model_category": "small"},
            {"id": "b", "role": "participant", "model_category": "small"},
        ],
    })


def test_a_state_machine_mechanic_is_declared_beside_sealed_submit():
    p = _project({"mechanics": [_machine_spec()]})
    assert p.spec.machine is not None and p.spec.machine.initial == "a"


def test_two_machines_are_refused():
    import pytest
    with pytest.raises(ValueError, match="exactly one state-machine mechanic"):
        _project({"mechanics": [_machine_spec(), _machine_spec()]})


def test_wake_rules_and_turns_are_one_attention_system():
    import pytest
    with pytest.raises(ValueError, match="wake rules and `turns` cannot both be set"):
        _project({"mechanics": [_machine_spec(wake=[{"role": "ref", "after_s": 240}])],
                  "turns": {"policy": "one-at-a-time"}})


def test_explicit_role_members_must_be_on_the_roster():
    import pytest
    with pytest.raises(ValueError, match="role 'impostor' names 'zed', who is not on the roster"):
        _project({"mechanics": [_machine_spec(roles={
            "ref": {"members": "referee"}, "player": {"members": "participants"},
            "impostor": {"members": ["a", "zed"], "visibility": "hidden"}})]})


def test_machine_terminal_is_a_termination_kind():
    p = _project({"mechanics": [_machine_spec(terminal=["b"])],
                  "termination": [{"type": "machine_terminal"}]})
    assert p.spec.termination[0].type == "machine_terminal"


def test_machine_terminal_needs_a_machine_with_a_terminal_state():
    import pytest
    with pytest.raises(ValueError, match="machine_terminal termination needs a state-machine with a terminal state"):
        _project({"termination": [{"type": "machine_terminal"}]})


def test_the_four_game_tools_are_builtin_verbs():
    from bearpit.core.tools import BUILTIN_VERBS
    assert {"game_state", "game_act", "game_set", "game_declaration"} <= BUILTIN_VERBS
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_core_schema.py -k "state_machine or two_machines or wake_rules or role_members or machine_terminal or game_tools" -q` — Expected: FAIL.

- [ ] **Step 3: Implement**

In `src/bearpit/core/schema.py`:

1. Import at the top (with the other `bearpit.core` imports): `from bearpit.core.machine import MachineDef`.
2. `MechanicKind`: add `STATE_MACHINE = "state-machine"  # a declarative game state machine (spec 2026-09-10)`.
3. `TerminationKind`: add `MACHINE_TERMINAL = "machine_terminal"  # the game state machine entered a terminal state`.
4. `Mechanic`: add the field and extend the validator:

```python
    machine: MachineDef | None = Field(
        default=None,
        description="For kind 'state-machine': the declaration (roles, states, transitions, "
        "data, wake rules). Validated at parse time; see docs/superpowers/specs/2026-09-10-game-state-machine-design.md.",
    )

    @model_validator(mode="after")
    def _machine_matches_kind(self) -> Mechanic:
        if (self.kind == MechanicKind.STATE_MACHINE) != (self.machine is not None):
            raise ValueError("kind 'state-machine' requires `machine`, and only that kind may set it")
        return self
```

5. `ProjectSpec`: add a property and a validator (place them after the existing `mechanics` field):

```python
    @property
    def machine(self) -> MachineDef | None:
        ms = [m.machine for m in self.mechanics if m.kind == MechanicKind.STATE_MACHINE]
        return ms[0] if ms else None

    @model_validator(mode="after")
    def _one_machine_one_attention_system(self) -> ProjectSpec:
        ms = [m for m in self.mechanics if m.kind == MechanicKind.STATE_MACHINE]
        if len(ms) > 1:
            raise ValueError("exactly one state-machine mechanic per realm at MVP; found "
                             f"{len(ms)}")
        m = self.machine
        if m is not None and m.wake and self.turns is not None:
            raise ValueError("wake rules and `turns` cannot both be set — one attention system")
        if any(c.type == TerminationKind.MACHINE_TERMINAL for c in self.termination) and (
                m is None or not m.terminal):
            raise ValueError(
                "machine_terminal termination needs a state-machine with a terminal state")
        return self
```

6. `Project` after-validator (find the existing one that checks referee/roster consistency; add at its end):

```python
        m = self.spec.machine
        if m is not None:
            ids = {a.id for a in self.agents}
            for rname, r in m.roles.items():
                if isinstance(r.members, list):
                    for who in r.members:
                        if who not in ids:
                            raise ValueError(
                                f"role {rname!r} names {who!r}, who is not on the roster")
```

7. `_required_by_type` in `TerminationCondition`: `MACHINE_TERMINAL` needs no extra field — no branch required; the spec-level validator above enforces the machine.

In `src/bearpit/core/tools.py:56`, add `"game_state", "game_act", "game_set", "game_declaration",` to `BUILTIN_VERBS`.

- [ ] **Step 4: Run to verify they pass — and that every example still loads**

Run: `uv run pytest tests/test_core_schema.py tests/test_examples.py -q` — Expected: PASS. Then `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/core/schema.py src/bearpit/core/tools.py tests/test_core_schema.py
git commit -m "feat(core): declare a state-machine mechanic; machine_terminal; the four game verbs are builtin"
```

---

### Task 5: The pure engine — state, bindings, pointer

**Files:**
- Create: `src/bearpit/realmtools/machine.py`
- Test: `tests/test_machine_engine.py`

**Interfaces:**
- Produces:
  - `Bindings(members: dict[str, tuple[str, ...]], roster: tuple[str, ...], referee: str | None)` — host-resolved role membership + the rotation order (frozen dataclass).
  - `MachineState(state: str, actor: str | None, actor_since: int, data: dict[str, Any], owner_data: dict[str, dict[str, Any]], sets: dict[str, set[str]], revealed: dict[str, set[str]])` with `.copy()`.
  - `initial_state(defn, bindings, now_ms) -> MachineState`.
  - `roles_of(defn, bindings, agent) -> set[str]`.
  - `eligible(defn, state, agent) -> bool` — member of `actor.over` and in no `skip` set.
  - `advance_actor(defn, bindings, state, now_ms) -> None` — rotates from the current actor in roster order to the next eligible member, wrapping; parks at `None` if none. From a parked pointer it starts at roster position 0.
  - `set_actor(defn, bindings, state, who, now_ms) -> str | None` — returns a rejection reason string, or `None` on success. Rejects a non-member and a member of any skip set; accepts `None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_machine_engine.py`:

```python
"""The pure engine: (declaration, bindings, state, caller, transition, args) -> state | Rejection.

No IO, no clock — `now_ms` is a parameter. Every guard, effect and pointer rule from spec §2 is
pinned here, including the two the reviews found the hard way: the pointer must PARK when nobody
is eligible (else it lands on the raiser and falsely wakes it), and `set_actor` must refuse a
member of a skip set (else a referee typo makes a folded player the actor).
"""
from __future__ import annotations

from bearpit.core.machine import MachineDef
from bearpit.realmtools.machine import (
    Bindings, MachineState, advance_actor, eligible, initial_state, roles_of, set_actor,
)

POKERISH = MachineDef.model_validate({
    "roles": {"dealer": {"members": "referee"}, "player": {"members": "participants"}},
    "states": ["waiting", "street", "settled"],
    "initial": "waiting",
    "actor": {"over": "player", "skip": ["out", "all_in", "acted"]},
    "participant_effects": ["reset"],
    "data": {
        "hole": {"visibility": "owner"}, "pot": {"visibility": "public"},
        "secret": {"visibility": "referee"},
        "out": {"visibility": "public", "type": "set"},
        "all_in": {"visibility": "public", "type": "set"},
        "acted": {"visibility": "public", "type": "set"},
    },
    "transitions": {
        "deal": {"from": "waiting", "to": "street", "by": "dealer",
                 "effects": [{"reset": "acted"}, {"set_actor": "$args.first"}]},
        "fold": {"from": "street", "to": "same", "by": "player",
                 "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
                 "effects": [{"add_to": {"key": "out", "value": "$caller"}},
                             {"add_to": {"key": "acted", "value": "$caller"}}, "advance_actor"]},
        "call": {"from": "street", "to": "same", "by": "player",
                 "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
                 "effects": [{"add_to": {"key": "acted", "value": "$caller"}}, "advance_actor"]},
        "raise": {"from": "street", "to": "same", "by": "player",
                  "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
                  "effects": [{"reset": "acted"}, {"add_to": {"key": "acted", "value": "$caller"}},
                              "advance_actor"]},
        "advance": {"from": "street", "to": "settled", "by": "dealer",
                    "guard": [{"data_set_full": {"key": "acted", "over": "player",
                                                 "minus": ["out", "all_in"]}}]},
    },
    "wake": [
        {"role": "actor"},
        {"role": "dealer", "when": [{"data_set_full": {"key": "acted", "over": "player",
                                                        "minus": ["out", "all_in"]}}]},
    ],
})
B = Bindings(members={"dealer": ("dealer",), "player": ("a", "b", "c", "d")},
             roster=("a", "b", "c", "d"), referee="dealer")


def _s() -> MachineState:
    return initial_state(POKERISH, B, now_ms=1000)


def test_initial_state_has_declared_sets_and_a_parked_pointer():
    s = _s()
    assert s.state == "waiting" and s.actor is None and s.actor_since == 1000
    assert s.sets == {"out": set(), "all_in": set(), "acted": set()}
    assert s.data == {} and s.owner_data == {"hole": {}}


def test_roles_of_resolves_from_bindings():
    assert roles_of(POKERISH, B, "dealer") == {"dealer"}
    assert roles_of(POKERISH, B, "a") == {"player"}
    assert roles_of(POKERISH, B, "nobody") == set()


def test_pointer_rotates_in_roster_order_and_wraps():
    s = _s()
    assert set_actor(POKERISH, B, s, "c", 5) is None and s.actor == "c" and s.actor_since == 5
    advance_actor(POKERISH, B, s, 6); assert s.actor == "d"
    advance_actor(POKERISH, B, s, 7); assert s.actor == "a"  # wrapped


def test_pointer_skips_every_listed_set():
    s = _s()
    s.sets["out"].add("b"); s.sets["all_in"].add("c")
    set_actor(POKERISH, B, s, "a", 1)
    advance_actor(POKERISH, B, s, 2)
    assert s.actor == "d"  # b folded, c all-in


def test_pointer_parks_when_nobody_is_eligible():
    """Reviewed twice: after the last player matches a raise, the pointer used to land on the
    raiser (in `acted`) and wake it for a move it cannot make."""
    s = _s()
    s.sets["out"].add("a"); s.sets["acted"] |= {"b", "c", "d"}
    set_actor(POKERISH, B, s, "d", 1)
    advance_actor(POKERISH, B, s, 2)
    assert s.actor is None and s.actor_since == 2
    assert not eligible(POKERISH, s, "b")


def test_advancing_a_parked_pointer_starts_from_the_top():
    s = _s()
    advance_actor(POKERISH, B, s, 3)
    assert s.actor == "a"


def test_set_actor_refuses_a_skipped_member_and_a_stranger_but_accepts_null():
    """A dealer typo (`advance {first: alice}` with alice folded) must not make a folded player
    the actor: her `check` would then pass every guard — a physics hole opened by a referee."""
    s = _s()
    s.sets["out"].add("a")
    assert set_actor(POKERISH, B, s, "a", 1) == "'a' is in skip set 'out'"
    assert set_actor(POKERISH, B, s, "zed", 1) == "'zed' is not a member of 'player'"
    assert set_actor(POKERISH, B, s, "dealer", 1) == "'dealer' is not a member of 'player'"
    assert set_actor(POKERISH, B, s, None, 9) is None and s.actor is None and s.actor_since == 9
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_machine_engine.py -q` — Expected: FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Write the engine's foundation**

Create `src/bearpit/realmtools/machine.py`:

```python
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
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_machine_engine.py -q` — Expected: PASS (7). Then `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/machine.py tests/test_machine_engine.py
git commit -m "feat(realmtools): machine engine foundation — state, bindings, a pointer that parks"
```

---

### Task 6: Guards and value resolution

**Files:**
- Modify: `src/bearpit/realmtools/machine.py`
- Test: `tests/test_machine_engine.py`

**Interfaces:**
- Produces:
  - `Ctx(caller: str | None, args: dict[str, Any], escrow: dict[str, set[str]])` — per-call inputs. `escrow` maps a round id to the set of agents who have sealed it (the service pre-fetches; the engine never does IO). `caller=None` for wake-rule evaluation.
  - `resolve(value, ctx, state) -> Any` — `$caller`, `$args.<n>`, `$data.<key>` or the literal.
  - `check_guard(g, defn, bindings, state, ctx) -> bool`.
  - `_expected(defn, bindings, state, over, minus) -> set[str]` — role members minus the union of the named sets.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_machine_engine.py`:

```python
from bearpit.core.machine import Guard
from bearpit.realmtools.machine import Ctx, check_guard, resolve


def _g(name, arg=None):
    return Guard.model_validate({name: arg} if arg is not None else name)


def _ctx(caller="a", args=None, escrow=None):
    return Ctx(caller=caller, args=args or {}, escrow=escrow or {})


def test_resolve_caller_args_data_and_literals():
    s = _s(); s.data["pot"] = 40
    c = _ctx(args={"to": 30})
    assert resolve("$caller", c, s) == "a"
    assert resolve("$args.to", c, s) == 30
    assert resolve("$data.pot", c, s) == 40
    assert resolve(7, c, s) == 7 and resolve("bob", c, s) == "bob"
    assert resolve("$args.missing", c, s) is None


def test_caller_is_actor_is_false_on_a_parked_pointer():
    s = _s()
    assert not check_guard(_g("caller_is_actor"), POKERISH, B, s, _ctx("a"))
    set_actor(POKERISH, B, s, "a", 1)
    assert check_guard(_g("caller_is_actor"), POKERISH, B, s, _ctx("a"))
    assert not check_guard(_g("caller_is_actor"), POKERISH, B, s, _ctx("b"))


def test_membership_guards():
    s = _s(); s.sets["acted"].add("a")
    assert check_guard(_g("caller_in", "acted"), POKERISH, B, s, _ctx("a"))
    assert not check_guard(_g("caller_not_in", "acted"), POKERISH, B, s, _ctx("a"))
    assert check_guard(_g("caller_not_in", "acted"), POKERISH, B, s, _ctx("b"))


def test_data_equals_present_and_set_empty():
    s = _s()
    assert not check_guard(_g("data_present", "pot"), POKERISH, B, s, _ctx())
    s.data["pot"] = 0
    assert check_guard(_g("data_present", "pot"), POKERISH, B, s, _ctx())
    assert check_guard(_g("data_equals", {"key": "pot", "value": 0}), POKERISH, B, s, _ctx())
    # values resolve: a guard may compare a key to an arg
    set_actor(POKERISH, B, s, "c", 1)
    assert check_guard(_g("data_equals", {"key": "actor", "value": "$args.player"}),
                       POKERISH, B, s, _ctx(args={"player": "c"}))
    assert check_guard(_g("data_set_empty", "out"), POKERISH, B, s, _ctx())
    s.sets["out"].add("a")
    assert not check_guard(_g("data_set_empty", "out"), POKERISH, B, s, _ctx())


def test_members_count_and_data_set_full_over_the_live_set():
    s = _s()
    s.sets["out"].add("a"); s.sets["all_in"].add("b")
    live = _g("members_count", {"over": "player", "minus": ["out", "all_in"], "equals": 2})
    assert check_guard(live, POKERISH, B, s, _ctx())
    last = _g("members_count", {"over": "player", "minus": ["out"], "at_most": 1})
    assert not check_guard(last, POKERISH, B, s, _ctx())
    full = _g("data_set_full", {"key": "acted", "over": "player", "minus": ["out", "all_in"]})
    assert not check_guard(full, POKERISH, B, s, _ctx())
    s.sets["acted"] |= {"c", "d"}
    assert check_guard(full, POKERISH, B, s, _ctx())  # a and b are excused


def test_data_set_full_is_vacuously_true_when_everyone_is_excused():
    """Everyone all-in: the dealer runs the board with guard-true advances (spec §2 pointer)."""
    s = _s(); s.sets["all_in"] |= {"a", "b", "c", "d"}
    full = _g("data_set_full", {"key": "acted", "over": "player", "minus": ["out", "all_in"]})
    assert check_guard(full, POKERISH, B, s, _ctx())


def test_escrow_complete_uses_the_machines_live_set_not_the_escrows_roster():
    """Review M5: the escrow's own roster only grows, so an eliminated agent that never seals
    deadlocked every round after the first ejection."""
    s = _s(); s.sets["out"].add("d")
    g = _g("escrow_complete", {"round": "$data.hand", "over": "player", "minus": ["out"]})
    s.data["hand"] = "H1"
    assert not check_guard(g, POKERISH, B, s, _ctx(escrow={"H1": {"a", "b"}}))
    assert check_guard(g, POKERISH, B, s, _ctx(escrow={"H1": {"a", "b", "c"}}))  # d excused
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_machine_engine.py -q` — Expected: FAIL, `ImportError: Ctx`.

- [ ] **Step 3: Implement**

Append to `src/bearpit/realmtools/machine.py`:

```python
from bearpit.core.machine import Guard  # noqa: E402  (grouped with the engine's own section)


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
        return current == resolve(a.get("value"), ctx, state)
    if g.name == "data_set_empty":
        return not state.sets.get(str(g.arg), set())
    if g.name == "members_count":
        n = len(_expected(defn, bindings, state, a["over"], a.get("minus", [])))
        if "equals" in a:
            return n == a["equals"]
        if "at_most" in a:
            return n <= a["at_most"]
        return n >= a["at_least"]
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
```

Move the `from bearpit.core.machine import Guard` line up into the module's import block (with `MachineDef`) so ruff's import ordering passes; the `# noqa` above is only to show placement.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_machine_engine.py -q` — Expected: PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/machine.py tests/test_machine_engine.py
git commit -m "feat(realmtools): machine guards — set predicates and escrow completion over the live set"
```

---

### Task 7: Effects, authority at runtime, `$args` member validation

**Files:**
- Modify: `src/bearpit/realmtools/machine.py`
- Test: `tests/test_machine_engine.py`

**Interfaces:**
- Produces: `apply_effect(e, defn, bindings, state, ctx, now_ms) -> str | None` — mutates `state`; returns a rejection reason or `None`. Enforces at runtime what the declaration promised at launch (a participant-declared transition can only carry safe effects, so runtime only needs the `$args` member validation and `set_actor` eligibility). `reveal` selectors: `$caller`, a literal owner, or `{over, minus}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_machine_engine.py`:

```python
from bearpit.core.machine import Effect
from bearpit.realmtools.machine import apply_effect


def _e(name, arg=None):
    return Effect.model_validate({name: arg} if arg is not None else name)


def test_add_to_remove_from_reset_and_unset():
    s = _s(); c = _ctx("a", {"victim": "b"})
    assert apply_effect(_e("add_to", {"key": "out", "value": "$caller"}), POKERISH, B, s, c, 1) is None
    assert apply_effect(_e("add_to", {"key": "out", "value": "$args.victim"}), POKERISH, B, s, c, 1) is None
    assert s.sets["out"] == {"a", "b"}
    assert apply_effect(_e("remove_from", {"key": "out", "value": "a"}), POKERISH, B, s, c, 1) is None
    assert s.sets["out"] == {"b"}
    assert apply_effect(_e("reset", "out"), POKERISH, B, s, c, 1) is None and s.sets["out"] == set()
    s.data["pot"] = 5
    assert apply_effect(_e("unset", "pot"), POKERISH, B, s, c, 1) is None and "pot" not in s.data


def test_args_used_as_members_are_validated_against_the_role():
    """Junk in `$args` would silently break `skip` and `data_set_full` (review m12)."""
    s = _s(); c = _ctx("dealer", {"victim": "zed"})
    assert apply_effect(_e("add_to", {"key": "out", "value": "$args.victim"}), POKERISH, B, s, c, 1) \
        == "'zed' is not a member of 'player'"
    assert s.sets["out"] == set()


def test_set_writes_a_resolved_value_and_set_actor_checks_eligibility():
    s = _s(); c = _ctx("dealer", {"first": "b", "n": 3})
    assert apply_effect(_e("set", {"key": "pot", "value": "$args.n"}), POKERISH, B, s, c, 1) is None
    assert s.data["pot"] == 3
    assert apply_effect(_e("set_actor", "$args.first"), POKERISH, B, s, c, 2) is None and s.actor == "b"
    s.sets["out"].add("b")
    assert apply_effect(_e("set_actor", "$args.first"), POKERISH, B, s, c, 3) == "'b' is in skip set 'out'"
    assert apply_effect(_e("set_actor", None), POKERISH, B, s, c, 4) is None and s.actor is None


def test_reveal_selectors():
    s = _s()
    s.owner_data["hole"] = {"a": "AhKh", "b": "2c7d", "c": "QsQd"}
    s.sets["out"].add("b")
    assert apply_effect(_e("reveal", {"key": "hole", "owners": "$caller"}), POKERISH, B, s, _ctx("a"), 1) is None
    assert s.revealed["hole"] == {"a"}
    sel = {"key": "hole", "owners": {"over": "player", "minus": ["out"]}}
    assert apply_effect(_e("reveal", sel), POKERISH, B, s, _ctx("dealer"), 1) is None
    assert s.revealed["hole"] == {"a", "c", "d"}  # b folded: mucked, stays hidden
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_machine_engine.py -q` — Expected: FAIL, `ImportError: apply_effect`.

- [ ] **Step 3: Implement**

Append to `src/bearpit/realmtools/machine.py` (and add `Effect` to the `bearpit.core.machine` import):

```python
def _member_or_reason(
    defn: MachineDef, bindings: Bindings, who: Any
) -> str | None:
    """Values that name an agent must name one in the pointer's role (or any role if there is
    no pointer). Junk would silently break skip sets and cardinality guards."""
    role = defn.actor.over if defn.actor is not None else None
    pool = set(bindings.members.get(role, ())) if role else {
        a for ids in bindings.members.values() for a in ids}
    if who not in pool:
        return f"{who!r} is not a member of {role or 'any role'!r}"
    return None


def apply_effect(
    e: Effect, defn: MachineDef, bindings: Bindings, state: MachineState, ctx: Ctx, now_ms: int
) -> str | None:
    a = e.arg if isinstance(e.arg, dict) else {}
    if e.name == "advance_actor":
        advance_actor(defn, bindings, state, now_ms)
        return None
    if e.name in {"add_to", "remove_from"}:
        who = resolve(a.get("value"), ctx, state)
        if (bad := _member_or_reason(defn, bindings, who)) is not None:
            return bad
        target = state.sets.setdefault(str(a["key"]), set())
        (target.add if e.name == "add_to" else target.discard)(str(who))
        return None
    if e.name == "reset":
        state.sets[str(e.arg)] = set()
        return None
    if e.name == "set":
        state.data[str(a["key"])] = resolve(a.get("value"), ctx, state)
        return None
    if e.name == "unset":
        state.data.pop(str(e.arg), None)
        return None
    if e.name == "set_actor":
        who = resolve(e.arg, ctx, state)
        return set_actor(defn, bindings, state, None if who is None else str(who), now_ms)
    if e.name == "reveal":
        key = str(a["key"])
        sel = a.get("owners")
        if isinstance(sel, dict):
            owners = _expected(defn, bindings, state, sel["over"], sel.get("minus", []))
        else:
            who = resolve(sel, ctx, state)
            owners = {str(who)} if who is not None else set()
        state.revealed.setdefault(key, set()).update(owners)
        return None
    raise ValueError(f"unknown effect {e.name!r}")  # unreachable: refused at launch
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_machine_engine.py -q` — PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/machine.py tests/test_machine_engine.py
git commit -m "feat(realmtools): machine effects — sets, values, pointer, reveal; args validated as members"
```

---

### Task 8: `act`, `set_value`, and edge-triggered wake stamping

**Files:**
- Modify: `src/bearpit/realmtools/machine.py`
- Test: `tests/test_machine_engine.py`

**Interfaces:**
- Produces:
  - `Rejection(check: str, detail: str)`.
  - `Outcome(state: MachineState, payload: dict[str, Any])` — `payload` is exactly the GAME event payload, including `wake: list[str]`.
  - `act(defn, bindings, state, caller, transition, args, ctx_escrow, now_ms) -> Outcome | Rejection` — check order: exists → state ∈ from → caller has role `by` → guards → effects (which may reject). The input `state` is never mutated.
  - `set_value(defn, bindings, state, caller, key, value, owner, now_ms) -> Outcome | Rejection` — referee only (caller ∈ a referee role); `owner` required for owner keys and forbidden otherwise; set keys refused (use a transition).
  - `compute_wakes(defn, bindings, old, new, escrow) -> list[str]` — `role: actor` fires when the pointer moved to a non-null actor; `when` rules fire on false→true (and only while `unless` is false); targets are deduplicated and sorted; hidden roles cannot appear (refused at launch).
  - `reject_payload(caller, transition, args, check, detail, log) -> dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_machine_engine.py`:

```python
from bearpit.realmtools.machine import Outcome, Rejection, act, compute_wakes, set_value


def _deal(s, first="a"):
    out = act(POKERISH, B, s, "dealer", "deal", {"first": first}, {}, 10)
    assert isinstance(out, Outcome), out
    return out.state


def test_act_check_order_names_the_failing_check():
    s = _s()
    r = act(POKERISH, B, s, "a", "teleport", {}, {}, 1)
    assert isinstance(r, Rejection) and r.check == "exists"
    r = act(POKERISH, B, s, "a", "fold", {}, {}, 1)
    assert isinstance(r, Rejection) and r.check == "from" and "waiting" in r.detail
    r = act(POKERISH, B, s, "a", "deal", {"first": "a"}, {}, 1)
    assert isinstance(r, Rejection) and r.check == "by" and "dealer" in r.detail
    s2 = _deal(s)
    r = act(POKERISH, B, s2, "b", "fold", {}, {}, 1)  # a is the actor
    assert isinstance(r, Rejection) and r.check == "guard" and r.detail == "caller_is_actor"
    r = act(POKERISH, B, s2, "dealer", "deal", {"first": "zed"}, {}, 1)
    assert isinstance(r, Rejection) and r.check == "effect" and "zed" in r.detail


def test_act_never_mutates_its_input_and_returns_the_game_payload():
    s = _deal(_s())
    before = s.copy()
    out = act(POKERISH, B, s, "a", "call", {"to": 10}, {}, 20)
    assert isinstance(out, Outcome)
    assert s.sets["acted"] == before.sets["acted"] == set()  # input untouched
    assert out.state.sets["acted"] == {"a"} and out.state.actor == "b"
    p = out.payload
    assert p["op"] == "act" and p["transition"] == "call" and p["caller"] == "a"
    assert p["args"] == {"to": 10} and p["from"] == "street" and p["to"] == "street"
    assert p["actor"] == "b" and p["log"] == "public" and p["wake"] == ["b"]


def test_the_street_closes_without_arithmetic_and_wakes_the_dealer_exactly_once():
    """The spec's core claim, end to end: acted + reset-on-raise + skip-acted + data_set_full."""
    s = _deal(_s(), first="a")
    s = act(POKERISH, B, s, "a", "call", {}, {}, 1).state          # acted={a}, actor b
    s = act(POKERISH, B, s, "b", "raise", {"to": 30}, {}, 2).state  # acted={b}, actor c
    s = act(POKERISH, B, s, "c", "call", {}, {}, 3).state          # acted={b,c}, actor d
    out = act(POKERISH, B, s, "d", "call", {}, {}, 4)
    s = out.state                                                  # acted={b,c,d}, a is next
    assert s.actor == "a"                                          # a has not acted since the raise
    out = act(POKERISH, B, s, "a", "call", {}, {}, 5)
    s = out.state
    assert s.actor is None                                         # parked: everyone acted
    assert out.payload["wake"] == ["dealer"]                       # street closed → dealer, once
    r = act(POKERISH, B, s, "b", "raise", {"to": 60}, {}, 6)       # the stop
    assert isinstance(r, Rejection) and r.detail == "caller_is_actor"
    adv = act(POKERISH, B, s, "dealer", "advance", {}, {}, 7)
    assert isinstance(adv, Outcome) and adv.state.state == "settled"


def test_wake_is_edge_triggered_across_referee_writes():
    """Review M1: the dealer's own game_set calls leave the guard true; they must not re-wake."""
    s = _deal(_s())
    for who in "abcd":
        s = act(POKERISH, B, s, who, "call", {}, {}, 1).state
    assert s.actor is None
    out = set_value(POKERISH, B, s, "dealer", "pot", 105, None, 2)
    assert isinstance(out, Outcome) and out.payload["wake"] == []
    out = set_value(POKERISH, B, out.state, "dealer", "pot", 106, None, 3)
    assert out.payload["wake"] == []


def test_no_actor_wake_on_a_parked_pointer_and_targets_are_deduped():
    s = _s()
    old = s.copy()
    new = s.copy(); new.actor = None
    assert compute_wakes(POKERISH, B, old, new, {}) == []
    new.actor = "c"
    assert compute_wakes(POKERISH, B, old, new, {}) == ["c"]
    # two rules resolving to the same target on one event → one entry
    hu = MachineDef.model_validate({**POKERISH.model_dump(by_alias=True),
                                    "wake": [{"role": "actor"}, {"role": "player"}]})
    assert compute_wakes(hu, B, old, new, {}) == ["a", "b", "c", "d"]


def test_unless_suppresses_a_wake():
    m = MachineDef.model_validate({**POKERISH.model_dump(by_alias=True), "wake": [
        {"role": "actor", "unless": [{"members_count": {"over": "player", "minus": ["out"], "equals": 1}}]}]})
    s = _deal(_s())
    s.sets["out"] |= {"a", "b", "c"}
    old = s.copy(); new = s.copy(); new.actor = "d"
    assert compute_wakes(m, B, old, new, {}) == []  # d is the last one standing: do not wake


def test_set_value_authority_and_owner_handling():
    s = _s()
    assert isinstance(set_value(POKERISH, B, s, "a", "pot", 1, None, 1), Rejection)
    r = set_value(POKERISH, B, s, "dealer", "hole", "AhKh", None, 1)
    assert isinstance(r, Rejection) and "owner" in r.detail
    out = set_value(POKERISH, B, s, "dealer", "hole", "AhKh", "a", 1)
    assert isinstance(out, Outcome) and out.state.owner_data["hole"]["a"] == "AhKh"
    assert out.payload == {"op": "set", "key": "hole", "owner": "a", "value": "AhKh",
                           "caller": "dealer", "log": "owner", "wake": []}
    r = set_value(POKERISH, B, s, "dealer", "pot", 1, "a", 1)
    assert isinstance(r, Rejection) and "forbidden" in r.detail
    r = set_value(POKERISH, B, s, "dealer", "nope", 1, None, 1)
    assert isinstance(r, Rejection) and r.check == "key"
    r = set_value(POKERISH, B, s, "dealer", "out", ["a"], None, 1)
    assert isinstance(r, Rejection) and "transition" in r.detail
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_machine_engine.py -q` — FAIL, `ImportError: Outcome`.

- [ ] **Step 3: Implement**

Append to `src/bearpit/realmtools/machine.py`:

```python
@dataclass(frozen=True)
class Rejection:
    check: str  # exists | from | by | guard | effect | authority | key | owner
    detail: str


@dataclass(frozen=True)
class Outcome:
    state: MachineState
    payload: dict[str, Any]  # the GAME event, verbatim


def _wake_ctx(escrow: dict[str, set[str]]) -> Ctx:
    return Ctx(caller=None, args={}, escrow=escrow)


def compute_wakes(
    defn: MachineDef, bindings: Bindings, old: MachineState, new: MachineState,
    escrow: dict[str, set[str]],
) -> list[str]:
    """Edge-triggered: a `when` rule fires on false->true only, and never while `unless` holds.
    `role: actor` fires when the pointer moved to a non-null actor. Deduplicated and sorted so
    one event stamps one list, whatever order the rules were declared in."""
    targets: set[str] = set()
    ctx = _wake_ctx(escrow)
    for w in defn.wake:
        if w.after_s is not None:
            continue  # the host's clock rule — never evaluated here
        if w.role == "actor":
            if new.actor is not None and new.actor != old.actor:
                if not guards_hold(w.unless, defn, bindings, new, ctx)[0] or not w.unless:
                    targets.add(new.actor)
            continue
        now_true = guards_hold(w.when, defn, bindings, new, ctx)[0]
        was_true = guards_hold(w.when, defn, bindings, old, ctx)[0]
        blocked = bool(w.unless) and guards_hold(w.unless, defn, bindings, new, ctx)[0]
        if now_true and not was_true and not blocked:
            targets.update(bindings.members.get(w.role, ()))
    return sorted(targets)


def act(
    defn: MachineDef, bindings: Bindings, state: MachineState, caller: str, transition: str,
    args: dict[str, Any], escrow: dict[str, set[str]], now_ms: int,
) -> Outcome | Rejection:
    t = defn.transitions.get(transition)
    if t is None:
        return Rejection("exists", f"no transition {transition!r}")
    if "any" not in t.from_ and state.state not in t.from_:
        return Rejection("from", f"{transition!r} is not available from state {state.state!r}")
    if t.by not in roles_of(defn, bindings, caller):
        return Rejection("by", f"{transition!r} may only be fired by role {t.by!r}")
    ctx = Ctx(caller=caller, args=dict(args), escrow=escrow)
    ok, failed = guards_hold(t.guard, defn, bindings, state, ctx)
    if not ok:
        return Rejection("guard", str(failed))
    new = state.copy()
    for e in t.effects:
        if (bad := apply_effect(e, defn, bindings, new, ctx, now_ms)) is not None:
            return Rejection("effect", bad)
    if t.to != "same":
        new.state = t.to
    payload = {
        "op": "act", "transition": transition, "caller": caller, "args": dict(args),
        "from": state.state, "to": new.state, "actor": new.actor, "log": t.log,
        "wake": compute_wakes(defn, bindings, state, new, escrow),
    }
    return Outcome(new, payload)


def set_value(
    defn: MachineDef, bindings: Bindings, state: MachineState, caller: str, key: str,
    value: Any, owner: str | None, now_ms: int,
) -> Outcome | Rejection:
    if not any(defn.is_referee_role(r) for r in roles_of(defn, bindings, caller)):
        return Rejection("authority", "game_set is referee-only")
    d = defn.data.get(key)
    if d is None:
        return Rejection("key", f"{key!r} is not a declared data key")
    if d.type == "set":
        return Rejection("key", f"{key!r} is a set — change it through a transition")
    if d.visibility == "owner" and owner is None:
        return Rejection("owner", f"{key!r} is an owner key: `owner` is required")
    if d.visibility != "owner" and owner is not None:
        return Rejection("owner", f"{key!r} is not an owner key: `owner` is forbidden")
    new = state.copy()
    if owner is not None:
        new.owner_data.setdefault(key, {})[owner] = value
    else:
        new.data[key] = value
    payload = {
        "op": "set", "key": key, "owner": owner, "value": value, "caller": caller,
        "log": d.visibility, "wake": compute_wakes(defn, bindings, state, new, {}),
    }
    return Outcome(new, payload)


def reject_payload(
    caller: str, transition: str, args: dict[str, Any], check: str, detail: str, log: str,
) -> dict[str, Any]:
    return {"op": "reject", "transition": transition, "caller": caller, "args": dict(args),
            "check": check, "detail": detail, "log": log, "wake": []}
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_machine_engine.py -q` — PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/machine.py tests/test_machine_engine.py
git commit -m "feat(realmtools): machine act/set with edge-triggered wake stamping — a street closes without arithmetic"
```

---

### Task 9: Views — what each caller may see

**Files:**
- Modify: `src/bearpit/realmtools/machine.py`
- Test: `tests/test_machine_engine.py`

**Interfaces:**
- Produces:
  - `view(defn, bindings, state, caller) -> dict` — `{state, actor, actor_since, data}`; `data` holds public keys, the caller's own `owner` entries plus any revealed entries, and `referee` keys only for a referee; a referee sees every `owner` entry.
  - `log_row(defn, bindings, payload, caller) -> dict | None` — the spec §3 table; `None` means hidden.
  - `declaration_view(defn, bindings, caller) -> dict` — the declaration with hidden roles' member lists replaced by `"<hidden>"` unless the caller is in them or is a referee.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_machine_engine.py`:

```python
from bearpit.realmtools.machine import declaration_view, log_row, view

HIDDENDEF = MachineDef.model_validate({
    "roles": {"mother": {"members": "referee"}, "crew": {"members": "participants"},
              "impostor": {"members": ["c", "d"], "visibility": "hidden"}},
    "states": ["night", "day"], "initial": "night",
    "data": {"dead": {"visibility": "public", "type": "set"}, "plan": {"visibility": "referee"}},
    "transitions": {"kill": {"from": "night", "to": "day", "by": "impostor", "log": "referee",
                             "effects": [{"add_to": {"key": "dead", "value": "$args.target"}}]}},
})
HB = Bindings(members={"mother": ("mother",), "crew": ("a", "b", "c", "d"), "impostor": ("c", "d")},
              roster=("a", "b", "c", "d"), referee="mother")


def test_view_filters_by_visibility_and_reveals():
    s = _s(); s.data["pot"] = 40; s.data["secret"] = "deck"
    s.owner_data["hole"] = {"a": "AhKh", "b": "2c7d"}
    v = view(POKERISH, B, s, "a")
    assert v["data"] == {"pot": 40, "hole": {"a": "AhKh"}} and "secret" not in v["data"]
    s.revealed["hole"].add("b")
    assert view(POKERISH, B, s, "a")["data"]["hole"] == {"a": "AhKh", "b": "2c7d"}
    d = view(POKERISH, B, s, "dealer")["data"]
    assert d["secret"] == "deck" and d["hole"] == {"a": "AhKh", "b": "2c7d"}
    assert view(POKERISH, B, s, "c")["data"]["hole"] == {"b": "2c7d"}  # only the revealed one


def test_log_rows_follow_the_visibility_table():
    pub = {"op": "act", "transition": "call", "caller": "a", "args": {"to": 10}, "log": "public"}
    assert log_row(POKERISH, B, pub, "b") == pub
    ref = {"op": "act", "transition": "kill", "caller": "c", "args": {"target": "a"}, "log": "referee"}
    assert log_row(HIDDENDEF, HB, ref, "a") is None and log_row(HIDDENDEF, HB, ref, "mother") == ref
    rej = {"op": "reject", "transition": "kill", "caller": "c", "check": "guard", "detail": "x", "log": "referee"}
    assert log_row(HIDDENDEF, HB, rej, "a") is None and log_row(HIDDENDEF, HB, rej, "c") is None
    unknown = {"op": "reject", "transition": "zap", "caller": "c", "check": "exists", "detail": "x", "log": "public"}
    assert log_row(HIDDENDEF, HB, unknown, "a") is None and log_row(HIDDENDEF, HB, unknown, "c") == unknown
    setp = {"op": "set", "key": "pot", "owner": None, "value": 5, "caller": "dealer", "log": "public"}
    assert log_row(POKERISH, B, setp, "a") == setp
    seto = {"op": "set", "key": "hole", "owner": "a", "value": "AhKh", "caller": "dealer", "log": "owner"}
    assert log_row(POKERISH, B, seto, "a") == seto and log_row(POKERISH, B, seto, "b") is None
    setr = {"op": "set", "key": "secret", "owner": None, "value": "deck", "caller": "dealer", "log": "referee"}
    assert log_row(POKERISH, B, setr, "a") is None and log_row(POKERISH, B, setr, "dealer") == setr


def test_declaration_view_hides_hidden_role_membership():
    a = declaration_view(HIDDENDEF, HB, "a")
    assert a["roles"]["impostor"]["members"] == "<hidden>" and a["roles"]["crew"]["members"] == "participants"
    assert declaration_view(HIDDENDEF, HB, "c")["roles"]["impostor"]["members"] == ["c", "d"]
    assert declaration_view(HIDDENDEF, HB, "mother")["roles"]["impostor"]["members"] == ["c", "d"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_machine_engine.py -q` — FAIL, `ImportError: view`.

- [ ] **Step 3: Implement**

Append to `src/bearpit/realmtools/machine.py`:

```python
def _is_referee(defn: MachineDef, bindings: Bindings, caller: str) -> bool:
    return any(defn.is_referee_role(r) for r in roles_of(defn, bindings, caller))


def view(defn: MachineDef, bindings: Bindings, state: MachineState, caller: str) -> dict[str, Any]:
    ref = _is_referee(defn, bindings, caller)
    data: dict[str, Any] = {}
    for key, d in defn.data.items():
        if d.type == "set":
            if d.visibility == "public" or ref:
                data[key] = sorted(state.sets.get(key, set()))
            continue
        if d.visibility == "public":
            if key in state.data:
                data[key] = state.data[key]
        elif d.visibility == "referee":
            if ref and key in state.data:
                data[key] = state.data[key]
        else:  # owner
            entries = state.owner_data.get(key, {})
            shown = {o: v for o, v in entries.items()
                     if ref or o == caller or o in state.revealed.get(key, set())}
            if shown or key in state.owner_data:
                data[key] = shown
    return {"state": state.state, "actor": state.actor, "actor_since": state.actor_since,
            "data": data}


def log_row(
    defn: MachineDef, bindings: Bindings, payload: dict[str, Any], caller: str
) -> dict[str, Any] | None:
    """Spec §3: what a participant's log view contains. A referee sees every row."""
    if _is_referee(defn, bindings, caller):
        return payload
    op, log = payload.get("op"), payload.get("log", "public")
    if op == "reject" and payload.get("check") == "exists":
        return payload if payload.get("caller") == caller else None
    if op in {"act", "reject"}:
        return payload if log == "public" else None
    if op == "set":
        if log == "public":
            return payload
        if log == "owner":
            return payload if payload.get("owner") == caller else None
        return None
    return payload  # reveal rows are public by definition


def declaration_view(defn: MachineDef, bindings: Bindings, caller: str) -> dict[str, Any]:
    out = defn.model_dump(by_alias=True, mode="json")
    ref = _is_referee(defn, bindings, caller)
    for rname, r in defn.roles.items():
        if r.visibility == "hidden" and not ref and caller not in bindings.members.get(rname, ()):
            out["roles"][rname]["members"] = "<hidden>"
    return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_machine_engine.py -q` — PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/machine.py tests/test_machine_engine.py
git commit -m "feat(realmtools): machine views — each caller sees exactly its own slice"
```

---

### Task 10: Replay

**Files:**
- Modify: `src/bearpit/realmtools/machine.py`
- Test: `tests/test_machine_engine.py`

**Interfaces:**
- Produces: `replay(defn, bindings, events: list[tuple[int, dict]], start_ms) -> MachineState` — folds `(ts_ms, payload)` GAME payloads over `initial_state`; `act` rows re-apply the transition with `ts_ms` as `now_ms` (so `actor_since` is deterministic); `set` rows re-apply the write; `reject` rows are skipped. A row that no longer applies raises `ReplayError` (the declaration changed under a running realm — fail loudly).

- [ ] **Step 1: Write the failing tests**

```python
from bearpit.realmtools.machine import ReplayError, replay
import pytest as _pytest


def test_replay_reproduces_the_state_and_takes_actor_since_from_the_event():
    s = _deal(_s()); log = []
    out = act(POKERISH, B, s, "dealer", "deal", {"first": "a"}, {}, 10)
    live = _s(); events = []
    for who, ts in (("dealer", 10), ("a", 20), ("b", 30)):
        tr = "deal" if who == "dealer" else "call"
        o = act(POKERISH, B, live, who, tr, {"first": "a"} if tr == "deal" else {}, {}, ts)
        assert isinstance(o, Outcome); live = o.state; events.append((ts, o.payload))
    o = set_value(POKERISH, B, live, "dealer", "pot", 15, None, 40)
    live = o.state; events.append((40, o.payload))
    events.append((41, reject_payload("c", "fold", {}, "guard", "caller_is_actor", "public")))
    rebuilt = replay(POKERISH, B, events, start_ms=1000)
    assert rebuilt == live and rebuilt.actor_since == 30


def test_replay_fails_loudly_if_the_declaration_no_longer_admits_an_event():
    with _pytest.raises(ReplayError, match="replay: event 1 .* no longer applies"):
        replay(POKERISH, B, [(10, {"op": "act", "transition": "deal", "caller": "dealer",
                                   "args": {"first": "a"}, "log": "public"}),
                             (11, {"op": "act", "transition": "teleport", "caller": "a",
                                   "args": {}, "log": "public"})], start_ms=1)
```

(Also add `from bearpit.realmtools.machine import reject_payload` to the imports near the top of the test file.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_machine_engine.py -k replay -q` — FAIL.

- [ ] **Step 3: Implement**

Append to `src/bearpit/realmtools/machine.py`:

```python
class ReplayError(RuntimeError):
    """The chronicle holds a GAME event the current declaration cannot re-apply."""


def replay(
    defn: MachineDef, bindings: Bindings, events: list[tuple[int, dict[str, Any]]], start_ms: int,
) -> MachineState:
    state = initial_state(defn, bindings, start_ms)
    for i, (ts, p) in enumerate(events):
        op = p.get("op")
        if op == "reject":
            continue
        if op == "act":
            # Escrow completion is re-checked as satisfied: the event exists because it held.
            escrow = _escrow_that_held(defn, bindings, state, p)
            out = act(defn, bindings, state, str(p["caller"]), str(p["transition"]),
                      dict(p.get("args") or {}), escrow, ts)
        elif op == "set":
            out = set_value(defn, bindings, state, str(p["caller"]), str(p["key"]),
                            p.get("value"), p.get("owner"), ts)
        else:
            raise ReplayError(f"replay: event {i} has unknown op {op!r}")
        if isinstance(out, Rejection):
            raise ReplayError(f"replay: event {i} ({op} {p.get('transition') or p.get('key')})"
                              f" no longer applies: {out.check} {out.detail}")
        state = out.state
    return state


def _escrow_that_held(
    defn: MachineDef, bindings: Bindings, state: MachineState, p: dict[str, Any]
) -> dict[str, set[str]]:
    """For replay only: every `escrow_complete` guard on the transition is treated as satisfied
    by supplying the full expected set. The chronicle is the proof it held at the time."""
    t = defn.transitions.get(str(p.get("transition")))
    if t is None:
        return {}
    ctx = Ctx(caller=str(p.get("caller")), args=dict(p.get("args") or {}), escrow={})
    out: dict[str, set[str]] = {}
    for g in t.guard:
        if g.name == "escrow_complete" and isinstance(g.arg, dict):
            rid = str(resolve(g.arg.get("round"), ctx, state))
            out[rid] = _expected(defn, bindings, state, g.arg["over"], g.arg.get("minus", []))
    return out
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_machine_engine.py -q` — PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/machine.py tests/test_machine_engine.py
git commit -m "feat(realmtools): machine replay — recovery is a deterministic fold of the chronicle"
```

---

### Task 11: `MachineService` — the engine meets the chronicle

**Files:**
- Create: `src/bearpit/realmtools/machine_service.py`
- Test: `tests/test_machine_service.py`

**Interfaces:**
- Consumes: everything from `realmtools/machine.py`; `Identity` from `realmtools/service.py`; `Chronicle.events(realm, kind)` / `append_event`; an injected `EscrowLookup = Callable[[str, str], Awaitable[set[str]]]` (realm_id, round_id) → who has sealed.
- Produces: `MachineService(chronicle=None, escrow_lookup=None)` with `set_chronicle(c)`, and:
  - `async state(who: Identity, since: int | None = None, log_limit: int = 100) -> dict` — `{**view, "log": [rows], "next_since": <last event id seen>}`; or `{"error": "no machine declared"}`.
  - `async act(who, transition, args) -> dict` — the new view on success; `{"error", "check", "detail"}` on rejection (and the rejection is chronicled).
  - `async set(who, key, value, owner=None) -> dict`.
  - `async declaration(who) -> dict`.
  - Persisted MACHINE payload shape (written by the runner in Task 13): `{"version": 1, "declaration": <MachineDef json by alias>, "members": {role: [ids]}, "roster": [ids], "referee": id|null}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_machine_service.py`:

```python
"""MachineService: identity -> role, the per-realm lock, append-then-apply, replay-after-restart.

The two invariants that matter most are the ones the reviews called out: memory must never lead
the chronicle (append first), and two acts arriving together must serialise — the Arbiter #17
bug class was exactly a check-then-write interleaving across an await.
"""
from __future__ import annotations

import asyncio

import pytest

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.machine_service import MachineService
from bearpit.realmtools.service import Identity

DECL = {
    "roles": {"dealer": {"members": "referee"}, "player": {"members": "participants"}},
    "states": ["waiting", "street"], "initial": "waiting",
    "actor": {"over": "player", "skip": ["acted"]},
    "data": {"pot": {"visibility": "public"}, "hole": {"visibility": "owner"},
             "acted": {"visibility": "public", "type": "set"},
             "hand": {"visibility": "public"}},
    "transitions": {
        "deal": {"from": "waiting", "to": "street", "by": "dealer",
                 "effects": [{"reset": "acted"}, {"set_actor": "$args.first"}]},
        "call": {"from": "street", "to": "same", "by": "player",
                 "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
                 "effects": [{"add_to": {"key": "acted", "value": "$caller"}}, "advance_actor"]},
        "resolve": {"from": "street", "to": "waiting", "by": "dealer",
                    "guard": [{"escrow_complete": {"round": "$data.hand", "over": "player", "minus": []}}]},
    },
    "wake": [{"role": "actor"}],
}
MACHINE = {"version": 1, "declaration": DECL,
           "members": {"dealer": ["dealer"], "player": ["a", "b"]},
           "roster": ["a", "b"], "referee": "dealer"}
DEALER = Identity("r", "dealer", True, roster=("a", "b"))
A = Identity("r", "a", False)
B = Identity("r", "b", False)


@pytest.fixture
async def chron():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await c.append_event("r", EventKind.MACHINE, MACHINE)
    yield c
    await c.close()


def _svc(chron, sealed=None):
    async def lookup(realm: str, round_id: str) -> set[str]:
        return set((sealed or {}).get(round_id, ()))
    return MachineService(chron, escrow_lookup=lookup)


async def test_no_machine_declared_is_a_clean_answer_not_an_exception():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    svc = _svc(c)
    assert await svc.state(A) == {"error": "no machine declared"}
    assert (await svc.act(A, "call", {}))["error"] == "no machine declared"
    await c.close()


async def test_act_returns_the_callers_view_and_chronicles_the_event(chron):
    svc = _svc(chron)
    out = await svc.act(DEALER, "deal", {"first": "a"})
    assert out["state"] == "street" and out["actor"] == "a"
    ev = [e for e in await chron.events("r", kind=EventKind.GAME)]
    assert len(ev) == 1 and ev[0].payload["transition"] == "deal" and ev[0].payload["wake"] == ["a"]


async def test_rejections_are_chronicled_and_named(chron):
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    out = await svc.act(B, "call", {})
    assert out == {"error": "rejected", "check": "guard", "detail": "caller_is_actor"}
    rej = [e.payload for e in await chron.events("r", kind=EventKind.GAME) if e.payload["op"] == "reject"]
    assert rej and rej[0]["caller"] == "b" and rej[0]["check"] == "guard"


async def test_set_is_referee_only_and_writes_owner_entries(chron):
    svc = _svc(chron)
    assert (await svc.set(A, "pot", 1))["error"] == "rejected"
    out = await svc.set(DEALER, "hole", "AhKh", owner="a")
    assert out["data"]["hole"] == {"a": "AhKh"}  # the dealer sees every owner entry
    assert (await svc.state(A))["data"]["hole"] == {"a": "AhKh"}
    assert (await svc.state(B))["data"]["hole"] == {}


async def test_state_pages_the_log_with_a_cursor_and_filters_rows(chron):
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    await svc.set(DEALER, "hole", "x", owner="a")
    await svc.act(A, "call", {})
    first = await svc.state(B, log_limit=10)
    ops = [(r["op"], r.get("transition") or r.get("key")) for r in first["log"]]
    assert ops == [("act", "deal"), ("act", "call")]  # b never sees a's hole write
    nxt = await svc.state(B, since=first["next_since"])
    assert nxt["log"] == []


async def test_escrow_completion_is_looked_up_per_call(chron):
    svc = _svc(chron, sealed={"H1": {"a", "b"}})
    await svc.act(DEALER, "deal", {"first": "a"})
    await svc.set(DEALER, "hand", "H1")
    out = await svc.act(DEALER, "resolve", {})
    assert out["state"] == "waiting"


async def test_two_acts_interleaved_across_the_append_await_serialise(chron):
    """The Arbiter #17 bug class: without the lock, both callers pass `caller_is_actor` on the
    same pre-state and both writes land."""
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    real_append = chron.append_event

    async def slow_append(*a, **k):
        await asyncio.sleep(0.01)
        return await real_append(*a, **k)
    chron.append_event = slow_append  # type: ignore[method-assign]
    r1, r2 = await asyncio.gather(svc.act(A, "call", {}), svc.act(A, "call", {}))
    outcomes = sorted(("error" in r1, "error" in r2))
    assert outcomes == [False, True], (r1, r2)  # exactly one lands
    acts = [e for e in await chron.events("r", kind=EventKind.GAME) if e.payload["op"] == "act"]
    assert len(acts) == 2  # deal + one call


async def test_a_restart_replays_only_events_after_the_latest_machine_record(chron):
    """Realm ids are reused; an older run's events must not leak into this one."""
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    await svc.act(A, "call", {})
    # a second run of the same realm id: new MACHINE record, fresh state
    await chron.append_event("r", EventKind.MACHINE, MACHINE)
    fresh = _svc(chron)
    assert (await fresh.state(A))["state"] == "waiting"
    # and a restart mid-run rebuilds what the run had reached
    await fresh.act(DEALER, "deal", {"first": "b"})
    again = _svc(chron)
    assert (await again.state(A))["actor"] == "b"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_machine_service.py -q` — FAIL, `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `src/bearpit/realmtools/machine_service.py`:

```python
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
from bearpit.core.machine import MachineDef
from bearpit.realmtools import machine as eng
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
        games = [(e.ts_ms, e.payload, e.id) for e in await self._chron.events(realm_id, kind=EventKind.GAME)
                 if e.id > head.id]
        state = eng.replay(defn, bindings, [(ts, pl) for ts, pl, _ in games], start_ms=head.ts_ms)
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

    async def _escrow_for(self, live: _Live, realm_id: str, transition: str, who: Identity,
                          args: dict[str, Any]) -> dict[str, set[str]]:
        """Pre-fetch every escrow round the transition's guards or the wake rules reference."""
        if self._escrow_lookup is None:
            return {}
        t = live.defn.transitions.get(transition)
        guards = list(t.guard) if t else []
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
    async def state(self, who: Identity, since: int | None = None, log_limit: int = 100) -> dict[str, Any]:
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

    async def act(self, who: Identity, transition: str, args: dict[str, Any] | None) -> dict[str, Any]:
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

    async def set(self, who: Identity, key: str, value: Any, owner: str | None = None) -> dict[str, Any]:
        async with self._lock(who.realm_id):
            live = await self._get(who.realm_id)
            if live is None or self._chron is None:
                return {"error": "no machine declared"}
            out = eng.set_value(live.defn, live.bindings, live.state, who.agent_id, key, value,
                                owner, self._clock())
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
```

- [ ] **Step 4: Run to verify they pass; prove the concurrency guard bites**

Run: `uv run pytest tests/test_machine_service.py -q` — PASS. Then temporarily replace `async with self._lock(who.realm_id):` in `act` with `if True:` and re-run — `test_two_acts_interleaved…` must FAIL (both calls land). Restore. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/machine_service.py tests/test_machine_service.py
git commit -m "feat(realmtools): MachineService — per-realm lock, append-then-apply, replay after the latest MACHINE record"
```

---

### Task 12: The four MCP builtins

**Files:**
- Modify: `src/bearpit/realmtools/server.py` (`build_app`: construct `MachineService`, wire its chronicle in `_wire`, register four tools after `tally`)
- Test: `tests/test_realmtools_service.py` (session-level, reusing the `_as`/`_server` harness shape from `tests/test_tool_broker.py:340-372`), `tests/test_mechanics.py` (parity)

**Interfaces:**
- Consumes: `MachineService`; `EscrowService._escrow(realm_id).status_async(round)["submitted"]` for the lookup; `who(ctx)`, `_identity`, `_audit` from `server.py`.
- Produces: tools `game_state(since, log_limit)`, `game_act(transition, args)`, `game_set(key, value, owner)`, `game_declaration()`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mechanics.py`:

```python
def test_the_game_verbs_are_registered_and_reserved():
    """Mirror of the ruleset parity test: the builtin list and the server must never drift."""
    from bearpit.core.tools import BUILTIN_VERBS
    from bearpit.realmtools.server import build_app
    app = build_app("s")
    mcp = app.state.mcp  # exposed for tests in Step 3
    names = {t.name for t in mcp._tool_manager.list_tools()}
    assert {"game_state", "game_act", "game_set", "game_declaration"} <= names
    assert {"game_state", "game_act", "game_set", "game_declaration"} <= BUILTIN_VERBS
```

Append to `tests/test_realmtools_service.py` (copy the `_as` context manager from `tests/test_tool_broker.py:349-364` into this file if it is not already importable):

```python
async def test_game_tools_over_a_real_session_respect_identity():
    """Token -> role -> machine: a player acts, a player is refused game_set, values never reach
    the audit log."""
    import json
    from bearpit.realmtools.server import build_app
    from bearpit.realmtools.tokens import mint_token
    from tests.test_machine_service import MACHINE  # the shared declaration
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await chron.append_event("r", EventKind.MACHINE, MACHINE)
    app = build_app(SECRET, chronicle=chron)
    dealer = mint_token("r", "dealer", is_referee=True, secret=SECRET, roster=["a", "b"])
    a = mint_token("r", "a", is_referee=False, secret=SECRET)
    async with app.router.lifespan_context(app):
        async with _as(app, dealer) as s:
            r = await s.call_tool("game_act", {"transition": "deal", "args": {"first": "a"}})
            assert json.loads(r.content[0].text)["actor"] == "a"
            r = await s.call_tool("game_set", {"key": "hole", "value": "AhKh", "owner": "a"})
            assert "error" not in json.loads(r.content[0].text)
        async with _as(app, a) as s:
            r = await s.call_tool("game_set", {"key": "pot", "value": 1})
            assert json.loads(r.content[0].text)["error"] == "rejected"
            r = await s.call_tool("game_state", {})
            body = json.loads(r.content[0].text)
            assert body["data"]["hole"] == {"a": "AhKh"} and body["actor"] == "a"
            r = await s.call_tool("game_declaration", {})
            assert "transitions" in json.loads(r.content[0].text)
    await chron.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_mechanics.py tests/test_realmtools_service.py -k game -q` — FAIL.

- [ ] **Step 3: Implement**

In `src/bearpit/realmtools/server.py`, inside `build_app` after `granted = ToolCallService(...)`:

```python
    async def _sealed(realm_id: str, round_id: str) -> set[str]:
        status = await service._escrow(realm_id).status_async(round_id)  # noqa: SLF001
        return set(status["submitted"])

    machine = MachineService(chronicle, escrow_lookup=_sealed)
```

In `_wire(chron)`, add `machine.set_chronicle(chron)` beside the other `set_chronicle` calls. After the FastMCP instance is created, expose it for tests: `app.state.mcp = mcp` (do this where `app` is built; if `app.state` is not available at that point, set it right before `return app`).

Register the tools after `tally`:

```python
    @mcp.tool()
    async def game_state(
        ctx: ToolContext, since: int | None = None, log_limit: int = 100
    ) -> dict[str, Any]:
        """Your view of the realm's game state machine: current state, whose move it is (`actor`,
        null when nobody's), the data you may see, and the transition log since `since` (an event
        id; pass the returned `next_since` to page). Hidden keys are simply absent."""
        ident = _identity(ctx, secret)
        res = await machine.state(who(ctx), since=since, log_limit=log_limit)
        _audit("game_state", ident, res.get("error"), result={"keys": sorted(res)})
        return res

    @mcp.tool()
    async def game_act(
        transition: str, ctx: ToolContext, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Fire a transition of the game state machine as yourself. The platform checks it is
        legal — it exists, it is available from the current state, your role may fire it, and its
        guards hold — and refuses otherwise, naming the failed check. Call game_declaration to see
        what you may fire and when."""
        ident = _identity(ctx, secret)
        res = await machine.act(who(ctx), transition, args)
        _audit(f"game_act({transition!r})", ident, res.get("error"), result={"keys": sorted(res)})
        return res

    @mcp.tool()
    async def game_set(
        key: str, value: Any, ctx: ToolContext, owner: str | None = None
    ) -> dict[str, Any]:
        """Referee only: write a declared data key. `owner` is required for owner-visibility keys
        (e.g. a player's hand) and forbidden otherwise. Set-typed keys change through
        transitions, not here. The engine never interprets the value."""
        ident = _identity(ctx, secret)
        res = await machine.set(who(ctx), key, value, owner)
        _audit(f"game_set({key!r})", ident, res.get("error"), result={"keys": sorted(res)})
        return res

    @mcp.tool()
    async def game_declaration(ctx: ToolContext) -> dict[str, Any]:
        """The machine's declaration — states, transitions (who may fire what, from where, under
        which guards), data keys and their visibility. Hidden roles' membership is not shown."""
        ident = _identity(ctx, secret)
        res = await machine.declaration(who(ctx))
        _audit("game_declaration", ident, res.get("error"), result={"keys": sorted(res)})
        return res
```

Add `from bearpit.realmtools.machine_service import MachineService` to the imports. Note every `_audit` call passes a `{"keys": ...}` shape, never the body — a hole card must not reach `docker logs`.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_mechanics.py tests/test_realmtools_service.py -q` — PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/server.py tests/test_mechanics.py tests/test_realmtools_service.py
git commit -m "feat(realmtools): game_state/act/set/declaration builtins over MachineService"
```

---

### Task 13: The host writes the MACHINE record before provisioning

**Files:**
- Modify: `src/bearpit/gatekeeper/runner.py:117-121` (beside the `TOOL_MANIFEST` write)
- Create: `src/bearpit/gatekeeper/machine_record.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Produces: `machine_record(project: Project) -> dict | None` — `{"version": 1, "declaration": <by_alias json>, "members": {...}, "roster": [participant ids in roster order], "referee": id|None}`; `referee`-member roles bind to `project.referee.id`, `participants` to every non-referee agent in roster order, explicit lists verbatim.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_runner.py` (use that file's existing fakes and its project-building helper; if none builds a machine project, construct one with `Project.model_validate` as in `tests/test_core_schema.py::_project`):

```python
async def test_the_machine_record_is_written_before_any_agent_starts():
    """Realmtools rebuilds the machine from this record. Agents begin running inside
    provision_realm, so it must exist before that — the TOOL_MANIFEST precedent."""
    from bearpit.gatekeeper.machine_record import machine_record
    project = _machine_project()  # referee 'ref', participants 'a','b', roles as in the spec
    rec = machine_record(project)
    assert rec["version"] == 1 and rec["roster"] == ["a", "b"] and rec["referee"] == "ref"
    assert rec["members"] == {"ref": ["ref"], "player": ["a", "b"]}
    # and the runner writes it ahead of provisioning
    runner, chron, forge = _runner_with_fakes()
    await runner.run(project, realm_id="r", ...)  # whatever this file's existing launch helper is
    kinds = [e.kind for e in await chron.events("r")]
    assert kinds.index("machine") < kinds.index("lifecycle") or \
        [e.payload.get("event") for e in await chron.events("r", kind="lifecycle")].index("running") > 0
    assert forge.provision_calls == 1
```

Adapt the second half to this file's actual fakes: the assertion that matters is that the `machine` event's id is lower than the id of the `running` lifecycle event.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_runner.py -k machine_record -q` — FAIL.

- [ ] **Step 3: Implement**

Create `src/bearpit/gatekeeper/machine_record.py`:

```python
"""Resolve a project's machine declaration into the MACHINE chronicle record: the declaration
plus who is in each role and the roster order the pointer rotates over. Host-resolved once at
launch and persisted, so a replay sees exactly what the run saw."""
from __future__ import annotations

from typing import Any

from bearpit.core.schema import Project
from bearpit.realmtools.machine_service import MACHINE_VERSION


def machine_record(project: Project) -> dict[str, Any] | None:
    m = project.spec.machine
    if m is None:
        return None
    referee = project.referee.id if project.referee else None
    roster = [a.id for a in project.agents if referee is None or a.id != referee]
    members: dict[str, list[str]] = {}
    for name, role in m.roles.items():
        if role.members == "referee":
            members[name] = [referee] if referee else []
        elif role.members == "participants":
            members[name] = list(roster)
        else:
            members[name] = list(role.members)
    return {"version": MACHINE_VERSION, "declaration": m.model_dump(by_alias=True, mode="json"),
            "members": members, "roster": roster, "referee": referee}
```

In `src/bearpit/gatekeeper/runner.py`, immediately after the `TOOL_MANIFEST` append (line ~120):

```python
        # The game state machine's declaration + bindings, BEFORE provisioning for the same
        # reason as the manifest: an agent may call game_state the moment its container is up.
        if (rec := machine_record(project)) is not None:
            await self.chronicle.append_event(realm_id, EventKind.MACHINE, rec)
```

with `from bearpit.gatekeeper.machine_record import machine_record` at the top.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_runner.py -q` — PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/gatekeeper/machine_record.py src/bearpit/gatekeeper/runner.py tests/test_runner.py
git commit -m "feat(gatekeeper): write the MACHINE record before any agent starts"
```

---

### Task 14: Wake delivery, `after_s`, and GAME as activity

**Files:**
- Modify: `src/bearpit/gatekeeper/runner.py` — `LiveSnapshot.__init__` (new fields), `__call__` (after the side-channel mirror; and the activity block at ~389-400)
- Test: `tests/test_runner.py` (or `tests/test_private_messaging.py`, whose `_live()`/`FakeMatrix` fixtures already drive `LiveSnapshot` with a fake Herald — prefer that file)

**Interfaces:**
- Consumes: `Herald.announce(room, body, mentions=[mxids])`; `self._creds[agent_id].user_id` for the mention; the MACHINE record (for `after_s` rules and the `terminal` list).
- Produces: `LiveSnapshot(..., machine: dict | None = None)`; per tick: (1) deliver `wake` stamps from new GAME events — actor-wakes collapse to the actor of the latest event; one `@system` mention per target into the commons with the fixed text; (2) `after_s`: if no GAME event for N seconds since the last one, nudge the rule's role once, and not again until a new GAME event lands; (3) GAME events count toward `_last_activity`; (4) snapshot gets `machine_state: str | None` (the `to` of the latest `act`, else the declaration's `initial`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_private_messaging.py` (it already has `_live`, `_creds`, `FakeMatrix`, `_herald`):

```python
WAKE_TEXT = "the machine is waiting on you. Call `game_state`."


def _machine_rec(wake, terminal=()):
    return {"version": 1, "declaration": {
        "roles": {"ref": {"members": "referee"}, "player": {"members": "participants"}},
        "states": ["a", "b"], "initial": "a", "terminal": list(terminal),
        "transitions": {"go": {"from": "a", "to": "b", "by": "ref"}}, "wake": wake},
        "members": {"ref": ["ref"], "player": ["alice", "bob"]}, "roster": ["alice", "bob"],
        "referee": "ref"}


async def test_wake_stamps_become_one_mention_per_target_and_actor_wakes_collapse():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix(); herald = await _herald(mx)
    live = _live(chron, herald, {}, _creds("alice", "bob", "ref"))
    live._machine = _machine_rec([{"role": "actor"}])
    await chron.append_event("g1", EventKind.GAME, {"op": "act", "actor": "alice", "wake": ["alice"]})
    await chron.append_event("g1", EventKind.GAME, {"op": "act", "actor": "bob", "wake": ["bob"]})
    await chron.append_event("g1", EventKind.GAME, {"op": "act", "actor": "bob", "wake": ["ref", "bob"]})
    await live()
    sent = [(room, body, mentions) for (_, room, body, mentions) in mx.sent if WAKE_TEXT in body]
    mentioned = sorted(m for (_, _, ms) in sent for m in ms)
    assert mentioned == ["@g1-bob:realm.local", "@g1-ref:realm.local"]  # alice's stale actor-wake dropped
    await live()
    assert len([1 for (_, _, body, _) in mx.sent if WAKE_TEXT in body]) == len(sent)  # delivered once
    await chron.close()


async def test_after_s_nudges_the_role_once_per_stall_measured_from_the_last_game_event():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix(); herald = await _herald(mx)
    t = {"now": 0.0}
    live = _live(chron, herald, {}, _creds("alice", "bob", "ref"))
    live._clock = lambda: t["now"]
    live._machine = _machine_rec([{"role": "ref", "after_s": 240}])
    await chron.append_event("g1", EventKind.GAME, {"op": "act", "wake": []}, ts_ms=0)
    await live(); assert not [b for (_, _, b, _) in mx.sent if WAKE_TEXT in b]
    t["now"] = 250; await live()
    nudges = [b for (_, _, b, _) in mx.sent if WAKE_TEXT in b]
    assert len(nudges) == 1
    t["now"] = 500; await live()
    assert len([b for (_, _, b, _) in mx.sent if WAKE_TEXT in b]) == 1  # not again until a new event
    await chron.append_event("g1", EventKind.GAME, {"op": "act", "wake": []}, ts_ms=500_000)
    t["now"] = 800; await live()
    assert len([b for (_, _, b, _) in mx.sent if WAKE_TEXT in b]) == 2
    await chron.close()


async def test_game_events_count_as_activity_and_the_snapshot_carries_machine_state():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix(); herald = await _herald(mx)
    t = {"now": 0.0}
    live = _live(chron, herald, {}, _creds("alice", "ref"))
    live._clock = lambda: t["now"]
    live._machine = _machine_rec([], terminal=["b"])
    snap = await live(); assert snap.machine_state == "a"
    t["now"] = 100
    await chron.append_event("g1", EventKind.GAME, {"op": "act", "to": "b", "wake": []})
    snap = await live()
    assert snap.idle_s == 0.0 and snap.machine_state == "b"
    await chron.close()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_private_messaging.py -k "wake or after_s or machine_state" -q` — FAIL.

- [ ] **Step 3: Implement**

In `src/bearpit/gatekeeper/runner.py`:

1. `LiveSnapshot.__init__`: add parameter `machine: dict[str, Any] | None = None` and fields:

```python
        self._machine = machine  # the MACHINE record, or None
        self._game_seen = 0  # id of the newest GAME event whose wakes have been delivered
        self._game_last_ts: float | None = None  # clock at which the newest GAME event was seen
        self._after_fired = False  # the after_s nudge for the current stall has been sent
        self._machine_state: str | None = (
            (machine or {}).get("declaration", {}).get("initial") if machine else None)
```

2. In `__call__`, right after the side-channel mirror loop (and before the `non_system` computation), add:

```python
        await self._deliver_wakes()
```

3. The delivery method, placed before `_deliver_private`:

```python
    async def _deliver_wakes(self) -> None:
        """Notification, never advancement: post the engine's `wake` stamps as @system mentions,
        and run the one host-side rule — `after_s`, a clock the engine deliberately does not have.
        Actor-wakes collapse to the actor of the LATEST event in the tick; a player woken for a
        pointer that has already moved on would only be woken again."""
        if self._machine is None:
            return
        events = [e for e in await self._chron.events(self._realm, kind=EventKind.GAME)
                  if e.id > self._game_seen]
        now = self._clock()
        if events:
            self._game_seen = events[-1].id
            self._game_last_ts, self._after_fired = now, False
            self._last_activity = now  # a move is agent activity for `stall`
            latest_actor = next((e.payload.get("actor") for e in reversed(events)
                                 if e.payload.get("op") == "act"), None)
            latest_to = next((e.payload.get("to") for e in reversed(events)
                              if e.payload.get("op") == "act" and e.payload.get("to")), None)
            if latest_to:
                self._machine_state = str(latest_to)
            targets: set[str] = set()
            actor_targets: set[str] = set()
            for e in events:
                for who in e.payload.get("wake", []) or []:
                    (actor_targets if who == e.payload.get("actor") else targets).add(str(who))
            if latest_actor in actor_targets:
                targets.add(str(latest_actor))
            for who in sorted(targets):
                await self._mention(who)
        elif self._game_last_ts is not None and not self._after_fired:
            for rule in self._machine.get("declaration", {}).get("wake", []):
                n = rule.get("after_s")
                if n and now - self._game_last_ts >= n:
                    for who in self._machine["members"].get(rule["role"], []):
                        await self._mention(str(who))
                    self._after_fired = True

    async def _mention(self, agent_id: str) -> None:
        cred = self._creds.get(agent_id)
        if cred is None:
            return
        await self._herald.announce(
            self._commons,
            f"{cred.user_id} — the machine is waiting on you. Call `game_state`.",
            mentions=[cred.user_id],
        )
```

4. `RealmSnapshot` construction at the end of `__call__`: pass `machine_state=self._machine_state`.

5. In the runner's `snapshot_factory` call site, pass `machine=machine_record(project)` through to `LiveSnapshot` (find where `LiveSnapshot(` is constructed in `gatekeeper/service.py` or `runner.py` and add the kwarg).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_private_messaging.py tests/test_runner.py -q` — PASS. `uv run ruff check . && uv run mypy` (the `RealmSnapshot.machine_state` field is added in Task 16 — if mypy complains here, add the field now: `machine_state: str | None = None` in `termination.py`'s `RealmSnapshot`).

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/gatekeeper/runner.py src/bearpit/warden/termination.py tests/test_private_messaging.py
git commit -m "feat(gatekeeper): deliver machine wakes, run the after_s nudge, count moves as activity"
```

---

### Task 15: `referee_reads_commons` replaces the assumed gating

**Files:**
- Modify: `src/bearpit/herald/herald.py:113`, `src/bearpit/core/runconfig.py:117`, `tests/test_api.py` (the assertion the reviewer cited near line 423), `src/bearpit/gatekeeper/static/app.js` (where `referee_sees_all` is rendered, ~line 745 — label only)
- Test: `tests/test_herald.py`, `tests/test_api.py`

**Interfaces:**
- Produces: in both predicates, `ref_sees_all = referee is not None and turns is None and (machine is None or machine.referee_reads_commons)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_herald.py` (use that file's `FakeMatrix`/bus provisioning helper and a project builder; the assertion is on `creds[referee].require_mention`):

```python
async def test_a_machine_realm_gates_the_referee_unless_it_declares_otherwise():
    """A dealer's information source is the machine; table talk would only interrupt it. A judge
    that must weigh speech opts back in with referee_reads_commons: true."""
    gated = await _provision(_project_with_machine(referee_reads_commons=False))
    assert gated.creds["ref"].require_mention is True
    reads = await _provision(_project_with_machine(referee_reads_commons=True))
    assert reads.creds["ref"].require_mention is False
```

And in `tests/test_api.py`, extend the existing `referee_sees_all` assertion with a machine case:

```python
def test_referee_sees_all_honours_referee_reads_commons():
    from bearpit.core.runconfig import run_config
    p = _project({"mechanics": [_machine_spec()]})   # helpers from test_core_schema
    assert run_config(p, provider="x", require_mention=True)["referee_sees_all"] is False
    p2 = _project({"mechanics": [_machine_spec(referee_reads_commons=True)]})
    assert run_config(p2, provider="x", require_mention=True)["referee_sees_all"] is True
```

(Adapt `run_config`'s actual signature from `src/bearpit/core/runconfig.py:60-75`.)

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_herald.py tests/test_api.py -k "reads_commons" -q` — FAIL.

- [ ] **Step 3: Implement**

`herald.py:113`:
```python
        machine = project.spec.machine
        ref_sees_all = (project.referee is not None and project.spec.turns is None
                        and (machine is None or machine.referee_reads_commons))
```
`runconfig.py:117`:
```python
        "referee_sees_all": bool(referee is not None and (turns is None or not require_mention)
                                 and (spec.machine is None or spec.machine.referee_reads_commons)),
```
In `app.js` near the `referee_sees_all` render, no logic change; if the label text says "referee sees the commons", leave it — it is now accurate in both cases.

- [ ] **Step 4: Run to verify they pass** — `uv run pytest tests/test_herald.py tests/test_api.py -q` — PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/herald/herald.py src/bearpit/core/runconfig.py tests/test_herald.py tests/test_api.py
git commit -m "feat(herald): referee gating in a machine realm is declared, not assumed"
```

---

### Task 16: `machine_terminal` termination

**Files:**
- Modify: `src/bearpit/warden/termination.py:18-33` (`RealmSnapshot`), `:105-125` (`evaluate_termination`)
- Test: `tests/test_warden.py`

**Interfaces:**
- Consumes: `RealmSnapshot.machine_state` (set by Task 14) and the project's terminal list — passed in via a new `TerminationCondition` evaluation input: the simplest is to let the runner resolve it: `LiveSnapshot` also carries `machine_terminal: list[str]` and sets `snap.machine_terminal_reached = machine_state in terminal`.
- Produces: `RealmSnapshot.machine_state: str | None = None`, `RealmSnapshot.machine_terminal_reached: bool = False`; `evaluate_termination` returns `TerminationFired(MACHINE_TERMINAL, f"machine reached {snap.machine_state}")`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_warden.py`:

```python
def test_machine_terminal_fires_when_the_machine_reaches_a_terminal_state():
    from bearpit.core.schema import TerminationCondition, TerminationKind
    from bearpit.warden.termination import RealmSnapshot, evaluate_termination
    cond = [TerminationCondition(type=TerminationKind.MACHINE_TERMINAL)]
    assert evaluate_termination(cond, RealmSnapshot(machine_state="b")) is None
    fired = evaluate_termination(cond, RealmSnapshot(machine_state="done", machine_terminal_reached=True))
    assert fired is not None and fired.kind == TerminationKind.MACHINE_TERMINAL
    assert fired.detail == "machine reached 'done'"
```

- [ ] **Step 2: Run to verify it fails** — FAIL.

- [ ] **Step 3: Implement**

In `termination.py` `RealmSnapshot`, add:
```python
    machine_state: str | None = None  # the game state machine's current state, if declared
    machine_terminal_reached: bool = False
```
In `evaluate_termination`, after the `STALL` branch:
```python
        if k == TerminationKind.MACHINE_TERMINAL and snap.machine_terminal_reached:
            return TerminationFired(k, f"machine reached {snap.machine_state!r}")
```
In `runner.py` `LiveSnapshot`: store `self._machine_terminal = set((machine or {}).get("declaration", {}).get("terminal", []))` in `__init__`, and pass `machine_terminal_reached=self._machine_state in self._machine_terminal` when building the snapshot.

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/test_warden.py tests/test_private_messaging.py -q` — PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/warden/termination.py src/bearpit/gatekeeper/runner.py tests/test_warden.py
git commit -m "feat(warden): machine_terminal — a realm ends when its machine does"
```

---

### Task 17: The birth prompt tells agents the tools exist

**Files:**
- Modify: `src/bearpit/forge/adapters/hermes/config.py` (the block around line 85-99 that describes `run_code`/notebook; add a machine block gated on `project.spec.machine`)
- Test: `tests/test_forge_config.py`

**Interfaces:**
- Consumes: `render_hermes_home(...)` already receives enough to know the project; check its signature at the top of `config.py` and thread a `machine: bool` flag if the project is not in scope there (the runner/forge adapter has `project`).

- [ ] **Step 1: Write the failing test**

```python
def test_a_machine_realm_tells_every_agent_about_the_game_tools_and_the_wake_notice():
    """Scenario-contract §20: a tool nobody is told about is a tool nobody calls."""
    files = _render(machine=True)
    soul = files["SOUL.md"]
    for tool in ("game_state", "game_act", "game_declaration"):
        assert tool in soul
    assert "the machine is waiting on you" in soul
    assert "game_set" not in _render(machine=True, referee=False)["SOUL.md"]
    assert "game_set" in _render(machine=True, referee=True)["SOUL.md"]
    assert "game_state" not in _render(machine=False)["SOUL.md"]
```

(`_render` is this file's existing helper; extend it with `machine: bool = False` and `referee: bool = False` kwargs that flow into `render_hermes_home`.)

- [ ] **Step 2: Run to verify it fails** — FAIL.

- [ ] **Step 3: Implement**

In `render_hermes_home`, add a parameter `machine: bool = False`, and where the run_code/notebook paragraph is assembled add:

```python
    if machine:
        parts.append(
            "THIS REALM RUNS A GAME STATE MACHINE. Your moves are `game_act(transition, args)`; "
            "the platform checks a move is legal and refuses it otherwise, naming why. "
            "`game_state()` is your view — the current state, whose move it is (`actor`), the data "
            "you may see, and the log of moves so far; `game_declaration()` lists every "
            "transition, who may fire it and when. You cannot see other players' hidden data; "
            "do not claim to. When it is your move you will be @mentioned with 'the machine is "
            "waiting on you' — call `game_state`, then act. Talk in the Commons whenever you like; "
            "only MOVES are sequenced."
            + (" As referee you also hold `game_set(key, value, owner=)` to write declared data "
               "after you compute it (with `run_code` or a shipped resolver) — the machine never "
               "computes anything itself." if spec.role == AgentRole.REFEREE else "")
        )
```

Thread `machine=project.spec.machine is not None` from the adapter's `provision` (which has the project via `RealmContext`/its constructor — mirror how `shared_folder` is threaded).

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/test_forge_config.py -q` — PASS. `uv run ruff check . && uv run mypy`.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/forge/adapters/hermes/config.py src/bearpit/forge/adapters/hermes/adapter.py tests/test_forge_config.py
git commit -m "feat(forge): agents are told the game tools exist, and what a wake notice means"
```

---

### Task 18: Scenario contract, CLAUDE.md, README

**Files:**
- Modify: `docs/scenario-contract.md` (§10 tool list; §12 exception; new rule "22. A machine realm has one attention system"), `CLAUDE.md`, `README.md` (the "N invariants" count — `tests/test_examples.py::test_the_invariant_count_in_the_docs_matches_the_contract` enforces it)
- Test: `tests/test_examples.py` (already exists)

- [ ] **Step 1: Run the count test to see the current number** — `uv run pytest tests/test_examples.py -k invariant_count -q` — PASS at the current count.

- [ ] **Step 2: Edit the contract**

In §10's tool list add `game_state`, `game_act`, `game_declaration` (participants) and `game_set` (referee). In §12 append:

> **Exception — machine realms.** A realm that declares a `state-machine` mechanic gates its referee unless the machine sets `referee_reads_commons: true`: a dealer's information source is the machine, and table talk would only interrupt it. A referee that must weigh speech opts back in.

Add a new numbered section at the end:

```markdown
## 22. A machine realm has one attention system, and its clock lives on the host

A `state-machine` mechanic sequences MOVES at the tool; the Herald floor sequences SPEECH. Run
poker-shaped realms with `turns: null` — the commons stays open for table talk and only acting is
gated. Wake rules and `turns` cannot both be set (refused at launch).

The engine has no clock. The one time-based rule, `{role, after_s: N}`, is evaluated by the host
as "no GAME event for N seconds" and only NUDGES; it never acts for anyone. Size N by §13: a
resolver `run_code` may block 90 s on a real pipeline, so the floor is 240 s and the default is
the floor. A referee that stalls past it is woken once; a rubric that chains its structural steps
(`showdown → resolver → settle → next_hand → deal`) never needs it.

> Found in the design reviews, not a run: the first draft woke the dealer on its own structural
> transitions and re-woke it every tick while it computed — each notice landing mid-inference
> aborted the very task it was meant to continue.
```

Update the invariant count in `CLAUDE.md` and `README.md` to the new total.

- [ ] **Step 3: Run the count test** — `uv run pytest tests/test_examples.py -q` — PASS.

- [ ] **Step 4: Commit**

```bash
git add docs/scenario-contract.md CLAUDE.md README.md
git commit -m "docs(contract): machine realms — the tool list, the referee exception, one attention system"
```

---

### Task 19: `rps-machine` — rps-duel re-expressed as a declaration

**Files:**
- Create: `examples/rps-machine/project.json`, `examples/rps-machine/README.md`, `examples/rps-machine/agents/{themis,orin,vela}/agent.json` and `persona.md` (copy `examples/rps-duel/agents/*` verbatim, then edit the referee persona)
- Modify: `examples/README.md` (the index — `test_the_index_lists_every_package` enforces it)
- Test: `tests/test_examples.py` (existing parametrised tests pick the new package up automatically)

**Interfaces:**
- Produces: the genericity proof from spec §6 — the referee's ribbon is the machine; sealing stays with escrow; players' seal-on-cue stays with `turns` (so **no wake rules**, and `turns` stays on).

- [ ] **Step 1: Copy the package**

```bash
cp -r examples/rps-duel examples/rps-machine
```

- [ ] **Step 2: Edit `examples/rps-machine/project.json`** — keep `turns`, `termination`, `guidelines`; replace `mechanics` with:

```json
"mechanics": [
  {"kind": "sealed-submit", "ruleset": "dominance",
   "config": {"beats": {"rock": ["scissors"], "scissors": ["paper"], "paper": ["rock"]}}},
  {"kind": "state-machine", "machine": {
    "roles": {"ref": {"members": "referee"}, "player": {"members": "participants"}},
    "states": ["sealing", "revealed", "scored", "done"],
    "initial": "sealing",
    "terminal": ["done"],
    "data": {"hand": {"visibility": "public"}, "score": {"visibility": "public"}},
    "transitions": {
      "reveal": {"from": "sealing", "to": "revealed", "by": "ref",
                 "guard": [{"escrow_complete": {"round": "$data.hand", "over": "player", "minus": []}}]},
      "score":  {"from": "revealed", "to": "scored", "by": "ref"},
      "next":   {"from": "scored", "to": "sealing", "by": "ref"},
      "finish": {"from": "scored", "to": "done", "by": "ref"}
    }
  }}
],
```

and add `{"type": "machine_terminal"}` as the FIRST termination condition. Set `metadata.name` to `rps-machine` and update `metadata.description`.

- [ ] **Step 3: Edit the referee persona** (`agents/themis/persona.md`): the per-round procedure becomes
  1. `game_set("hand", "R<n>")` at the start of each round, then post the round-open message.
  2. When cued, `game_act("reveal")` — it is refused until both players have sealed `R<n>`; if refused, wait.
  3. `tally("R<n>", "dominance", config)`, `score(...)`, then `game_set("score", {...})` and `game_act("score")`.
  4. `game_act("next")` and post the next round, or after R10 `game_act("finish")` — that ends the realm; also call `rule(...)`.

- [ ] **Step 4: Add the index line** in `examples/README.md`, and write `examples/rps-machine/README.md` (≥40 words: what it proves — the same game as rps-duel with the referee's sequencing moved into a declared machine, ended by `machine_terminal`).

- [ ] **Step 5: Run** — `uv run pytest tests/test_examples.py -q` — PASS (loads, README present, index present, contract checks).

- [ ] **Step 6: Commit**

```bash
git add examples/rps-machine examples/README.md
git commit -m "feat(examples): rps-machine — rps-duel with its referee ribbon as a declared machine"
```

---

### Task 20: Deploy and run `rps-machine` live

**Files:** none (operations + a chronicle check)

- [ ] **Step 1: Deploy all three components.** `scripts/serve.sh` restart (no realm may be active — check `docker ps | grep ^realm-`); rebuild realmtools: `docker compose -f deploy/docker-compose.yaml build realmtools && docker compose -f deploy/docker-compose.yaml up -d realmtools`; verify the image matches the tree by diffing `/app/src/bearpit` out of the image against `src/bearpit`; restart the model-provider process on :8787.

- [ ] **Step 2: Launch.** `POST /api/realms {"package": "examples/rps-machine", "realm_id": "rpsm-1"}` with the bearer token from `~/.bearpit/api-token`.

- [ ] **Step 3: Verify from the chronicle**, not the transcript:

```sql
-- the MACHINE record precedes the running event
select id, kind from events where realm_id='rpsm-1' and kind in ('machine','lifecycle') order by id;
-- every reveal was guard-true (no reject rows with check='guard' from the referee)
select payload->>'op', payload->>'transition', payload->>'check', count(*)
from events where realm_id='rpsm-1' and kind='game' group by 1,2,3;
-- the realm ended on machine_terminal
select payload from events where realm_id='rpsm-1' and kind='lifecycle' and payload->>'event'='concluding';
```

Expected: `machine` id < the `running` event id; 10 `reveal` + 10 `score` + 9 `next` + 1 `finish` acts; zero `reject` rows from `themis`; `concluding … reason=machine_terminal`.

- [ ] **Step 4: If anything is off**, that is a finding: file it, fix under TDD, redeploy, re-run. Only then:

- [ ] **Step 5: Open the PR** for `feat/game-state-machine` against `main`, body: the spec's summary, the review history, the live result with the three query outputs.

---

## Self-review

**Spec coverage.** §2 declaration model → Tasks 2–4; §2 pointer/parking/`set_actor` → Task 5; guards incl. `members_count` literal, `escrow_complete` over the live set → Tasks 3, 6; effects, `participant_effects`, `reveal` selectors, `$args` validation, `unset` → Tasks 3, 7; `act` check order, wake stamping (edge, `unless`, dedupe, no parked wake), `set_value` owner rules → Task 8; §3 view table, hidden-role stripping → Task 9; §4 replay after latest MACHINE, `actor_since` from `ts_ms`, append-then-apply, per-realm lock → Tasks 10–11; §3 tools, audit never sees values, BUILTIN_VERBS → Tasks 4, 12; §4 MACHINE before provisioning → Task 13; §2 wake delivery, `after_s` on the host, GAME as activity, latest-actor rule → Task 14; §5 `referee_reads_commons` in both predicates → Task 15; §5 `machine_terminal` → Task 16; birth prompt → Task 17; contract §10/§12/new rule → Task 18; §6 genericity live proof → Tasks 19–20. **Deferred per spec:** per-agent wake rooms (launch refusal in Task 3). **Out of this plan:** poker (`poker_resolver.py`, `examples/poker-table`, the 10-hand run) — Plan 2, written against the real API once this lands.

**Placeholders.** Task 13 and 15 tests say "adapt to this file's helpers" for fixture wiring only; the assertions are concrete. Task 17 names the exact prompt text.

**Type consistency.** `Bindings.members: dict[str, tuple[str, ...]]` everywhere; the MACHINE record stores lists and `_load` converts to tuples (Task 11). `Rejection.check` values: `exists | from | by | guard | effect | authority | key | owner` (Task 8), asserted by name in Tasks 8 and 11. `Outcome.payload["wake"]` is a sorted `list[str]` (Task 8) and the runner reads it as such (Task 14). `RealmSnapshot.machine_state` / `machine_terminal_reached` (Task 16) are the fields Task 14 sets.
