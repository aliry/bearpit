"""Private agent notes — the `remember` / `recall` tools (an agent's own scratchpad).

Why the platform owns this rather than the runtime: a Hermes agent DOES have `memory` and
`write_file`, but they are runtime tools, not MCP tools — so an agent whose backend executes
tools natively cannot reach them at all, and we deliberately keep realm agents off the host
filesystem. A note store on the realm's own MCP server works for EVERY provider, is scoped to the
caller by its token, and lands in the Chronicle like everything else.

Why it matters: each Hermes turn is a fresh conversation, so without somewhere durable to think,
an agent re-derives its whole world from the chat log every turn. The social-deduction literature
is blunt that a running private scratchpad — "who do I suspect, and on what specific evidence" —
is the highest-leverage single technique for making LLM agents reason instead of vibe.

Privacy: `recall` filters by the CALLER's agent id, taken from the verified token, never from an
argument — an agent can only ever read its own notes.
"""

from __future__ import annotations

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.service import Identity

_MAX_NOTE_CHARS = 4000
_MAX_RECALL = 40  # most recent notes returned; a runaway note-taker can't blow up its own context


class NoteService:
    def __init__(self, chronicle: Chronicle | None = None) -> None:
        self._chron = chronicle

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle

    def _c(self) -> Chronicle:
        if self._chron is None:
            raise RuntimeError("NoteService has no chronicle connected")
        return self._chron

    async def remember(self, who: Identity, text: str) -> str:
        if not text or not text.strip():
            raise ValueError("remember needs a non-empty note")
        note = text.strip()[:_MAX_NOTE_CHARS]
        await self._c().append_event(
            who.realm_id, EventKind.NOTE, {"agent": who.agent_id, "text": note}
        )
        return "Noted (private to you)."

    async def recall(self, who: Identity) -> list[str]:
        """This agent's own notes, oldest first. Never another agent's."""
        events = await self._c().events(who.realm_id, kind=EventKind.NOTE)
        mine = [str(e.payload.get("text", "")) for e in events
                if e.payload.get("agent") == who.agent_id]
        return mine[-_MAX_RECALL:]
