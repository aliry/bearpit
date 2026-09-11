"""The pure engine: (declaration, bindings, state, caller, transition, args) -> state | Rejection.

No IO, no clock — `now_ms` is a parameter. Every guard, effect and pointer rule from spec §2 is
pinned here, including the two the reviews found the hard way: the pointer must PARK when nobody
is eligible (else it lands on the raiser and falsely wakes it), and `set_actor` must refuse a
member of a skip set (else a referee typo makes a folded player the actor).
"""
from __future__ import annotations

from bearpit.core.machine import MachineDef
from bearpit.realmtools.machine import (
    Bindings,
    MachineState,
    advance_actor,
    eligible,
    initial_state,
    roles_of,
    set_actor,
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
    advance_actor(POKERISH, B, s, 6)
    assert s.actor == "d"
    advance_actor(POKERISH, B, s, 7)
    assert s.actor == "a"  # wrapped


def test_pointer_skips_every_listed_set():
    s = _s()
    s.sets["out"].add("b")
    s.sets["all_in"].add("c")
    set_actor(POKERISH, B, s, "a", 1)
    advance_actor(POKERISH, B, s, 2)
    assert s.actor == "d"  # b folded, c all-in


def test_pointer_parks_when_nobody_is_eligible():
    """Reviewed twice: after the last player matches a raise, the pointer used to land on the
    raiser (in `acted`) and wake it for a move it cannot make."""
    s = _s()
    s.sets["out"].add("a")
    s.sets["acted"] |= {"b", "c", "d"}
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
