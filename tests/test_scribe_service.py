"""Service wiring + the `arealm assist` driver (Task 7).

build_scribe wires the real components; ScribeSession holds the running history so a multi-turn
conversation accumulates. The create flow is exercised end-to-end with fakes (no network).
"""

from __future__ import annotations

from pathlib import Path

from fakes import FakeLLMBackend, FakePackageStore
from typer.testing import CliRunner

from agentrealm.cli.main import app
from agentrealm.scribe.loop import ScribeLoop
from agentrealm.scribe.service import ScribeSession, build_scribe
from agentrealm.scribe.types import Completion, ToolCall, Usage

runner = CliRunner()

_VALID_SPEC = {
    "metadata": {"name": "duel"},
    "spec": {"goals": ["win"], "termination": [{"type": "duration", "limit": "30m"}]},
    "agents": [
        {"id": "alice", "persona": "You are Alice.", "goals": ["win"]},
        {"id": "bob", "persona": "You are Bob."},
    ],
}


def _tc(tool: str, **args: object) -> Completion:
    call = ToolCall(id=tool, name=tool, arguments=args)
    return Completion(text=None, tool_calls=[call], usage=Usage())


def test_build_scribe_returns_a_wired_loop(tmp_path: Path) -> None:
    loop = build_scribe("http://models.test/v1", tmp_path)
    assert isinstance(loop, ScribeLoop)


async def test_session_runs_a_create_end_to_end(tmp_path: Path) -> None:
    store = FakePackageStore()
    backend = FakeLLMBackend(
        [
            _tc("create_scenario", name="duel", spec=_VALID_SPEC),
            Completion(text="Created duel — ready to launch.", tool_calls=[], usage=Usage()),
        ]
    )
    loop = build_scribe("http://models.test/v1", tmp_path, backend=backend, store=store)
    session = ScribeSession(loop)
    events = [e async for e in session.send("make a two-agent duel called duel")]

    assert store.writes == ["duel"]
    assert events[-1].kind == "final"
    assert events[-1].text == "Created duel — ready to launch."
    # the whole turn is recorded in history for the next message (loop-owned appends)
    assert session.history[0].content == "make a two-agent duel called duel"
    assert session.history[-1].role == "assistant"
    assert session.history[-1].content == "Created duel — ready to launch."


async def test_guided_three_turn_exchange_keeps_history_wellformed(tmp_path: Path) -> None:
    """question -> answer -> question -> answer -> draft: each completion must receive the FULL
    well-formed conversation so far (tool calls answered, user replies in place)."""
    store = FakePackageStore()
    backend = FakeLLMBackend(
        [
            _tc("ask_user", question="How many agents?", options=["2", "3"]),
            _tc("ask_user", question="How does it end?", options=["verdict"]),
            _tc("propose_scenario", spec=_VALID_SPEC),
        ]
    )
    loop = build_scribe("http://models.test/v1", tmp_path, backend=backend, store=store)
    session = ScribeSession(loop)

    e1 = [e async for e in session.send("make a duel")]
    e2 = [e async for e in session.send("2")]
    e3 = [e async for e in session.send("verdict")]
    assert [e.kind for e in e1] == ["question"]
    assert [e.kind for e in e2] == ["question"]
    assert [e.kind for e in e3] == ["draft"]
    assert session.draft == _VALID_SPEC
    assert store.writes == []  # nothing written — approval is the platform's job

    # every completion saw a well-formed conversation: each tool call answered before the next
    # user message, and the newest user message last
    for i, expected_last in enumerate(["make a duel", "2", "verdict"]):
        messages = backend.calls[i][0]
        assert messages[-1].role == "user"
        assert messages[-1].content == expected_last
        answered = {m.tool_call_id for m in messages if m.role == "tool"}
        for m in messages:
            for call in m.tool_calls:
                assert call.id in answered
    # the second turn saw the first Q&A: assistant ask_user + tool result + the user's answer
    roles = [m.role for m in backend.calls[1][0]]
    assert roles == ["system", "user", "assistant", "tool", "user"]


def test_assist_command_is_registered() -> None:
    result = runner.invoke(app, ["assist", "--help"])
    assert result.exit_code == 0
    assert "assist" in result.output.lower() or "scribe" in result.output.lower()
