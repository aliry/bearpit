"""`validate_scenario` — the load-bearing safety check Scribe runs before any write.

It does two things: (1) re-runs `core.schema` model validation (so a hand-mutated `Project` that
violates a cross-field rule is caught, not written), and (2) applies a checklist derived from the
scenario-contract invariants (`docs/scenario-contract.md`). A scenario that violates a contract
invariant "does not fail loudly; it produces a plausible-looking transcript in which nothing
actually happened" — so these checks return actionable, plain-language messages the model relays.

This is a starter slice (4 invariants); it grows toward the full 18 with the Plan-3 skill pack.
Every check here holds for every bundled example (Scribe must never flag a package the platform
itself ships), and fires on a hand-built violation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from bearpit.core.schema import Project

# Invariant #1 — "nothing is real until a tool is called": a referee changes state ONLY through
# these tools, and its rubric must say so. Prose ("Cass is ejected") ejects nobody.
_STATE_CHANGE_TOOLS = ("rule", "eliminate", "score", "penalize", "flag", "tally")

# Invariant #3/#14 — phrasing that only makes sense when the platform enforces turn-taking. If a
# scenario reads as turn-based but sets no `turns` policy, it silently runs always-on/parallel.
_TURN_PHRASES = (
    "on your turn",
    "when it's your turn",
    "when it is your turn",
    "take turns",
    "turn order",
    "roster order",
    "your turn to",
    "wait for your turn",
    "each round",
    "per round",
    "next round",
    "this round",
)

# `@system`/`@everyone`/... are platform/broadcast handles, not roster ids.
_GOAL_MENTION_ALLOW = frozenset({"system", "everyone", "all", "room", "here", "channel"})

_MENTION_RE = re.compile(r"@([a-z0-9][a-z0-9-]*)")


@dataclass
class ValidationResult:
    """The outcome of validating a scenario: `ok` iff there are no `errors` (warnings don't block).

    Warnings are advisory; only errors make `ok` false and block a write.
    """

    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_scenario(project: Project) -> ValidationResult:
    """Validate a scenario against the schema and the contract checklist.

    Returns a `ValidationResult`; `ok` is false if any hard error was found. A schema failure short-
    circuits the contract checks (there is nothing coherent to check against a malformed manifest).
    """
    errors: list[str] = []
    warnings: list[str] = []

    try:
        Project.model_validate(project.model_dump(by_alias=True))
    except ValidationError as exc:
        for e in exc.errors():
            loc = ".".join(str(p) for p in e["loc"]) or "(root)"
            errors.append(f"schema: {loc}: {e['msg']}")
        return ValidationResult(ok=False, errors=errors, warnings=warnings)

    _check_referee_rubric(project, errors)
    _check_termination(project, errors)
    _check_turns(project, errors)
    _check_goal_agent_refs(project, errors)
    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)


def _scenario_text(project: Project) -> str:
    """All author-written prose a check might scan: guidelines, restrictions, goals, personas,
    per-agent goals, and the referee rubric."""
    parts: list[str] = list(project.spec.goals)
    if project.spec.guidelines:
        parts.append(project.spec.guidelines)
    if project.spec.restrictions:
        parts.append(project.spec.restrictions)
    for a in project.agents:
        if a.persona:
            parts.append(a.persona)
        if a.rubric:
            parts.append(a.rubric)
        parts.extend(a.goals)
    return "\n".join(parts)


def _check_referee_rubric(project: Project, errors: list[str]) -> None:
    """Invariant #1: a referee's rubric must instruct tool-based state changes."""
    ref = project.referee
    if ref is None:
        return
    rubric = ref.rubric or ""
    ends_on_verdict = ref.powers is not None and ref.powers.verdict_ends_realm
    if ends_on_verdict and not rubric.strip():
        errors.append(
            f"referee {ref.id!r} ends the realm by verdict but has no rubric — its rubric must "
            "instruct tool-based state changes (name a tool: rule/eliminate/score/penalize/flag)."
        )
        return
    if rubric.strip() and not any(t in rubric.lower() for t in _STATE_CHANGE_TOOLS):
        errors.append(
            f"referee {ref.id!r} rubric must instruct tool-based state changes — name the exact "
            "tool for each step (rule/eliminate/score/penalize/flag/tally). The platform records "
            "tool calls, not prose: a verdict posted as text ends nothing."
        )


def _check_termination(project: Project, errors: list[str]) -> None:
    """Invariant #5: a scored/verdict scenario must declare a termination condition."""
    scored = project.referee is not None or bool(project.spec.mechanics)
    if scored and not project.spec.termination:
        errors.append(
            "a scored/verdict scenario must declare at least one termination condition in "
            "spec.termination (referee_verdict / file / duration / stall) — without a "
            "deterministic ending the realm just runs until it is killed."
        )


def _check_turns(project: Project, errors: list[str]) -> None:
    """Invariant #3/#14: a turn-based scenario must set a `turns` policy."""
    if project.spec.turns is not None:
        return
    blob = _scenario_text(project).lower()
    hit = next((p for p in _TURN_PHRASES if p in blob), None)
    if hit is not None:
        errors.append(
            f"scenario reads as turn-based (it says {hit!r}) but sets no `turns` policy — add a "
            "turns block, or it runs always-on and parallel with no rounds and no floor control."
        )


def _check_goal_agent_refs(project: Project, errors: list[str]) -> None:
    """Invariant #14 corollary: every agent id @referenced in a goal must exist in the roster."""
    ids = {a.id for a in project.agents}
    goals: list[str] = list(project.spec.goals)
    for a in project.agents:
        goals.extend(a.goals)
    unknown: set[str] = set()
    for text in goals:
        for mention in _MENTION_RE.findall(text.lower()):
            if mention not in ids and mention not in _GOAL_MENTION_ALLOW:
                unknown.add(mention)
    for mention in sorted(unknown):
        errors.append(
            f"a goal references unknown agent id {mention!r} (@{mention}) — the roster has "
            f"{sorted(ids)}. Fix the id, or add the agent."
        )
