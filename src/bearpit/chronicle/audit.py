"""Integrity findings derived from a finished realm's own chronicle.

A realm can report success and be worthless. Four archived realms (#90) hold verdicts scored on
content no participant ever posted: every container was healthy, termination fired normally, the
verdict event is well-formed, and the console renders them exactly like a sound run. The only
record that they are void is a GitHub issue.

These checks read what the chronicle already holds and say what it shows. They add no scenario
rule and make no judgement a reader could not make from the transcript — they just make it
unnecessary to read 141 messages to notice that none of them came from a participant.

Deliberately NOT here: anything that matches a runtime's own text. The interrupt cascade announces
itself with Hermes's `⚡ Interrupting current task`, which would be the easiest signal to grep for
and the wrong one to depend on — it would go stale the moment a second RuntimeAdapter lands. Every
check below reads only who posted, when, and what the run was configured to do.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bearpit.chronicle.chronicle import Chronicle, EventKind

_REPEAT_FLOOR = 10      # below this a repeat or two proves nothing
_REPEAT_RATIO = 0.5     # strictly below: the 0.5 boundary holds both a damaged and a sound realm


def _normalise(body: str) -> str:
    """Collapse the parts that vary between repeats of the same line — digits and whitespace — so
    a progress notice posted twenty times counts once."""
    return re.sub(r"\s+", " ", re.sub(r"\d+", "#", body.lower().strip()))[:160]


@dataclass(frozen=True)
class Finding:
    """One thing the chronicle shows about a run. `code` is stable for machines, `detail` is a
    sentence for a human reading the realm page."""

    code: str
    detail: str


def _running_config(events: Any) -> dict[str, Any] | None:
    for e in events:
        if e.kind == EventKind.LIFECYCLE and (e.payload or {}).get("event") == "running":
            cfg = (e.payload or {}).get("config")
            return cfg if isinstance(cfg, dict) else {}
    return None


async def audit_realm(chron: Chronicle, realm_id: str) -> list[Finding]:
    """What this realm's own record says about whether its result means anything."""
    events = list(await chron.events(realm_id))
    config = _running_config(events)
    if config is None:  # never started: no roster to reason about, so nothing to say
        return []

    agents = config.get("agents") or []
    referee = config.get("referee")
    participants = [
        str(a["id"]) for a in agents
        if isinstance(a, dict) and a.get("id") and a.get("id") != referee
        and "participant" in str(a.get("role", "")).lower()
    ]
    findings: list[Finding] = []

    # The defect behind #90, named directly. `require_mention: false` ungates everyone, which
    # includes the referee: it is woken by every message and answers each one, and each answer
    # interrupts the floor-holder mid-inference. Without a turn floor there is no one to interrupt,
    # so free response on its own is a legitimate choice and not reported.
    if config.get("require_mention") is False and referee:
        floor = (" It also ran a turn floor, so each of those replies interrupted whoever held it."
                 if config.get("turns") else "")
        findings.append(Finding(
            "ungated_referee",
            f"This run set `require_mention: false` with a referee ({referee}) on the roster, so "
            f"the referee was woken by every message and answered each one.{floor} Participants "
            f"may never have completed an inference.",
        ))

    if not participants:
        return findings

    prefixes = {p: f"@{realm_id}-{p}:" for p in participants}
    spoke: list[tuple[int, str, str]] = []
    for m in await chron.messages(realm_id):
        for pid, prefix in prefixes.items():
            if m.sender.startswith(prefix):
                spoke.append((m.ts_ms, pid, m.body or ""))
                break

    # A verdict is the run's whole output. One issued over silence is not a close call.
    if spoke == [] and any(e.kind == EventKind.VERDICT for e in events):
        findings.append(Finding(
            "verdict_without_participation",
            f"A verdict was issued but none of the {len(participants)} participants "
            f"({', '.join(sorted(participants))}) posted anything. Whatever the verdict describes, "
            f"it is not in this transcript.",
        ))

    # Rounds that completed with nobody speaking in them. Reported by number, so the finding points
    # at the stretch of transcript that is empty rather than asking the reader to find it.
    empty: list[int] = []
    window_start = 0
    for e in events:
        if e.kind != EventKind.TURN or (e.payload or {}).get("event") != "round_complete":
            continue
        rnd = (e.payload or {}).get("completed")
        if not isinstance(rnd, int):
            continue
        if not any(window_start <= ts <= e.ts_ms for ts, _, _ in spoke):
            empty.append(rnd)
        window_start = e.ts_ms
    if empty:
        shown = ", ".join(str(r) for r in empty[:12]) + ("…" if len(empty) > 12 else "")
        findings.append(Finding(
            "rounds_without_participation",
            f"{len(empty)} round(s) completed with no participant message: {shown}. "
            f"Anything the referee scored for those rounds was not said in them.",
        ))

    # A transcript that is mostly repeats. Counting messages is not counting content: the four runs
    # in #90 have 22-87 participant messages each, and the issue's own table records 0, 0, 1 and 4
    # SUBSTANTIVE posts, because the rest are a runtime's progress notices and agents insisting
    # they have already answered. Collapsing digits and whitespace turns "2 min elapsed, iteration
    # 1/90" and "3 min elapsed, iteration 2/90" into one line, so the measure is how many distinct
    # things were said rather than how many times the bus carried something.
    #
    # Calibrated against the archive, not guessed. Distinct/total for the four damaged runs is
    # 0.24, 0.13, 0.11, 0.50; for four sound ones 1.00, 1.00, 1.00, 0.50. The threshold is strictly
    # below 0.5 because the two 0.50s are a damaged run and a sound one, and `ungated_referee`
    # already catches that damaged run — a check that fires on a healthy realm is a check that gets
    # switched off. The floor of 10 excludes short realms, where a repeat or two proves nothing.
    bodies = [b for _, _, b in spoke]
    if len(bodies) >= _REPEAT_FLOOR:
        distinct = len({_normalise(b) for b in bodies})
        if distinct / len(bodies) < _REPEAT_RATIO:
            findings.append(Finding(
                "transcript_mostly_repeats",
                f"Participants sent {len(bodies)} messages but only {distinct} distinct ones. A "
                f"transcript this repetitive usually means they were interrupted before finishing "
                f"a thought, not that they said little.",
            ))
    return findings
