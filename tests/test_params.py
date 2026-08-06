"""Scenario parameters — notation, resolution, and binding (ADR-003, #41).

The parser is where this feature is sharpest: a placeholder can carry a default and a
description, both optional, both able to contain the characters used to delimit them. Every
escape and precedence rule below is a decision from the ADR, asserted rather than assumed.
"""

import pytest

from bearpit.core.params import (
    ParameterError,
    bind,
    missing_values,
    parse_placeholders,
    resolve_values,
    scan,
    substitute,
    validate_values,
)
from bearpit.core.schema import (
    AgentRole,
    AgentSpec,
    ModelRef,
    Project,
    ProjectMeta,
    ProjectSpec,
)


def _project(**kw) -> Project:
    """A minimal valid project; each test overrides only the prose it cares about."""
    metadata = kw.pop("metadata", None) or ProjectMeta(name="Duel")
    spec = kw.pop("spec", None) or ProjectSpec(goals=["win"])
    agents = kw.pop("agents", None) or [
        AgentSpec(id="orin", model=ModelRef(provider="azure", model="m", api_key_ref="k"))
    ]
    return Project(metadata=metadata, spec=spec, agents=agents)


# ---------------------------------------------------------------- notation

def test_every_documented_form_parses() -> None:
    (p,) = parse_placeholders("${a}")
    assert (p.name, p.default, p.description) == ("a", None, None)
    (p,) = parse_placeholders("${a,10}")
    assert (p.name, p.default, p.description) == ("a", "10", None)
    (p,) = parse_placeholders("${a,10,Points to win}")
    assert (p.name, p.default, p.description) == ("a", "10", "Points to win")
    (p,) = parse_placeholders("${a,,Points to win}")
    assert (p.name, p.default, p.description) == ("a", None, "Points to win"), (
        "an empty middle means NO default, so the author is still warned"
    )


def test_a_description_may_contain_commas() -> None:
    """The description is the last part, so it keeps its commas — no escaping needed for the
    most common case of writing a normal English sentence."""
    (p,) = parse_placeholders("${target,10,Points needed, before the bell}")
    assert p.default == "10"
    assert p.description == "Points needed, before the bell"


def test_a_default_may_contain_an_escaped_comma_or_brace() -> None:
    (p,) = parse_placeholders(r"${greeting,Hello\, Vela}")
    assert p.default == "Hello, Vela"
    (p,) = parse_placeholders(r"${json,\{\}}")
    assert p.default == "{}"


def test_a_doubled_dollar_is_an_escape_not_a_placeholder() -> None:
    """`resource_files` and `local_skills` are real files an agent reads and may legitimately
    contain shell or template syntax."""
    assert parse_placeholders("$${HOME}") == []
    assert substitute("$${HOME}", {}) == "${HOME}"
    assert substitute("export PATH=$${PATH}:/x", {}) == "export PATH=${PATH}:/x"


def test_invalid_names_stay_literal() -> None:
    """No existing scenario may become accidentally parameterised by this feature shipping."""
    for text in ("${1bad}", "${a b}", "${}", "${-x}"):
        assert parse_placeholders(text) == [], text
        assert substitute(text, {}) == text, text


def test_substitute_fills_and_blanks() -> None:
    assert substitute("Reach ${n,10} points", {"n": "25"}) == "Reach 25 points"
    assert substitute("Reach ${n,10} points", {}) == "Reach  points", (
        "a name with no value renders empty; whether that is allowed is the launcher's call"
    )


# ---------------------------------------------------------------- scanning

def test_scan_finds_parameters_in_every_prose_field() -> None:
    project = _project(
        metadata=ProjectMeta(name="Duel", description="A duel to ${target,10}"),
        spec=ProjectSpec(
            goals=["Reach ${target} points"],
            guidelines="Be ${tone,civil}",
            restrictions="Never ${forbidden,cheat}",
        ),
        agents=[
            AgentSpec(
                id="orin",
                model=ModelRef(provider="azure", model="m", api_key_ref="k"),
                description="plays as ${style,aggressive}",
                goals=["beat ${rival,vela}"],
                responsibilities=["report to ${boss,themis}"],
                persona="You are ${persona_name,Orin}",
                resource_files={"brief.md": "Budget is ${budget,100}"},
                local_skills={"play.md": "Open with ${opening,rock}"},
            ),
            # rubric is referee-only, enforced by the schema
            AgentSpec(
                id="themis", role=AgentRole.REFEREE,
                model=ModelRef(provider="azure", model="m", api_key_ref="k"),
                rubric="Score on ${criterion,speed}",
            ),
        ],
    )
    names = [p.name for p in scan(project)]
    assert names == ["target", "tone", "forbidden", "style", "rival", "boss",
                     "persona_name", "budget", "opening", "criterion"], (
        "ordered by first appearance, so the launch form is stable run to run"
    )


