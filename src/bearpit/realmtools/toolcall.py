"""Granted tools — an agent invoking a capability the HOST performs on its behalf (ADR-004 §3).

The shape is `run_code`'s, verbatim, and for the same reason: realmtools records the intent and
waits, the host executes it and answers. There the reason was Docker; here it is credentials and
the internet. **realmtools holds neither.** A search key in this small agent-facing server would
turn any bug in it into a leaked credential, and an internet route would turn it into the
realm's escape hatch.

Two checks happen here rather than on the host, because both should cost the agent a message
rather than a turn: the grant, and the quota.

The grant check is the one that actually enforces ADR-004. The spike in #51 established that
filtering the tool *list* is a UX control — a tool hidden from `tools/list` still executes when a
caller names it directly — so the list keeps agents from wasting turns and **this** keeps them
from using what they were not given.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.manifest import ManifestReader
from bearpit.realmtools.service import Identity

MAX_ARGS_CHARS = 8000
MAX_RESULT_CHARS = 16000
_POLL_S = 0.25
_WAIT_S = 60.0  # the host drains on its tick (5s); this is the ceiling before we give up


class ToolCallService:
    def __init__(self, chronicle: Chronicle | None = None,
                 manifests: ManifestReader | None = None) -> None:
        self._chron = chronicle
        self._manifests = manifests or ManifestReader(chronicle)

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle
        self._manifests.set_chronicle(chronicle)

    def _c(self) -> Chronicle:
        if self._chron is None:
            raise RuntimeError("ToolCallService has no chronicle connected")
        return self._chron

    async def policy(self, realm_id: str, tool: str) -> dict[str, Any]:
        """This realm's `spec.tools[tool]` block, from the tool manifest (#65).

        Read rather than injected: realmtools is stateless and shared across realms, so the run
        record is the only per-realm configuration it can be sure is the one that actually ran.

        It reads the manifest rather than rescanning lifecycle events for two reasons — the
        manifest is small where a lifecycle row carries the whole project snapshot, and it is
        cached per realm, so a quota check costs nothing after the first.
        """
        return await self._manifests.policy(realm_id, tool)

    async def used(self, realm_id: str, agent_id: str, tool: str) -> int:
        """How many times this agent has already called this tool in this realm.

        Counted from TOOL_CALL rather than TOOL_RESULT on purpose: a call that failed, timed out
        or was never answered still consumed the thing a quota exists to bound. Counting only
        successes would make a flapping tool free.
        """
        return sum(
            1
            for ev in await self._c().events(realm_id, kind=EventKind.TOOL_CALL)
            if ev.payload.get("agent") == agent_id and ev.payload.get("tool") == tool
        )

    async def call(
        self,
        who: Identity,
        tool: str,
        args: dict[str, Any],
        *,
        wait_s: float = _WAIT_S,
        sleep: Any = None,
    ) -> dict[str, Any]:
        """Record the intent, wait for the host's answer, return it.

        Raises PermissionError for an ungranted tool — the caller turns that into a refusal the
        agent can read. Everything else answers with a dict: a tool that is merely broken must
        cost one call, never the realm.
        """
        if tool not in who.grants:
            raise PermissionError(
                f"{tool!r} is not one of your tools"
                + (f" (you have: {', '.join(who.grants)})" if who.grants else " (you have none)")
            )
        blob = json.dumps(args, default=str)
        if len(blob) > MAX_ARGS_CHARS:
            raise ValueError(f"arguments too long (max {MAX_ARGS_CHARS} chars)")

        policy = await self.policy(who.realm_id, tool)
        quota = policy.get("max_calls_per_agent")
        if isinstance(quota, int) and quota >= 0:
            used = await self.used(who.realm_id, who.agent_id, tool)
            if used >= quota:
                # Not an exception: the agent should be able to carry on without this tool, and a
                # raised error reads to a model like something it should retry.
                return {
                    "error": f"you have used {tool!r} {used}/{quota} times, which is all this "
                             f"scenario allows you — carry on without it",
                    "quota_exhausted": True,
                }

        req = uuid.uuid4().hex[:16]
        await self._c().append_event(
            who.realm_id, EventKind.TOOL_CALL,
            {"id": req, "agent": who.agent_id, "tool": tool, "args": args},
        )
        napper = sleep or asyncio.sleep
        waited = 0.0
        while waited < wait_s:
            for ev in await self._c().events(who.realm_id, kind=EventKind.TOOL_RESULT):
                if ev.payload.get("id") != req:
                    continue
                if ev.payload.get("ok"):
                    return {"result": str(ev.payload.get("result", ""))[:MAX_RESULT_CHARS]}
                return {"error": str(ev.payload.get("error", "the tool failed"))}
            await napper(_POLL_S)
            waited += _POLL_S
        return {"error": f"{tool!r} did not answer in time"}


__all__ = ["MAX_ARGS_CHARS", "MAX_RESULT_CHARS", "ToolCallService"]
