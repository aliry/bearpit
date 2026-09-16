"""The timeline read-model: what changed, through one caller's eyes."""
from __future__ import annotations

from typing import Any

from bearpit.core.machine import MachineDef
from bearpit.realmtools import machine as eng
from bearpit.realmtools import timeline as tl

DECL: dict[str, Any] = {
    "states": ["open", "closed"],
    "initial": "open",
    "terminal": ["closed"],
    "roles": {"players": {"members": ["a", "b"]}, "judge": {"members": "referee"}},
    "data": {
        "tally": {"visibility": "public", "type": "value"},
        "picked": {"visibility": "public", "type": "set"},
        "secret": {"visibility": "owner", "type": "value"},
        "notes": {"visibility": "referee", "type": "value"},
        # A PUBLIC key that holds an object. rps-machine's `score` and poker's `stacks` are both
        # this shape, and every fixture here previously paired object values with owner
        # visibility — so the differ's wrong branch produced right-looking output (#123).
        "board": {"visibility": "public", "type": "value"},
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


def test_diff_reports_a_scalar_move() -> None:
    changes = tl.diff_views(
        _fixture()[0],
        {"state": "open", "actor": None, "data": {"tally": 30}},
        {"state": "open", "actor": None, "data": {"tally": 370}},
    )
    assert changes == [{"kind": "value", "key": "tally", "from": 30, "to": 370}]


def test_diff_reports_state_and_actor_by_kind_not_by_reserved_key() -> None:
    changes = tl.diff_views(
        _fixture()[0],
        {"state": "open", "actor": "a", "data": {}},
        {"state": "closed", "actor": "b", "data": {}},
    )
    assert {"kind": "state", "from": "open", "to": "closed"} in changes
    assert {"kind": "actor", "from": "a", "to": "b"} in changes
    assert all("key" not in c for c in changes)


def test_diff_reports_set_additions_and_a_reset() -> None:
    grow = tl.diff_views(_fixture()[0], {"data": {"picked": []}}, {"data": {"picked": ["a"]}})
    assert grow == [{"kind": "set", "key": "picked", "added": ["a"], "removed": []}]
    reset = tl.diff_views(_fixture()[0], {"data": {"picked": ["a", "b"]}}, {"data": {"picked": []}})
    assert reset == [{"kind": "set", "key": "picked", "added": [], "removed": ["a", "b"]}]


def test_diff_reports_owner_entries_per_owner() -> None:
    changes = tl.diff_views(
        _fixture()[0],
        {"data": {"secret": {}}}, {"data": {"secret": {"a": "z"}}},
    )
    assert changes == [
        {"kind": "owner", "key": "secret", "owner": "a", "from": None, "to": "z"}
    ]


def test_a_public_object_valued_key_is_not_reported_as_owner_data() -> None:
    """The kind comes from the DECLARATION, never from what the value happens to look like.

    `owner` means per-owner entries of an owner-visibility key. A public map's inner keys are not
    owners, and a consumer that trusts the field is misled. Found on a live rps-machine run, where
    a public `score` object rendered as `score{orin}` (#123)."""
    defn, _ = _fixture()
    changes = tl.diff_views(
        defn, {"data": {"board": {"orin": 0, "vela": 0}}},
        {"data": {"board": {"orin": 1, "vela": 0}}},
    )
    assert changes, "a changed public object reported nothing at all"
    assert not any(c["kind"] == "owner" for c in changes), (
        f"a public object-valued key was reported as owner data: {changes}"
    )
    assert not any("owner" in c for c in changes), (
        f"a change record carries a fabricated 'owner' field: {changes}"
    )


def test_a_public_object_still_reports_per_entry_deltas() -> None:
    """Correctness must not cost readability: `stacks vega 2000 -> 1940` is the useful row."""
    defn, _ = _fixture()
    changes = tl.diff_views(
        defn, {"data": {"board": {"orin": 0, "vela": 0}}},
        {"data": {"board": {"orin": 1, "vela": 0}}},
    )
    assert changes == [
        {"kind": "map", "key": "board", "entry": "orin", "from": 0, "to": 1}
    ], changes


def test_an_owner_key_is_still_owner_even_when_it_looks_like_anything_else() -> None:
    defn, _ = _fixture()
    changes = tl.diff_views(
        defn, {"data": {"secret": {}}}, {"data": {"secret": {"a": "z"}}},
    )
    assert [c["kind"] for c in changes] == ["owner"], changes


def test_a_declared_set_is_a_set_even_when_empty_on_both_sides_of_a_rename() -> None:
    """Dispatch is by declaration, so a set never degrades into a value or a map."""
    defn, _ = _fixture()
    changes = tl.diff_views(
        defn, {"data": {"picked": []}}, {"data": {"picked": ["a"]}},
    )
    assert [c["kind"] for c in changes] == ["set"], changes


def test_actor_since_is_not_a_change() -> None:
    changes = tl.diff_views(
        _fixture()[0],
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
