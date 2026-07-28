"""Termination evaluation (M5, §11, FR-8) — a pure function over a realm snapshot.

A realm ends on the FIRST matching condition. Conditions are OR-ed; the manual kill switch
is always available even if not declared. Keeping this pure makes every condition type
deterministically testable; the Warden loop just feeds it periodic snapshots.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

from bearpit.core.schema import TerminationCondition, TerminationKind, parse_duration


@dataclass(frozen=True)
class RealmSnapshot:
    """A point-in-time view the Warden loop assembles from its watchers."""

    elapsed_s: float = 0.0
    messages: list[tuple[str, str]] = field(default_factory=list)  # (channel, body)
    files: list[str] = field(default_factory=list)  # shared-folder paths present
    file_contents: dict[str, str] = field(default_factory=dict)  # path -> content
    spend: dict[str, tuple[float, float | None]] = field(default_factory=dict)  # agent->(spend,cap)
    verdict: str | None = None  # referee verdict outcome, if issued
    idle_s: float = 0.0  # seconds since the last agent message (for the `stall` condition)
    manual_stop: bool = False


@dataclass(frozen=True)
class TerminationFired:
    kind: TerminationKind
    detail: str


def _text_match(haystack: str, needle: str, mode: str) -> bool:
    if mode == "exact":
        return haystack == needle
    if mode == "regex":
        return re.search(needle, haystack) is not None  # validated at parse time
    return needle in haystack  # substring (default)


def _file_matches(cond: TerminationCondition, snap: RealmSnapshot) -> int:
    hits = 0
    for path in snap.files:
        if cond.path and not fnmatch.fnmatch(path, cond.path):
            continue
        if cond.content_match and not _text_match(
            snap.file_contents.get(path, ""), cond.content_match, cond.match_mode
        ):
            continue
        hits += 1
    return hits


def _message_matches(cond: TerminationCondition, snap: RealmSnapshot) -> int:
    hits = 0
    for channel, body in snap.messages:
        if cond.channel and channel != cond.channel:
            continue
        if cond.pattern and _text_match(body, cond.pattern, cond.match_mode):
            hits += 1
    return hits


def _duration_fired(cond: TerminationCondition, snap: RealmSnapshot) -> bool:
    if cond.limit is None:
        return False
    return snap.elapsed_s >= parse_duration(cond.limit)


def _budget_fired(cond: TerminationCondition, snap: RealmSnapshot) -> bool:
    capped = [(s, c) for s, c in snap.spend.values() if c is not None]
    if not capped:
        return False
    scope = cond.scope or "any_agent"
    all_capped = len(capped) == len(snap.spend)  # every agent has a cap
    if scope == "all_agents":
        return all_capped and all(s >= c for s, c in capped)
    if scope == "realm_total":
        # the realm collectively exhausted its aggregate budget (only meaningful if all capped)
        return all_capped and sum(s for s, _ in capped) >= sum(c for _, c in capped)
    return any(s >= c for s, c in capped)  # any_agent (default)


def evaluate_termination(
    conditions: list[TerminationCondition], snap: RealmSnapshot
) -> TerminationFired | None:
    for cond in conditions:
        k = cond.type
        if k == TerminationKind.MANUAL and snap.manual_stop:
            return TerminationFired(k, "operator stop")
        if k == TerminationKind.DURATION and _duration_fired(cond, snap):
            return TerminationFired(k, f"reached {cond.limit}")
        if k == TerminationKind.FILE and _file_matches(cond, snap) >= (cond.count or 1):
            return TerminationFired(k, f"file(s) matched {cond.path}")
        if k == TerminationKind.MESSAGE and _message_matches(cond, snap) >= (cond.count or 1):
            return TerminationFired(k, f"message matched {cond.pattern!r}")
        if k == TerminationKind.BUDGET_EXHAUSTED and _budget_fired(cond, snap):
            return TerminationFired(k, f"budget exhausted ({cond.scope or 'any_agent'})")
        if k == TerminationKind.REFEREE_VERDICT and snap.verdict is not None:
            return TerminationFired(k, f"verdict: {snap.verdict}")
        if k == TerminationKind.STALL and cond.limit and snap.idle_s >= parse_duration(cond.limit):
            return TerminationFired(k, f"no agent message for {cond.limit} (idle)")

    # the kill switch is always available, even if `manual` was never declared
    if snap.manual_stop:
        return TerminationFired(TerminationKind.MANUAL, "operator stop")
    return None
