"""In-memory fakes for Scribe unit tests (Protocol-for-IO, the project's convention).

These stand in for the real I/O collaborators so the loop/tools/backend can be exercised with no
network, no filesystem package store, and fully-scripted model output.
"""

from __future__ import annotations

from typing import Any

from bearpit.core.schema import Project
from bearpit.scribe.types import Completion, Message, ToolSpec


class FakeLLMBackend:
    """An `LLMBackend` that replays a scripted list of `Completion`s.

    Each `complete()` pops the next scripted completion and records the call (messages, tools,
    model, effort) so a test can assert on what the loop sent.
    """

    def __init__(self, completions: list[Completion]) -> None:
        self._queue = list(completions)
        self.calls: list[tuple[list[Message], list[ToolSpec], str, str | None]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str,
        effort: str | None = None,
    ) -> Completion:
        self.calls.append((list(messages), list(tools), model, effort))
        if not self._queue:
            raise AssertionError("FakeLLMBackend exhausted — loop asked for one too many")
        return self._queue.pop(0)


class FakePackageStore:
    """An in-memory `PackageStore` holding `{name: Project}`."""

    def __init__(self, projects: dict[str, Project] | None = None) -> None:
        self._projects: dict[str, Project] = dict(projects or {})
        self.writes: list[str] = []

    async def list(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "title": p.metadata.name,
                "agents": len(p.agents),
                "summary": p.metadata.description or "",
            }
            for name, p in sorted(self._projects.items())
        ]

    async def read(self, name: str) -> Project:
        if name not in self._projects:
            raise KeyError(name)
        return self._projects[name]

    async def write(self, name: str, project: Project) -> None:
        self._projects[name] = project
        self.writes.append(name)

    async def has_user(self, name: str) -> bool:
        return name in self._projects


class FakeVersions:
    """A no-op `Versions` that records snapshot calls (used before the real Versions is wired)."""

    def __init__(self) -> None:
        self.snapshots: list[tuple[str, Project | None]] = []

    async def snapshot(self, name: str, project: Project | None) -> str:
        self.snapshots.append((name, project))
        return f"v{len(self.snapshots)}"


class FakeMemory:
    """An in-memory `Memory` with the same verbs as the real markdown store."""

    def __init__(self, notes: list[str] | None = None) -> None:
        self._notes: list[str] = list(notes or [])
        self.remembered: list[tuple[str, str, list[str]]] = []

    async def remember(self, text: str, kind: str, tags: list[str] | None = None) -> str:
        self.remembered.append((text, kind, list(tags or [])))
        self._notes.append(text)
        return f"m{len(self._notes)}"

    async def recall(self, limit: int = 20) -> list[str]:
        return list(self._notes[-limit:])

    async def search(self, query: str) -> list[str]:
        return [n for n in self._notes if query.lower() in n.lower()]


# --- a fake provider profile ---------------------------------------------------------------------
# The platform's two policy transforms are driven by PROFILE FIELDS, never by a provider's name, so
# tests exercise them through a stand-in pipeline that declares all of them. This is exactly the
# shape a contributed provider arrives in.
FLAT = "fake-flat"

FLAT_RATE_PROFILE: dict[str, Any] = {
    "label": "Fake flat-rate",
    "description": "A fixed-price, slow pipeline used to exercise the policy fields.",
    "api_key_ref": "fake-main",
    "flat_rate": True,
    "min_budget_usd": 25.0,
    "min_turn_seconds": 240.0,
    "setup_hint": "run `pit keys add fake-main`",
    "categories": {
        "small": {"model": "fake-s", "effort": "low",
                  "input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6,
                  "context_length": 200000},
        "medium": {"model": "fake-m", "effort": "medium",
                   "input_cost_per_token": 3e-6, "output_cost_per_token": 1.5e-5,
                   "context_length": 200000},
        "large": {"model": "fake-l", "effort": "high",
                  "input_cost_per_token": 3e-6, "output_cost_per_token": 1.5e-5,
                  "context_length": 200000},
    },
}


def flat_rate_table() -> dict[str, dict[str, Any]]:
    """The shipped provider table plus the fake flat-rate pipeline (a fresh copy each call)."""
    import copy

    from bearpit.core.providers import default_providers

    cfg = default_providers()
    cfg[FLAT] = copy.deepcopy(FLAT_RATE_PROFILE)
    return cfg
