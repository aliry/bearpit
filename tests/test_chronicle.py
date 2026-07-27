"""Chronicle: append, query, transcript, final-report aggregation (on in-memory SQLite)."""

import pytest

from agentrealm.chronicle import Chronicle, EventKind


@pytest.fixture
async def chron():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    yield c
    await c.close()


async def test_append_and_query(chron: Chronicle):
    await chron.record_message("r1", "commons", "@vela", "hello", ts_ms=1000)
    await chron.record_message("r1", "commons", "@orin", "hi", ts_ms=2000)
    await chron.append_event("r1", EventKind.SPEND, {"agent": "vela", "usd": 0.02}, ts_ms=1500)
    await chron.record_message("r2", "commons", "@x", "other realm", ts_ms=1000)  # isolation

    msgs = await chron.messages("r1")
    assert [m.sender for m in msgs] == ["@vela", "@orin"]  # ordered by ts
    assert len(await chron.messages("r2")) == 1
    spend = await chron.events("r1", kind=EventKind.SPEND)
    assert len(spend) == 1 and spend[0].payload["agent"] == "vela"


async def test_transcript(chron: Chronicle):
    await chron.record_message("r1", "commons", "@vela", "rock", ts_ms=1000)
    await chron.record_message("r1", "dm", "@orin", "paper", ts_ms=2000)
    t = await chron.transcript("r1")
    assert "commons · @vela: rock" in t
    assert t.index("rock") < t.index("paper")  # chronological


async def test_final_report_aggregates(chron: Chronicle):
    for agent, usd in [("vela", 0.03), ("orin", 0.02), ("vela", 0.01)]:
        await chron.append_event("r1", EventKind.SPEND, {"agent": agent, "usd": usd})
    await chron.append_event("r1", EventKind.SCORE, {"agent": "orin", "delta": 5})
    await chron.append_event("r1", EventKind.SCORE, {"agent": "vela", "delta": 3})
    await chron.append_event("r1", EventKind.VERDICT, {"outcome": "orin wins 5-3"})
    await chron.record_message("r1", "commons", "@vela", "gg")

    report = await chron.final_report("r1", title="The Duel")
    assert "# The Duel — final report" in report
    assert "orin wins 5-3" in report
    assert "vela: $0.0400" in report  # summed 0.03 + 0.01
    assert "total: $0.0600" in report
    assert "- orin: 5" in report  # scores, highest first
    assert "1 messages, 6 events" in report


async def test_append_only_has_no_mutation_api(chron: Chronicle):
    # the append-only invariant: no update/delete methods are exposed
    assert not hasattr(chron, "update_event")
    assert not hasattr(chron, "delete_event")
    assert not hasattr(chron, "delete_message")
