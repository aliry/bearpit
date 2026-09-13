"""GET /api/realms/{realm_id}/machine — a machine realm's state and timeline, per seat.

Builds a REAL in-memory chronicle and seeds it with real MACHINE + GAME events (the shape
`machine_record()` produces — see `src/bearpit/gatekeeper/machine_record.py`), so the true path
runs end to end: the last-MACHINE-wins rule, the `e.id > head.id` filter, and the engine's own
visibility rules — nothing here is mocked.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.testclient import TestClient

from bearpit.chronicle import Chronicle, EventKind
from bearpit.gatekeeper.api import create_app
from bearpit.realmtools.machine_service import MACHINE_VERSION


class FakeManager:
    def __init__(self, max_active=6):
        self.runs = {}
        self.started = []
        self.stopped = []
        self.projects = {}
        self.parameters = {}
        self.max_active = max_active

    def start(self, realm_id, project, *, require_mention=True, parameters=None,
              allow_provider_fallback=False):
        self.started.append((realm_id, len(project.agents)))
        self.projects[realm_id] = project

    def stop(self, realm_id):
        self.stopped.append(realm_id)

    def active(self):
        return [r for r, _ in self.started]


# A synthetic declaration — no scenario this platform ships declares "tally"/"picked"/"secret"/
# "notes"/"open"/"closed". One key of each visibility, so a participant lens has something to
# lose: "tally" (public), "picked" (public set), "secret" (owner), "notes" (referee-only).
DECL: dict[str, Any] = {
    "states": ["open", "closed"],
    "initial": "open",
    "terminal": ["closed"],
    "roles": {"players": {"members": ["a", "b"]}, "judge": {"members": "referee"}},
    "data": {
        "tally": {"visibility": "public", "type": "value"},
        "picked": {"visibility": "public", "type": "set"},
        "secret": {"visibility": "owner", "type": "value"},
        "notes": {"visibility": "referee", "type": "value"},
    },
    "transitions": {
        "bump": {"from": ["open"], "to": "same", "by": "players", "effects": []},
        "close": {"from": ["open"], "to": "closed", "by": "judge", "effects": []},
    },
}
MEMBERS = {"players": ["a", "b"], "judge": ["j"]}
ROSTER = ["a", "b"]
REFEREE = "j"


def _machine_payload(
    *, version: int = MACHINE_VERSION, declaration: dict[str, Any] = DECL,
    members: dict[str, list[str]] = MEMBERS, roster: list[str] = ROSTER,
    referee: str | None = REFEREE,
) -> dict[str, Any]:
    return {"version": version, "declaration": declaration, "members": members,
            "roster": roster, "referee": referee}


# One row of each visibility a "set" write can carry: an owner write only "a" can see, a
# referee-only write nobody else can see, and a public write everyone can see.
EVENTS: list[dict[str, Any]] = [
    {"op": "set", "caller": "j", "key": "secret", "owner": "a", "value": "z", "log": "owner"},
    {"op": "set", "caller": "j", "key": "notes", "value": "n", "log": "referee"},
    {"op": "set", "caller": "j", "key": "tally", "value": 5, "log": "public"},
]


async def _seed(chron: Chronicle, realm_id: str, machine: dict[str, Any] | None,
                 events: list[dict[str, Any]] | None = None) -> None:
    if machine is not None:
        await chron.append_event(realm_id, EventKind.MACHINE, machine)
    for ev in events or []:
        await chron.append_event(realm_id, EventKind.GAME, ev)


async def test_a_realm_with_no_machine_event_returns_null_not_404() -> None:
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        await chron.append_event("plain", EventKind.LIFECYCLE, {"event": "running"})
        app = create_app(chron=chron, manager=FakeManager())
        with TestClient(app) as c:
            r = c.get("/api/realms/plain/machine")
            assert r.status_code == 200
            assert r.json() == {"machine": None}
    finally:
        await chron.close()


async def test_a_machine_realm_returns_declaration_state_and_one_row_per_visible_event() -> None:
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        await _seed(chron, "arena", _machine_payload(), EVENTS)
        app = create_app(chron=chron, manager=FakeManager())
        with TestClient(app) as c:
            r = c.get("/api/realms/arena/machine", params={"as": "j"})
            assert r.status_code == 200
            m = r.json()["machine"]
            assert m["as"] == "j"
            assert m["seats"] == ["a", "b", "j"]
            assert m["declaration"]["initial"] == "open"
            assert m["state"]["state"] == "open"  # no `act` fired, only `set`s
            assert m["error"] is None
            # the referee sees all three writes: owner, referee-only, and public
            assert len(m["timeline"]) == 3
            assert m["state"]["data"]["notes"] == "n"
            assert m["state"]["data"]["tally"] == 5
    finally:
        await chron.close()


async def test_omitting_as_defaults_to_the_referee() -> None:
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        await _seed(chron, "arena", _machine_payload(), EVENTS)
        app = create_app(chron=chron, manager=FakeManager())
        with TestClient(app) as c:
            r = c.get("/api/realms/arena/machine")
            assert r.status_code == 200
            m = r.json()["machine"]
            assert m["as"] == "j"
            assert len(m["timeline"]) == 3  # the referee's own (full) view
    finally:
        await chron.close()


async def test_a_participant_lens_sees_strictly_less_and_no_referee_key_leaks() -> None:
    """The security-relevant case. `a` owns the owner-visibility write and so sees it plus the
    public one (2 rows); the referee-only write must never surface, neither as a row nor folded
    into the runtime state or log a participant can retrieve.

    Scope of the "no referee-only key" check: the runtime `state` + `timeline`, not the
    `declaration`. The declaration is the game's own rulebook — every caller is shown the full
    schema, including that a `notes` field exists and who may see it (`declaration_view` in
    `bearpit/realmtools/machine.py` only redacts hidden ROLE membership, never data key names;
    `tests/test_machine_timeline.py::test_a_participant_lens_sees_strictly_less` scopes its own
    "did a referee key leak" check the same way). What must never leak to a participant is a
    referee-only key's *value*, or its name surfacing in a row/change they were not meant to see.
    """
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        await _seed(chron, "arena", _machine_payload(), EVENTS)
        app = create_app(chron=chron, manager=FakeManager())
        with TestClient(app) as c:
            ref = c.get("/api/realms/arena/machine", params={"as": "j"}).json()["machine"]
            seat = c.get("/api/realms/arena/machine", params={"as": "a"}).json()["machine"]
            assert len(seat["timeline"]) < len(ref["timeline"])
            assert len(seat["timeline"]) == 2
            assert "notes" not in seat["state"]["data"]
            runtime = json.dumps({"state": seat["state"], "timeline": seat["timeline"]})
            assert "notes" not in runtime, "a referee-only key leaked into a seat's runtime view"
    finally:
        await chron.close()


async def test_an_id_in_no_role_is_400_not_a_silent_participant_view() -> None:
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        await _seed(chron, "arena", _machine_payload(), EVENTS)
        app = create_app(chron=chron, manager=FakeManager())
        with TestClient(app) as c:
            r = c.get("/api/realms/arena/machine", params={"as": "nobody"})
            assert r.status_code == 400
    finally:
        await chron.close()


async def test_a_version_mismatch_returns_null_not_a_raise() -> None:
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        await _seed(chron, "stale", _machine_payload(version=MACHINE_VERSION + 1), EVENTS)
        app = create_app(chron=chron, manager=FakeManager())
        with TestClient(app) as c:
            r = c.get("/api/realms/stale/machine")
            assert r.status_code == 200
            assert r.json() == {"machine": None}
    finally:
        await chron.close()


async def test_a_chronicle_that_stops_replaying_returns_200_with_error_and_the_good_prefix() -> (
    None
):
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        broken = [*EVENTS, {"op": "teleport", "caller": "a"}]  # unknown op: replay must stop here
        await _seed(chron, "damaged", _machine_payload(), broken)
        app = create_app(chron=chron, manager=FakeManager())
        with TestClient(app) as c:
            r = c.get("/api/realms/damaged/machine", params={"as": "j"})
            assert r.status_code == 200
            m = r.json()["machine"]
            assert m["error"] is not None
            assert len(m["timeline"]) == 3  # the rows that did replay survive the failure
    finally:
        await chron.close()


async def test_a_referee_absent_from_every_role_is_still_a_valid_default_and_seat() -> None:
    """Correction 3: `machine_record()` only puts the referee into `members` when some role's
    `members` resolves to "referee" — a machine that never gives the referee a role (it only
    rules on the game, e.g. via out-of-band verdicts) would otherwise leave `?as=` unset AND
    `?as=<referee>` 400ing on the platform's own default caller. `seats` must include the
    referee even when no role binds them.
    """
    decl_no_referee_role: dict[str, Any] = {
        "states": ["open", "closed"], "initial": "open", "terminal": ["closed"],
        "roles": {"players": {"members": ["a", "b"]}},
        "transitions": {"bump": {"from": ["open"], "to": "closed", "by": "players",
                                  "effects": []}},
    }
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        machine = _machine_payload(
            declaration=decl_no_referee_role, members={"players": ["a", "b"]},
        )
        await _seed(chron, "orphan-ref", machine)
        app = create_app(chron=chron, manager=FakeManager())
        with TestClient(app) as c:
            default = c.get("/api/realms/orphan-ref/machine")
            assert default.status_code == 200
            m = default.json()["machine"]
            assert m["as"] == "j"
            assert "j" in m["seats"]

            explicit = c.get("/api/realms/orphan-ref/machine", params={"as": "j"})
            assert explicit.status_code == 200
    finally:
        await chron.close()


async def test_the_last_machine_event_wins_and_old_game_events_are_dropped() -> None:
    """A realm id can be reused. The head must be the LAST MACHINE event, and only GAME events
    with `id > head.id` count — a stale write from the previous incarnation must never resurface.
    """
    decl_v2: dict[str, Any] = {
        "states": ["ready", "closed"], "initial": "ready", "terminal": ["closed"],
        "roles": {"players": {"members": ["a", "b"]}, "judge": {"members": "referee"}},
        "data": {"tally": {"visibility": "public", "type": "value"}},
        "transitions": {
            "bump": {"from": ["ready"], "to": "same", "by": "players", "effects": []},
            "close": {"from": ["ready"], "to": "closed", "by": "judge", "effects": []},
        },
    }
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    try:
        await _seed(chron, "recycled", _machine_payload())
        await chron.append_event("recycled", EventKind.GAME, {
            "op": "set", "caller": "j", "key": "tally", "value": 111, "log": "public",
        })
        await _seed(chron, "recycled", _machine_payload(declaration=decl_v2))
        await chron.append_event("recycled", EventKind.GAME, {
            "op": "set", "caller": "j", "key": "tally", "value": 42, "log": "public",
        })
        app = create_app(chron=chron, manager=FakeManager())
        with TestClient(app) as c:
            r = c.get("/api/realms/recycled/machine", params={"as": "j"})
            assert r.status_code == 200
            m = r.json()["machine"]
            assert m["declaration"]["initial"] == "ready"  # the SECOND declaration, not the first
            assert m["state"]["state"] == "ready"
            assert len(m["timeline"]) == 1  # only the write after the second MACHINE event
            assert m["timeline"][0]["payload"]["value"] == 42
            values = [row["payload"]["value"] for row in m["timeline"]]
            assert 111 not in values, "a stale write from the previous incarnation resurfaced"
    finally:
        await chron.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
