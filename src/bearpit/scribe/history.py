"""Per-scenario persistent Scribe conversation history + auto-summarization (spec §19).

`HistoryStore` keeps one plain-JSON file per scenario under `<root>/history/` — inspectable and
hand-editable, mirroring the memory store's transparency. A corrupt file never crashes a session
open: it is logged and treated as empty. `compact` collapses the OLDER portion of a history into a
single LLM-written "conversation so far" summary message (a visible thread message, not hidden
state) while the recent tail stays verbatim; `estimate_chars` + `history_max_chars` drive the
auto-trigger the session checks before each turn.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from bearpit.scribe.backend import LLMBackend
from bearpit.scribe.types import Message, ToolCall

log = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 60_000

SUMMARY_PREFIX = "(Conversation so far, summarized)"
_SUMMARY_SYSTEM = (
    "Summarize this scenario-authoring conversation so far: decisions made, the current "
    "scenario state, user preferences, open questions. Compact bullet points."
)


def history_max_chars() -> int:
    """The auto-summarize threshold (chars), from `SCRIBE_HISTORY_MAX_CHARS` when set."""
    raw = os.environ.get("SCRIBE_HISTORY_MAX_CHARS", "")
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_CHARS


def _message_to_dict(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_calls:
        out["tool_calls"] = [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in m.tool_calls
        ]
    if m.tool_call_id is not None:
        out["tool_call_id"] = m.tool_call_id
    return out


def _message_from_dict(d: dict[str, Any]) -> Message:
    calls = [
        ToolCall(
            id=str(c.get("id", "")),
            name=str(c.get("name", "")),
            arguments=dict(c.get("arguments") or {}),
        )
        for c in d.get("tool_calls") or []
        if isinstance(c, dict)
    ]
    content = d.get("content")
    return Message(
        role=str(d.get("role", "")),
        content=str(content) if content is not None else None,
        tool_calls=calls,
        tool_call_id=d.get("tool_call_id"),
    )


class HistoryStore:
    """Plain-JSON per-scenario conversation histories under `<root>/history/<scenario>.json`."""

    def __init__(self, root: Path) -> None:
        self._dir = Path(root) / "history"

    def _path(self, scenario: str) -> Path:
        # Scenario names are slug-safe already, but never let one traverse out of the store.
        if not scenario or Path(scenario).name != scenario or scenario in (".", ".."):
            raise ValueError(f"invalid scenario name {scenario!r}")
        return self._dir / f"{scenario}.json"

    async def load(self, scenario: str) -> list[Message]:
        """The stored history, `[]` if none. A corrupt/unreadable file is logged and treated as
        empty — never crash a session open."""
        path = self._path(scenario)
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            log.warning("unreadable scribe history %s — starting fresh", path)
            return []
        if not isinstance(data, list):
            log.warning("malformed scribe history %s (not a list) — starting fresh", path)
            return []
        return [_message_from_dict(d) for d in data if isinstance(d, dict)]

    async def save(self, scenario: str, history: list[Message]) -> None:
        path = self._path(scenario)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([_message_to_dict(m) for m in history], indent=2))

    async def delete(self, scenario: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path(scenario).unlink()

    async def has(self, scenario: str) -> bool:
        return self._path(scenario).exists()


def estimate_chars(history: list[Message]) -> int:
    """A cheap context-size proxy: total content length including tool-call payloads."""
    total = 0
    for m in history:
        total += len(m.content or "")
        for c in m.tool_calls:
            total += len(c.name)
            try:
                total += len(json.dumps(c.arguments))
            except (TypeError, ValueError):
                total += len(str(c.arguments))
    return total


def _render_plain(messages: list[Message]) -> str:
    """The old messages as plain text for the summarizer (tool calls inlined, compact)."""
    lines: list[str] = []
    for m in messages:
        parts: list[str] = []
        if m.content:
            parts.append(m.content)
        for c in m.tool_calls:
            try:
                args = json.dumps(c.arguments)
            except (TypeError, ValueError):
                args = str(c.arguments)
            parts.append(f"[tool_call {c.name} {args}]")
        if parts:
            lines.append(f"{m.role}: " + "\n".join(parts))
    return "\n".join(lines)


async def compact(
    history: list[Message], backend: LLMBackend, model: str, keep_tail: int = 12
) -> list[Message]:
    """Collapse everything but the last `keep_tail` messages into one summary message.

    The boundary never splits an assistant tool_calls message from its `role="tool"` answers —
    it moves earlier until clean. Returns `[summary-as-user-message, *tail]`; a history with
    nothing older than the tail is returned unchanged (no LLM call). `keep_tail=0` is the §19
    "Summarize now" control: the whole thread collapses to just the summary.
    """
    cut = max(0, len(history) - keep_tail)
    while 0 < cut < len(history) and history[cut].role == "tool":
        cut -= 1  # keep a tool answer with the assistant message that called for it
    old, tail = list(history[:cut]), list(history[cut:])
    if not old:
        return list(history)
    completion = await backend.complete(
        [
            Message(role="system", content=_SUMMARY_SYSTEM),
            Message(role="user", content=_render_plain(old)),
        ],
        [],
        model,
    )
    summary = (completion.text or "").strip() or "(no summary produced)"
    return [Message(role="user", content=f"{SUMMARY_PREFIX}\n{summary}"), *tail]
