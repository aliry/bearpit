"""The declaration, driven through the pure engine. A live hand takes twenty minutes to reach
the line where a raise reopens a street; here it takes two seconds."""
from __future__ import annotations

import pytest

from bearpit.core import load_package
from bearpit.realmtools import machine as eng

SEATS = ["vega", "orion", "lyra", "rigel", "mira", "nova"]


@pytest.fixture(scope="module")
def defn():
    project = load_package("examples/poker-table")
    assert project.spec.machine is not None
    return project.spec.machine


@pytest.fixture
def bindings():
    return eng.Bindings(members={"dealer": ("pitboss",), "player": tuple(SEATS)},
                        roster=tuple(SEATS) + ("pitboss",), referee="pitboss")


def _act(defn, bindings, state, caller, transition, **args):
    out = eng.act(defn, bindings, state, caller, transition, args, {}, 0)
    assert isinstance(out, eng.Outcome), f"{transition} refused: {out}"
    return out.state


def _open_hand(defn, bindings):
    state = eng.initial_state(defn, bindings, 0)
    state = eng.set_value(defn, bindings, state, "pitboss", "seed_commit", "c0ffee", None,
                          {}, 0).state
    return _act(defn, bindings, state, "pitboss", "deal", first="lyra", bet_level="20")


def test_the_deck_must_be_committed_before_it_is_dealt(defn, bindings):
    """`deal` is guarded on seed_commit so the dealer cannot see the deck and then choose it."""
    state = eng.initial_state(defn, bindings, 0)
    out = eng.act(defn, bindings, state, "pitboss", "deal", {"first": "lyra"}, {}, 0)
    assert isinstance(out, eng.Rejection) and out.check == "guard"


def _preflop_to_close(defn, bindings):
    """Preflop: lyra calls, rigel raises, two fold, vega calls, orion folds, lyra calls again.
    Shared by the two tests below — a test that returns a value trips pytest's return-not-none
    warning, so the sequence lives here."""
    s = _open_hand(defn, bindings)
    s = _act(defn, bindings, s, "lyra", "call", to="20")
    s = _act(defn, bindings, s, "rigel", "raise", to="60")
    s = _act(defn, bindings, s, "mira", "fold")
    s = _act(defn, bindings, s, "nova", "fold")
    s = _act(defn, bindings, s, "vega", "call", to="60")
    s = _act(defn, bindings, s, "orion", "fold")
    s = _act(defn, bindings, s, "lyra", "call", to="60")
    return s


def test_a_raise_reopens_the_street_for_everyone_who_had_already_acted(defn, bindings):
    """The whole street-closing mechanism: acted collects actions, a raise resets it, and the
    pointer can therefore come back to a seat that has already called. Get this wrong and the
    street closes at the old price with players still owing chips."""
    s = _open_hand(defn, bindings)
    assert s.state == "preflop" and s.actor == "lyra"

    s = _act(defn, bindings, s, "lyra", "call", to="20")
    assert s.sets["acted"] == {"lyra"} and s.actor == "rigel"

    s = _act(defn, bindings, s, "rigel", "raise", to="60")
    assert s.sets["acted"] == {"rigel"}, "a raise must clear everyone else's acceptance"
    assert s.data["bet_level"] == "60"
    assert s.actor == "mira"

    s = _act(defn, bindings, s, "mira", "fold")
    s = _act(defn, bindings, s, "nova", "fold")
    s = _act(defn, bindings, s, "vega", "call", to="60")
    s = _act(defn, bindings, s, "orion", "fold")
    assert s.actor == "lyra", "the pointer must return to the seat the raise reopened"

    s = _act(defn, bindings, s, "lyra", "call", to="60")
    assert s.actor is None, "with the street closed the pointer parks; it never lands on a raiser"


def test_a_seat_cannot_act_twice_at_the_same_price(defn, bindings):
    s = _open_hand(defn, bindings)
    s = _act(defn, bindings, s, "lyra", "call", to="20")
    out = eng.act(defn, bindings, s, "lyra", "call", {"to": "20"}, {}, 0)
    assert isinstance(out, eng.Rejection) and out.check == "guard"


def test_the_dealer_cannot_advance_a_street_that_is_still_open(defn, bindings):
    s = _open_hand(defn, bindings)
    s = _act(defn, bindings, s, "lyra", "call", to="20")
    out = eng.act(defn, bindings, s, "pitboss", "advance", {"first": "lyra"}, {}, 0)
    assert isinstance(out, eng.Rejection) and out.check == "guard"


