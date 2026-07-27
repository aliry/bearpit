"""The authoring loop (Task 6 + spec §18 guided tools).

Drives context assembly -> complete() -> tool dispatch -> loop, against a fully-scripted
FakeLLMBackend (no network), asserting the tools actually ran and the final text was streamed.
The loop owns every history append except the user's message, so the well-formedness assertions
here are what keep multi-turn guided conversations coherent.
"""

from __future__ import annotations

import json

from fakes import FakeLLMBackend, FakeMemory, FakePackageStore, FakeVersions

from agentrealm.scribe.loop import ScribeLoop
from agentrealm.scribe.tools import AuthoringTools
from agentrealm.scribe.types import Completion, Message, ToolCall, Usage

_VALID_SPEC = {
    "metadata": {"name": "duel"},
    "spec": {"goals": ["win"], "termination": [{"type": "duration", "limit": "30m"}]},
    "agents": [
        {"id": "alice", "persona": "You are Alice.", "goals": ["win"]},
        {"id": "bob", "persona": "You are Bob."},
    ],
}

# Schema-valid but contract-invalid: a verdict-ending referee whose rubric names no tool.
_CONTRACT_INVALID_SPEC = {
    "metadata": {"name": "bad"},
    "spec": {"termination": []},
    "agents": [
        {
            "id": "judge",
            "role": "referee",
            "rubric": "Judge the debate and declare a winner.",
            "powers": {"verdict_ends_realm": True},
        },
        {"id": "al", "persona": "x"},
    ],
}


def _tc(tool: str, **args: object) -> Completion:
    call = ToolCall(id=tool, name=tool, arguments=args)
    return Completion(text=None, tool_calls=[call], usage=Usage())


def _final(text: str) -> Completion:
    return Completion(text=text, tool_calls=[], usage=Usage())


def _user(text: str) -> Message:
    return Message(role="user", content=text)


def _loop(backend: FakeLLMBackend, store: FakePackageStore, **kw: int) -> ScribeLoop:
    tools = AuthoringTools(store, FakeVersions(), FakeMemory())
    return ScribeLoop(backend, tools, FakeMemory(), persona="You are Scribe.", **kw)


def _assert_wellformed(history: list[Message]) -> None:
    """Every assistant tool_call id must be answered by a following role='tool' message."""
    answered = {m.tool_call_id for m in history if m.role == "tool"}
    for m in history:
        for call in m.tool_calls:
            assert call.id in answered, f"tool call {call.id!r} has no tool result"


async def test_turn_runs_tools_then_yields_final_text() -> None:
    store = FakePackageStore()
    backend = FakeLLMBackend(
        [
            _tc("validate_scenario", spec=_VALID_SPEC),
            _tc("create_scenario", name="duel", spec=_VALID_SPEC),
            _final("Done — created duel."),
        ]
    )
    loop = _loop(backend, store)
    history = [_user("make a duel")]
    events = [e async for e in loop.turn(history)]

    assert store.writes == ["duel"]  # the tool actually ran inside the turn
    kinds = [e.kind for e in events]
    assert kinds.count("tool_call") == 2
    assert kinds.count("tool_result") == 2
    assert events[-1].kind == "final"
    assert events[-1].text == "Done — created duel."
    tool_names = [e.name for e in events if e.kind == "tool_call"]
    assert tool_names == ["validate_scenario", "create_scenario"]
    # the loop recorded the whole turn in the passed-in history
    _assert_wellformed(history)
    assert history[-1].role == "assistant"
    assert history[-1].content == "Done — created duel."


async def test_turn_assembles_persona_and_memory_into_context() -> None:
    store = FakePackageStore()
    backend = FakeLLMBackend([_final("hi")])
    tools = AuthoringTools(store, FakeVersions(), FakeMemory())
    memory = FakeMemory(["prefer sealed votes"])
    loop = ScribeLoop(backend, tools, memory, persona="You are Scribe.")
    history = [_user("earlier"), Message(role="assistant", content="ok"), _user("make a duel")]
    _ = [e async for e in loop.turn(history)]

    sent_messages = backend.calls[0][0]
    assert sent_messages[0].role == "system"
    assert "You are Scribe." in (sent_messages[0].content or "")
    assert "prefer sealed votes" in (sent_messages[0].content or "")
    # prior history + the new user message are appended after the system prompt
    assert sent_messages[1].content == "earlier"
    assert sent_messages[-1].content == "make a duel"


