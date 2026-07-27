"""Border States — deterministic order adjudicator (Full core, convoy-free).

Runs inside the Cartographer's container via run_code. `resolve_season` reads board.json (next to
this file), applies every power's sealed orders, resolves them simultaneously by the Full-core
Diplomacy rules (support, support-cut, bounces, dislodgement, retreats, and — on a Fall —
builds/disbands), persists the new board, and returns a human-readable report.

No network, no randomness: identical inputs give identical output. All units are equal-strength
armies; all provinces are land (no convoys/fleets), which removes Diplomacy's circular paradoxes.
"""

from __future__ import annotations

import json
import os
import re
from collections import deque

_DIR = os.path.dirname(os.path.abspath(__file__))
_BOARD = os.path.join(_DIR, "board.json")

# order tuples: ("HOLD",) | ("MOVE",dest) | ("SHOLD",target) | ("SMOVE",target,dest)


def _load(path: str | None = None) -> dict:
    with open(path or _BOARD) as f:
        return json.load(f)


def _save(board: dict, path: str | None = None) -> None:
    with open(path or _BOARD, "w") as f:
        json.dump(board, f, indent=2)


def _parse_orders(text: str, power: str, units: dict, adj: dict) -> tuple[dict, list[str]]:
    """One power's order block -> {province: order-tuple}. Foreign/illegal/duplicate orders are
    dropped (the unit falls back to HOLD) with a note, so a slip never wedges the season."""
    orders: dict[str, tuple] = {}
    notes: list[str] = []
    for raw in re.split(r"[;\n]+", text or ""):
        line = raw.strip()
        if not line:
            continue
        t = line.upper().split()
        prov = t[0] if t else ""
        if prov not in units or units[prov] != power:
            notes.append(f"{line!r}: not your unit — ignored")
            continue
        if prov in orders:
            notes.append(f"{line!r}: duplicate order for {prov} — kept the first")
            continue
        order: tuple | None = None
        if len(t) == 2 and t[1] == "HOLD":
            order = ("HOLD",)
        elif len(t) == 3 and t[1] == "MOVE":
            dest = t[2]
            order = ("MOVE", dest) if dest in adj.get(prov, []) else None
            if order is None:
                notes.append(f"{line!r}: {dest} not adjacent to {prov} — held")
        elif len(t) == 4 and t[1] == "SUPPORT" and t[3] == "HOLD":
            tgt = t[2]
            order = ("SHOLD", tgt) if tgt in adj.get(prov, []) else None
            if order is None:
                notes.append(f"{line!r}: can't support a hold in {tgt} (not adjacent) — held")
        elif len(t) == 5 and t[1] == "SUPPORT" and t[3] == "MOVE":
            tgt, dest = t[2], t[4]
            order = ("SMOVE", tgt, dest) if dest in adj.get(prov, []) else None
            if order is None:
                notes.append(f"{line!r}: can't support a move into {dest} (not adjacent) — held")
        else:
            notes.append(f"{line!r}: unrecognized order — held")
        if order is not None:
            orders[prov] = order
    return orders, notes


def _resolve(units: dict, orders: dict, adj: dict, homes: dict):
    """Resolve one season. Returns (landing, disbanded, events) where landing maps each surviving
    unit's origin province -> its province after moves+retreats, disbanded is the list of origins
    whose unit was destroyed, and events is a list of human-readable outcome strings."""
    movers = {p: o[1] for p, o in orders.items() if o[0] == "MOVE"}  # origin -> dest
    attacks_on: dict[str, set] = {}
    for p, dest in movers.items():
        attacks_on.setdefault(dest, set()).add(p)

    def cut(sp: str, order: tuple) -> bool:
        aimed = order[2] if order[0] == "SMOVE" else None  # attack from the target dest never cuts
        return any(atk != aimed for atk in attacks_on.get(sp, ()))

    sup_hold: dict[str, int] = {}
    sup_move: dict[tuple, int] = {}
    for sp, o in orders.items():
        if o[0] == "SHOLD":
            tgt = o[1]
            if tgt in units and tgt not in movers and tgt in adj[sp] and not cut(sp, o):
                sup_hold[tgt] = sup_hold.get(tgt, 0) + 1
        elif o[0] == "SMOVE":
            tgt, dest = o[1], o[2]
            if tgt in units and dest in adj[sp] and not cut(sp, o):
                sup_move[(tgt, dest)] = sup_move.get((tgt, dest), 0) + 1

    def astr(p: str) -> int:
        return 1 + sup_move.get((p, movers[p]), 0)

    def hstr(p: str) -> int:
        return 1 if p in movers else 1 + sup_hold.get(p, 0)

    # fixed point: a move stays successful until proven bounced (monotone -> converges)
    success = {p: True for p in movers}
    changed = True
    while changed:
        changed = False
        for p in list(movers):
            if not success[p]:
                continue
            dest = movers[p]
            rival = max((astr(q) for q in attacks_on[dest] if q != p), default=0)
            occ = units.get(dest)
            occ_leaves = dest in movers and success.get(dest) and movers.get(dest) != p
            defend, occ_stays = 0, None
            if occ is not None and not occ_leaves:
                occ_stays = occ
                defend = astr(dest) if movers.get(dest) == p else hstr(dest)  # head-to-head vs hold
            fail = astr(p) <= rival or astr(p) <= defend
            # no self-dislodgement: your move can't evict your own staying unit
            if not fail and occ_stays == units[p] and defend < astr(p):
                fail = True
            if fail:
                success[p] = False
                changed = True

    landing = {p: (movers[p] if p in movers and success[p] else p) for p in units}
    entrants = {movers[p]: p for p in movers if success[p]}

    events: list[str] = []
    for p in movers:
        dest = movers[p]
        events.append(f"{p}->{dest}: {'moved' if success[p] else 'bounced'}")

    # dislodgement: a unit that stayed but whose province was entered by another
    dislodged = [p for p in units if landing[p] == p and entrants.get(p, p) != p]
    standoff = {d for d in attacks_on if d not in entrants and len(attacks_on[d]) >= 2}
    occupied_after = {landing[p] for p in units if p not in dislodged}
    disbanded: list[str] = []
    home_of = {prov: pw for pw, provs in homes.items() for prov in provs}

    for p in sorted(dislodged):
        attacker_origin = entrants[p]
        cands = [
            n for n in adj[p]
            if n != attacker_origin and n not in occupied_after and n not in standoff
        ]
        # retreat priority: own home provinces first, then alphabetical
        cands.sort(key=lambda n: (home_of.get(n) != units[p], n))
        if cands:
            landing[p] = cands[0]
            occupied_after.add(cands[0])
            events.append(f"{p}: dislodged -> retreats to {cands[0]}")
        else:
            disbanded.append(p)
            landing.pop(p, None)
            events.append(f"{p}: dislodged -> no retreat, DISBANDED")

    return landing, disbanded, events


