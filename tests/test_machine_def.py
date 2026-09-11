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
])
def test_bad_structure_is_refused_with_a_precise_message(bad, message):
    with pytest.raises(ValidationError) as exc:
        MachineDef.model_validate(bad)
    assert message in str(exc.value)
