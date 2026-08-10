"""AuthoringTools: the verbs the loop dispatches (Task 3).

Validate-before-write is the load-bearing behaviour: a create/edit that fails validation must
return the problems and NOT touch the store; a passing one snapshots then writes.
"""

from __future__ import annotations

import json

from fakes import FakeMemory, FakePackageStore, FakeVersions

from bearpit.core.schema import (
    AgentSpec,
    Project,
    ProjectMeta,
    ProjectSpec,
    TerminationCondition,
    TerminationKind,
)
from bearpit.scribe.tools import TOOL_SPECS, AuthoringTools
from bearpit.scribe.types import ToolCall


def _tools(store: FakePackageStore) -> tuple[AuthoringTools, FakeVersions]:
    versions = FakeVersions()
    return AuthoringTools(store, versions, FakeMemory()), versions


def _mini() -> Project:
    return Project(
        metadata=ProjectMeta(name="mini-duel", description="a tiny duel"),
        spec=ProjectSpec(
            goals=["settle it"],
            termination=[TerminationCondition(type=TerminationKind.DURATION, limit="30m")],
        ),
        agents=[
            AgentSpec(id="alice", persona="You are Alice.", goals=["win"]),
            AgentSpec(id="bob", persona="You are Bob."),
        ],
    )


_VALID_SPEC = {
    "metadata": {"name": "duel"},
    "spec": {"goals": ["win"], "termination": [{"type": "duration", "limit": "30m"}]},
    "agents": [
        {"id": "alice", "persona": "You are Alice.", "goals": ["win"]},
        {"id": "bob", "persona": "You are Bob."},
    ],
}


async def test_create_valid_spec_writes_and_snapshots() -> None:
    store = FakePackageStore()
    tools, versions = _tools(store)
    result = await tools.dispatch(
        ToolCall(id="1", name="create_scenario", arguments={"name": "duel", "spec": _VALID_SPEC})
    )
    assert store.writes == ["duel"]
    assert len(versions.snapshots) == 1
    assert versions.snapshots[0] == ("duel", None)  # None = pre-create
    assert len(store._projects["duel"].agents) == 2
    assert "duel" in result


async def test_create_invalid_spec_returns_errors_and_does_not_write() -> None:
    store = FakePackageStore()
    tools, versions = _tools(store)
    bad = {
        "metadata": {"name": "bad"},
        "spec": {"termination": []},  # scored (has a referee) but no termination
        "agents": [
            {
                "id": "judge",
                "role": "referee",
                "rubric": "Judge the debate and declare a winner.",  # names no tool
                "powers": {"verdict_ends_realm": True},
            },
            {"id": "al", "persona": "x"},
        ],
    }
    result = await tools.dispatch(
        ToolCall(id="1", name="create_scenario", arguments={"name": "bad", "spec": bad})
    )
    assert store.writes == []
    assert versions.snapshots == []
    assert "termination" in result
    assert "tool-based state changes" in result


async def test_edit_applies_project_merge_patch() -> None:
    store = FakePackageStore({"mini-duel": _mini()})
    tools, versions = _tools(store)
    patch = {"project": {"spec": {"guidelines": "Be kind."}}}
    result = await tools.dispatch(
        ToolCall(id="1", name="edit_scenario", arguments={"name": "mini-duel", "patch": patch})
    )
    assert store.writes == ["mini-duel"]
    assert store._projects["mini-duel"].spec.guidelines == "Be kind."
    # unchanged fields survive the merge
    assert store._projects["mini-duel"].spec.goals == ["settle it"]
    assert versions.snapshots[0][0] == "mini-duel"
    assert versions.snapshots[0][1] is not None  # prior state was snapshotted
    assert "mini-duel" in result


async def test_edit_replaces_a_whole_agent() -> None:
    store = FakePackageStore({"mini-duel": _mini()})
    tools, _ = _tools(store)
    await tools.dispatch(
        ToolCall(
            id="1",
            name="edit_scenario",
            arguments={
                "name": "mini-duel",
                "patch": {
                    "agent": {
                        "id": "alice",
                        "replace": {"id": "alice", "persona": "New Alice.", "goals": ["dominate"]},
                    }
                },
            },
        )
    )
    alice = next(a for a in store._projects["mini-duel"].agents if a.id == "alice")
    assert alice.persona == "New Alice."
    assert alice.goals == ["dominate"]
    # bob is untouched
    bob = next(a for a in store._projects["mini-duel"].agents if a.id == "bob")
    assert bob.persona == "You are Bob."


