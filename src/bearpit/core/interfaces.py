"""The three narrow interfaces the runtime-agnostic core speaks (principle 8).

- RuntimeAdapter — birth/death of an agent on some runtime (Hermes is adapter #1).
- Bus — inter-agent messaging + channel management (Matrix/Herald is impl #1).
- Observation — how the platform watches a realm (Chronicle/Warden consume this).

`RuntimeAdapter` is implemented, by forge/adapters/hermes/. `Bus` and `Observation` are declared
seams, NOT yet wired: Herald and Chronicle predate them and expose their own shapes
(`provision_bus`/`announce`/`mirror`, not `create_channel`/`post`/`history`). They are kept here
because they define what a second implementation would have to satisfy — but nothing consumes them
today, and a docstring claiming otherwise would send a contributor looking for wiring that does not
exist.

Keeping them as Protocols means the core never imports a runtime.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Protocol, runtime_checkable

from bearpit.core.schema import AgentSpec


@dataclass(frozen=True)
class RealmContext:
    """Everything an adapter needs about the realm an agent is born into."""

    realm_id: str
    network: str  # per-realm private network name/id
    model_base_url: str  # LiteLLM proxy endpoint
    bus_homeserver: str  # e.g. Matrix homeserver URL
    commons_room: str
    shared_folder: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentHandle:
    """Opaque reference to a provisioned agent, returned by RuntimeAdapter.provision."""

    agent_id: str
    container_id: str | None = None
    home_volume: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentArtifacts:
    """What `collect` returns at teardown: paths to snapshots/logs/authored skills."""

    home_snapshot: str | None = None
    logs: list[str] = field(default_factory=list)
    authored_skills: list[str] = field(default_factory=list)


@runtime_checkable
class RuntimeAdapter(Protocol):
    """Birth/death of an agent. The whole config surface is materialized at provision;
    after that the agent is a black box (principle 1)."""

    def provision(self, spec: AgentSpec, realm: RealmContext) -> AgentHandle: ...
    def start(self, handle: AgentHandle) -> None: ...
    def stop(self, handle: AgentHandle, grace: timedelta) -> None: ...
    def collect(self, handle: AgentHandle) -> AgentArtifacts: ...


@dataclass(frozen=True)
class Message:
    channel: str
    sender: str
    body: str
    ts_ms: int
    attachments: tuple[str, ...] = ()


@runtime_checkable
class Bus(Protocol):
    """Inter-agent messaging + channel management. Channel membership is physics."""

    def create_channel(self, name: str, members: list[str]) -> str: ...
    def post(self, channel: str, sender: str, body: str) -> None: ...
    def history(self, channel: str, since_ms: int = 0) -> list[Message]: ...


@runtime_checkable
class Observation(Protocol):
    """How the platform watches a realm: a normalized event stream fed into the Chronicle."""

    def stream(self, realm_id: str) -> Iterator[dict[str, Any]]: ...
