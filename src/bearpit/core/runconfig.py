"""A snapshot of the configuration a realm ACTUALLY ran with.

Not the manifest — the manifest is not what runs. Between "launch" and the first token, the
platform rewrites the project three times:

  * `resolve_project`                    picks each agent's concrete model + reasoning effort from
                                         its `model_category` and the ACTIVE provider's table.
  * `pace_turns_for_provider`            raises `silence_timeout_s` to the provider's floor, as a
                                         turn on a slow pipeline is several model calls.
  * `raise_budgets_for_flat_rate_provider`  lifts a too-tight cap on a flat-rate pipeline.

So a scenario that says `silence_timeout_s: 120`, `max_usd: 2` and `model_category: large` may well
have run at 240s, $25 and a concrete large-tier model. Rendering the manifest on the realm page
would therefore show a configuration that never existed — and every question you ask a finished run
("was it mention-gated? which model was the referee on? why did the floor pass so fast?") is a
question about the RESOLVED values.

This is captured once, at launch, into the realm's `running` lifecycle event, so it stays true for
an archived realm and survives a restart of the platform.
"""

from __future__ import annotations

from typing import Any

from bearpit.core.schema import Project


def _effective_skills(agent: Any) -> list[str]:
    """Every skill the agent actually carries. Mirrors forge.skills.skill_texts — which core cannot
    import (forge depends on core, not the other way round), so the ONE rule it duplicates is the
    role default. If that rule ever changes, both move together."""
    role_default = "referee-basics" if str(agent.role) == "referee" else "agent-basics"
    names = [role_default]
    names += [s.ref for s in agent.skills if str(s.source) == "builtin" and s.ref != role_default]
    names += [f"{n} (local)" for n in sorted(agent.local_skills)]
    return names


def _agent_row(project: Project, agent: Any) -> dict[str, Any]:
    model = agent.model  # resolved by then; None only if the resolver was skipped
    pm = agent.private_messaging
    return {
        "id": agent.id,
        "role": str(agent.role),
        # what the scenario ASKED for, and what it actually GOT
        "model_category": str(agent.model_category) if agent.model_category else None,
        "model": model.model if model else None,
        "effort": (model.effort if model else None),
        "provider": (model.provider if model else None),
        "context_length": (model.context_length if model else None),
        "budget_usd": agent.budget.max_usd,
        "on_exhausted": str(agent.budget.on_exhausted),
        # the skills it ACTUALLY carried, not just the declared ones: Forge always seeds the role
        # core on top (see forge.skills.skill_texts), so listing only the manifest's list would
        # under-report what the agent was actually told. Local skills are named too.
        "skills": _effective_skills(agent),
        "private_messaging": {
            "enabled": pm.enabled,
            "peers": list(pm.peers),
            "include_referee": pm.include_referee,
            "max_per_round": pm.max_per_round,
        },
        "verdict_ends_realm": bool(agent.powers and agent.powers.verdict_ends_realm),
    }


def run_config(
    project: Project,
    provider: str,
    *,
    require_mention: bool,
    parameters: dict[str, str] | None = None,
    provider_fallback: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The resolved, effective configuration of one run — JSON-safe, for the chronicle + UI.

    `parameters` are the values this run was launched with (ADR-003). They are recorded even
    though the bound project already contains the substituted prose: reading a value back out of
    finished prose is guesswork, and comparing two runs of one scenario is the whole point of
    having parameters.

    `provider_fallback` is set only when `provider` is a substitution for a configured provider
    that could not be resolved (#47). A finished realm's spend is otherwise impossible to explain:
    the record would say `azure` with nothing to say the operator had chosen something else."""
    spec = project.spec
    env = spec.environment
    turns = spec.turns
    referee = project.referee

    return {
        "provider": provider,
        "provider_fallback": dict(provider_fallback) if provider_fallback else None,
        "parameters": dict(parameters or {}),
        "package": project.source,   # None for a project that was not loaded from a package
        # TURN CONTROL — the single most consequential switch, and the one people ask about first
        "turns": None if turns is None else {
            "policy": str(turns.policy),
            "advance": str(turns.advance),
            "enforcement": str(turns.enforcement),   # physics = the room refuses off-turn posts
            "order": str(turns.order),
            "referee_cue": str(turns.referee_cue),
            "silence_timeout_s": turns.silence_timeout_s,   # may have been RAISED to a floor
            "min_rounds_before_verdict": turns.min_rounds_before_verdict,
            "retire_after_misses": turns.retire_after_misses,
        },
        "free_response": not require_mention,
        # `require_mention` gates PARTICIPANTS. The referee is exempt in a realm without turns —
        # otherwise it would never receive the debate it exists to judge.
        "require_mention": require_mention,
        "referee_sees_all": bool(referee is not None and (turns is None or not require_mention)),
        "referee_opens": spec.referee_opens,
        "stall_nudge": spec.stall_nudge,
        "provide_tools": spec.provide_tools,
        "environment": {
            "network_egress": str(env.network_egress),
            "shared_folder": env.shared_folder.enabled,
            "allow_side_channels": env.allow_side_channels,
        },
        "mechanics": [
            {"kind": str(m.kind), "ruleset": m.ruleset} for m in spec.mechanics
        ],
        "termination": [
            {"type": str(c.type), "limit": c.limit, "channel": c.channel,
             "pattern": c.pattern, "path": c.path, "count": c.count}
            for c in project.effective_termination
        ],
        "referee": None if referee is None else referee.id,
        "agents": [_agent_row(project, a) for a in project.agents],
    }
