"""`table_talk` — a referee reading the floor it is not standing on.

A machine realm's referee is deliberately not in the commons: its information source is the MACHINE
record, and being in the feed means being woken by every word (see `runconfig.referee_sees_all` for
what that cost). But a scenario may still have a rule about what agents SAY — poker forbids naming
a card — and a referee that cannot read the floor cannot enforce one. Across 297 realms not a single
violation has ever been issued, and poker's 33+ card-namings went uncaught because the dealer was
never shown them.

So the referee pulls instead of being pushed: it calls this when it is already awake, gets what was
said since it last looked, and rules. No extra wake, no firehose, and the scenario still decides —
the declaration's `referee_reads_commons` is the gate, exactly as before; only its implementation
moved from the Herald's mention gate to here.

Read-only. Nothing here writes, scores or decides; what the referee does with what it reads is the
referee's business, and it goes through the Arbiter tools like every other ruling.
"""

from __future__ import annotations

from typing import Any

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.service import Identity

MAX_ROWS = 200
MAX_BODY_CHARS = 600


class TableService:
    def __init__(self, chronicle: Chronicle | None = None) -> None:
        self._chron = chronicle

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle

    def _c(self) -> Chronicle:
        if self._chron is None:
            raise RuntimeError("TableService has no chronicle")
        return self._chron

    async def _commons_room(self, realm_id: str) -> str | None:
        """The commons room id, from the `running` lifecycle record the host wrote at provision."""
        for e in await self._c().events(realm_id, kind=EventKind.LIFECYCLE):
            room = e.payload.get("commons_room")
            if room:
                return str(room)
        return None

    async def _may_read(self, realm_id: str) -> bool:
        """The declaration's own gate. A realm with no machine does not reach this tool's caller
        anyway — its referee is already in the commons — so an absent MACHINE record is a no."""
        machines = await self._c().events(realm_id, kind=EventKind.MACHINE)
        if not machines:
            return False
        decl = machines[-1].payload.get("declaration") or {}
        return bool(decl.get("referee_reads_commons"))

    async def read(
        self, who: Identity, *, since_ms: int | None = None, limit: int = 50
    ) -> dict[str, Any]:
        if not who.is_referee:
            raise PermissionError("table_talk is referee-only")
        if not await self._may_read(who.realm_id):
            # Refused by the SCENARIO, not by the platform: an author who left the referee off the
            # floor meant it. Say which, so nobody goes looking for a permissions bug.
            raise PermissionError(
                "this realm's declaration does not set referee_reads_commons; the floor is not "
                "yours to read"
            )
        room = await self._commons_room(who.realm_id)
        msgs = await self._c().messages(who.realm_id, channel=room)
        rows = [
            {"ts": m.ts_ms, "sender": m.sender, "body": (m.body or "")[:MAX_BODY_CHARS]}
            for m in msgs
            if since_ms is None or m.ts_ms > since_ms
        ]
        n = max(1, min(int(limit or 50), MAX_ROWS))
        clipped = rows[-n:]
        return {
            "messages": clipped,
            # The caller's cursor for next time. Its own `ts` — not "now" — so a message that
            # lands while it is thinking is still waiting on the next call rather than skipped.
            "cursor": clipped[-1]["ts"] if clipped else since_ms,
            "more_before": len(rows) > len(clipped),
        }
