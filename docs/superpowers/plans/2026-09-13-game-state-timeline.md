# Game State Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a machine realm's game state and its changes inside the console transcript, driven entirely by the machine's declaration, with a per-seat lens.

**Architecture:** One new engine generator (`replay_steps`) turns the existing replay into a per-event walk. A new read-model module diffs the *caller's view* before and after each event, so privacy is a property of the construction rather than a filter. One new read-only endpoint serves declaration + state + timeline. The console merges those rows into the message feed by timestamp.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, pytest; vanilla JS console; Node `vm` smoke harness.

**Spec:** `docs/superpowers/specs/2026-09-13-game-state-timeline-design.md`

## Global Constraints

- **No scenario vocabulary anywhere.** No file in this plan may contain the strings `poker`, `hole`, `pot`, `blind`, `street`, `rps`, or any other scenario-specific term, in code, comments, or identifiers. Scenario names may appear only in test fixtures, and those fixtures must be synthetic — not a copy of a shipped scenario.
- **`tests/test_public_surface.py` forbids private-fork vocabulary.** Never write the bare words `shim`, `copilot`, `subscription`, or `ali`. Run `uv run pytest tests/test_public_surface.py` before every commit.
- **Fail closed.** Anything unrecognised (an unknown op, an unknown caller, an unreadable record) yields *less* information, never more.
- **Read-only.** No endpoint or function in this plan mutates a realm, the chronicle, or any machine state.
- Line length 100. `uv run ruff check .` and `uv run mypy` must both be clean. Package manager is `uv`; never pip.
- Commit style: imperative subject, body explains why.

---

### Task 1: `replay_steps` — replay as a per-event walk

**Files:**
- Modify: `src/bearpit/realmtools/machine.py` (the `replay` function, ~line 433)
- Test: `tests/test_machine_replay_steps.py` (create)

**Interfaces:**
- Consumes: existing `initial_state`, `act`, `set_value`, `_escrow_that_held`, `Rejection`, `ReplayError`, `MachineState`.
- Produces:
  ```python
  def replay_steps(
      defn: MachineDef, bindings: Bindings,
      events: list[tuple[int, dict[str, Any]]], start_ms: int,
  ) -> Iterator[tuple[int, dict[str, Any], MachineState]]: ...
  ```
  Yields `(ts_ms, payload, state_after)` once per event, **including `reject` events**, which yield the unchanged state. Task 2 consumes this.

- [ ] **Step 1: Write the failing test**

Create `tests/test_machine_replay_steps.py`. Build the declaration inline — do not import a shipped scenario.

