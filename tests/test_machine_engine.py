"""The pure engine: (declaration, bindings, state, caller, transition, args) -> state | Rejection.

No IO, no clock — `now_ms` is a parameter. Every guard, effect and pointer rule from spec §2 is
pinned here, including the two the reviews found the hard way: the pointer must PARK when nobody
is eligible (else it lands on the raiser and falsely wakes it), and `set_actor` must refuse a
member of a skip set (else a referee typo makes a folded player the actor).
"""
from __future__ import annotations

import pytest as _pytest

from bearpit.core.machine import Effect, Guard, MachineDef
from bearpit.realmtools.machine import (
    Bindings,
    Ctx,
    MachineState,
    Outcome,
    Rejection,
    ReplayError,
    act,
    advance_actor,
    apply_effect,
    check_guard,
    compute_wakes,
    declaration_view,
    eligible,
    initial_state,
    log_row,
    reject_payload,
    replay,
    resolve,
    roles_of,
    set_actor,
    set_value,
    view,
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
    assert not check_guard(_g("data_present", "actor"), POKERISH, B, s, _ctx())  # parked
    s.data["pot"] = 0
    assert check_guard(_g("data_present", "pot"), POKERISH, B, s, _ctx())
    assert check_guard(_g("data_equals", {"key": "pot", "value": 0}), POKERISH, B, s, _ctx())
    # values resolve: a guard may compare a key to an arg
    set_actor(POKERISH, B, s, "c", 1)
    assert check_guard(_g("data_present", "actor"), POKERISH, B, s, _ctx())  # has an actor now
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


def _e(name, arg=None):
    return Effect.model_validate({name: arg} if arg is not None else name)


def test_add_to_remove_from_reset_and_unset():
    s = _s()
    c = _ctx("a", {"victim": "b"})
    add_caller = _e("add_to", {"key": "out", "value": "$caller"})
    assert apply_effect(add_caller, POKERISH, B, s, c, 1) is None
    add_victim = _e("add_to", {"key": "out", "value": "$args.victim"})
    assert apply_effect(add_victim, POKERISH, B, s, c, 1) is None
    assert s.sets["out"] == {"a", "b"}
    remove_a = _e("remove_from", {"key": "out", "value": "a"})
    assert apply_effect(remove_a, POKERISH, B, s, c, 1) is None
    assert s.sets["out"] == {"b"}
    assert apply_effect(_e("reset", "out"), POKERISH, B, s, c, 1) is None
    assert s.sets["out"] == set()
    s.data["pot"] = 5
    assert apply_effect(_e("unset", "pot"), POKERISH, B, s, c, 1) is None
    assert "pot" not in s.data


def test_args_used_as_members_are_validated_against_the_role():
    """Junk in `$args` would silently break `skip` and `data_set_full` (review m12)."""
    s = _s()
    c = _ctx("dealer", {"victim": "zed"})
    add_victim = _e("add_to", {"key": "out", "value": "$args.victim"})
    assert apply_effect(add_victim, POKERISH, B, s, c, 1) == "'zed' is not a member of 'player'"
    assert s.sets["out"] == set()


def test_set_writes_a_resolved_value_and_set_actor_checks_eligibility():
    s = _s()
    c = _ctx("dealer", {"first": "b", "n": 3})
    set_pot = _e("set", {"key": "pot", "value": "$args.n"})
    assert apply_effect(set_pot, POKERISH, B, s, c, 1) is None
    assert s.data["pot"] == 3
    set_first = _e("set_actor", "$args.first")
    assert apply_effect(set_first, POKERISH, B, s, c, 2) is None
    assert s.actor == "b"
    s.sets["out"].add("b")
    assert apply_effect(set_first, POKERISH, B, s, c, 3) == "'b' is in skip set 'out'"
    assert apply_effect(_e("set_actor", None), POKERISH, B, s, c, 4) is None
    assert s.actor is None


def test_set_actor_fails_closed_on_a_malformed_arg():
    """`set_actor` takes a VALUE — an agent id, a `$`-reference, or an explicit null. A dict
    arrives whenever a declaration reaches for a shape that does not exist (`{set_actor: {who:
    …}}`), and it resolves to no agent id at all. It must be refused exactly like a stranger's
    id: the pointer is the floor, and a floor handed to nobody stalls the realm silently."""
    s = _s()
    c = _ctx("dealer", {"first": "a"})
    for bad in ({"who": 1}, {"who": None}, {}):
        r = apply_effect(_e("set_actor", bad), POKERISH, B, s, c, 1)
        assert r is not None and "is not a member of 'player'" in r, bad
        assert s.actor is None  # and the pointer never moved
    # ...while an explicit null still parks it, which is a different thing entirely
    assert set_actor(POKERISH, B, s, "a", 1) is None and s.actor == "a"
    assert apply_effect(_e("set_actor", None), POKERISH, B, s, c, 2) is None and s.actor is None


def test_reveal_selectors():
    s = _s()
    s.owner_data["hole"] = {"a": "AhKh", "b": "2c7d", "c": "QsQd"}
    s.sets["out"].add("b")
    reveal_caller = _e("reveal", {"key": "hole", "owners": "$caller"})
    assert apply_effect(reveal_caller, POKERISH, B, s, _ctx("a"), 1) is None
    assert s.revealed["hole"] == {"a"}
    sel = {"key": "hole", "owners": {"over": "player", "minus": ["out"]}}
    assert apply_effect(_e("reveal", sel), POKERISH, B, s, _ctx("dealer"), 1) is None
    assert s.revealed["hole"] == {"a", "c", "d"}  # b folded: mucked, stays hidden


def test_a_new_owner_value_is_private_again_even_after_the_old_one_was_revealed():
    """A reveal discloses one VALUE, not a standing right to read the key. `revealed` is never
    cleared by anything in the vocabulary — `reset` takes a set key, `unset` takes a public one —
    so without this, a game with repeated rounds leaks every later secret to everyone who saw the
    first showdown. Found by the poker table: after hand one's showdown, hand two's hole cards
    were public to the whole table from the moment they were dealt."""
    s = _s()
    s.owner_data["hole"] = {"a": "AhKh", "b": "2c7d"}
    assert apply_effect(_e("reveal", {"key": "hole", "owners": "$caller"}),
                        POKERISH, B, s, _ctx("a"), 1) is None
    assert s.revealed["hole"] == {"a"}

    out = set_value(POKERISH, B, s, "dealer", "hole", "QsQd", "a", {}, 2)
    assert isinstance(out, Outcome), out
    assert out.state.revealed["hole"] == set(), "a fresh secret is a secret again"
    assert view(POKERISH, B, out.state, "b")["data"].get("hole", {}) == {"b": "2c7d"}, \
        "an opponent sees only its own hand once the revealed one has been replaced"
    # and the seat itself still sees what it holds
    assert view(POKERISH, B, out.state, "a")["data"]["hole"] == {"a": "QsQd"}
    # the write does not disturb anyone else's disclosure
    assert s.revealed["hole"] == {"a"}, "the original state is untouched by the copy"


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
    r = act(POKERISH, B, s, "dealer", "deal", {"first": "zed"}, {}, 1)
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
    # b is woken by the `actor` rule, so it is stamped as an ACTOR wake — the host collapses
    # those to the latest event's actor, and must not do that to a role wake (review I1).
    assert p["actor"] == "b" and p["log"] == "public"
    assert p["wake"] == [] and p["wake_actor"] == ["b"]


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
    assert out.payload["wake_actor"] == []                         # the pointer parked: no actor
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
    out = set_value(POKERISH, B, s, "dealer", "pot", 105, None, {}, 2)
    assert isinstance(out, Outcome) and out.payload["wake"] == []
    assert out.payload["wake_actor"] == []
    out = set_value(POKERISH, B, out.state, "dealer", "pot", 106, None, {}, 3)
    assert out.payload["wake"] == [] and out.payload["wake_actor"] == []


def test_no_actor_wake_on_a_parked_pointer_and_targets_are_deduped():
    s = _s()
    old = s.copy()
    new = s.copy()
    new.actor = None
    assert compute_wakes(POKERISH, B, old, new, {}) == ([], [])
    new.actor = "c"
    assert compute_wakes(POKERISH, B, old, new, {}) == ([], ["c"])
    # two rules resolving to the same target on one event → one entry per list: the actor-wake for
    # c and a player-wide `when` rule that flips true on this event both name c
    hu = MachineDef.model_validate({**POKERISH.model_dump(by_alias=True), "wake": [
        {"role": "actor"}, {"role": "player", "when": [{"data_present": "pot"}]}]})
    new.data["pot"] = 1  # false on old, true on new → the player rule fires
    assert compute_wakes(hu, B, old, new, {}) == (["a", "b", "c", "d"], ["c"])


def test_an_actor_wake_honours_when_as_a_level_filter():
    """`when`/`unless` on `role: actor` are level filters on the new state — the pointer move
    itself is the edge (review I2)."""
    m = MachineDef.model_validate({**POKERISH.model_dump(by_alias=True), "wake": [
        {"role": "actor", "when": [{"data_present": "pot"}]}]})
    s = _s()
    old = s.copy()
    new = s.copy()
    new.actor = "c"
    assert compute_wakes(m, B, old, new, {}) == ([], [])
    new.data["pot"] = 1
    assert compute_wakes(m, B, old, new, {}) == ([], ["c"])


def test_unless_suppresses_a_wake():
    unless = [{"members_count": {"over": "player", "minus": ["out"], "equals": 1}}]
    m = MachineDef.model_validate({**POKERISH.model_dump(by_alias=True),
                                    "wake": [{"role": "actor", "unless": unless}]})
    s = _deal(_s())
    s.sets["out"] |= {"a", "b", "c"}
    old = s.copy()
    new = s.copy()
    new.actor = "d"
    assert compute_wakes(m, B, old, new, {}) == ([], [])  # d is the last one standing: no wake


def test_set_value_authority_and_owner_handling():
    s = _s()
    assert isinstance(set_value(POKERISH, B, s, "a", "pot", 1, None, {}, 1), Rejection)
    r = set_value(POKERISH, B, s, "dealer", "hole", "AhKh", None, {}, 1)
    assert isinstance(r, Rejection) and "owner" in r.detail
    out = set_value(POKERISH, B, s, "dealer", "hole", "AhKh", "a", {}, 1)
    assert isinstance(out, Outcome) and out.state.owner_data["hole"]["a"] == "AhKh"
    assert out.payload == {"op": "set", "key": "hole", "owner": "a", "value": "AhKh",
                           "caller": "dealer", "log": "owner", "wake": [], "wake_actor": []}
    r = set_value(POKERISH, B, s, "dealer", "pot", 1, "a", {}, 1)
    assert isinstance(r, Rejection) and "forbidden" in r.detail
    r = set_value(POKERISH, B, s, "dealer", "nope", 1, None, {}, 1)
    assert isinstance(r, Rejection) and r.check == "key"
    r = set_value(POKERISH, B, s, "dealer", "out", ["a"], None, {}, 1)
    assert isinstance(r, Rejection) and "transition" in r.detail
    r = set_value(POKERISH, B, s, "dealer", "hole", "x", "zed", {}, 1)
    assert isinstance(r, Rejection) and r.check == "owner" and "zed" in r.detail


HIDDENDEF = MachineDef.model_validate({
    "roles": {"mother": {"members": "referee"}, "crew": {"members": "participants"},
              "impostor": {"members": ["c", "d"], "visibility": "hidden"}},
    "states": ["night", "day"], "initial": "night",
    "participant_effects": ["add_to"],
    "data": {"dead": {"visibility": "public", "type": "set"}, "plan": {"visibility": "referee"}},
    "transitions": {"kill": {"from": "night", "to": "day", "by": "impostor", "log": "referee",
                             "effects": [{"add_to": {"key": "dead", "value": "$args.target"}}]}},
})
HB = Bindings(members={"mother": ("mother",), "crew": ("a", "b", "c", "d"), "impostor": ("c", "d")},
              roster=("a", "b", "c", "d"), referee="mother")


def test_view_filters_by_visibility_and_reveals():
    s = _s()
    s.data["pot"] = 40
    s.data["secret"] = "deck"
    s.owner_data["hole"] = {"a": "AhKh", "b": "2c7d"}
    v = view(POKERISH, B, s, "a")
    assert v["data"] == {"pot": 40, "hole": {"a": "AhKh"}, "out": [], "all_in": [], "acted": []}
    assert "secret" not in v["data"]
    s.revealed["hole"].add("b")
    assert view(POKERISH, B, s, "a")["data"]["hole"] == {"a": "AhKh", "b": "2c7d"}
    d = view(POKERISH, B, s, "dealer")["data"]
    assert d["secret"] == "deck" and d["hole"] == {"a": "AhKh", "b": "2c7d"}
    assert view(POKERISH, B, s, "c")["data"]["hole"] == {"b": "2c7d"}  # only the revealed one


def test_log_rows_follow_the_visibility_table():
    pub = {"op": "act", "transition": "call", "caller": "a", "args": {"to": 10}, "log": "public"}
    assert log_row(POKERISH, B, pub, "b") == pub
    ref = {"op": "act", "transition": "kill", "caller": "c", "args": {"target": "a"},
           "log": "referee"}
    assert log_row(HIDDENDEF, HB, ref, "a") is None
    assert log_row(HIDDENDEF, HB, ref, "mother") == ref
    rej = {"op": "reject", "transition": "kill", "caller": "c", "check": "guard", "detail": "x",
           "log": "referee"}
    assert log_row(HIDDENDEF, HB, rej, "a") is None
    assert log_row(HIDDENDEF, HB, rej, "c") is None
    unknown = {"op": "reject", "transition": "zap", "caller": "c", "check": "exists",
               "detail": "x", "log": "public"}
    assert log_row(HIDDENDEF, HB, unknown, "a") is None
    assert log_row(HIDDENDEF, HB, unknown, "c") == unknown
    setp = {"op": "set", "key": "pot", "owner": None, "value": 5, "caller": "dealer",
            "log": "public"}
    assert log_row(POKERISH, B, setp, "a") == setp
    seto = {"op": "set", "key": "hole", "owner": "a", "value": "AhKh", "caller": "dealer",
            "log": "owner"}
    assert log_row(POKERISH, B, seto, "a") == seto and log_row(POKERISH, B, seto, "b") is None
    setr = {"op": "set", "key": "secret", "owner": None, "value": "deck", "caller": "dealer",
            "log": "referee"}
    assert log_row(POKERISH, B, setr, "a") is None and log_row(POKERISH, B, setr, "dealer") == setr


def test_declaration_view_hides_hidden_role_membership():
    a = declaration_view(HIDDENDEF, HB, "a")
    assert a["roles"]["impostor"]["members"] == "<hidden>"
    assert a["roles"]["crew"]["members"] == "participants"
    assert declaration_view(HIDDENDEF, HB, "c")["roles"]["impostor"]["members"] == ["c", "d"]
    assert declaration_view(HIDDENDEF, HB, "mother")["roles"]["impostor"]["members"] == ["c", "d"]


def test_log_row_fails_closed_on_an_unknown_op():
    unknown = {"op": "mystery", "log": "public"}
    assert log_row(POKERISH, B, unknown, "a") is None
    assert log_row(POKERISH, B, unknown, "dealer") == unknown


def test_replay_reproduces_the_state_and_takes_actor_since_from_the_event():
    live = _s()
    events = []
    for who, ts in (("dealer", 10), ("a", 20), ("b", 30)):
        tr = "deal" if who == "dealer" else "call"
        o = act(POKERISH, B, live, who, tr, {"first": "a"} if tr == "deal" else {}, {}, ts)
        assert isinstance(o, Outcome)
        live = o.state
        events.append((ts, o.payload))
    o = set_value(POKERISH, B, live, "dealer", "pot", 15, None, {}, 40)
    live = o.state
    events.append((40, o.payload))
    events.append((41, reject_payload("c", "fold", {}, "guard", "caller_is_actor", "public")))
    rebuilt = replay(POKERISH, B, events, start_ms=1000)
    assert rebuilt == live and rebuilt.actor_since == 30


def test_replay_fails_loudly_if_the_declaration_no_longer_admits_an_event():
    with _pytest.raises(ReplayError, match="replay: event 1 .* no longer applies"):
        replay(POKERISH, B, [(10, {"op": "act", "transition": "deal", "caller": "dealer",
                                   "args": {"first": "a"}, "log": "public"}),
                             (11, {"op": "act", "transition": "teleport", "caller": "a",
                                   "args": {}, "log": "public"})], start_ms=1)


def test_a_role_rule_that_names_the_current_actor_is_stamped_as_a_ROLE_wake():
    """Review I1 / gap T1: the two lists exist so the host never has to GUESS which rule stamped
    an id. A role rule whose targets happen to include the current actor must land in `wake` —
    the host collapses `wake_actor` to the latest event's actor, and collapsing this one dropped
    that agent's wake entirely."""
    m = MachineDef.model_validate({**POKERISH.model_dump(by_alias=True), "wake": [
        {"role": "actor"}, {"role": "player", "when": [{"data_present": "pot"}]}]})
    s = _s()
    old = s.copy()
    new = s.copy()
    new.actor = "c"
    new.data["pot"] = 1
    wake, wake_actor = compute_wakes(m, B, old, new, {})
    assert wake == ["a", "b", "c", "d"]  # c is here because the ROLE rule named it...
    assert wake_actor == ["c"]           # ...and here because the pointer moved to it
    # ...and the same on a real payload: one act that both trips the role rule and moves the
    # pointer stamps the new actor in BOTH lists.
    decl = m.model_dump(by_alias=True)
    decl["transitions"]["bet"] = {
        "from": "street", "to": "same", "by": "dealer",
        "effects": [{"set": {"key": "pot", "value": "$args.n"}}, "advance_actor"]}
    m2 = MachineDef.model_validate(decl)
    out = act(m2, B, _deal(_s(), first="a"), "dealer", "bet", {"n": 10}, {}, 5)
    assert isinstance(out, Outcome) and out.payload["actor"] == "b"
    assert out.payload["wake"] == ["a", "b", "c", "d"] and out.payload["wake_actor"] == ["b"]