def test_scan_records_where_each_parameter_is_used() -> None:
    """The launch form shows this, and it is what makes a typo visible rather than silent."""
    project = _project(
        spec=ProjectSpec(goals=["Reach ${target,10}", "and hold ${target}"]),
        agents=[AgentSpec(id="orin", model=ModelRef(provider="azure", model="m", api_key_ref="k"),
                          persona="aim for ${target}")],
    )
    (p,) = scan(project)
    assert p.occurrences == ["spec.goals[0]", "spec.goals[1]", "agents.orin.persona"]


def test_executable_fields_are_never_scanned() -> None:
    """`termination.pattern` is a regex in which ${x} is valid syntax. Substituting it would
    silently rewrite a termination condition — a realm that never ends (#30)."""
    from bearpit.core.schema import TerminationCondition, TerminationKind
    project = _project(spec=ProjectSpec(
        goals=["win"],
        termination=[TerminationCondition(
            type=TerminationKind.MESSAGE, pattern=r"DONE\{2\}|${x,1}", channel="commons")],
    ))
    assert scan(project) == []


# ---------------------------------------------------------------- precedence

def test_the_manifest_overrides_inline_and_says_so() -> None:
    project = _project(
        spec=ProjectSpec(
            goals=["Reach ${target,10,inline description}"],
            parameters={"target": {"default": "25", "description": "manifest description"}},
        )
    )
    (p,) = scan(project)
    assert p.default == "25"
    assert p.description == "manifest description"
    assert p.default_origin == "manifest" and p.description_origin == "manifest"
    assert p.inline_default == "10"
    assert p.overridden is True, (
        "the surfaces render this as '25 (manifest, overrides inline 10)' — an override the "
        "author cannot see is the whole risk of letting the manifest win"
    )


def test_inline_is_used_when_the_manifest_says_nothing() -> None:
    project = _project(spec=ProjectSpec(
        goals=["Reach ${target,10,how many}"],
        parameters={"target": {"type": "int", "min": 1}},
    ))
    (p,) = scan(project)
    assert (p.default, p.default_origin) == ("10", "inline")
    assert (p.description, p.description_origin) == ("how many", "inline")
    assert p.type == "int" and p.minimum == 1.0
    assert p.overridden is False


def test_a_manifest_entry_for_an_unused_parameter_is_an_error() -> None:
    """The schema's own note about spec-level `duration`: two ways to say the same thing, one
    silently inert, is how a scenario ends up with no backstop at all."""
    project = _project(spec=ProjectSpec(goals=["win"], parameters={"ghost": {"default": "x"}}))
    with pytest.raises(ParameterError, match="no scenario text uses"):
        scan(project)


def test_conflicting_inline_defaults_are_an_error() -> None:
    project = _project(spec=ProjectSpec(goals=["${x,1}", "${x,2}"]))
    with pytest.raises(ParameterError, match="two different inline defaults"):
        scan(project)


def test_the_same_name_may_repeat_with_the_default_given_once() -> None:
    project = _project(spec=ProjectSpec(goals=["${x,1}", "${x}"]))
    (p,) = scan(project)
    assert p.default == "1"


def test_a_default_outside_its_choices_is_an_error() -> None:
    project = _project(spec=ProjectSpec(
        goals=["${mood,furious}"], parameters={"mood": {"choices": ["calm", "tense"]}}))
    with pytest.raises(ParameterError, match="not one of its choices"):
        scan(project)


def test_an_unknown_type_is_rejected_by_the_schema() -> None:
    """Caught at construction, before scan() ever runs — earlier and stronger than a scan check."""
    from pydantic import ValidationError
    with pytest.raises(ValidationError, match="type"):
        ProjectSpec(goals=["${x,1}"], parameters={"x": {"type": "date"}})


def test_an_unknown_type_still_errors_on_the_raw_dict_path() -> None:
    """scan() also accepts an unvalidated doc — the scenario editor previews parameters before a
    model exists — so the check stays as a backstop for that path."""
    project = _project(spec=ProjectSpec(goals=["${x,1}"]))
    object.__setattr__(project.spec, "parameters", {"x": {"type": "date"}})
    with pytest.raises(ParameterError, match="expected one of"):
        scan(project)


# ---------------------------------------------------------------- values

def test_required_means_no_effective_default() -> None:
    project = _project(spec=ProjectSpec(goals=["${a,1}", "${b}"]))
    a, b = scan(project)
    assert a.required is False and b.required is True
    assert [p.name for p in missing_values([a, b], {})] == ["b"]
    assert missing_values([a, b], {"b": "x"}) == []


def test_resolution_prefers_supplied_then_default_then_empty() -> None:
    project = _project(spec=ProjectSpec(goals=["${a,1}", "${b,2}", "${c}"]))
    params = scan(project)
    assert resolve_values(params, {"a": "9"}) == {"a": "9", "b": "2", "c": ""}