```python
"""`replay_steps` must walk the same path `replay` walks, one event at a time."""
from __future__ import annotations

from typing import Any

import pytest

from bearpit.core.schema import MachineDef
from bearpit.realmtools import machine as eng

DECL: dict[str, Any] = {
    "initial": "open",
    "terminal": ["closed"],
    "roles": {"players": {"members": ["a", "b"]}, "judge": {"members": ["j"], "referee": True}},
    "data": {
        "tally": {"visibility": "public", "type": "value"},
        "picked": {"visibility": "public", "type": "set"},
        "secret": {"visibility": "owner", "type": "value"},
    },
    "transitions": {
        "bump": {"from": ["open"], "to": "same", "by": "players", "effect": []},
        "close": {"from": ["open"], "to": "closed", "by": "judge", "effect": []},
    },
}


def _fixture() -> tuple[MachineDef, eng.Bindings]:
    defn = MachineDef.model_validate(DECL)
    bindings = eng.Bindings(
        members={"players": ("a", "b"), "judge": ("j",)}, roster=("a", "b"), referee="j"
    )
    return defn, bindings


def test_steps_yield_one_row_per_event_and_end_where_replay_ends() -> None:
    defn, bindings = _fixture()
    events: list[tuple[int, dict[str, Any]]] = [
        (10, {"op": "set", "caller": "j", "key": "tally", "value": 1}),
        (20, {"op": "set", "caller": "j", "key": "tally", "value": 2}),
        (30, {"op": "act", "caller": "a", "transition": "bump", "args": {}}),
    ]
    steps = list(eng.replay_steps(defn, bindings, events, start_ms=0))
    assert [ts for ts, _, _ in steps] == [10, 20, 30]
    assert [s.data.get("tally") for _, _, s in steps] == [1, 2, 2]
    assert steps[-1][2].data == eng.replay(defn, bindings, events, start_ms=0).data


def test_a_rejection_yields_a_row_with_state_unchanged() -> None:
    defn, bindings = _fixture()
    events: list[tuple[int, dict[str, Any]]] = [
        (10, {"op": "set", "caller": "j", "key": "tally", "value": 7}),
        (20, {"op": "reject", "caller": "a", "transition": "close", "check": "by", "detail": "x"}),
    ]
    steps = list(eng.replay_steps(defn, bindings, events, start_ms=0))
    assert len(steps) == 2, "a rejection is a timeline row, not a skipped event"
    assert steps[1][2].data.get("tally") == 7
    assert steps[0][2].data == steps[1][2].data


def test_each_yielded_state_is_independent_of_later_mutation() -> None:
    """A consumer keeps these snapshots; the walk must not hand out one aliased object."""
    defn, bindings = _fixture()
    events: list[tuple[int, dict[str, Any]]] = [
        (10, {"op": "set", "caller": "j", "key": "tally", "value": 1}),
        (20, {"op": "set", "caller": "j", "key": "tally", "value": 2}),
    ]
    kept = [s for _, _, s in eng.replay_steps(defn, bindings, events, start_ms=0)]
    assert kept[0].data.get("tally") == 1, "the first snapshot was mutated by the second step"


def test_an_unknown_op_still_raises() -> None:
    defn, bindings = _fixture()
    events: list[tuple[int, dict[str, Any]]] = [(10, {"op": "teleport", "caller": "a"})]
    with pytest.raises(eng.ReplayError):
        list(eng.replay_steps(defn, bindings, events, start_ms=0))
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_machine_replay_steps.py -q`
Expected: FAIL — `module 'bearpit.realmtools.machine' has no attribute 'replay_steps'`.

If any test fails for a *different* reason (e.g. the declaration is rejected by `MachineDef`), fix the fixture until the only failure is the missing function. Report the corrected fixture in your report.

- [ ] **Step 3: Add `replay_steps` and rebuild `replay` on top of it**

Replace the body of `replay` in `src/bearpit/realmtools/machine.py`. Keep `replay`'s signature and its docstring intent; it must remain the public entry point every existing caller uses.

```python
def replay_steps(
    defn: MachineDef, bindings: Bindings,
    events: list[tuple[int, dict[str, Any]]], start_ms: int,
) -> Iterator[tuple[int, dict[str, Any], MachineState]]:
    """Walk the chronicle one event at a time, yielding the state after each.

    `replay` is this walk's last step. A reader that wants the state *between* events — the
    timeline view — consumes the walk instead, so there is exactly one implementation of what a
    GAME event means. Rejections yield the unchanged state: they never moved the machine, but
    they are rows a reader must see.

    Each yielded state is a copy. Consumers keep these snapshots to diff against one another, and
    handing out the live object would alias every snapshot to the final state.
    """
    state = initial_state(defn, bindings, start_ms)
    for i, (ts, p) in enumerate(events):
        op = p.get("op")
        if op == "reject":
            yield ts, p, state.copy()
            continue
        if op == "act":
            # Escrow completion is re-checked as satisfied: the event exists because it held.
            escrow = _escrow_that_held(defn, bindings, state, p)
            out = act(defn, bindings, state, str(p["caller"]), str(p["transition"]),
                      dict(p.get("args") or {}), escrow, ts)
        elif op == "set":
            out = set_value(defn, bindings, state, str(p["caller"]), str(p["key"]),
                            p.get("value"), p.get("owner"), {}, ts)
        else:
            raise ReplayError(f"replay: event {i} has unknown op {op!r}")
        if isinstance(out, Rejection):
            raise ReplayError(f"replay: event {i} ({op} {p.get('transition') or p.get('key')})"
                              f" no longer applies: {out.check} {out.detail}")
        state = out.state
        yield ts, p, state.copy()


def replay(
    defn: MachineDef, bindings: Bindings, events: list[tuple[int, dict[str, Any]]], start_ms: int,
) -> MachineState:
    state = initial_state(defn, bindings, start_ms)
    for _ts, _payload, stepped in replay_steps(defn, bindings, events, start_ms):
        state = stepped
    return state
```

