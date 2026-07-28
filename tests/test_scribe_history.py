"""Per-scenario persistent history + auto-summarization (#75, spec §19).

HistoryStore round-trips Message dataclasses (incl. tool plumbing) through plain JSON and never
crashes on a corrupt file. `compact` collapses the older portion into one visible summary message
without ever splitting an assistant tool_calls message from its answers. ScribeSession persists
after every completed turn, auto-compacts over the threshold (emitting the notice), resumes stored
history, and attaches a create-wizard history at bind time. All fakes, no network.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fakes import FakeLLMBackend, FakePackageStore

from bearpit.scribe.history import (
    SUMMARY_PREFIX,
    HistoryStore,
    compact,
    estimate_chars,
)
from bearpit.scribe.service import ScribeSession, build_scribe, visible_history
from bearpit.scribe.types import Completion, Message, ToolCall, Usage


def _final(text: str) -> Completion:
    return Completion(text=text, tool_calls=[], usage=Usage())


def _tool_turn(call_id: str = "c1") -> list[Message]:
    """An assistant tool_calls message + its role="tool" answer."""
    call = ToolCall(id=call_id, name="read_scenario", arguments={"name": "duel"})
    return [
        Message(role="assistant", content=None, tool_calls=[call]),
        Message(role="tool", content="{...}", tool_call_id=call_id),
    ]


# --- HistoryStore -------------------------------------------------------------


async def test_store_round_trips_tool_plumbing(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    history = [
        Message(role="user", content="make a duel"),
        *_tool_turn(),
        Message(role="assistant", content="Done."),
    ]
    await store.save("duel", history)
    assert await store.load("duel") == history
    assert (tmp_path / "history" / "duel.json").exists()  # plain JSON, inspectable


async def test_store_load_missing_is_empty(tmp_path: Path) -> None:
    assert await HistoryStore(tmp_path).load("nope") == []


async def test_store_corrupt_file_is_empty(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    (tmp_path / "history").mkdir(parents=True)
    (tmp_path / "history" / "duel.json").write_text("{not json")
    assert await store.load("duel") == []
    (tmp_path / "history" / "duel.json").write_text('{"a": 1}')  # json but not a list
    assert await store.load("duel") == []


async def test_store_delete(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    await store.save("duel", [Message(role="user", content="hi")])
    assert await store.has("duel")
    await store.delete("duel")
    assert not await store.has("duel")
    await store.delete("duel")  # deleting again is a no-op, not an error


async def test_store_rejects_path_traversal(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    for bad in ("../evil", "a/b", "", ".."):
        with pytest.raises(ValueError):
            await store.load(bad)


# --- estimate_chars + compact -------------------------------------------------


def test_estimate_chars_counts_tool_payloads() -> None:
    history = [Message(role="user", content="hi"), *_tool_turn()]
    # content "hi" (2) + tool result "{...}" (5) + call name + json args are all counted
    assert estimate_chars(history) > 2 + 5 + len("read_scenario")


async def test_compact_summarizes_old_and_keeps_tail() -> None:
    backend = FakeLLMBackend([_final("- decided: a duel\n- open: duration")])
    history = [Message(role="user", content=f"msg {i}") for i in range(10)]
    out = await compact(history, backend, "claude-sonnet-5", keep_tail=4)
    assert out[0].role == "user"
    assert out[0].content is not None
    assert out[0].content.startswith(SUMMARY_PREFIX)
    assert "- decided: a duel" in out[0].content
    assert out[1:] == history[-4:]
    # ONE completion, no tools offered
    assert len(backend.calls) == 1
    messages, tools, _model, _effort = backend.calls[0]
    assert tools == []
    assert messages[0].role == "system"
    assert "msg 0" in (messages[1].content or "")


async def test_compact_never_splits_a_tool_call_group() -> None:
    backend = FakeLLMBackend([_final("summary")])
    history = [
        Message(role="user", content="one"),
        Message(role="user", content="two"),
        *_tool_turn("c1"),  # boundary at keep_tail=1 would land on the role="tool" answer
    ]
    out = await compact(history, backend, "m", keep_tail=1)
    # the assistant call + its answer moved into the tail together
    assert [m.role for m in out] == ["user", "assistant", "tool"]
    assert out[1].tool_calls[0].id == "c1"


async def test_compact_keep_tail_zero_collapses_everything() -> None:
    backend = FakeLLMBackend([_final("just the summary")])
    history = [Message(role="user", content="a"), Message(role="assistant", content="b")]
    out = await compact(history, backend, "m", keep_tail=0)
    assert len(out) == 1
    assert out[0].content == f"{SUMMARY_PREFIX}\njust the summary"


async def test_compact_short_history_is_unchanged_no_llm_call() -> None:
    backend = FakeLLMBackend([])  # any completion request would blow up
    history = [Message(role="user", content="a")]
    assert await compact(history, backend, "m", keep_tail=12) == history
    assert backend.calls == []


# --- visible_history ----------------------------------------------------------


def test_visible_history_keeps_prose_and_questions_skips_plumbing() -> None:
    ask = ToolCall(id="q1", name="ask_user", arguments={"question": "How long?", "options": []})
    history = [
        Message(role="system", content="persona"),
        Message(role="user", content="make a duel"),
        Message(role="assistant", content=None, tool_calls=[ask]),
        Message(role="tool", content="(question shown)", tool_call_id="q1"),
        Message(role="user", content="SYSTEM REMINDER: your tools ARE available."),
        Message(role="assistant", content=""),  # empty carrier
        Message(role="assistant", content="Done."),
    ]
    assert visible_history(history) == [
        {"role": "user", "text": "make a duel"},
        {"role": "assistant", "text": "How long?"},
        {"role": "assistant", "text": "Done."},
    ]


# --- ScribeSession persistence ------------------------------------------------


def _session(
    tmp_path: Path,
    backend: FakeLLMBackend,
    *,
    scenario: str | None = None,
    max_chars: int | None = None,
) -> tuple[ScribeSession, HistoryStore]:
    loop = build_scribe(
        "http://models.test/v1", tmp_path, backend=backend, store=FakePackageStore()
    )
    store = HistoryStore(tmp_path)
    return (
        ScribeSession(loop, scenario=scenario, history_store=store, max_chars=max_chars),
        store,
    )


async def test_turn_persists_history_to_the_store(tmp_path: Path) -> None:
    session, store = _session(tmp_path, FakeLLMBackend([_final("Hello!")]), scenario="duel")
    _ = [e async for e in session.send("hi")]
    saved = await store.load("duel")
    assert [m.role for m in saved] == ["user", "assistant"]
    assert saved[-1].content == "Hello!"


async def test_over_threshold_compacts_first_and_emits_notice(tmp_path: Path) -> None:
    backend = FakeLLMBackend([_final("the summary"), _final("ok")])
    session, store = _session(tmp_path, backend, scenario="duel", max_chars=10)
    session.history = [Message(role="user", content=f"msg {i} " + "x" * 20) for i in range(20)]
    events = [e async for e in session.send("continue")]
    assert events[0].kind == "notice"
    assert "summarized" in events[0].text
    assert session.history[0].content is not None
    assert session.history[0].content.startswith(SUMMARY_PREFIX)
    assert len(session.history) == 1 + 12 + 2  # summary + kept tail + this turn's user/assistant
    assert (await store.load("duel"))[-1].content == "ok"  # post-turn save includes the reply


async def test_over_threshold_but_all_tail_is_left_alone(tmp_path: Path) -> None:
    """A short-but-huge history (≤ keep_tail messages) has nothing older to fold — no notice."""
    backend = FakeLLMBackend([_final("ok")])
    session, _ = _session(tmp_path, backend, scenario="duel", max_chars=10)
    session.history = [Message(role="user", content="x" * 50)]
    events = [e async for e in session.send("continue")]
    assert [e.kind for e in events] == ["final"]


async def test_under_threshold_does_not_compact(tmp_path: Path) -> None:
    backend = FakeLLMBackend([_final("ok")])
    session, _ = _session(tmp_path, backend, scenario="duel", max_chars=10_000)
    session.history = [Message(role="user", content="short")]
    events = [e async for e in session.send("continue")]
    assert [e.kind for e in events] == ["final"]


async def test_threshold_env_var_is_honored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SCRIBE_HISTORY_MAX_CHARS", "10")
    backend = FakeLLMBackend([_final("the summary"), _final("ok")])
    session, _ = _session(tmp_path, backend, scenario="duel")  # no max_chars param -> env
    session.history = [Message(role="user", content=f"msg {i}") for i in range(20)]
    events = [e async for e in session.send("continue")]
    assert events[0].kind == "notice"


async def test_resume_loads_stored_history(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    prior = [Message(role="user", content="earlier"), Message(role="assistant", content="yes")]
    await store.save("duel", prior)
    loop = build_scribe(
        "http://models.test/v1", tmp_path, backend=FakeLLMBackend([]), store=FakePackageStore()
    )
    session = await ScribeSession.resume(loop, scenario="duel", history_store=store)
    assert session.history == prior


async def test_bind_saves_a_create_session_under_the_new_name(tmp_path: Path) -> None:
    session, store = _session(tmp_path, FakeLLMBackend([_final("drafting...")]))  # unbound
    _ = [e async for e in session.send("make a duel")]
    assert not await store.has("duel")  # nothing persisted while unbound
    await session.bind("duel")
    saved = await store.load("duel")
    assert saved[0].content == "make a duel"


async def test_compact_now_collapses_and_saves(tmp_path: Path) -> None:
    backend = FakeLLMBackend([_final("everything, condensed")])
    session, store = _session(tmp_path, backend, scenario="duel")
    session.history = [
        Message(role="user", content="a"),
        Message(role="assistant", content="b"),
        *_tool_turn(),
    ]
    await session.compact_now()
    assert len(session.history) == 1
    assert session.history[0].content == f"{SUMMARY_PREFIX}\neverything, condensed"
    assert await store.load("duel") == session.history
