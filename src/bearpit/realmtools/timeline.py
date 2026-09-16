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


def diff_views(
    defn: MachineDef, before: dict[str, Any], after: dict[str, Any]
) -> list[dict[str, Any]]:
    """What moved between two `eng.view` results, each change named by its DECLARATION.

    The kind of a key is a property of what the scenario declared, never of what the value happens
    to look like at one moment. An earlier version dispatched on the runtime value — a list was a
    set, an object was owner data — and it was wrong for every public key that holds an object:
    rps-machine's `score` and poker's `stacks` both reported `kind: "owner"` with an `owner` field
    naming something that is not an owner at all (#123). It rendered plausibly, which is why the
    fixtures missed it: every one of them paired an object value with owner visibility, so the
    wrong branch produced the right-looking row.

    So the four kinds come from the declaration:

      * a declared set          -> `set`, with what was added and removed
      * owner visibility        -> `owner`, one change per owner entry
      * a value holding an object -> `map`, one change per entry — the same readable per-entry
        delta, making no claim about ownership
      * anything else           -> `value`

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
        declared = defn.data.get(key)
        # An undeclared key cannot reach here through `view()`, which only emits declared keys.
        # If one ever does, fall back on shape — but never to `owner`, which would fabricate the
        # one field a reader is entitled to trust.
        is_set = declared.type == "set" if declared else isinstance(bv or av, list)
        is_owner = declared.visibility == "owner" if declared else False

        if is_set:
            bs, as_ = set(bv or []), set(av or [])
            out.append({"kind": "set", "key": key,
                        "added": sorted(as_ - bs), "removed": sorted(bs - as_)})
        elif is_owner:
            bd: dict[str, Any] = bv or {}
            ad: dict[str, Any] = av or {}
            for owner in sorted(set(bd) | set(ad)):
                if bd.get(owner) != ad.get(owner):
                    out.append({"kind": "owner", "key": key, "owner": owner,
                                "from": bd.get(owner), "to": ad.get(owner)})
        elif isinstance(bv, dict) or isinstance(av, dict):
            bm: dict[str, Any] = bv or {}
            am: dict[str, Any] = av or {}
            for entry in sorted(set(bm) | set(am)):
                if bm.get(entry) != am.get(entry):
                    out.append({"kind": "map", "key": key, "entry": entry,
                                "from": bm.get(entry), "to": am.get(entry)})
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
                rows.append({"ts": ts, "payload": visible,
                             "changes": diff_views(defn, seen, nxt)})
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
