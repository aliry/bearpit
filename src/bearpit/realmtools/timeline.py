"""A read-only timeline over a machine realm's chronicle: what changed, through one caller's eyes.

Nothing here knows what any scenario is about. Every key, every grouping and every label comes
from the machine's own declaration, so a scenario this module has never seen renders correctly the
first time.

The load-bearing decision is that changes are computed by diffing the caller's VIEW, never the
raw state. Diffing the state and filtering afterwards leaks twice: a hidden key that moved still
shows as 'something changed', and the gap where a row would have been is itself information. A key
the caller cannot see never enters the comparison, so it can neither appear nor be inferred.
"""
from __future__ import annotations

from typing import Any

from bearpit.core.machine import MachineDef
from bearpit.realmtools import machine as eng


def diff_views(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """What moved between two `eng.view` results.

    Shape is driven by what the view holds: a list is a set, a dict is an owner map, anything else
    is a scalar. The declaration already decided which is which, so this needs no schema.

    `actor_since` is deliberately absent: it moves whenever `actor` does and adds nothing.
    """
    out: list[dict[str, Any]] = []
    if before.get("state") != after.get("state"):
        out.append({"kind": "state", "from": before.get("state"), "to": after.get("state")})
    if before.get("actor") != after.get("actor"):
        out.append({"kind": "actor", "from": before.get("actor"), "to": after.get("actor")})

    b_data: dict[str, Any] = before.get("data") or {}
    a_data: dict[str, Any] = after.get("data") or {}
    for key in sorted(set(b_data) | set(a_data)):
        bv, av = b_data.get(key), a_data.get(key)
        if bv == av:
            continue
        if isinstance(bv, list) or isinstance(av, list):
            bs, as_ = set(bv or []), set(av or [])
            out.append({"kind": "set", "key": key,
                        "added": sorted(as_ - bs), "removed": sorted(bs - as_)})
        elif isinstance(bv, dict) or isinstance(av, dict):
            bd: dict[str, Any] = bv or {}
            ad: dict[str, Any] = av or {}
            for owner in sorted(set(bd) | set(ad)):
                if bd.get(owner) != ad.get(owner):
                    out.append({"kind": "owner", "key": key, "owner": owner,
                                "from": bd.get(owner), "to": ad.get(owner)})
        else:
            out.append({"kind": "value", "key": key, "from": bv, "to": av})
    return out


def build_timeline(
    defn: MachineDef, bindings: eng.Bindings,
    events: list[tuple[int, dict[str, Any]]], start_ms: int, caller: str,
) -> dict[str, Any]:
    """Replay the chronicle, emitting one row per event the caller is allowed to see.

    A replay failure is reported, not raised. This serves a console: showing the prefix that did
    replay plus the reason it stopped is strictly more useful than a 500, and a realm whose
    chronicle has been damaged is exactly when someone needs to look at it.
    """
    rows: list[dict[str, Any]] = []
    seen = eng.view(defn, bindings, eng.initial_state(defn, bindings, start_ms), caller)
    error: str | None = None
    try:
        for ts, payload, after in eng.replay_steps(defn, bindings, events, start_ms):
            nxt = eng.view(defn, bindings, after, caller)
            visible = eng.log_row(defn, bindings, payload, caller)
            if visible is not None:
                rows.append({"ts": ts, "payload": visible, "changes": diff_views(seen, nxt)})
            # Advance past a row the caller may not see, so whatever it moved is dropped rather
            # than folded into the next visible row. Misattributing a hidden actor's change to
            # the next agent to act would be worse than omitting it.
            seen = nxt
    except (eng.ReplayError, KeyError) as exc:
        # `replay_steps` raises a bare KeyError on a row missing caller/key/transition. Kept
        # narrow on purpose: catching ValueError too would report a bug in this module's own
        # diffing as 'the chronicle is damaged', sending the reader to the wrong place entirely.
        error = f"the chronicle stops replaying here: {exc}"
    return {"state": seen, "timeline": rows, "error": error}
