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
    m = MachineDef.model_validate(
        _with(
            data={"acted": {"visibility": "public", "type": "set"}},
            transitions={
                "go": {
                    "from": "a",
                    "to": "b",
                    "by": "player",
                    "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
                    "effects": [
                        {"add_to": {"key": "acted", "value": "$caller"}},
                        "advance_actor",
                    ],
                }
            },
            actor={"over": "player", "skip": ["acted"]},
        )
    )
    g = m.transitions["go"].guard
    assert [x.name for x in g] == ["caller_is_actor", "caller_not_in"] and g[1].arg == "acted"
    e = m.transitions["go"].effects
    assert [x.name for x in e] == ["add_to", "advance_actor"]


@pytest.mark.parametrize("bad, message", [
    (_with(initial="zzz"), "initial 'zzz' is not a declared state"),
    (
        _with(transitions={"go": {"from": "a", "to": "nope", "by": "ref"}}),
        "transition 'go': 'nope' is not a declared state",
    ),
    (
        _with(transitions={"go": {"from": "a", "to": "b", "by": "judge"}}),
        "transition 'go': 'judge' is not a declared role",
    ),
    (_with(actor={"over": "nobody"}), "actor.over 'nobody' is not a declared role"),
    (
        _with(actor={"over": "player", "skip": ["out"]}),
        "actor.skip 'out' is not a declared set key",
    ),
    (
        _with(
            data={"pot": {"visibility": "public"}},
            actor={"over": "player", "skip": ["pot"]},
        ),
        "actor.skip 'pot' is not a declared set key",
    ),
    (_with(terminal=["zzz"]), "terminal 'zzz' is not a declared state"),
    (
        _with(
            transitions={
                "go": {"from": "a", "to": "b", "by": "ref", "guard": ["teleport"]}
            }
        ),
        "transition 'go': unknown guard 'teleport'",
    ),
    (
        _with(
            transitions={
                "go": {"from": "a", "to": "b", "by": "ref", "effects": ["explode"]}
            }
        ),
        "transition 'go': unknown effect 'explode'",
    ),
    (
        _with(
            transitions={
                "go": {
                    "from": "a",
                    "to": "b",
                    "by": "ref",
                    "effects": [{"reset": "pot"}],
                }
            },
            data={"pot": {"visibility": "public"}},
        ),
        "transition 'go': 'reset' needs a set key, 'pot' is not one",
    ),
    (
        _with(
            transitions={
                "go": {
                    "from": "a",
                    "to": "b",
                    "by": "ref",
                    "effects": [{"unset": "s"}],
                }
            },
            data={"s": {"visibility": "public", "type": "set"}},
        ),
        "transition 'go': 'unset' on a set key 's' — use 'reset'",
    ),
    (
        _with(
            transitions={
                "go": {
                    "from": "a",
                    "to": "b",
                    "by": "ref",
                    "effects": [{"set": {"key": "nope", "value": 1}}],
                }
            },
        ),
        "transition 'go': 'set' on undeclared key 'nope'",
    ),
    (
        _with(
            transitions={
                "go": {
                    "from": "a",
                    "to": "b",
                    "by": "ref",
                    "effects": [{"set": {"key": "hole", "value": 1}}],
                }
            },
            data={"hole": {"visibility": "owner"}},
        ),
        "transition 'go': 'set' on owner-visibility key 'hole'",
    ),
    (
        _with(
            transitions={
                "go": {
                    "from": "a",
                    "to": "b",
                    "by": "ref",
                    "effects": [{"reveal": {"key": "pot", "owners": "$caller"}}],
                }
            },
            data={"pot": {"visibility": "public"}},
        ),
        "transition 'go': 'reveal' needs an owner-visibility key, got 'pot'",
    ),
    (
        _with(data={"mine": {"visibility": "owner", "type": "set"}}),
        "data key 'mine': an owner-visibility key cannot be a set",
    ),
    # A guard that reads a key must NAME one. `data_set_full` was missing from the key-reading
    # set entirely, so a keyless one passed launch and raised KeyError('key') inside check_guard
    # on the first act — uncaught, mid-realm (review I2).
    (
        _with(data={"acted": {"visibility": "public", "type": "set"}},
              transitions={"go": {"from": "a", "to": "b", "by": "ref",
                                  "guard": [{"data_set_full": {"over": "player"}}]}}),
        "transition 'go': guard 'data_set_full' needs a key",
    ),
    (
        _with(transitions={"go": {"from": "a", "to": "b", "by": "ref",
                                  "guard": [{"data_equals": {"value": 1}}]}}),
        "transition 'go': guard 'data_equals' needs a key",
    ),
    (
        _with(transitions={"go": {"from": "a", "to": "b", "by": "ref",
                                  "guard": ["data_present"]}}),
        "transition 'go': guard 'data_present' needs a key",
    ),
    (
        _with(data={"acted": {"visibility": "public", "type": "set"}},
              transitions={"go": {"from": "a", "to": "b", "by": "ref",
                                  "guard": [{"data_set_full": {"key": "ghost",
                                                               "over": "player"}}]}}),
        "transition 'go': guard 'data_set_full' reads undeclared key 'ghost'",
    ),
    # ...and a DECLARED key of the wrong type is just as silent: a set guard over a value key is
    # permanently false (data_set_full) or permanently true (data_set_empty).
    (
        _with(data={"pot": {"visibility": "public"}},
              transitions={"go": {"from": "a", "to": "b", "by": "ref",
                                  "guard": [{"data_set_full": {"key": "pot",
                                                               "over": "player"}}]}}),
        "transition 'go': guard 'data_set_full' needs a set key, 'pot' is not one",
    ),
    (
        _with(data={"pot": {"visibility": "public"}},
              transitions={"go": {"from": "a", "to": "b", "by": "ref",
                                  "guard": [{"caller_in": "pot"}]}}),
        "transition 'go': guard 'caller_in' needs a set key, 'pot' is not one",
    ),
    (
        _with(data={"pot": {"visibility": "public"}},
              transitions={"go": {"from": "a", "to": "b", "by": "ref",
                                  "guard": [{"data_set_empty": "pot"}]}}),
        "transition 'go': guard 'data_set_empty' needs a set key, 'pot' is not one",
    ),
    # escrow_complete's round may be `$data.<key>` — an undeclared one resolves to None, and the
    # guard then fails closed forever against the round literally named "None".
    (
        _with(transitions={"go": {"from": "a", "to": "b", "by": "ref",
                                  "guard": [{"escrow_complete": {"round": "$data.ghost",
                                                                 "over": "player"}}]}}),
        "transition 'go': escrow_complete.round reads undeclared key 'ghost'",
    ),
    # `set` may not launder a hidden value into a public key: the write is legal per-key, and the
    # result is the referee's secret in everyone's view (review M3).
    (
        _with(data={"pot": {"visibility": "public"}, "secret": {"visibility": "referee"}},
              transitions={"go": {"from": "a", "to": "b", "by": "ref",
                                  "effects": [{"set": {"key": "pot",
                                                       "value": "$data.secret"}}]}}),
        "transition 'go': 'set' copies 'secret' (referee-visibility) into public key 'pot'",
    ),
    # Reachability (review M6)
    (
        _with(terminal=["b"],
              transitions={"go": {"from": "a", "to": "a", "by": "ref"}}),
        "no transition reaches a terminal state — machine_terminal could never fire",
    ),
    (
        _with(transitions={"go": {"from": "b", "to": "a", "by": "ref"}}),
        "initial state 'a' has no outgoing transition",
    ),
])
def test_bad_structure_is_refused_with_a_precise_message(bad, message):
    with pytest.raises(ValidationError) as exc:
        MachineDef.model_validate(bad)
    assert message in str(exc.value)


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
           transitions={"go": {"from": "a", "to": "b", "by": "player",
                               "effects": [{"reset": "acted"}]}}),
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
                                        "guard": [{"data_equals":
                                                   {"key": "secret", "value": 1}}]}}},
     "transition 'go': public log but guard 'data_equals' reads referee-visibility key 'secret'"),
    # cardinality compares to a literal, never to something an agent wrote
    ({**HIDDEN, "transitions": {"go": {"from": "a", "to": "b", "by": "ref",
                                        "guard": [{"members_count":
                                                   {"over": "player", "equals": "$data.n"}}]}}},
     "transition 'go': members_count N must be a literal"),
    # a guard may not name a key the declaration never declared
    ({**HIDDEN, "transitions": {"go": {"from": "a", "to": "b", "by": "ref",
                                        "guard": [{"caller_in": "ghost"}]}}},
     "transition 'go': guard 'caller_in' reads undeclared key 'ghost'"),
    # wake rules
    ({**HIDDEN, "wake": [{"role": "impostor"}]},
     "wake rule targets hidden role 'impostor' — deferred until per-agent wake rooms exist"),
    ({**HIDDEN, "wake": [{"role": "ghost"}]}, "wake rule: 'ghost' is not a declared role"),
    ({**HIDDEN, "wake": [{"role": "ref"}]},
     "wake rule: role 'ref' needs `when` or `after_s` — only `actor` may be bare"),
    ({**HIDDEN, "wake": [{"role": "ref", "after_s": 30}]},
     "wake rule: after_s 30 is below the floor of 240"),
    # `actor` is the pointer, not a role with members — a timed nudge at it reaches nobody
    ({**HIDDEN, "wake": [{"role": "actor", "after_s": 240}]},
     "wake rule: after_s needs a declared role"),
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
        transitions={"go": {"from": "a", "to": "b", "by": "player",
                             "effects": [{"reset": "acted"}]}},
    ))
    assert m.participant_effects == ["reset"]


def test_after_s_is_absent_by_default_and_the_floor_applies_only_to_a_set_value():
    from bearpit.core.machine import AFTER_S_FLOOR
    m = MachineDef.model_validate(_with(
        data={"pot": {"visibility": "public"}},
        wake=[{"role": "ref", "when": [{"data_present": "pot"}]}]))
    assert m.wake[0].after_s is None  # omitted → not a time rule
    m2 = MachineDef.model_validate(_with(wake=[{"role": "ref", "after_s": AFTER_S_FLOOR}]))
    assert m2.wake[0].after_s == AFTER_S_FLOOR  # the floor itself is accepted