Add `Iterator` to the `typing` import at the top of the file if it is not already imported — prefer `from collections.abc import Iterator` if the file already imports from `collections.abc`, otherwise add that import.

- [ ] **Step 4: Run the new test and the whole existing suite**

Run: `uv run pytest tests/test_machine_replay_steps.py -q`
Expected: PASS (4 tests).

Run: `uv run pytest -q`
Expected: the full suite still passes. `replay` has many existing callers (`MachineService._load` among them) and this task rewrites its body — a regression here is the main risk of this task. If anything fails, fix it before committing.

Run: `uv run ruff check . && uv run mypy`
Expected: both clean.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/machine.py tests/test_machine_replay_steps.py
git commit -m "feat(machine): expose replay as a per-event walk"
```

---

### Task 2: the view differ and the timeline read-model

**Files:**
- Create: `src/bearpit/realmtools/timeline.py`
- Test: `tests/test_machine_timeline.py` (create)

**Interfaces:**
- Consumes: `eng.replay_steps` (Task 1), and the existing `eng.view`, `eng.log_row`, `eng.initial_state`, `eng.ReplayError`.
- Produces:
  ```python
  def diff_views(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]: ...
  def build_timeline(
      defn: MachineDef, bindings: Bindings,
      events: list[tuple[int, dict[str, Any]]], start_ms: int, caller: str,
  ) -> dict[str, Any]: ...
  ```
  `build_timeline` returns `{"state": <view dict>, "timeline": [row, ...], "error": str | None}`
  where each row is `{"ts": int, "payload": dict, "changes": [change, ...]}`.
  Task 3 calls `build_timeline`.

Change shapes, exactly as the spec's §3.3 table defines them:
```python
{"kind": "state",  "from": "open",  "to": "closed"}
{"kind": "actor",  "from": "a",     "to": "b"}
{"kind": "value",  "key": "tally",  "from": 30, "to": 370}
{"kind": "set",    "key": "picked", "added": ["a"], "removed": []}
{"kind": "owner",  "key": "secret", "owner": "a", "from": None, "to": "z"}
```

- [ ] **Step 1: Write the failing test**

Create `tests/test_machine_timeline.py`. Reuse the `DECL` shape from Task 1 — copy it into this file rather than importing across test modules.

```python
"""The timeline read-model: what changed, through one caller's eyes."""
from __future__ import annotations

from typing import Any

from bearpit.core.schema import MachineDef
from bearpit.realmtools import machine as eng
from bearpit.realmtools import timeline as tl

DECL: dict[str, Any] = {
    "initial": "open",
    "terminal": ["closed"],
    "roles": {"players": {"members": ["a", "b"]}, "judge": {"members": ["j"], "referee": True}},
    "data": {
        "tally": {"visibility": "public", "type": "value"},
        "picked": {"visibility": "public", "type": "set"},
        "secret": {"visibility": "owner", "type": "value"},
        "notes": {"visibility": "referee", "type": "value"},
    },
    "transitions": {
        "bump": {"from": ["open"], "to": "same", "by": "players", "effect": []},
        "close": {"from": ["open"], "to": "closed", "by": "judge", "effect": []},
    },
}


def _fixture() -> tuple[MachineDef, eng.Bindings]:
    defn = MachineDef.model_validate(DECL)
    bindings = eng.Bindings(
        members={"players": ("a", "b"), "judge": ("j",)}, roster=("a", "b"), referee="j"
    )
    return defn, bindings


def test_diff_reports_a_scalar_move() -> None:
    changes = tl.diff_views(
        {"state": "open", "actor": None, "data": {"tally": 30}},
        {"state": "open", "actor": None, "data": {"tally": 370}},
    )
    assert changes == [{"kind": "value", "key": "tally", "from": 30, "to": 370}]


def test_diff_reports_state_and_actor_by_kind_not_by_reserved_key() -> None:
    changes = tl.diff_views(
        {"state": "open", "actor": "a", "data": {}},
        {"state": "closed", "actor": "b", "data": {}},
    )
    assert {"kind": "state", "from": "open", "to": "closed"} in changes
    assert {"kind": "actor", "from": "a", "to": "b"} in changes
    assert all("key" not in c for c in changes)


