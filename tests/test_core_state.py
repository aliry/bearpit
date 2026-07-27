"""State-machine transition rules."""

import pytest

from agentrealm.core.state import (
    AgentState,
    InvalidTransition,
    RealmState,
    agent_can_transition,
    agent_transition,
    realm_can_transition,
    realm_transition,
)


def test_realm_happy_path():
    s = RealmState.DRAFT
    path = (RealmState.PROVISIONING, RealmState.RUNNING, RealmState.CONCLUDING, RealmState.ARCHIVED)
    for nxt in path:
        s = realm_transition(s, nxt)
    assert s == RealmState.ARCHIVED


def test_realm_illegal():
    assert not realm_can_transition(RealmState.DRAFT, RealmState.RUNNING)
    with pytest.raises(InvalidTransition):
        realm_transition(RealmState.ARCHIVED, RealmState.RUNNING)


def test_agent_starve_recover_and_kill():
    assert agent_can_transition(AgentState.RUNNING, AgentState.STARVED)
    assert agent_can_transition(AgentState.STARVED, AgentState.RUNNING)  # budget top-up
    assert agent_can_transition(AgentState.RUNNING, AgentState.KILLED)
    s = agent_transition(AgentState.PROVISIONING, AgentState.RUNNING)
    s = agent_transition(s, AgentState.STOPPED)
    s = agent_transition(s, AgentState.ARCHIVED)
    assert s == AgentState.ARCHIVED
    with pytest.raises(InvalidTransition):
        agent_transition(AgentState.ARCHIVED, AgentState.RUNNING)
