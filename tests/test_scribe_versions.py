"""Version snapshots + the preview diff (Task 5).

Every write snapshots the prior state first, so a direct-apply edit is always revertible. The
timestamp comes from an injected clock so ids are deterministic in tests.
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeMemory, FakePackageStore

from agentrealm.core.schema import (
    AgentSpec,
    Project,
    ProjectMeta,
    ProjectSpec,
    TerminationCondition,
    TerminationKind,
)
from agentrealm.scribe.tools import AuthoringTools
from agentrealm.scribe.types import ToolCall
from agentrealm.scribe.versions import Versions, diff_projects


def _dur() -> TerminationCondition:
    return TerminationCondition(type=TerminationKind.DURATION, limit="30m")


def _mini(desc: str = "a tiny duel") -> Project:
    return Project(
        metadata=ProjectMeta(name="mini-duel", description=desc),
        spec=ProjectSpec(goals=["settle it"], termination=[_dur()]),
        agents=[
            AgentSpec(id="alice", persona="You are Alice.", goals=["win"]),
            AgentSpec(id="bob", persona="You are Bob."),
        ],
    )


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


async def test_snapshot_then_list_returns_it(tmp_path: Path) -> None:
    versions = Versions(tmp_path, clock=_Clock())
    vid = await versions.snapshot("mini-duel", _mini())
    listed = await versions.list("mini-duel")
    assert [v["id"] for v in listed] == [vid]
    assert listed[0]["pre_create"] is False


async def test_pre_create_snapshot_is_marked(tmp_path: Path) -> None:
    versions = Versions(tmp_path, clock=_Clock())
    vid = await versions.snapshot("new-one", None)
    listed = await versions.list("new-one")
    assert listed[0]["id"] == vid
    assert listed[0]["pre_create"] is True


async def test_revert_restores_prior_via_the_store(tmp_path: Path) -> None:
    versions = Versions(tmp_path, clock=_Clock())
    store = FakePackageStore()
    vid = await versions.snapshot("mini-duel", _mini(desc="the original"))
    await versions.revert("mini-duel", vid, store)
    assert store.writes == ["mini-duel"]
    assert store._projects["mini-duel"].metadata.description == "the original"


def test_diff_projects_shows_added_removed_and_changed() -> None:
    before = _mini(desc="a tiny duel")
    after = Project(
        metadata=ProjectMeta(name="mini-duel", description="a bigger duel"),
        spec=ProjectSpec(
            goals=["settle it", "and fast"], guidelines="Be kind.", termination=[_dur()]
        ),
        agents=before.agents,
    )
    diff = diff_projects(before, after)
    assert "~ metadata.description" in diff
    assert "+ spec.guidelines" in diff
    assert "+ spec.goals.1" in diff


def test_diff_projects_from_nothing_is_all_additions() -> None:
    diff = diff_projects(None, _mini())
    assert all(line.startswith("+") for line in diff.splitlines())
    assert "+ metadata.name" in diff


async def test_authoring_tools_snapshot_to_disk_on_edit(tmp_path: Path) -> None:
    """Wiring: AuthoringTools with the REAL Versions snapshots the prior state before writing."""
    versions = Versions(tmp_path / "versions", clock=_Clock())
    store = FakePackageStore({"mini-duel": _mini()})
    tools = AuthoringTools(store, versions, FakeMemory())
    patch = {"project": {"spec": {"guidelines": "Be kind."}}}
    await tools.dispatch(
        ToolCall(id="1", name="edit_scenario", arguments={"name": "mini-duel", "patch": patch})
    )
    listed = await versions.list("mini-duel")
    assert len(listed) == 1
    assert listed[0]["pre_create"] is False  # the prior state was captured
