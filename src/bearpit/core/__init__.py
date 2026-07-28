"""Domain model: manifest/package schema, state machines, core interfaces.

Interfaces: RuntimeAdapter (birth/death), Bus (messages), Observation (watching).
"""

from bearpit.core.interfaces import (
    AgentArtifacts,
    AgentHandle,
    Bus,
    Message,
    Observation,
    RealmContext,
    RuntimeAdapter,
)
from bearpit.core.package import PackageError, load_package
from bearpit.core.schema import (
    AgentRole,
    AgentSpec,
    Environment,
    Mechanic,
    Project,
    ProjectMeta,
    ProjectSpec,
    Turns,
    parse_duration,
)
from bearpit.core.state import (
    AgentState,
    InvalidTransition,
    RealmState,
    agent_transition,
    realm_transition,
)

__all__ = [
    "AgentArtifacts",
    "AgentHandle",
    "AgentRole",
    "AgentSpec",
    "AgentState",
    "Bus",
    "Environment",
    "InvalidTransition",
    "Mechanic",
    "Message",
    "Observation",
    "PackageError",
    "Project",
    "ProjectMeta",
    "ProjectSpec",
    "RealmContext",
    "RealmState",
    "RuntimeAdapter",
    "Turns",
    "agent_transition",
    "load_package",
    "parse_duration",
    "realm_transition",
]