async def test_turn_folds_context_note_into_the_system_prompt() -> None:
    backend = FakeLLMBackend([_final("ok")])
    loop = _loop(backend, FakePackageStore())
    _ = [e async for e in loop.turn([_user("hi")], context_note="Current scenario 'duel': {}")]

    system = backend.calls[0][0][0]
    assert "Current scenario 'duel'" in (system.content or "")


async def test_max_steps_guard_stops_a_runaway_loop() -> None:
    store = FakePackageStore()
    # A backend that only ever asks for another tool call — the loop must not spin forever.
    backend = FakeLLMBackend([_tc("list_scenarios") for _ in range(3)])
    loop = _loop(backend, store, max_steps=3)
    events = [e async for e in loop.turn([_user("go")])]

    assert len(backend.calls) == 3  # stopped exactly at the limit, never over-called
    assert events[-1].kind == "final"
    assert "stop" in events[-1].text.lower()


# --- ask_user: ends the turn, hands the floor to the user -------------------------------------
async def test_ask_user_ends_the_turn_and_yields_the_question_with_options() -> None:
    backend = FakeLLMBackend(
        [_tc("ask_user", question="How should it end?", options=["referee verdict", "duration"])]
    )
    loop = _loop(backend, FakePackageStore())
    history = [_user("make a duel")]
    events = [e async for e in loop.turn(history)]

    assert len(backend.calls) == 1  # the turn ended on the question — no further completion
    assert [e.kind for e in events] == ["question"]
    assert events[0].text == "How should it end?"
    assert events[0].name == '["referee verdict", "duration"]'
    # history stays well-formed: the tool call got a synthetic answer, ready for the user's reply
    _assert_wellformed(history)
    assert history[-1].role == "tool"
    assert "user's next message" in (history[-1].content or "")


# --- propose_scenario: validate without writing --------------------------------------------
async def test_propose_scenario_invalid_feeds_errors_back_and_continues() -> None:
    store = FakePackageStore()
    backend = FakeLLMBackend(
        [
            _tc("propose_scenario", spec=_CONTRACT_INVALID_SPEC),
            _tc("propose_scenario", spec=_VALID_SPEC),
        ]
    )
    loop = _loop(backend, store)
    history = [_user("make a duel")]
    events = [e async for e in loop.turn(history)]

    assert store.writes == []  # propose NEVER writes
    assert len(backend.calls) == 2  # invalid propose continued the loop; valid one ended it
    results = [e for e in events if e.kind == "tool_result"]
    assert len(results) == 1
    assert "tool-based state changes" in results[0].text
    # the errors went back to the model as the tool result of the failed propose
    retry_messages = backend.calls[1][0]
    assert any(
        m.role == "tool" and "tool-based state changes" in (m.content or "")
        for m in retry_messages
    )
    assert events[-1].kind == "draft"


async def test_propose_scenario_valid_yields_the_draft_and_ends_the_turn() -> None:
    backend = FakeLLMBackend([_tc("propose_scenario", spec=_VALID_SPEC)])
    loop = _loop(backend, FakePackageStore())
    history = [_user("make a duel")]
    events = [e async for e in loop.turn(history)]

    assert len(backend.calls) == 1
    assert [e.kind for e in events] == ["draft"]
    assert json.loads(events[0].text) == _VALID_SPEC
    _assert_wellformed(history)
    assert "approval" in (history[-1].content or "")


