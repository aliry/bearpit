"""Scribe service wiring + per-conversation session state.

`build_scribe` assembles the whole assistant — `OpenAIBackend` + `ApiPackageStore` + `Memory` +
`Versions` + `AuthoringTools` + the persona — into a `ScribeLoop`. `ScribeSession` holds
one conversation's running history so successive `send()`s accumulate. Both the backend and the
package store are injectable so tests drive a full turn with fakes and no network.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from bearpit.scribe.backend import DEFAULT_MODEL, LLMBackend, OpenAIBackend
from bearpit.scribe.history import HistoryStore, compact, estimate_chars, history_max_chars
from bearpit.scribe.loop import LoopEvent, ScribeLoop
from bearpit.scribe.memory import Memory
from bearpit.scribe.store import ApiPackageStore, PackageStore
from bearpit.scribe.tools import AuthoringTools
from bearpit.scribe.types import Message
from bearpit.scribe.versions import Versions

# mode -> system-prompt file. "cli" is the original direct-apply assistant (`pit assist`);
# the guided modes drive the UI wizard/edit flows (spec §18): ask_user Q&A + propose-don't-write.
_PERSONA_FILES = {
    "cli": "persona.md",
    "guided-create": "persona_guided_create.md",
    "guided-edit": "persona_guided_edit.md",
}


def _persona_text(mode: str) -> str:
    fname = _PERSONA_FILES.get(mode)
    if fname is None:
        raise ValueError(f"unknown scribe mode {mode!r} (use cli / guided-create / guided-edit)")
    return (Path(__file__).parent / fname).read_text()


def _examples_dir() -> Path:
    return Path(os.environ.get("BEARPIT_EXAMPLES_DIR", "examples"))


def build_scribe(
    base_url: str,
    root: str | Path,
    *,
    backend: LLMBackend | None = None,
    store: PackageStore | None = None,
    api_key: str = "",
    model: str = DEFAULT_MODEL,
    mode: str = "cli",
) -> ScribeLoop:
    """Wire a `ScribeLoop` from an OpenAI-compatible base URL and a data root (memory + versions +
    scenarios live under `root`). `backend`/`store` may be injected (tests); by default they are the
    real `OpenAIBackend` and `ApiPackageStore`. `mode` selects the persona: "cli" (direct-apply, the
    `pit assist` REPL) or "guided-create"/"guided-edit" (the UI's propose-then-approve flows)."""
    root = Path(root)
    memory = Memory(root / "memory")
    versions = Versions(root / "versions")
    if store is None:
        store = ApiPackageStore(user_dir=root / "scenarios", example_dirs=[_examples_dir()])
    if backend is None:
        backend = OpenAIBackend(base_url, api_key=api_key, model=model)
    tools = AuthoringTools(store, versions, memory)
    return ScribeLoop(backend, tools, memory, persona=_persona_text(mode), model=model)


def visible_history(history: list[Message]) -> list[dict[str, str]]:
    """The user-facing thread for the UI to re-render on resume: user/assistant prose plus the
    questions `ask_user` posed, minus loop plumbing (tool results, empty tool-call carriers,
    injected SYSTEM REMINDER nudges). The compaction summary IS visible — spec §19."""
    out: list[dict[str, str]] = []
    for m in history:
        if m.role not in ("user", "assistant"):
            continue
        text = (m.content or "").strip()
        if text and not text.startswith("SYSTEM REMINDER:"):
            out.append({"role": m.role, "text": text})
        if m.role == "assistant":
            for call in m.tool_calls:
                if call.name == "ask_user":
                    q = str((call.arguments or {}).get("question", "")).strip()
                    if q:
                        out.append({"role": "assistant", "text": q})
    return out


class ScribeSession:
    """One conversation: holds the running history and drives a turn per user message.

    The session appends only the user's message; the LOOP owns every other history append
    (assistant tool_calls, tool results, final text), so the conversation stays well-formed even
    when a turn ends early on a question or draft. `draft` holds the latest valid
    `propose_scenario` spec (what the approve endpoint writes); `context_note` is extra per-session
    context folded into the system prompt each turn (edit mode: the current scenario JSON).

    With `scenario` + `history_store` set, the history persists (spec §19): saved after every
    completed turn and auto-summarized before a turn once `estimate_chars` crosses the threshold
    (`max_chars`, default from `SCRIBE_HISTORY_MAX_CHARS`). Create-wizard sessions start unbound
    and attach via `bind()` at approve time; edit sessions resume via `ScribeSession.resume()`.
    """

    def __init__(
        self,
        loop: ScribeLoop,
        *,
        context_note: str | None = None,
        scenario: str | None = None,
        history_store: HistoryStore | None = None,
        max_chars: int | None = None,
    ) -> None:
        self._loop = loop
        self.history: list[Message] = []
        self.context_note = context_note
        self.draft: dict[str, Any] | None = None
        self.scenario = scenario
        self._history_store = history_store
        self._max_chars = max_chars

    @classmethod
    async def resume(
        cls,
        loop: ScribeLoop,
        *,
        scenario: str,
        history_store: HistoryStore,
        context_note: str | None = None,
        max_chars: int | None = None,
    ) -> ScribeSession:
        """A session bound to `scenario` with its stored history loaded (edit mode)."""
        session = cls(
            loop,
            context_note=context_note,
            scenario=scenario,
            history_store=history_store,
            max_chars=max_chars,
        )
        session.history = await history_store.load(scenario)
        return session

    async def bind(self, scenario: str) -> None:
        """Attach an unbound (create-wizard) session to its scenario and persist the history, so
        a later "Edit with assistant" resumes the authoring conversation."""
        self.scenario = scenario
        await self._save()

    async def _save(self) -> None:
        if self._history_store is not None and self.scenario is not None:
            await self._history_store.save(self.scenario, self.history)

    async def compact_now(self) -> None:
        """Collapse the WHOLE thread to the summary (the §19 "Summarize now" control)."""
        self.history = await compact(
            self.history, self._loop.backend, self._loop.model, keep_tail=0
        )
        await self._save()

    async def send(self, user_msg: str) -> AsyncIterator[LoopEvent]:
        """Run one turn for `user_msg`, streaming its events; the history accumulates in place."""
        limit = self._max_chars if self._max_chars is not None else history_max_chars()
        if self.history and estimate_chars(self.history) > limit:
            compacted = await compact(self.history, self._loop.backend, self._loop.model)
            if compacted != self.history:  # a history all-tail (≤ keep_tail msgs) is left alone
                self.history = compacted
                await self._save()
                yield LoopEvent("notice", "(history summarized to stay within context)")
        self.history.append(Message(role="user", content=user_msg))
        async for event in self._loop.turn(self.history, context_note=self.context_note):
            if event.kind == "draft":
                try:
                    data = json.loads(event.text)
                except ValueError:
                    data = None
                if isinstance(data, dict):
                    self.draft = data
            yield event
        await self._save()