async def test_read_and_list_scenarios_return_expected_shapes() -> None:
    store = FakePackageStore({"mini-duel": _mini()})
    tools, _ = _tools(store)
    listed = json.loads(await tools.dispatch(ToolCall(id="1", name="list_scenarios", arguments={})))
    assert listed == [{"name": "mini-duel", "title": "mini-duel", "agents": 2,
                       "summary": "a tiny duel"}]
    read_call = ToolCall(id="2", name="read_scenario", arguments={"name": "mini-duel"})
    detail = json.loads(await tools.dispatch(read_call))
    assert detail["metadata"]["name"] == "mini-duel"
    assert [a["id"] for a in detail["agents"]] == ["alice", "bob"]


async def test_read_missing_scenario_is_a_friendly_message() -> None:
    store = FakePackageStore()
    tools, _ = _tools(store)
    call = ToolCall(id="1", name="read_scenario", arguments={"name": "nope"})
    result = await tools.dispatch(call)
    assert "nope" in result
    assert store.writes == []


async def test_validate_tool_reports_ok_and_problems() -> None:
    store = FakePackageStore()
    tools, _ = _tools(store)
    ok = await tools.dispatch(
        ToolCall(id="1", name="validate_scenario", arguments={"spec": _VALID_SPEC})
    )
    assert "valid" in ok.lower()
    bad = await tools.dispatch(
        ToolCall(
            id="2",
            name="validate_scenario",
            arguments={
                "spec": {
                    "metadata": {"name": "b"},
                    "spec": {"termination": [{"type": "duration", "limit": "30m"}]},
                    "agents": [{"id": "al", "persona": "Seal your vote on your turn."},
                               {"id": "bo", "persona": "x"}],
                }
            },
        )
    )
    assert "turns" in bad
    assert store.writes == []  # validate never writes


async def test_preview_changes_returns_a_diff() -> None:
    store = FakePackageStore({"mini-duel": _mini()})
    tools, _ = _tools(store)
    patch = {"project": {"spec": {"guidelines": "Be kind."}}}
    diff = await tools.dispatch(
        ToolCall(id="1", name="preview_changes", arguments={"name": "mini-duel", "patch": patch})
    )
    assert "guidelines" in diff
    assert "Be kind." in diff
    assert store.writes == []  # preview never writes


def test_tool_specs_cover_the_authoring_verbs() -> None:
    names = {t.name for t in TOOL_SPECS}
    assert names == {
        "list_scenarios",
        "read_scenario",
        "create_scenario",
        "edit_scenario",
        "validate_scenario",
        "list_skills",
        "list_tools",
        "read_skill",
        "preview_changes",
        "ask_user",
        "propose_scenario",
    }
    for t in TOOL_SPECS:
        assert t.parameters.get("type") == "object"


# --- granting tools while authoring (#59) ------------------------------------------------------
async def test_list_tools_reports_what_can_actually_be_granted(monkeypatch, tmp_path):
    """The assistant must not be able to invent a tool. It can only grant what this platform has,
    and it should be able to tell the user when a tool would need a key they have not added."""
    import json as _json

    monkeypatch.setenv("HOME", str(tmp_path))
    tools, _ = _tools(FakePackageStore())
    listed = _json.loads(await tools.dispatch(ToolCall(id="1", name="list_tools", arguments={})))
    names = {t["name"] for t in listed}
    assert "web_fetch" in names
    fetch = next(t for t in listed if t["name"] == "web_fetch")
    assert fetch["ready"] is True and fetch["needs_key_ref"] is None
    assert fetch["risk"] == "contained"
    assert fetch["description"]


def test_a_draft_granting_a_tool_that_does_not_exist_is_refused():
    """Well-formed but inert: the name passes the schema, the agent never sees the tool, and the
    prose still tells it to look things up. Caught at proposal time, it is a fixable draft."""
    from bearpit.scribe.tools import draft_problems

    spec = {
        "apiVersion": "bearpit/v1alpha1", "kind": "Project",
        "metadata": {"name": "research"},
        "spec": {"goals": ["find out"], "guidelines": "g",
                 "termination": [{"type": "duration", "limit": "1h"}]},
        "agents": [{"id": "analyst", "model_category": "medium", "persona": "p",
                    "tools": ["web_crawl"]}],
    }
    problems = draft_problems(spec)
    assert problems and "web_crawl" in problems and "analyst" in problems


def test_a_draft_granting_a_real_tool_passes():
    from bearpit.scribe.tools import draft_problems

    spec = {
        "apiVersion": "bearpit/v1alpha1", "kind": "Project",
        "metadata": {"name": "research"},
        "spec": {"goals": ["find out"], "guidelines": "g",
                 "termination": [{"type": "duration", "limit": "1h"}]},
        "agents": [{"id": "analyst", "model_category": "medium", "persona": "p",
                    "tools": ["web_fetch"]}],
    }
    assert draft_problems(spec) is None
