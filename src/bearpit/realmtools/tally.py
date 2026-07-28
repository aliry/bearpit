"""Deterministic tally rulesets (M10, §9.5).

Pure functions over revealed submissions {agent: payload}. This is the "adjudicate" half of the
sealed-submit mechanic: given what everyone actually submitted, compute the result by a named
ruleset — never by an LLM's arithmetic (the whole point, per the POC findings).

Every ruleset is GENERIC and content-agnostic — the platform ships NO single game's rules
(Principle 10 / ADR-002). A game's specific relation (e.g. rock-paper-scissors) lives in the
SCENARIO and is passed to the `dominance` ruleset as a beat map in `config`, never hard-coded here.

Two shapes of result: competition rulesets pick a winning *agent* (dominance, high-bid); voting
rulesets pick a winning *option* (plurality, majority, unanimous). `TallyResult.kind` disambiguates;
`result` is None for a tie / no-decision.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TallyResult:
    ruleset: str
    result: str | None  # winning agent | winning option | None (tie / no decision)
    kind: str  # "agent" | "option" | "tie"
    detail: dict[str, Any] = field(default_factory=dict)


class TallyError(ValueError):
    pass


# Every ruleset is (submissions, config) -> TallyResult. Most ignore config; `dominance` reads the
# scenario-supplied beat map from it. Keeping one signature lets the dispatch stay a plain lookup.
Ruleset = Callable[[dict[str, str], dict[str, Any]], TallyResult]


def _dominance(sub: dict[str, str], config: dict[str, Any]) -> TallyResult:
    """Generic non-transitive dominance (the rock-paper-scissors shape, and any like it). A
    submission wins if its token beats EVERY other submitted token per a caller-supplied beat map.
    Content-agnostic: the beat relation is the SCENARIO's, passed in
    ``config['beats'] = {token: [tokens it beats]}`` — never hard-coded here (Principle 10). A token
    absent from the map is invalid: it cannot win, but a valid token beats it. Works for 2 players
    (pairwise) or N (a token that beats all others present)."""
    raw = config.get("beats") if isinstance(config, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise TallyError("dominance needs config['beats'] = {token: [tokens it beats]}")
    beats = {
        str(k).strip().lower():
        {str(x).strip().lower() for x in (v if isinstance(v, (list, tuple, set)) else [v])}
        for k, v in raw.items()
    }
    moves = {a: str(p).strip().lower() for a, p in sub.items()}

    def wins(a: str) -> bool:
        m = moves[a]
        if m not in beats:
            return False  # an invalid token can never win
        others = [o for b, o in moves.items() if b != a]
        return bool(others) and all(o not in beats or o in beats[m] for o in others)

    winners = [a for a in moves if wins(a)]
    detail: dict[str, Any] = {"moves": moves}
    if len(winners) == 1:
        return TallyResult("dominance", winners[0], "agent", detail=detail)
    return TallyResult("dominance", None, "tie", detail=detail)


def _bid(sub: dict[str, str], pick: Callable[[Any], int], name: str) -> TallyResult:
    """Shared integer-bid tally: `pick` is max (high-bid) or min (low-bid / reverse auction)."""
    bids: dict[str, int] = {}
    for agent, raw in sub.items():
        try:
            bids[agent] = int(str(raw).strip())
        except ValueError:
            raise TallyError(f"{name} needs integer bids, got {raw!r}") from None
    if not bids:
        return TallyResult(name, None, "tie")
    target = pick(bids.values())
    leaders = [a for a, v in bids.items() if v == target]
    if len(leaders) != 1:
        return TallyResult(name, None, "tie", detail={"bids": bids})
    return TallyResult(name, leaders[0], "agent", detail={"bids": bids, "winning_bid": target})


def _high_bid(sub: dict[str, str], config: dict[str, Any]) -> TallyResult:
    return _bid(sub, max, "high-bid")


def _low_bid(sub: dict[str, str], config: dict[str, Any]) -> TallyResult:
    return _bid(sub, min, "low-bid")  # reverse / procurement auction: lowest bid wins


def _counts(sub: dict[str, str]) -> Counter[str]:
    return Counter(v.strip() for v in sub.values())


def _plurality(sub: dict[str, str], config: dict[str, Any]) -> TallyResult:
    counts = _counts(sub)
    if not counts:
        return TallyResult("plurality", None, "tie")
    ranked = counts.most_common()
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return TallyResult("plurality", None, "tie", detail={"counts": dict(counts)})
    return TallyResult("plurality", ranked[0][0], "option", detail={"counts": dict(counts)})


def _majority(sub: dict[str, str], config: dict[str, Any]) -> TallyResult:
    counts = _counts(sub)
    n = sum(counts.values())
    for option, c in counts.items():
        if c * 2 > n:
            return TallyResult("majority", option, "option", detail={"counts": dict(counts)})
    return TallyResult("majority", None, "tie", detail={"counts": dict(counts)})


def _unanimous(sub: dict[str, str], config: dict[str, Any]) -> TallyResult:
    options = {v.strip() for v in sub.values()}
    if sub and len(options) == 1:
        return TallyResult("unanimous", next(iter(options)), "option")
    return TallyResult("unanimous", None, "tie", detail={"options": sorted(options)})


_RULESETS: dict[str, Ruleset] = {
    "dominance": _dominance,
    "high-bid": _high_bid,
    "low-bid": _low_bid,
    "plurality": _plurality,
    "majority": _majority,
    "unanimous": _unanimous,
}

# The built-in names, mirrored in core.schema.BUILTIN_RULESETS for parse-time validation.
BUILTIN_RULESETS = frozenset(_RULESETS)
RULESETS = tuple(_RULESETS)


def register_ruleset(name: str, fn: Ruleset) -> None:
    """Extension point (§9.5 / #31): add a deterministic tally ruleset so a scenario isn't limited
    to the built-in set. Name a custom ruleset ``custom:<x>`` so the manifest validator accepts it
    without knowing it at parse time."""
    _RULESETS[name] = fn


def tally(
    ruleset: str, submissions: dict[str, str], config: dict[str, Any] | None = None
) -> TallyResult:
    fn = _RULESETS.get(ruleset)
    if fn is None:
        raise TallyError(f"unknown ruleset {ruleset!r} (have: {', '.join(sorted(_RULESETS))})")
    return fn(submissions, config or {})
