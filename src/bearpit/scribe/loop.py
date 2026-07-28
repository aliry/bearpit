"""The Scribe conversation loop (§6) — a standard tool-use loop the service owns.

For each user turn: assemble context (persona + recalled memory + the running history, which must
already end with the user's newest message), call `backend.complete(..., TOOL_SPECS)`, dispatch any
tool calls through `AuthoringTools`, append the results, and loop until the model returns final
text with no tool calls (or `max_steps` is hit). The LOOP owns every history append (assistant
tool_calls, tool results, the final text) so the conversation stays well-formed even across turns
that end early on a question or draft; the session appends only the user's message.

Two tools end the turn instead of looping (spec §18): `ask_user` hands the floor back to the user
(the UI renders the question + option chips), and `propose_scenario` validates a full draft without
writing — valid drafts go to the user for approval, invalid ones bounce back as the tool result so
the model fixes and re-proposes. The loop also detects "identity bail" completions (the model
claiming its tools are unavailable — a system-prompt conflict, #74) and retries them with a
corrective reminder, bounded, before letting the text through.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from bearpit.scribe.backend import LLMBackend
from bearpit.scribe.tools import TOOL_SPECS, AuthoringTools, MemoryStore, draft_problems
from bearpit.scribe.types import Message, ToolCall


@dataclass(frozen=True)
class LoopEvent:
    """One streamed event from a turn.

    `kind` is one of: `text` (assistant prose before/without tool calls), `tool_call` (a tool is
    about to run — `name` is the tool, `text` a compact arg preview), `tool_result` (the tool's
    result string, in `text`), `question` (ask_user ended the turn — the question in `text`, the
    suggested options as a JSON array in `name`), `draft` (propose_scenario ended the turn — the
    validated manifest as JSON in `text`), `notice` (a loop-level aside, e.g. an anti-bail nudge),
    `final` (the turn's closing reply, in `text`).
    """

    kind: str
    text: str = ""
    name: str | None = None


def _arg_preview(call: ToolCall) -> str:
    try:
        blob = json.dumps(call.arguments)
    except (TypeError, ValueError):
        blob = str(call.arguments)
    return blob if len(blob) <= 200 else blob[:197] + "..."


# --- anti-bail (#74): a completion with no tool calls whose text claims the tools don't work ----
_BAIL_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"don't have (access to )?(the |these |those )?tools",
        r"tools (aren't|are not) (wired|available)",
        r"can't actually (write|create|call)",
        r"not available in this session",
        r"if you run this in",
    )
)
_BAIL_REMINDER = (
    "SYSTEM REMINDER: your authoring tools (list_scenarios, read_scenario, validate_scenario, "
    "propose_scenario, ask_user, create_scenario, edit_scenario, preview_changes) ARE available "
    "in this session and work. Do not claim otherwise. Continue the task now by calling the "
    "appropriate tool."
)
# A failed tool EMISSION: the model produced tool-call markup that didn't parse into a call (seen
# live: a 4.2 KB propose_scenario with a dropped closing brace that no JSON repair could rescue).
# Letting it through would show the user raw JSON — nudge the model to re-emit instead.
_BROKEN_CALL_REMINDER = (
    "SYSTEM REMINDER: your last tool call was malformed (invalid JSON) and was NOT executed — "
    "nothing happened. Re-emit the same tool call now, as strictly valid JSON: every brace and "
    "bracket closed, all strings terminated. Do not apologize or explain; just make the call."
)
# A FABRICATED action: the model claims it proposed/updated/built something — or announces it is
# about to — but the turn produced no draft and no question (seen live: "Proposed the full updated
# manifest for your review" after 6.8K reasoning tokens and ZERO tool calls). Prose changes
# nothing; nudge it to actually make the call.
_CLAIM_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bproposed\b",
        r"\bdraft (is )?ready\b",
        r"for your review",
        r"\bI'?ve (added|updated|created|built|drafted|shortened|renamed|removed)\b",
        r"\bI'?ll (build|create|draft|propose)\b",
        r"\blet me (build|create|draft|propose)\b",
    )
)
_FABRICATION_REMINDER = (
    "SYSTEM REMINDER: you described a proposal or change, but you called NO tool this turn — "
    "nothing was proposed and the user saw no draft. Prose does nothing. Call propose_scenario "
    "NOW with the complete manifest (or ask_user if you genuinely need an answer first). The "
    "summary text comes AFTER the call, never instead of it."
)
_MAX_NUDGES = 2  # corrective retries per turn before the bail text is let through as final


def _is_bail(text: str) -> bool:
    plain = text.replace("’", "'")  # models often emit a typographic apostrophe
    return any(p.search(plain) for p in _BAIL_PATTERNS)


def _claims_action(text: str) -> bool:
    plain = text.replace("’", "'")
    return any(p.search(plain) for p in _CLAIM_PATTERNS)


# Synthetic tool results for the turn-ending tools, so the history stays well-formed (every
# tool_call id gets an answering role="tool" message) across the early return.
_QUESTION_RESULT = "(question shown; the user's next message is their answer)"
_DRAFT_RESULT = "(draft shown to the user for approval)"
_SKIPPED_RESULT = "(not executed — the turn ended before this call ran)"
_STEP_LIMIT_TEXT = "(stopped: reached the step limit without a final answer — try again)"


class ScribeLoop:
    """Owns one authoring turn end-to-end (context -> complete -> dispatch -> loop)."""

    def __init__(
        self,
        backend: LLMBackend,
        tools: AuthoringTools,
        memory: MemoryStore,
        persona: str,
        model: str = "claude-sonnet-5",
        max_steps: int = 12,
        validate_draft: Callable[[dict[str, Any]], str | None] = draft_problems,
    ) -> None:
        self._backend = backend
        self._tools = tools
        self._memory = memory
        self._persona = persona
        self._model = model
        self._max_steps = max_steps
        self._validate_draft = validate_draft

    @property
    def backend(self) -> LLMBackend:
        """The loop's model backend — sessions reuse it for history summarization."""
        return self._backend

    @property
    def model(self) -> str:
        return self._model

    async def _system(self, context_note: str | None) -> Message:
        system = self._persona
        recalled = await self._memory.recall()
        if recalled:
            notes = "\n".join(f"- {n}" for n in recalled)
            system = f"{system}\n\n## Remembered notes (curatable; may be stale)\n{notes}"
        if context_note:
            system = f"{system}\n\n## Session context\n{context_note}"
        return Message(role="system", content=system)

    async def turn(
        self, history: list[Message], context_note: str | None = None
    ) -> AsyncIterator[LoopEvent]:
        """Run one turn, yielding `LoopEvent`s until a final reply, a question/draft, or the step
        limit. `history` must already end with the user's newest message; the loop appends
        everything the turn produces to it in place."""
        system = await self._system(context_note)
        nudges = 0
        acted = False  # has this turn executed any tool yet? (guards the fabrication nudge)
        for _ in range(self._max_steps):
            completion = await self._backend.complete([system, *history], TOOL_SPECS, self._model)
            if not completion.tool_calls:
                text = completion.text or ""
                history.append(Message(role="assistant", content=text))
                if "<tool_call" in text and nudges < _MAX_NUDGES:
                    nudges += 1
                    history.append(Message(role="user", content=_BROKEN_CALL_REMINDER))
                    yield LoopEvent("notice", "(nudged: a malformed tool call was re-requested)")
                    continue
                if _is_bail(text) and nudges < _MAX_NUDGES:
                    nudges += 1
                    history.append(Message(role="user", content=_BAIL_REMINDER))
                    yield LoopEvent(
                        "notice", "(nudged: the model claimed its tools were unavailable)"
                    )
                    continue
                if _claims_action(text) and not acted and nudges < _MAX_NUDGES:
                    nudges += 1
                    history.append(Message(role="user", content=_FABRICATION_REMINDER))
                    yield LoopEvent(
                        "notice", "(nudged: a change was described but no tool was called)"
                    )
                    continue
                yield LoopEvent("final", text)
                return
            acted = True
            if completion.text:
                yield LoopEvent("text", completion.text)
            history.append(
                Message(
                    role="assistant",
                    content=completion.text,
                    tool_calls=completion.tool_calls,
                )
            )
            calls = list(completion.tool_calls)
            for i, call in enumerate(calls):
                ending: LoopEvent | None = None
                args = call.arguments or {}
                if call.name == "ask_user":
                    raw = args.get("options")
                    options = [str(o) for o in raw] if isinstance(raw, list) else []
                    history.append(
                        Message(role="tool", content=_QUESTION_RESULT, tool_call_id=call.id)
                    )
                    ending = LoopEvent(
                        "question", text=str(args.get("question", "")), name=json.dumps(options)
                    )
                elif call.name == "propose_scenario":
                    spec = args.get("spec")
                    spec = spec if isinstance(spec, dict) else {}
                    problems = self._validate_draft(spec)
                    if problems is not None:  # bounce back for the model to fix and re-propose
                        yield LoopEvent("tool_call", text=_arg_preview(call), name=call.name)
                        yield LoopEvent("tool_result", text=problems, name=call.name)
                        history.append(
                            Message(role="tool", content=problems, tool_call_id=call.id)
                        )
                        continue
                    history.append(
                        Message(role="tool", content=_DRAFT_RESULT, tool_call_id=call.id)
                    )
                    ending = LoopEvent("draft", text=json.dumps(spec))
                else:
                    yield LoopEvent("tool_call", text=_arg_preview(call), name=call.name)
                    result = await self._tools.dispatch(call)
                    yield LoopEvent("tool_result", text=result, name=call.name)
                    history.append(Message(role="tool", content=result, tool_call_id=call.id))
                if ending is not None:  # turn over — answer any not-yet-run calls, then hand off
                    for later in calls[i + 1 :]:
                        history.append(
                            Message(role="tool", content=_SKIPPED_RESULT, tool_call_id=later.id)
                        )
                    yield ending
                    return
        history.append(Message(role="assistant", content=_STEP_LIMIT_TEXT))
        yield LoopEvent("final", _STEP_LIMIT_TEXT)