def test_diff_reports_set_additions_and_a_reset() -> None:
    grow = tl.diff_views({"data": {"picked": []}}, {"data": {"picked": ["a"]}})
    assert grow == [{"kind": "set", "key": "picked", "added": ["a"], "removed": []}]
    reset = tl.diff_views({"data": {"picked": ["a", "b"]}}, {"data": {"picked": []}})
    assert reset == [{"kind": "set", "key": "picked", "added": [], "removed": ["a", "b"]}]


def test_diff_reports_owner_entries_per_owner() -> None:
    changes = tl.diff_views(
        {"data": {"secret": {}}}, {"data": {"secret": {"a": "z"}}},
    )
    assert changes == [
        {"kind": "owner", "key": "secret", "owner": "a", "from": None, "to": "z"}
    ]


def test_actor_since_is_not_a_change() -> None:
    changes = tl.diff_views(
        {"state": "open", "actor": "a", "actor_since": 1, "data": {}},
        {"state": "open", "actor": "a", "actor_since": 999, "data": {}},
    )
    assert changes == []


def _events() -> list[tuple[int, dict[str, Any]]]:
    return [
        (10, {"op": "set", "caller": "j", "key": "secret", "owner": "a",
              "value": "z", "log": "owner"}),
        (20, {"op": "set", "caller": "j", "key": "notes", "value": "n", "log": "referee"}),
        (30, {"op": "set", "caller": "j", "key": "tally", "value": 5, "log": "public"}),
    ]


def test_the_referee_lens_sees_every_row_and_every_change() -> None:
    defn, bindings = _fixture()
    out = tl.build_timeline(defn, bindings, _events(), start_ms=0, caller="j")
    assert out["error"] is None
    assert len(out["timeline"]) == 3
    assert out["state"]["data"]["notes"] == "n"


def test_a_participant_lens_sees_strictly_less() -> None:
    """The referee-only write must not appear as a row, nor as an anonymous 'something changed'."""
    defn, bindings = _fixture()
    ref = tl.build_timeline(defn, bindings, _events(), start_ms=0, caller="j")
    seat = tl.build_timeline(defn, bindings, _events(), start_ms=0, caller="a")
    assert len(seat["timeline"]) < len(ref["timeline"])
    assert "notes" not in seat["state"]["data"]
    flat = [c for row in seat["timeline"] for c in row["changes"]]
    assert not any(c.get("key") == "notes" for c in flat), "a referee key leaked into a seat lens"


def test_an_owner_write_is_visible_to_its_owner_and_not_to_the_other_seat() -> None:
    defn, bindings = _fixture()
    mine = tl.build_timeline(defn, bindings, _events(), start_ms=0, caller="a")
    theirs = tl.build_timeline(defn, bindings, _events(), start_ms=0, caller="b")
    mine_flat = [c for row in mine["timeline"] for c in row["changes"]]
    theirs_flat = [c for row in theirs["timeline"] for c in row["changes"]]
    assert any(c.get("key") == "secret" for c in mine_flat)
    assert not any(c.get("key") == "secret" for c in theirs_flat)


def test_a_corrupt_chronicle_returns_what_replayed_plus_an_error() -> None:
    """A UI endpoint must degrade, not 500: show the good prefix and say what stopped it."""
    defn, bindings = _fixture()
    events = [*_events(), (40, {"op": "teleport", "caller": "a"})]
    out = tl.build_timeline(defn, bindings, events, start_ms=0, caller="j")
    assert out["error"] is not None
    assert len(out["timeline"]) == 3, "the rows that did replay must survive the failure"
    assert out["state"]["data"]["tally"] == 5
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_machine_timeline.py -q`
Expected: FAIL — no module named `bearpit.realmtools.timeline`.

- [ ] **Step 3: Write `src/bearpit/realmtools/timeline.py`**

```python
"""A read-only timeline over a machine realm's chronicle: what changed, through one caller's eyes.

Nothing here knows what any scenario is about. Every key, every grouping and every label comes
from the machine's own declaration, so a scenario this module has never seen renders correctly the
first time.

The load-bearing decision is that changes are computed by diffing the caller's VIEW, never the
raw state. Diffing the state and filtering afterwards leaks twice: a hidden key that moved still
shows as 'something changed', and the gap where a row would have been is itself information. A key
the caller cannot see never enters the comparison, so it can neither appear nor be inferred.
"""
from __future__ import annotations

