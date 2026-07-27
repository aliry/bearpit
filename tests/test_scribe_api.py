"""Guided-authoring endpoints (#73, #75): session open, NDJSON message stream, deterministic
approve, and per-scenario history persistence (resume, reuse, delete, summarize).

FastAPI TestClient over `create_app` with an injected fake backend/store/root — no network, no
network, no writes outside tmp_path. The load-bearing behaviours: openings are canned (no model
call), the model never writes in UI mode, and approve is the only create-path write.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import FakeLLMBackend, FakePackageStore
from starlette.testclient import TestClient

from agentrealm.chronicle import Chronicle
from agentrealm.core.schema import (
    AgentSpec,
    Project,
    ProjectMeta,
    ProjectSpec,
    TerminationCondition,
    TerminationKind,
)
from agentrealm.gatekeeper.api import create_app
from agentrealm.scribe.types import Completion, ToolCall, Usage

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


def _mini(name: str = "mini-duel") -> Project:
    return Project(
        metadata=ProjectMeta(name=name, description="a tiny duel"),
        spec=ProjectSpec(
            goals=["settle it"],
            termination=[TerminationCondition(type=TerminationKind.DURATION, limit="30m")],
        ),
        agents=[
            AgentSpec(id="alice", persona="You are Alice.", goals=["win"]),
            AgentSpec(id="bob", persona="You are Bob."),
        ],
    )


@pytest.fixture
async def chron() -> Any:
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    yield c
    await c.close()


def _client(
    chron: Chronicle, tmp_path: Path, backend: FakeLLMBackend, store: FakePackageStore
) -> TestClient:
    app = create_app(
        chron=chron, scribe_backend=backend, scribe_store=store, scribe_root=tmp_path / "scribe"
    )
    return TestClient(app)


def _ndjson(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_create_session_returns_a_canned_opening(chron: Chronicle, tmp_path: Path) -> None:
    backend = FakeLLMBackend([])  # any completion request would blow up — openings are canned
    with _client(chron, tmp_path, backend, FakePackageStore()) as c:
        r = c.post("/api/scribe/session", json={"mode": "create"})
        assert r.status_code == 200
        body = r.json()
        assert body["session_id"]
        assert body["opening"]["kind"] == "question"
        assert "short description" in body["opening"]["text"]
        assert body["opening"]["options"] == []
        assert backend.calls == []  # deterministic — NO model call


def test_edit_session_returns_package_and_opening(chron: Chronicle, tmp_path: Path) -> None:
    store = FakePackageStore({"mini-duel": _mini()})
    backend = FakeLLMBackend([])
    with _client(chron, tmp_path, backend, store) as c:
        r = c.post("/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"})
        assert r.status_code == 200
        body = r.json()
        assert "mini-duel" in body["opening"]["text"]
        assert body["package"]["metadata"]["name"] == "mini-duel"
        assert [a["id"] for a in body["package"]["agents"]] == ["alice", "bob"]
        assert backend.calls == []


def test_edit_session_unknown_scenario_is_404(chron: Chronicle, tmp_path: Path) -> None:
    with _client(chron, tmp_path, FakeLLMBackend([]), FakePackageStore()) as c:
        r = c.post("/api/scribe/session", json={"mode": "edit", "scenario": "nope"})
        assert r.status_code == 404


def test_bad_mode_is_400(chron: Chronicle, tmp_path: Path) -> None:
    with _client(chron, tmp_path, FakeLLMBackend([]), FakePackageStore()) as c:
        assert c.post("/api/scribe/session", json={"mode": "wat"}).status_code == 400


def test_message_requires_an_existing_session(chron: Chronicle, tmp_path: Path) -> None:
    with _client(chron, tmp_path, FakeLLMBackend([]), FakePackageStore()) as c:
        r = c.post("/api/scribe/message", json={"session_id": "s-nope", "text": "hi"})
        assert r.status_code == 404


def test_message_streams_question_and_draft_events(chron: Chronicle, tmp_path: Path) -> None:
    backend = FakeLLMBackend(
        [
            _tc("ask_user", question="How long?", options=["30m", "1h"]),
            _tc("propose_scenario", spec=_VALID_SPEC),
        ]
    )
    store = FakePackageStore()
    with _client(chron, tmp_path, backend, store) as c:
        sid = c.post("/api/scribe/session", json={"mode": "create"}).json()["session_id"]

        r = c.post("/api/scribe/message", json={"session_id": sid, "text": "make a duel"})
        assert r.status_code == 200
        assert "application/x-ndjson" in r.headers["content-type"]
        events = _ndjson(r.text)
        assert [e["kind"] for e in events] == ["question"]
        assert events[0]["text"] == "How long?"
        assert json.loads(events[0]["name"]) == ["30m", "1h"]

        r = c.post("/api/scribe/message", json={"session_id": sid, "text": "30m"})
        events = _ndjson(r.text)
        assert [e["kind"] for e in events] == ["draft"]
        assert json.loads(events[0]["text"]) == _VALID_SPEC
        assert store.writes == []  # a draft is NOT a write


def test_approve_with_no_draft_is_409(chron: Chronicle, tmp_path: Path) -> None:
    with _client(chron, tmp_path, FakeLLMBackend([]), FakePackageStore()) as c:
        sid = c.post("/api/scribe/session", json={"mode": "create"}).json()["session_id"]
        r = c.post("/api/scribe/approve", json={"session_id": sid})
        assert r.status_code == 409


def test_approve_unknown_session_is_404(chron: Chronicle, tmp_path: Path) -> None:
    with _client(chron, tmp_path, FakeLLMBackend([]), FakePackageStore()) as c:
        assert c.post("/api/scribe/approve", json={"session_id": "s-no"}).status_code == 404


def test_approve_writes_the_stashed_draft(chron: Chronicle, tmp_path: Path) -> None:
    backend = FakeLLMBackend([_tc("propose_scenario", spec=_VALID_SPEC)])
    store = FakePackageStore()
    with _client(chron, tmp_path, backend, store) as c:
        sid = c.post("/api/scribe/session", json={"mode": "create"}).json()["session_id"]
        c.post("/api/scribe/message", json={"session_id": sid, "text": "make a duel"})

        r = c.post("/api/scribe/approve", json={"session_id": sid})
        assert r.status_code == 200
        assert r.json() == {"name": "duel"}
        assert store.writes == ["duel"]
        # the write was snapshotted (pre-create) in the session's version store
        snaps = list((tmp_path / "scribe" / "versions" / "duel").glob("*.json"))
        assert len(snaps) == 1


def test_approve_existing_user_scenario_is_409(chron: Chronicle, tmp_path: Path) -> None:
    backend = FakeLLMBackend([_tc("propose_scenario", spec=_VALID_SPEC)])
    store = FakePackageStore({"duel": _mini("duel")})  # the name is already taken
    with _client(chron, tmp_path, backend, store) as c:
        sid = c.post("/api/scribe/session", json={"mode": "create"}).json()["session_id"]
        c.post("/api/scribe/message", json={"session_id": sid, "text": "make a duel"})

        r = c.post("/api/scribe/approve", json={"session_id": sid})
        assert r.status_code == 409
        assert store.writes == []


def test_approve_is_create_mode_only(chron: Chronicle, tmp_path: Path) -> None:
    store = FakePackageStore({"mini-duel": _mini()})
    with _client(chron, tmp_path, FakeLLMBackend([]), store) as c:
        sid = c.post(
            "/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"}
        ).json()["session_id"]
        r = c.post("/api/scribe/approve", json={"session_id": sid})
        assert r.status_code == 409


# --- per-scenario history persistence (#75, spec §19) --------------------------


def _final(text: str) -> Completion:
    return Completion(text=text, tool_calls=[], usage=Usage())


def _history_file(tmp_path: Path, scenario: str) -> Path:
    return tmp_path / "scribe" / "history" / f"{scenario}.json"


def _seed_history(tmp_path: Path, scenario: str, messages: list[dict[str, Any]]) -> None:
    f = _history_file(tmp_path, scenario)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(messages))


def test_edit_session_resumes_stored_history(chron: Chronicle, tmp_path: Path) -> None:
    _seed_history(tmp_path, "mini-duel", [
        {"role": "user", "content": "earlier message"},
        {"role": "assistant", "content": "earlier reply"},
        {"role": "tool", "content": "plumbing", "tool_call_id": "x"},
    ])
    backend = FakeLLMBackend([_final("continuing")])
    store = FakePackageStore({"mini-duel": _mini()})
    with _client(chron, tmp_path, backend, store) as c:
        r = c.post("/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"})
        assert r.status_code == 200
        assert r.json()["history"] == [  # the visible thread only — no tool plumbing
            {"role": "user", "text": "earlier message"},
            {"role": "assistant", "text": "earlier reply"},
        ]
        # the next turn continues FROM the stored history
        sid = r.json()["session_id"]
        c.post("/api/scribe/message", json={"session_id": sid, "text": "more"})
        sent = backend.calls[0][0]
        assert [m.content for m in sent if m.role == "user"][0] == "earlier message"


def test_edit_session_is_reused_per_scenario(chron: Chronicle, tmp_path: Path) -> None:
    """Revisiting the assist page must resume the SAME live session, not mint an orphan."""
    backend = FakeLLMBackend([_final("noted")])
    store = FakePackageStore({"mini-duel": _mini()})
    with _client(chron, tmp_path, backend, store) as c:
        first = c.post("/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"})
        sid = first.json()["session_id"]
        assert first.json()["history"] == []
        c.post("/api/scribe/message", json={"session_id": sid, "text": "shorten it"})

        again = c.post("/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"})
        assert again.json()["session_id"] == sid  # the same live session
        assert again.json()["history"] == [
            {"role": "user", "text": "shorten it"},
            {"role": "assistant", "text": "noted"},
        ]


def test_edit_turn_persists_history_across_sessions(chron: Chronicle, tmp_path: Path) -> None:
    backend = FakeLLMBackend([_final("done")])
    store = FakePackageStore({"mini-duel": _mini()})
    with _client(chron, tmp_path, backend, store) as c:
        sid = c.post(
            "/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"}
        ).json()["session_id"]
        c.post("/api/scribe/message", json={"session_id": sid, "text": "tweak it"})
        saved = json.loads(_history_file(tmp_path, "mini-duel").read_text())
        assert [m["role"] for m in saved] == ["user", "assistant"]


def test_approve_attaches_history_to_the_new_scenario(chron: Chronicle, tmp_path: Path) -> None:
    backend = FakeLLMBackend([_tc("propose_scenario", spec=_VALID_SPEC)])
    with _client(chron, tmp_path, backend, FakePackageStore()) as c:
        sid = c.post("/api/scribe/session", json={"mode": "create"}).json()["session_id"]
        c.post("/api/scribe/message", json={"session_id": sid, "text": "make a duel"})
        assert not _history_file(tmp_path, "duel").exists()  # unbound until approve

        assert c.post("/api/scribe/approve", json={"session_id": sid}).status_code == 200
        saved = json.loads(_history_file(tmp_path, "duel").read_text())
        assert saved[0] == {"role": "user", "content": "make a duel"}


def test_history_delete_removes_file_and_live_session(chron: Chronicle, tmp_path: Path) -> None:
    backend = FakeLLMBackend([_final("ok")])
    store = FakePackageStore({"mini-duel": _mini()})
    with _client(chron, tmp_path, backend, store) as c:
        sid = c.post(
            "/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"}
        ).json()["session_id"]
        c.post("/api/scribe/message", json={"session_id": sid, "text": "hi"})
        assert _history_file(tmp_path, "mini-duel").exists()

        r = c.delete("/api/scribe/history/mini-duel")
        assert r.status_code == 200
        assert r.json() == {"deleted": "mini-duel"}
        assert not _history_file(tmp_path, "mini-duel").exists()
        # the live session was dropped: the old sid is gone and a re-open mints a fresh one
        assert c.post(
            "/api/scribe/message", json={"session_id": sid, "text": "hi"}
        ).status_code == 404
        fresh = c.post("/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"})
        assert fresh.json()["session_id"] != sid
        assert fresh.json()["history"] == []


def test_history_delete_unknown_scenario_is_404(chron: Chronicle, tmp_path: Path) -> None:
    with _client(chron, tmp_path, FakeLLMBackend([]), FakePackageStore()) as c:
        assert c.delete("/api/scribe/history/nope").status_code == 404


def test_summarize_live_session_collapses_thread(chron: Chronicle, tmp_path: Path) -> None:
    backend = FakeLLMBackend([_final("noted"), _final("- wants a shorter duel")])
    store = FakePackageStore({"mini-duel": _mini()})
    with _client(chron, tmp_path, backend, store) as c:
        sid = c.post(
            "/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"}
        ).json()["session_id"]
        c.post("/api/scribe/message", json={"session_id": sid, "text": "shorten it"})

        r = c.post("/api/scribe/history/mini-duel/summarize")
        assert r.status_code == 200
        (only,) = r.json()["history"]
        assert only["role"] == "user"
        assert only["text"].startswith("(Conversation so far, summarized)")
        assert "- wants a shorter duel" in only["text"]
        # both the live session and the stored file now hold just the summary
        saved = json.loads(_history_file(tmp_path, "mini-duel").read_text())
        assert len(saved) == 1
        again = c.post("/api/scribe/session", json={"mode": "edit", "scenario": "mini-duel"})
        assert again.json()["session_id"] == sid
        assert again.json()["history"] == r.json()["history"]


def test_summarize_stored_history_without_live_session(chron: Chronicle, tmp_path: Path) -> None:
    _seed_history(tmp_path, "mini-duel", [
        {"role": "user", "content": "make it meaner"},
        {"role": "assistant", "content": "done"},
    ])
    backend = FakeLLMBackend([_final("- meaner duel agreed")])
    store = FakePackageStore({"mini-duel": _mini()})
    with _client(chron, tmp_path, backend, store) as c:
        r = c.post("/api/scribe/history/mini-duel/summarize")
        assert r.status_code == 200
        (only,) = r.json()["history"]
        assert "- meaner duel agreed" in only["text"]
        saved = json.loads(_history_file(tmp_path, "mini-duel").read_text())
        assert len(saved) == 1


def test_summarize_unknown_scenario_is_404(chron: Chronicle, tmp_path: Path) -> None:
    with _client(chron, tmp_path, FakeLLMBackend([]), FakePackageStore()) as c:
        assert c.post("/api/scribe/history/nope/summarize").status_code == 404
