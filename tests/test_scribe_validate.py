"""validate_scenario: schema + a starter slice of the scenario-contract checklist (Task 2).

Two guarantees: (a) every bundled example validates clean — Scribe must never flag a package the
platform itself ships and runs; (b) each contract invariant we encode fires on a hand-built bad
manifest with an actionable message. Plus a round-trip through the real ApiPackageStore.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from bearpit.core.package import load_package
from bearpit.core.schema import (
    AgentRole,
    AgentSpec,
    Project,
    ProjectMeta,
    ProjectSpec,
    RefereePowers,
    TerminationCondition,
    TerminationKind,
)
from bearpit.scribe.store import ApiPackageStore
from bearpit.scribe.validate import validate_scenario

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
PACKAGES = sorted(p.name for p in EXAMPLES.iterdir() if (p / "project.json").exists())


def _dur(limit: str = "30m") -> TerminationCondition:
    return TerminationCondition(type=TerminationKind.DURATION, limit=limit)


def test_every_example_validates_clean() -> None:
    bad: dict[str, list[str]] = {}
    for name in PACKAGES:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            project = load_package(EXAMPLES / name)
        result = validate_scenario(project)
        if not result.ok:
            bad[name] = result.errors
    assert not bad, f"examples must validate clean, but: {bad}"


def test_valid_minimal_scenario_is_ok() -> None:
    project = Project(
        metadata=ProjectMeta(name="mini-duel", description="a tiny duel"),
        spec=ProjectSpec(goals=["settle it"], termination=[_dur()]),
        agents=[
            AgentSpec(id="alice", persona="You are Alice.", goals=["win"]),
            AgentSpec(id="bob", persona="You are Bob."),
        ],
    )
    assert validate_scenario(project).ok


def test_referee_rubric_must_name_a_state_change_tool() -> None:
    project = Project(
        metadata=ProjectMeta(name="bad-rubric"),
        spec=ProjectSpec(termination=[_dur()]),
        agents=[
            AgentSpec(
                id="judge",
                role=AgentRole.REFEREE,
                rubric="Judge the debate and declare a winner.",
                powers=RefereePowers(verdict_ends_realm=True),
            ),
            AgentSpec(id="alice", persona="You are Alice."),
        ],
    )
    result = validate_scenario(project)
    assert not result.ok
    assert any("tool-based state changes" in e for e in result.errors), result.errors


def test_scored_scenario_must_declare_termination() -> None:
    project = Project(
        metadata=ProjectMeta(name="bad-term"),
        spec=ProjectSpec(termination=[]),
        agents=[
            AgentSpec(
                id="judge",
                role=AgentRole.REFEREE,
                rubric="Call rule() to conclude.",
                powers=RefereePowers(verdict_ends_realm=True),
            ),
            AgentSpec(id="alice", persona="You are Alice."),
        ],
    )
    result = validate_scenario(project)
    assert not result.ok
    assert any("termination" in e for e in result.errors), result.errors


def test_turn_based_language_requires_a_turns_policy() -> None:
    project = Project(
        metadata=ProjectMeta(name="bad-turns"),
        spec=ProjectSpec(termination=[_dur()]),
        agents=[
            AgentSpec(id="alice", persona="Seal your vote on your turn."),
            AgentSpec(id="bob", persona="You are Bob."),
        ],
    )
    result = validate_scenario(project)
    assert not result.ok
    assert any("turns" in e for e in result.errors), result.errors


def test_goal_referencing_unknown_agent_id() -> None:
    project = Project(
        metadata=ProjectMeta(name="bad-goal"),
        spec=ProjectSpec(termination=[_dur()]),
        agents=[
            AgentSpec(id="alice", persona="You are Alice.", goals=["Recruit @ghost to your side."]),
            AgentSpec(id="bob", persona="You are Bob."),
        ],
    )
    result = validate_scenario(project)
    assert not result.ok
    assert any("ghost" in e for e in result.errors), result.errors


async def test_api_store_write_then_read_round_trips(tmp_path: Path) -> None:
    store = ApiPackageStore(user_dir=tmp_path / "scenarios")
    project = Project(
        metadata=ProjectMeta(name="mini-duel", description="a tiny duel"),
        spec=ProjectSpec(goals=["settle it"], termination=[_dur()]),
        agents=[
            AgentSpec(id="alice", persona="You are Alice.", goals=["win"]),
            AgentSpec(id="bob", persona="You are Bob."),
        ],
    )
    await store.write("mini-duel", project)
    loaded = await store.read("mini-duel")
    assert loaded.metadata.name == "mini-duel"
    assert [a.id for a in loaded.agents] == ["alice", "bob"]
    assert loaded.agents[0].persona == "You are Alice."
    listing = await store.list()
    assert listing == [{"name": "mini-duel", "title": "mini-duel", "agents": 2,
                        "summary": "a tiny duel"}]
