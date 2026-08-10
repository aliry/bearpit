"""Reading the per-realm tool manifest (#65, ADR-004).

This is what lets the Realmtools container hold **no tool plugins**. It describes a granted tool
entirely from a record the host wrote — name, description, parameter schema, policy — so a
third-party package's import-time code and dependency tree never enter the agent-facing server.

The manifest is **descriptive, never authoritative**. What an agent may call comes from its signed
token and nothing else (`toolcall.ToolCallService.call`). This module only answers "and what does
that tool look like?".

Cached per realm with a short TTL: `tools/list` runs on every agent connection and the answer
changes only when a realm starts.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from bearpit.chronicle import Chronicle, EventKind

log = logging.getLogger(__name__)

TTL_S = 30.0
SUPPORTED_VERSION = 1

# What a granted tool looks like when the manifest cannot describe it: callable, but unconstrained.
# Hiding it instead would make a grant invisible with nothing said, which is the failure this whole
# design removes; an open schema costs the model a guess, which is recoverable.
_PLACEHOLDER: dict[str, Any] = {
    "available": True,
    "description": "A tool granted to you. Its description could not be loaded; "
                   "pass whatever arguments it needs as named values.",
    "params": {"type": "object"},
    "policy": {},
}


class ManifestReader:
    def __init__(self, chronicle: Chronicle | None = None) -> None:
        self._chron = chronicle
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def set_chronicle(self, chronicle: Chronicle) -> None:
        self._chron = chronicle
        self._cache.clear()

    async def _load(self, realm_id: str) -> dict[str, Any]:
        if self._chron is None:
            return {}
        events = await self._chron.events(realm_id, kind=EventKind.TOOL_MANIFEST)
        if not events:
            return {}
        # LAST wins. A realm id can be reused, and the newest manifest is the one describing the
        # run that is happening now.
        payload = events[-1].payload
        version = payload.get("version")
        if version != SUPPORTED_VERSION:
            log.warning("realm %s tool manifest is version %r, expected %r — describing granted "
                        "tools generically", realm_id, version, SUPPORTED_VERSION)
            return {}
        tools = payload.get("tools")
        return dict(tools) if isinstance(tools, dict) else {}

    async def tools(self, realm_id: str, *, now: float | None = None) -> dict[str, Any]:
        """This realm's tool descriptions, keyed by name. `{}` when there is no usable manifest."""
        clock = time.monotonic() if now is None else now
        hit = self._cache.get(realm_id)
        if hit is not None and clock - hit[0] < TTL_S:
            return hit[1]
        loaded = await self._load(realm_id)
        self._cache[realm_id] = (clock, loaded)
        return loaded

    async def describe(
        self, realm_id: str, name: str, *, now: float | None = None
    ) -> dict[str, Any]:
        """One granted tool's description, falling back to an unconstrained placeholder."""
        entry = (await self.tools(realm_id, now=now)).get(name)
        if not isinstance(entry, dict):
            log.warning("realm %s grants %r but the manifest does not describe it", realm_id, name)
            return dict(_PLACEHOLDER)
        return entry

    async def policy(self, realm_id: str, name: str) -> dict[str, Any]:
        """The realm's `spec.tools[name]` block for this tool."""
        entry = (await self.tools(realm_id)).get(name)
        block = entry.get("policy") if isinstance(entry, dict) else None
        return dict(block) if isinstance(block, dict) else {}


__all__ = ["SUPPORTED_VERSION", "TTL_S", "ManifestReader"]
