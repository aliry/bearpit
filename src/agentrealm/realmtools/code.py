"""`run_code` — an agent executing Python IN ITS OWN CONTAINER.

The agents are Hermes containers with a real filesystem and interpreter, but a model backend that
executes tools natively can call only MCP tools, putting Hermes' own `execute_code` out of reach.
Nor will we hand a realm agent a backend's built-in shell: that subprocess runs on the OPERATOR'S
HOST, not in the agent's sandbox.

So the platform brokers it, using the same shape as `send_private`: realmtools records the INTENT
(an EXEC event) and the HOST — which already holds Docker and knows every agent's container —
performs it and answers with EXEC_RESULT. realmtools itself never gets a Docker socket: a socket
there would turn any bug in this small server into host root.

The authority is unchanged: the code runs as the agent's own user, inside the agent's own
container, under the egress policy already applied to it. An agent cannot reach another agent's
container, because the host maps agent -> container from the CALLER'S VERIFIED TOKEN, never from
a tool argument.
"""

from __future__ import annotations

import asyncio
import uuid

from agentrealm.chronicle import Chronicle, EventKind
from agentrealm.realmtools.service import Identity

MAX_CODE_CHARS = 16000
MAX_OUTPUT_CHARS = 8000
_POLL_S = 0.25
_WAIT_S = 90.0  # the host drains on its tick (5s); this is the ceiling before we give up


class CodeService:
    def __init__(self, chronicle: Chronicle | None = None) -> None:
        self._chron = chronicle

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle

    def _c(self) -> Chronicle:
        if self._chron is None:
            raise RuntimeError("CodeService has no chronicle connected")
        return self._chron

    async def run(
        self, who: Identity, code: str, *, wait_s: float = _WAIT_S,
        sleep: object = None,
    ) -> dict[str, object]:
        if not code or not code.strip():
            raise ValueError("run_code needs some code to run")
        if len(code) > MAX_CODE_CHARS:
            raise ValueError(f"code too long (max {MAX_CODE_CHARS} chars)")
        req = uuid.uuid4().hex[:16]
        await self._c().append_event(
            who.realm_id, EventKind.EXEC,
            {"id": req, "agent": who.agent_id, "code": code},
        )
        napper = sleep or asyncio.sleep
        waited = 0.0
        while waited < wait_s:
            for ev in await self._c().events(who.realm_id, kind=EventKind.EXEC_RESULT):
                if ev.payload.get("id") == req:
                    return {
                        "exit_code": ev.payload.get("exit_code"),
                        "output": str(ev.payload.get("output", ""))[:MAX_OUTPUT_CHARS],
                    }
            await napper(_POLL_S)  # type: ignore[operator]
            waited += _POLL_S
        return {"error": "the code did not finish in time", "exit_code": None, "output": ""}
