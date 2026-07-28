"""Realm and agent state machines (architecture §11).

Transitions are explicit so the lifecycle engine (Warden) and tests can validate any
state change against the allowed set rather than scattering the rules through the code.
"""

from __future__ import annotations

from enum import StrEnum


class RealmState(StrEnum):
    DRAFT = "draft"
    PROVISIONING = "provisioning"
    RUNNING = "running"
    CONCLUDING = "concluding"
    ARCHIVED = "archived"
    FAILED = "failed"


class AgentState(StrEnum):
    PROVISIONING = "provisioning"
    RUNNING = "running"
    STARVED = "starved"  # budget exhausted; container alive, model calls fail
    STOPPED = "stopped"
    KILLED = "killed"
    ARCHIVED = "archived"
    FAILED = "failed"


REALM_TRANSITIONS: dict[RealmState, frozenset[RealmState]] = {
    RealmState.DRAFT: frozenset({RealmState.PROVISIONING, RealmState.FAILED}),
    RealmState.PROVISIONING: frozenset({RealmState.RUNNING, RealmState.FAILED}),
    RealmState.RUNNING: frozenset({RealmState.CONCLUDING, RealmState.FAILED}),
    RealmState.CONCLUDING: frozenset({RealmState.ARCHIVED, RealmState.FAILED}),
    RealmState.ARCHIVED: frozenset(),
    RealmState.FAILED: frozenset(),
}

AGENT_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.PROVISIONING: frozenset({AgentState.RUNNING, AgentState.FAILED}),
    AgentState.RUNNING: frozenset(
        {AgentState.STARVED, AgentState.STOPPED, AgentState.KILLED, AgentState.FAILED}
    ),
    AgentState.STARVED: frozenset({AgentState.RUNNING, AgentState.STOPPED, AgentState.KILLED}),
    AgentState.STOPPED: frozenset({AgentState.ARCHIVED}),
    AgentState.KILLED: frozenset({AgentState.ARCHIVED}),
    AgentState.ARCHIVED: frozenset(),
    AgentState.FAILED: frozenset(),
}


class InvalidTransition(ValueError):
    """Raised when a state change is not in the allowed transition set."""


def realm_can_transition(frm: RealmState, to: RealmState) -> bool:
    return to in REALM_TRANSITIONS[frm]


def agent_can_transition(frm: AgentState, to: AgentState) -> bool:
    return to in AGENT_TRANSITIONS[frm]


def realm_transition(frm: RealmState, to: RealmState) -> RealmState:
    if not realm_can_transition(frm, to):
        raise InvalidTransition(f"realm: {frm.value} -> {to.value} not allowed")
    return to


def agent_transition(frm: AgentState, to: AgentState) -> AgentState:
    if not agent_can_transition(frm, to):
        raise InvalidTransition(f"agent: {frm.value} -> {to.value} not allowed")
    return to
