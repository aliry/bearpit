"""The dealer's enforcement path, driven against the REAL poker declaration (#108).

`reopen` has never fired in 54 chronicled hands; `all_in` once; `fold_for` three times. Declared
Physics that has never executed is untested Physics, and these three are the ones a dealer reaches
for in exactly the situations nobody plans — a seat shoving, a seat stalling, action re-opening
behind a raise.

The pairing that matters is `all_in` + `reopen`. A `raise` resets `acted` wholesale, so everyone
must act again and no dealer intervention is needed. `all_in` deliberately does NOT: it only marks
the shover. So when an all-in raises the price, the seats who already called are still in `acted`
and the machine will happily close the street on them — `reopen` is the only way action gets back
to them, and it is the dealer's job to notice. These tests pin what the machine does so the
scenario's rubric can be held to it.
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
    assert defn is not None
    bindings = eng.Bindings(
        members={"dealer": ("pitboss",), "player": SEATS}, roster=SEATS, referee="pitboss")
    return defn, bindings


def _street(defn, bindings, *, acted=(), out=(), all_in=(), actor="vega", bet="20"):
    state = eng.initial_state(defn, bindings, 0)
    state.state = "preflop"
    state.actor = actor
    state.data["bet_level"] = bet
    state.sets["acted"] = set(acted)
    state.sets["out"] = set(out)
    state.sets["all_in"] = set(all_in)
    return state


def _act(defn, bindings, state, caller, transition, args=None):
    return eng.act(defn, bindings, state, caller, transition, args or {}, {}, 0)


def test_all_in_marks_the_shover_without_reopening_the_street(table):
    """The load-bearing asymmetry: unlike `raise`, an all-in leaves everyone else's `acted` alone.
    If this ever changes, `reopen` becomes dead and the rubric that calls for it becomes wrong."""
    defn, bindings = table
    state = _street(defn, bindings, acted=("orion", "lyra"), actor="vega")
    out = _act(defn, bindings, state, "vega", "all_in", {"to": "2000"})
    assert not isinstance(out, eng.Rejection), getattr(out, "detail", "")
    assert "vega" in out.state.sets["all_in"]
    assert out.state.sets["acted"] == {"orion", "lyra", "vega"}, "an all-in reset the street"


def test_a_raise_does_reopen_the_street_for_everyone(table):
    """The contrast that makes the test above meaningful."""
    defn, bindings = table
    state = _street(defn, bindings, acted=("orion", "lyra"), actor="vega")
    out = _act(defn, bindings, state, "vega", "raise", {"to": "120"})
    assert not isinstance(out, eng.Rejection), getattr(out, "detail", "")
    assert out.state.sets["acted"] == {"vega"}, "a raise failed to reset the street"
    assert out.state.data["bet_level"] == "120"


def test_reopen_returns_one_seat_to_the_action_and_gives_it_the_floor(table):
    """The path that has never run live. After an all-in raises the price, this is how a seat that
    already called gets to answer it."""
    defn, bindings = table
    state = _street(defn, bindings, acted=("orion", "lyra", "vega"), all_in=("vega",),
                    actor="rigel")
    out = _act(defn, bindings, state, "pitboss", "reopen", {"player": "orion"})
    assert not isinstance(out, eng.Rejection), getattr(out, "detail", "")
    assert "orion" not in out.state.sets["acted"], "the seat is still marked as having acted"
    assert out.state.actor == "orion", "reopen did not hand the seat the floor"
    assert out.state.sets["acted"] == {"lyra", "vega"}, "reopen disturbed the other seats"


def test_reopen_is_the_dealers_alone(table):
    """A seat that could reopen its own action could act twice on one street."""
    defn, bindings = table
    state = _street(defn, bindings, acted=("orion", "vega"), actor="rigel")
    out = _act(defn, bindings, state, "orion", "reopen", {"player": "orion"})
    assert isinstance(out, eng.Rejection) and out.check == "by"


def test_a_reopened_seat_can_then_act_again(table):
    """End to end: the point of reopening is that the seat's next act is legal, and `caller_not_in
    acted` was the guard that would have refused it a moment earlier."""
    defn, bindings = table
    state = _street(defn, bindings, acted=("orion", "lyra", "vega"), all_in=("vega",),
                    actor="rigel", bet="2000")
    refused = _act(defn, bindings, state, "orion", "call", {"to": "2000"})
    assert isinstance(refused, eng.Rejection), "a seat acted twice on one street"

    reopened = _act(defn, bindings, state, "pitboss", "reopen", {"player": "orion"})
    assert not isinstance(reopened, eng.Rejection)
    now = _act(defn, bindings, reopened.state, "orion", "call", {"to": "2000"})
    assert not isinstance(now, eng.Rejection), getattr(now, "detail", "")
    assert "orion" in now.state.sets["acted"]


def test_fold_for_takes_a_stalled_seat_out_and_moves_the_action_on(table):
    defn, bindings = table
    state = _street(defn, bindings, acted=("orion",), actor="vega")
    out = _act(defn, bindings, state, "pitboss", "fold_for", {"player": "vega"})
    assert not isinstance(out, eng.Rejection), getattr(out, "detail", "")
    assert "vega" in out.state.sets["out"]
    assert out.state.actor != "vega", "the folded seat still holds the floor"


def test_fold_for_only_folds_the_seat_that_actually_holds_the_action(table):
    """Its first guard. A dealer folding the wrong seat would eject a player who did nothing."""
    defn, bindings = table
    state = _street(defn, bindings, actor="vega")
    out = _act(defn, bindings, state, "pitboss", "fold_for", {"player": "rigel"})
    assert isinstance(out, eng.Rejection) and out.check == "guard"