def _build_disband(units: dict, owners: dict, homes: dict, adj: dict, centers: set) -> list[str]:
    """Fall year-end: true up each power's unit count to its owned-center count. Mutates units."""
    events: list[str] = []
    by_power: dict[str, list[str]] = {}
    for prov, pw in units.items():
        by_power.setdefault(pw, []).append(prov)
    owned: dict[str, int] = {}
    for _c, pw in owners.items():
        if pw:
            owned[pw] = owned.get(pw, 0) + 1
    for pw in sorted(set(list(by_power) + list(owned))):
        have = len(by_power.get(pw, []))
        want = owned.get(pw, 0)
        if want > have:  # BUILD on vacant owned home provinces (capital first, then hinterland)
            for prov in homes.get(pw, []):
                if want <= have:
                    break
                if prov not in units and owners.get(prov, pw) == pw:  # vacant + still ours
                    units[prov] = pw
                    have += 1
                    events.append(f"{pw}: builds an army in {prov}")
        elif have > want:  # DISBAND the rearmost: farthest from home, keep the capital, then alpha
            homeset = set(homes.get(pw, []))
            cap = homes.get(pw, [None])[0]
            dist = _dist_to(adj, homeset)
            rear = sorted(by_power[pw], key=lambda pr: (-dist.get(pr, 99), pr == cap, pr))
            for prov in rear[: have - want]:
                del units[prov]
                events.append(f"{pw}: disbands the army in {prov}")
    return events


def _dist_to(adj: dict, targets: set) -> dict:
    """BFS hop distance from every province to the nearest target province."""
    dist = {t: 0 for t in targets}
    q = deque(targets)
    while q:
        cur = q.popleft()
        for n in adj.get(cur, []):
            if n not in dist:
                dist[n] = dist[cur] + 1
                q.append(n)
    return dist


def _next_season(label: str) -> str:
    m = re.match(r"[Yy](\d+)-(\w+)", label)
    if not m:
        return label
    year, season = int(m.group(1)), m.group(2).lower()
    return f"Y{year}-Fall" if season == "spring" else f"Y{year + 1}-Spring"


def resolve_season(label: str, orders_by_power: dict) -> str:
    """Resolve one season's sealed orders. Persists board.json and returns the public report."""
    b = _load()
    adj, homes = b["adjacency"], b["homes"]
    units: dict[str, str] = dict(b["units"])
    owners: dict[str, str] = dict(b.get("owners", {}))
    centers = set(b["centers"])
    is_fall = label.strip().upper().endswith("FALL")

    parsed: dict[str, tuple] = {}
    notes: list[str] = []
    for power, text in orders_by_power.items():
        po, pn = _parse_orders(text, power, units, adj)
        parsed.update(po)
        notes.extend(pn)
    orders = {p: parsed.get(p, ("HOLD",)) for p in units}

    landing, disbanded, events = _resolve(units, orders, adj, homes)

    new_units: dict[str, str] = {}
    for origin, pw in units.items():
        if origin in disbanded:
            continue
        new_units[landing[origin]] = pw

    captures: list[str] = []
    if is_fall:
        for prov, pw in list(new_units.items()):
            if prov in centers and owners.get(prov) != pw:
                owners[prov] = pw
                captures.append(f"{pw} captures {prov}")
        events += _build_disband(new_units, owners, homes, adj, centers)

    b["units"] = new_units
    b["owners"] = owners
    b["season"] = _next_season(label)
    _save(b)

    # supply-center tally
    tally: dict[str, int] = {}
    for _c, pw in owners.items():
        if pw:
            tally[pw] = tally.get(pw, 0) + 1
    tally_line = ", ".join(f"{pw} {n}" for pw, n in sorted(tally.items(), key=lambda x: -x[1]))

    lines = [f"=== {label} resolved ==="]
    lines += events or ["(all units held)"]
    if captures:
        lines.append("CAPTURES: " + "; ".join(captures))
    if notes:
        lines.append("ORDER NOTES: " + " | ".join(notes))
    lines.append("BOARD: " + ", ".join(f"{prov}({pw})" for prov, pw in sorted(b["units"].items())))
    lines.append(f"CENTERS: {tally_line}")
    lines.append(f"NEXT: {b['season']}")
    return "\n".join(lines)
