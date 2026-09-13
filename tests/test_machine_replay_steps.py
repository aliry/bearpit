"""`replay_steps` must walk the same path `replay` walks, one event at a time."""
from __future__ import annotations

from typing import Any

import pytest

from bearpit.core.machine import MachineDef
from bearpit.realmtools import machine as eng

DECL: dict[str, Any] = {
    "states": ["open", "closed"],
    "initial": "open",
    "terminal": ["closed"],
    "roles": {"players": {"members": ["a", "b"]}, "judge": {"members": "referee"}},
    "data": {
        "tally": {"visibility": "public", "type": "value"},
        "picked": {"visibility": "public", "type": "set"},
        "secret": {"visibility": "owner", "type": "value"},
    },
    "transitions": {
        "bump": {"from": ["open"], "to": "same", "by": "players", "effects": []},
        "close": {"from": ["open"], "to": "closed", "by": "judge", "effects": []},
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