def test_a_whole_hand_runs_from_the_deal_to_a_settled_pot(defn, bindings):
    s = _preflop_to_close(defn, bindings)
    for transition, to in (("advance", "flop"), ("advance2", "turn_st"), ("advance3", "river")):
        s = _act(defn, bindings, s, "pitboss", transition, first="lyra")
        assert s.state == to and s.actor == "lyra" and s.sets["acted"] == set()
        for seat in ("lyra", "rigel", "vega"):
            s = _act(defn, bindings, s, seat, "check")
        assert s.actor is None

    s = _act(defn, bindings, s, "pitboss", "to_showdown")
    assert s.state == "showdown"
    assert s.revealed["hole"] == {"lyra", "rigel", "vega"}, "mucked hands stay face down"

    s = _act(defn, bindings, s, "pitboss", "settle")
    s = _act(defn, bindings, s, "pitboss", "next_hand")
    assert s.state == "waiting" and "seed_commit" not in s.data

    s = eng.set_value(defn, bindings, s, "pitboss", "seed_commit", "d00d", None, {}, 0).state
    s = _act(defn, bindings, s, "pitboss", "deal", first="rigel", bet_level="20")
    s = _act(defn, bindings, s, "rigel", "fold")
    s = _act(defn, bindings, s, "mira", "fold")
    s = _act(defn, bindings, s, "nova", "fold")
    s = _act(defn, bindings, s, "vega", "fold")
    s = _act(defn, bindings, s, "orion", "fold")
    s = _act(defn, bindings, s, "pitboss", "award")
    assert s.state == "settled", "five folds leave one seat and the pot is awarded uncontested"

    s = _act(defn, bindings, s, "pitboss", "finish")
    assert s.state == "done" and s.state in defn.terminal


def test_a_folded_seat_never_sees_another_seats_cards(defn, bindings):
    """Hole cards are owner-visibility: each seat sees its own entry and the dealer sees all of
    them. At the showdown the live hands turn face up for everyone — and a folded hand does not,
    because a seat that paid to fold does not owe the table a look at what it folded."""
    s = _open_hand(defn, bindings)
    s = eng.set_value(defn, bindings, s, "pitboss", "hole", "Ah Kh", "vega", {}, 0).state
    s = eng.set_value(defn, bindings, s, "pitboss", "hole", "2c 7d", "mira", {}, 0).state

    assert eng.view(defn, bindings, s, "mira")["data"]["hole"] == {"mira": "2c 7d"}
    assert eng.view(defn, bindings, s, "vega")["data"]["hole"] == {"vega": "Ah Kh"}
    assert eng.view(defn, bindings, s, "pitboss")["data"]["hole"] == {
        "mira": "2c 7d", "vega": "Ah Kh"}

    s = _act(defn, bindings, s, "lyra", "call", to="20")
    s = _act(defn, bindings, s, "rigel", "call", to="20")
    s = _act(defn, bindings, s, "mira", "fold")
    s = _act(defn, bindings, s, "pitboss", "to_showdown")

    shown = eng.view(defn, bindings, s, "lyra")["data"]["hole"]
    assert shown.get("vega") == "Ah Kh", "a live hand is turned face up for the whole table"
    assert "mira" not in shown, "a mucked hand stays the folder's own business"


def test_a_seat_that_folded_before_a_raise_does_not_hold_the_street_open(defn, bindings):
    """The `minus: [out, all_in]` on every street-closing guard is load-bearing, and nothing else
    in this file exercises it: a fold normally lands the folder back in `acted` via its own
    effect, so the guard would close anyway. But a raise RESETS `acted`, and a seat that folded
    before that raise can never re-enter it. Without the `minus` clause the expected set would
    keep naming that seat for ever and the street could never close — a permanent deadlock, in a
    live hand, with no error to read."""
    s = _open_hand(defn, bindings)
    s = _act(defn, bindings, s, "lyra", "fold")
    assert s.sets["out"] == {"lyra"} and "lyra" in s.sets["acted"]

    s = _act(defn, bindings, s, "rigel", "raise", to="60")
    assert s.sets["acted"] == {"rigel"}, "the raise cleared the fold out of `acted`"
    assert s.sets["out"] == {"lyra"}, "...but not out of `out` — the fold still stands"

    for seat in ("mira", "nova", "vega", "orion"):
        s = _act(defn, bindings, s, seat, "call", to="60")
    assert s.actor is None, "every seat that can act has acted, so the pointer parks"

    # the guard the dealer is woken by, evaluated exactly as the declaration writes it
    s = _act(defn, bindings, s, "pitboss", "advance", first="rigel")
    assert s.state == "flop", "the street closed with a pre-raise folder still in `out`"


def test_every_seat_fired_transition_carries_both_of_its_guards(defn, bindings):
    """`caller_is_actor` and `caller_not_in: acted` are defence in depth for each other — today
    the pointer's `skip` set means an acted seat can never be the actor, so a behavioural test
    cannot tell the two apart and dropping either guard would leave the suite green. Pin them
    structurally instead: a seat may act only when the pointer is on it AND it has not already
    acted at this price."""
    seat_fired = [n for n, t in defn.transitions.items() if t.by == "player"]
    assert sorted(seat_fired) == ["all_in", "call", "check", "fold", "raise"]
    for name in seat_fired:
        guards = {g.name for g in defn.transitions[name].guard}
        assert guards == {"caller_is_actor", "caller_not_in"}, f"{name} lost a guard"