def test_supplied_values_are_validated_before_a_container_exists() -> None:
    project = _project(spec=ProjectSpec(
        goals=["${n,5}", "${mood,calm}"],
        parameters={"n": {"type": "int", "min": 1, "max": 10},
                    "mood": {"choices": ["calm", "tense"]}},
    ))
    params = scan(project)
    assert validate_values(params, {"n": "7", "mood": "tense"}) == []
    assert "whole number" in " ".join(validate_values(params, {"n": "2.5"}))
    assert "at most 10" in " ".join(validate_values(params, {"n": "99"}))
    assert "at least 1" in " ".join(validate_values(params, {"n": "0"}))
    assert "must be one of" in " ".join(validate_values(params, {"mood": "furious"}))
    assert "not a parameter" in " ".join(validate_values(params, {"nope": "x"}))


# ---------------------------------------------------------------- binding

def test_bind_substitutes_prose_and_leaves_structure_alone() -> None:
    project = _project(
        metadata=ProjectMeta(name="Duel", description="to ${target,10}"),
        spec=ProjectSpec(goals=["Reach ${target} points"], guidelines="Be ${tone,civil}"),
        agents=[AgentSpec(
            id="orin", name="Orin",
            model=ModelRef(provider="azure", model="gpt-5.4", api_key_ref="azure-main"),
            persona="You are ${persona_name,Orin}, aiming for ${target}",
            resource_files={"brief.md": "target ${target}"},
        )],
    )
    bound = bind(project, resolve_values(scan(project), {"target": "25"}))
    assert bound.metadata.description == "to 25"
    assert bound.spec.goals == ["Reach 25 points"]
    assert bound.spec.guidelines == "Be civil"
    assert bound.agents[0].persona == "You are Orin, aiming for 25"
    assert bound.agents[0].resource_files["brief.md"] == "target 25"
    # structure untouched
    assert bound.agents[0].id == "orin" and bound.agents[0].name == "Orin"
    assert bound.agents[0].model.model == "gpt-5.4"


def test_bind_returns_a_new_project_and_leaves_the_original_alone() -> None:
    """The original is what a scenario EDIT writes back; binding it in place would persist one
    run's values into the file."""
    project = _project(spec=ProjectSpec(goals=["Reach ${target,10}"]))
    bound = bind(project, {"target": "25"})
    assert bound.spec.goals == ["Reach 25"]
    assert project.spec.goals == ["Reach ${target,10}"], "source project must be unmodified"


def test_bind_revalidates_so_an_overlong_substitution_fails_loudly() -> None:
    """Substitution can push a field past its max_length. That must fail at load, naming the
    field, rather than deep inside provisioning."""
    project = _project(spec=ProjectSpec(goals=["${blob}"]))
    with pytest.raises(Exception) as exc:      # pydantic ValidationError
        bind(project, {"blob": "x" * 1001})    # GoalText caps at 1000
    assert "goals" in str(exc.value)


def test_a_scenario_without_placeholders_is_untouched() -> None:
    project = _project(spec=ProjectSpec(goals=["win the duel"]))
    assert scan(project) == []
    assert bind(project, {}).spec.goals == ["win the duel"]


def test_bind_preserves_loader_populated_files() -> None:
    """`resource_files` and `local_skills` are `exclude=True`, so a dump/validate round-trip drops
    them silently. `_project_snapshot` carries them alongside for the same reason. Missing this
    deletes every reference file an agent was given: the agent still boots, just without the
    material it was told to read."""
    project = _project(agents=[AgentSpec(
        id="orin", model=ModelRef(provider="azure", model="m", api_key_ref="k"),
        resource_files={"brief.md": "budget ${budget,100}", "map.txt": "no placeholders"},
        local_skills={"play.md": "open with ${opening,rock}"},
    )])
    bound = bind(project, {"budget": "250", "opening": "paper"})
    assert bound.agents[0].resource_files == {
        "brief.md": "budget 250", "map.txt": "no placeholders"}
    assert bound.agents[0].local_skills == {"play.md": "open with paper"}


def test_the_run_records_which_values_it_used() -> None:
    """The bound project already holds the substituted prose, but reading a value back OUT of
    finished prose is guesswork. Comparing two runs of one scenario is the whole point of having
    parameters, so the values are recorded as data (ADR-003)."""
    from bearpit.core.runconfig import run_config

    project = _project(spec=ProjectSpec(goals=["reach ${target,10}"]))
    cfg = run_config(project, "azure", require_mention=True, parameters={"target": "25"})
    assert cfg["parameters"] == {"target": "25"}
    # and a scenario with none records an empty map rather than omitting the key, so a reader
    # never has to distinguish "no parameters" from "an older run that did not record them"
    assert run_config(project, "azure", require_mention=True)["parameters"] == {}
