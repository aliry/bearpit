"""The dealer may not fold the last live seat — against the REAL poker declaration.

This is not a hypothetical. `fold_for` has fired twice in the whole chronicle and one of those
folded the seat that was about to win the hand (`poker-demo`), leaving zero live players. The
pitboss persona warns against it in capitals; the machine allowed it anyway, which is the whole
distinction between law and physics.

The test drives `examples/poker-table`'s own machine so it pins the shipped scenario rather than a
copy of it that could drift.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bearpit.core import load_package
from bearpit.realmtools import machine as eng

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
SEATS = ("vega", "orion", "lyra", "rigel", "mira", "nova")


@pytest.fixture
def table():
    project = load_package(EXAMPLES / "poker-table")
    defn = project.spec.machine
    assert defn is not None, "poker-table no longer declares a machine"
    bindings = eng.Bindings(
        members={"dealer": ("pitboss",), "player": SEATS},
        roster=SEATS, referee="pitboss",
    )
    return defn, bindings


def _street(defn, bindings, *, out: tuple[str, ...], actor: str):
    """A state mid-hand with `out` already folded and `actor` to act."""
    state = eng.initial_state(defn, bindings, 0)
    state.state = "preflop"
    state.actor = actor
    state.sets["out"] = set(out)
    return state


def test_the_dealer_may_fold_a_seat_while_others_are_still_live(table):
    """The guard must not break the legitimate use: a stalled seat folded mid-hand."""
    defn, bindings = table
    state = _street(defn, bindings, out=("orion", "lyra"), actor="vega")
    out = eng.act(defn, bindings, state, "pitboss", "fold_for", {"player": "vega"}, {}, 0)
    assert not isinstance(out, eng.Rejection), getattr(out, "detail", "")
    assert "vega" in out.state.sets["out"]


def test_the_dealer_may_not_fold_the_last_live_seat(table):
    """poker-demo, replayed: five seats out, the dealer folds the sixth — the winner."""
    defn, bindings = table
    state = _street(defn, bindings,
                    out=("orion", "lyra", "rigel", "mira", "nova"), actor="vega")
    out = eng.act(defn, bindings, state, "pitboss", "fold_for", {"player": "vega"}, {}, 0)
    assert isinstance(out, eng.Rejection), "the last live seat was folded — the hand has no winner"
    assert out.check == "guard"


def test_a_seat_folding_itself_is_still_law_not_physics(table):
    """Deliberately NOT guarded. Across 243 chronicled folds the minimum live-before was 2 — a
    player has never folded as the last seat — so this stays a rule the referee may rule on
    rather than one the machine makes impossible. The assertion pins that choice: if someone adds
    the guard to `fold` too, they should have new evidence and should update this test."""
    defn, _ = table
    names = [g.model_dump(exclude_none=True)["name"] for g in defn.transitions["fold"].guard]
    assert "members_count" not in names
