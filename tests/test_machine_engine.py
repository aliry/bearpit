"""The pure engine: (declaration, bindings, state, caller, transition, args) -> state | Rejection.

No IO, no clock — `now_ms` is a parameter. Every guard, effect and pointer rule from spec §2 is
pinned here, including the two the reviews found the hard way: the pointer must PARK when nobody
is eligible (else it lands on the raiser and falsely wakes it), and `set_actor` must refuse a
member of a skip set (else a referee typo makes a folded player the actor).
"""
from __future__ import annotations

from bearpit.core.machine import Guard, MachineDef
from bearpit.realmtools.machine import (
    Bindings,
    Ctx,
    MachineState,
    advance_actor,
    check_guard,
    eligible,
    initial_state,
    resolve,
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


def _g(name, arg=None):
    return Guard.model_validate({name: arg} if arg is not None else name)


def _ctx(caller="a", args=None, escrow=None):
    return Ctx(caller=caller, args=args or {}, escrow=escrow or {})


def test_resolve_caller_args_data_and_literals():
    s = _s()
    s.data["pot"] = 40
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
    s = _s()
    s.sets["acted"].add("a")
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
    s.sets["out"].add("a")
    s.sets["all_in"].add("b")
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
    s = _s()
    s.sets["all_in"] |= {"a", "b", "c", "d"}
    full = _g("data_set_full", {"key": "acted", "over": "player", "minus": ["out", "all_in"]})
    assert check_guard(full, POKERISH, B, s, _ctx())


def test_escrow_complete_uses_the_machines_live_set_not_the_escrows_roster():
    """Review M5: the escrow's own roster only grows, so an eliminated agent that never seals
    deadlocked every round after the first ejection."""
    s = _s()
    s.sets["out"].add("d")
    g = _g("escrow_complete", {"round": "$data.hand", "over": "player", "minus": ["out"]})
    s.data["hand"] = "H1"
    assert not check_guard(g, POKERISH, B, s, _ctx(escrow={"H1": {"a", "b"}}))
    assert check_guard(g, POKERISH, B, s, _ctx(escrow={"H1": {"a", "b", "c"}}))  # d excused
