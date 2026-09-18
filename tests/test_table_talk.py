"""`table_talk`: a machine realm's referee reading the floor it is not standing on.

The gate is the SCENARIO's, not the platform's — a declaration that leaves the referee off the
floor meant it, and this tool must not be a way around that. The rest is paging: a cursor the
caller carries, so a second call returns what was said since the first and not the whole realm.
"""
from __future__ import annotations

import pytest

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.service import Identity
from bearpit.realmtools.table import TableService

ROOM = "!commons:realm.local"
REF = Identity("r", "pitboss", True, roster=("vega", "rigel"))
SEAT = Identity("r", "vega", False)


def _machine(reads_commons: bool) -> dict:
    return {
        "version": 1,
        "declaration": {
            "roles": {"dealer": {"members": "referee"}, "player": {"members": "participants"}},
            "states": ["s"], "initial": "s",
            "transitions": {"go": {"from": "s", "to": "same", "by": "dealer"}},
            "referee_reads_commons": reads_commons,
        },
        "members": {"dealer": ["pitboss"], "player": ["vega", "rigel"]},
        "roster": ["vega", "rigel"], "referee": "pitboss",
    }


async def _realm(reads_commons: bool = True) -> Chronicle:
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await c.append_event("r", EventKind.LIFECYCLE, {"event": "running", "commons_room": ROOM})
    await c.append_event("r", EventKind.MACHINE, _machine(reads_commons))
    for i, (who, body) in enumerate([
        ("@vega:realm.local", "limped in"),
        ("@rigel:realm.local", "folded 3s Jh from the cutoff"),
        ("@vega:realm.local", "priced it at 28%"),
    ]):
        await c.record_message("r", ROOM, who, body, ts_ms=1000 + i)
    await c.record_message("r", "!dm:realm.local", "@vega:realm.local", "psst", ts_ms=1500)
    return c


async def test_a_seat_may_not_read_the_floor_through_this_tool():
    c = await _realm()
    with pytest.raises(PermissionError, match="referee-only"):
        await TableService(c).read(SEAT)
    await c.close()


async def test_the_scenarios_gate_is_honoured_and_names_itself():
    """A declaration that kept the referee off the floor is not overridden by a platform tool —
    and the refusal says it was the scenario, so nobody hunts for a permissions bug."""
    c = await _realm(reads_commons=False)
    with pytest.raises(PermissionError, match="declaration"):
        await TableService(c).read(REF)
    await c.close()


async def test_the_referee_reads_the_commons_and_not_the_dms():
    c = await _realm()
    out = await TableService(c).read(REF)
    bodies = [m["body"] for m in out["messages"]]
    assert bodies == ["limped in", "folded 3s Jh from the cutoff", "priced it at 28%"]
    assert "psst" not in bodies, "a private message reached the referee through the floor reader"
    await c.close()


async def test_the_cursor_returns_only_what_is_new():
    c = await _realm()
    svc = TableService(c)
    first = await svc.read(REF, limit=2)
    assert [m["body"] for m in first["messages"]] == ["folded 3s Jh from the cutoff",
                                                      "priced it at 28%"]
    assert first["more_before"] is True
    later = await svc.read(REF, since_ms=first["cursor"])
    assert later["messages"] == [], "the cursor replayed messages the referee had already seen"
    await c.record_message("r", ROOM, "@rigel:realm.local", "check", ts_ms=2000)
    after = await svc.read(REF, since_ms=first["cursor"])
    assert [m["body"] for m in after["messages"]] == ["check"]
    await c.close()


async def test_the_cursor_is_the_last_message_not_now():
    """If the cursor were "now", a message that landed while the referee was thinking would be
    skipped forever — the one class of miss this tool exists to prevent."""
    c = await _realm()
    out = await TableService(c).read(REF)
    assert out["cursor"] == 1002
    await c.close()


async def test_an_empty_floor_keeps_the_callers_cursor():
    c = await _realm()
    out = await TableService(c).read(REF, since_ms=9999)
    assert out["messages"] == [] and out["cursor"] == 9999
    await c.close()