from typing import Any

from bearpit.core.schema import MachineDef
from bearpit.realmtools import machine as eng


def diff_views(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """What moved between two `eng.view` results.

    Shape is driven by what the view holds: a list is a set, a dict is an owner map, anything else
    is a scalar. The declaration already decided which is which, so this needs no schema.

    `actor_since` is deliberately absent: it moves whenever `actor` does and adds nothing.
    """
    out: list[dict[str, Any]] = []
    if before.get("state") != after.get("state"):
        out.append({"kind": "state", "from": before.get("state"), "to": after.get("state")})
    if before.get("actor") != after.get("actor"):
        out.append({"kind": "actor", "from": before.get("actor"), "to": after.get("actor")})

    b_data: dict[str, Any] = before.get("data") or {}
    a_data: dict[str, Any] = after.get("data") or {}
    for key in sorted(set(b_data) | set(a_data)):
        bv, av = b_data.get(key), a_data.get(key)
        if bv == av:
            continue
        if isinstance(bv, list) or isinstance(av, list):
            bs, as_ = set(bv or []), set(av or [])
            out.append({"kind": "set", "key": key,
                        "added": sorted(as_ - bs), "removed": sorted(bs - as_)})
        elif isinstance(bv, dict) or isinstance(av, dict):
            bd: dict[str, Any] = bv or {}
            ad: dict[str, Any] = av or {}
            for owner in sorted(set(bd) | set(ad)):
                if bd.get(owner) != ad.get(owner):
                    out.append({"kind": "owner", "key": key, "owner": owner,
                                "from": bd.get(owner), "to": ad.get(owner)})
        else:
            out.append({"kind": "value", "key": key, "from": bv, "to": av})
    return out


def build_timeline(
    defn: MachineDef, bindings: Bindings,
    events: list[tuple[int, dict[str, Any]]], start_ms: int, caller: str,
) -> dict[str, Any]:
    """Replay the chronicle, emitting one row per event the caller is allowed to see.

    A replay failure is reported, not raised. This serves a console: showing the prefix that did
    replay plus the reason it stopped is strictly more useful than a 500, and a realm whose
    chronicle has been damaged is exactly when someone needs to look at it.
    """
    rows: list[dict[str, Any]] = []
    state = eng.initial_state(defn, bindings, start_ms)
    seen = eng.view(defn, bindings, state, caller)
    error: str | None = None
    try:
        for ts, payload, after in eng.replay_steps(defn, bindings, events, start_ms):
            nxt = eng.view(defn, bindings, after, caller)
            visible = eng.log_row(defn, bindings, payload, caller)
            if visible is not None:
                rows.append({"ts": ts, "payload": visible, "changes": diff_views(seen, nxt)})
            seen, state = nxt, after
    except (eng.ReplayError, KeyError, ValueError) as exc:
        # `replay_steps` raises a bare KeyError on a row missing caller/key/transition.
        error = f"the chronicle stops replaying here: {exc}"
    return {"state": seen, "timeline": rows, "error": error}
```

Note the `Bindings` reference in the signature: import it as `from bearpit.realmtools.machine import Bindings` or annotate as `eng.Bindings`. Pick one and be consistent; mypy must pass either way.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_machine_timeline.py -q`
Expected: PASS (10 tests).

If `test_a_participant_lens_sees_strictly_less` fails, the bug is real and is the whole point of the module — do not weaken the test. Check that `eng.log_row` is being consulted and that the diff runs on views rather than states.

Run: `uv run pytest -q && uv run ruff check . && uv run mypy`
Expected: all clean.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/realmtools/timeline.py tests/test_machine_timeline.py
git commit -m "feat(machine): a timeline read-model that diffs the lens, not the state"
```

---

### Task 3: the endpoint

**Files:**
- Modify: `src/bearpit/gatekeeper/api.py` (add a route beside `/api/realms/{realm_id}/transcript`, ~line 1295)
- Test: `tests/test_api_machine_timeline.py` (create)

**Interfaces:**
- Consumes: `bearpit.realmtools.timeline.build_timeline` (Task 2); the existing `get_chron()`, `EventKind`, and the MACHINE-record shape.
- Produces: `GET /api/realms/{realm_id}/machine?as=<agent_id>` returning
  ```json
  {"machine": null}
  ```
  for a realm with no machine, or
  ```json
  {"machine": {"declaration": {...}, "as": "a", "seats": ["a","b","j"],
               "state": {...}, "timeline": [...], "error": null}}
  ```
  Task 4 consumes this.

**How to reconstruct the machine** — mirror `MachineService._load` in `src/bearpit/realmtools/machine_service.py`, which is the existing authority on this. In particular: take the **last** MACHINE event (a realm id can be reused), check `version == MACHINE_VERSION`, and filter GAME events to `e.id > head.id`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_api_machine_timeline.py`. Follow the existing API-test conventions in `tests/` — find a test that builds a TestClient with a fake chronicle and copy its setup exactly rather than inventing one. Cover:

1. a realm with no MACHINE event returns `{"machine": null}` and HTTP 200 (not 404 — a realm simply may not have a machine);
2. a realm with a machine returns a declaration, a state, and one row per visible event;
3. `?as=` a participant returns strictly fewer rows than `?as=` the referee, and no referee-only key appears anywhere in the participant payload;
4. `?as=` an id that is in no role returns HTTP 400 — never a silent fall-through to the participant view;
5. omitting `?as=` defaults to the referee;
6. a MACHINE record whose `version` does not match `MACHINE_VERSION` returns `{"machine": null}` rather than raising;
7. a realm whose GAME events no longer replay returns HTTP 200 with a non-null `error` and the rows that did replay.

Write these as real assertions against real response bodies. A test that only asserts `response.status_code == 200` does not count as covering its case.

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_api_machine_timeline.py -q`
Expected: FAIL with 404s — the route does not exist.

- [ ] **Step 3: Add the route**

```python
    @app.get("/api/realms/{realm_id}/machine")
    async def realm_machine(realm_id: str, as_: str | None = Query(None, alias="as")) -> dict:
        """The realm's game state and how it got there, through one seat's eyes.

        Read-only, and rebuilt from the chronicle on every call rather than read from the live
        runner: that way it works identically on a finished realm, and a live realm's growing
        timeline can never be served from a stale cache.
        """
        machines = await get_chron().events(realm_id, kind=EventKind.MACHINE)
        if not machines:
            return {"machine": None}
        head = machines[-1]  # LAST wins: a realm id can be reused.
        if head.payload.get("version") != MACHINE_VERSION:
            return {"machine": None}
        try:
            defn = MachineDef.model_validate(head.payload["declaration"])
        except ValidationError:
            # Launch validation grows stricter, so a declaration legal when chronicled can be
            # refused by the schema that reloads it. Never let pydantic's text, which names
            # transitions and keys, reach a caller.
            return {"machine": None}
        members: dict[str, list[str]] = head.payload.get("members") or {}
        bindings = Bindings(
            members={r: tuple(ids) for r, ids in members.items()},
            roster=tuple(head.payload.get("roster") or ()),
            referee=head.payload.get("referee"),
        )
        seats = sorted({a for ids in members.values() for a in ids})
        caller = as_ or head.payload.get("referee") or (seats[0] if seats else "")
        if caller not in seats:
            # Fail closed and loudly. Falling through would silently serve the participant view
            # for a typo'd id, which reads as "this seat saw nothing".
            raise HTTPException(400, "unknown seat")
        raw = await get_chron().events(realm_id, kind=EventKind.GAME)
        events = [(e.ts_ms, e.payload) for e in raw if e.id > head.id]
        built = build_timeline(defn, bindings, events, start_ms=head.ts_ms, caller=caller)
        return {"machine": {
            "declaration": declaration_view(defn, bindings, caller),
            "as": caller, "seats": seats, **built,
        }}
```

Add the imports this needs at the top of `api.py`, following the file's existing import style:
`MachineDef` from `bearpit.core.schema`; `Bindings` and `declaration_view` from `bearpit.realmtools.machine`; `MACHINE_VERSION` from `bearpit.realmtools.machine_service`; `build_timeline` from `bearpit.realmtools.timeline`; `ValidationError` from `pydantic`; and `Query`/`HTTPException` from `fastapi` if not already imported.

Give the route a precise return annotation matching the file's convention (`dict[str, Any]`), since mypy runs strict.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_api_machine_timeline.py -q` — expected PASS.
Run: `uv run pytest -q && uv run ruff check . && uv run mypy` — expected all clean.

- [ ] **Step 5: Commit**

```bash
git add src/bearpit/gatekeeper/api.py tests/test_api_machine_timeline.py
git commit -m "feat(api): serve a machine realm's state and timeline per seat"
```

---

### Task 4: the console

**Files:**
- Modify: `src/bearpit/gatekeeper/static/app.js` (the realm route, `route(/^\/realm\/(.+)$/, ...)` at ~line 450, and the transcript rendering at ~line 513 and ~line 893)
- Modify: `tests/ui_smoke.mjs` (add checks)

**Interfaces:**
- Consumes: `GET /api/realms/{id}/machine?as=` (Task 3).

- [ ] **Step 1: Read the existing realm view first**

Read `src/bearpit/gatekeeper/static/app.js` around lines 440–560 and 880–930 before writing anything. There is no `realmPage` function — the realm view is an anonymous route handler. Match the surrounding style exactly: this file is dependency-free vanilla JS and the realm CSP blocks external hosts, so no libraries, no frameworks, no template engines.

- [ ] **Step 2: Fetch the machine alongside the transcript**

The realm route already does `Promise.all` over status and transcript. Add the machine fetch to it, tolerating failure the same way the existing `outputs` fetch does (`try/catch`, fall back to nothing) so a realm without a machine — or an API error — renders exactly as it does today.

- [ ] **Step 3: Merge rows into the message feed**

Build one array of `{ts, kind: "msg"|"game", ...}`, sort by `ts`, and render. A game row renders compactly and distinctly from a message. Render, generically:
- `payload.op === "act"` → the caller, the transition name, and its args
- `payload.op === "set"` → the key (and owner when present)
- `payload.op === "reject"` → the caller, what was refused, and `check`/`detail`, styled as a refusal — these are often the most informative rows on the page
- then the row's `changes`, each formatted by its `kind` alone:
  - `state` → `state <from> → <to>`
  - `actor` → `actor → <to>`
  - `value` → `<key> <from> → <to>`
  - `set` → `<key> +<added>` / `<key> −<removed>` / `<key> reset` when it emptied
  - `owner` → `<key> {<owner>: <to>}`

A `kind` the console does not recognise must render as nothing rather than `undefined`.

- [ ] **Step 4: Add the seat lens**

A `<select>` listing `seats` plus the current `as`, which on change re-fetches `/api/realms/{id}/machine?as=<seat>` and re-renders only the merged feed. Label it "Viewing as". Do not re-fetch the transcript — the messages do not change with the lens.

- [ ] **Step 5: Show current state**

Above the feed, render the `state` object: the FSM state, the actor, and each data key. Owner maps render per owner. Keep it to a compact single block, not a panel.

- [ ] **Step 6: Add smoke checks to `tests/ui_smoke.mjs`**

Follow the harness's existing structure exactly (read its header first). Add at least three checks:
1. a realm whose `/machine` returns `{"machine": null}` renders the realm view unchanged — no empty block, no error;
2. game rows and message rows appear interleaved in timestamp order, not grouped;
3. a `changes` entry with an unrecognised `kind` renders nothing rather than the string `undefined`.

**Prove each check fails when its behaviour is broken.** Temporarily break the behaviour, run the harness, confirm the check reports FAIL, then restore. Quote the failure lines in your report — a check not proven to fail is not a check.

- [ ] **Step 7: Verify**

Run: `uv run pytest -q && uv run ruff check . && uv run mypy` — expected all clean.
Run: `node tests/ui_smoke.mjs` — expected every check PASS and a SUMMARY line.

- [ ] **Step 8: Commit**

```bash
git add src/bearpit/gatekeeper/static/app.js tests/ui_smoke.mjs
git commit -m "feat(ui): game state reads alongside the conversation"
```
