"""Private messaging service — the agent-facing `send_private` tool's backend.

An agent calls `send_private(to, message)`; this records the intent as a PRIVATE Chronicle event
({from, to, text}). The realmtools process cannot post to Matrix itself, so the HOST (which owns
the DM rooms + the agents' Matrix tokens) reads these events each tick and delivers each message
into the recipient's private DM room — where the normal mirror captures it and the recipient
receives it. A tool call is far more reliable for an LLM than manually targeting a Matrix room.
"""

from __future__ import annotations

import re

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.service import Identity


class PrivateMessageService:
    def __init__(self, chronicle: Chronicle | None = None) -> None:
        self._chron = chronicle

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle

    def _c(self) -> Chronicle:
        if self._chron is None:
            raise RuntimeError("PrivateMessageService has no chronicle connected")
        return self._chron

    async def send(self, who: Identity, to: str, message: str) -> str:
        """Queue a private message from the caller to `to`. Delivery (and the permission gate — a
        DM room only exists for allowed pairs) happens host-side."""
        recipient = re.sub(r"[^a-z0-9-]+", "", (to or "").strip().lower())
        if not recipient:
            raise ValueError("send_private needs a recipient agent id")
        if recipient == who.agent_id:
            raise ValueError("you can't privately message yourself")
        if not message or not message.strip():
            raise ValueError("send_private needs a non-empty message")
        await self._c().append_event(
            who.realm_id, EventKind.PRIVATE,
            {"from": who.agent_id, "to": recipient, "text": message},
        )
        return f"Delivered privately to {recipient}."