# --- anti-bail (#74): tool-denial completions get a corrective retry --------------------------
async def test_bail_text_gets_a_corrective_retry_and_the_task_completes() -> None:
    store = FakePackageStore()
    backend = FakeLLMBackend(
        [
            _final("I don't have access to the tools in this session, so I can't proceed."),
            _tc("create_scenario", name="duel", spec=_VALID_SPEC),
            _final("Created duel."),
        ]
    )
    loop = _loop(backend, store)
    history = [_user("make a duel")]
    events = [e async for e in loop.turn(history)]

    assert store.writes == ["duel"]  # the nudge got the model back on task
    assert [e.kind for e in events if e.kind == "notice"] == ["notice"]
    assert "nudged" in next(e.text for e in events if e.kind == "notice")
    assert events[-1].kind == "final"
    assert events[-1].text == "Created duel."
    # the retried completion saw the corrective reminder as the newest user message
    retry_messages = backend.calls[1][0]
    assert retry_messages[-1].role == "user"
    assert "ARE available" in (retry_messages[-1].content or "")


async def test_persistent_bail_surfaces_as_final_after_bounded_retries() -> None:
    bail = "The Scribe tools aren't wired into this session."
    backend = FakeLLMBackend([_final(bail), _final(bail), _final(bail)])
    loop = _loop(backend, FakePackageStore())
    events = [e async for e in loop.turn([_user("go")])]

    assert len(backend.calls) == 3  # original + exactly 2 retries
    assert [e.kind for e in events] == ["notice", "notice", "final"]
    assert events[-1].text == bail  # after bounded retries the text is let through


async def test_normal_final_text_is_not_mistaken_for_a_bail() -> None:
    backend = FakeLLMBackend([_final("Here's a draft plan — shall I build it?")])
    loop = _loop(backend, FakePackageStore())
    events = [e async for e in loop.turn([_user("plan a duel")])]

    assert len(backend.calls) == 1
    assert [e.kind for e in events] == ["final"]


async def test_fabricated_action_claim_gets_a_corrective_retry() -> None:
    # seen live: "Proposed the full updated manifest for your review" with ZERO tool calls —
    # the loop must nudge the model into actually calling propose_scenario
    backend = FakeLLMBackend(
        [
            _final("Added Zara as a fourth explorer. Proposed the full updated manifest."),
            _tc("propose_scenario", spec=_VALID_SPEC),
        ]
    )
    loop = _loop(backend, FakePackageStore())
    events = [e async for e in loop.turn([_user("add Zara")])]

    assert "no tool was called" in next(e.text for e in events if e.kind == "notice")
    assert events[-1].kind == "draft"  # the retry produced the real proposal
    retry_messages = backend.calls[1][0]
    assert "Prose does nothing" in (retry_messages[-1].content or "")


async def test_action_claim_after_a_real_tool_call_is_not_nudged() -> None:
    # once the turn HAS executed a tool, a summary like "I've updated it" is legitimate
    backend = FakeLLMBackend(
        [
            _tc("list_scenarios"),
            _final("I've updated my notes — ready when you are."),
        ]
    )
    loop = _loop(backend, FakePackageStore())
    events = [e async for e in loop.turn([_user("hi")])]

    assert len(backend.calls) == 2  # no third (nudge) completion
    assert events[-1].kind == "final"


async def test_broken_tool_call_markup_gets_a_corrective_retry() -> None:
    # a failed tool EMISSION (raw <tool_call> markup surviving as text, e.g. truncated JSON past
    # no JSON repair could rescue) must never reach the user — the loop re-requests the call
    store = FakePackageStore()
    backend = FakeLLMBackend(
        [
            _final('<tool_call>{"name": "create_scenario", "arguments": {broken'),
            _tc("create_scenario", name="duel", spec=_VALID_SPEC),
            _final("Created duel."),
        ]
    )
    loop = _loop(backend, store)
    events = [e async for e in loop.turn([_user("make a duel")])]

    assert store.writes == ["duel"]
    assert "malformed tool call" in next(e.text for e in events if e.kind == "notice")
    assert events[-1].kind == "final" and events[-1].text == "Created duel."
    # the retry saw the corrective instruction to re-emit valid JSON
    retry_messages = backend.calls[1][0]
    assert "malformed" in (retry_messages[-1].content or "")
