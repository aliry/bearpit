"""Arbiter (v2 The Law): platform-maintained scoreboard, penalties, referee-only gating."""

import pytest

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools import ArbiterService, Identity

REF = Identity("duel", "themis", True)
PLAYER = Identity("duel", "vela", False)


@pytest.fixture
async def arb():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    yield ArbiterService(c), c
    await c.close()


async def test_platform_keeps_the_running_score(arb):
    a, chron = arb
    # the whole point: the platform accumulates the score across many awards (referee can't)
    await a.score(REF, "vela", 1, "round 1")
    await a.score(REF, "orin", 1, "round 2")
    board = await a.score(REF, "vela", 1, "round 3")
    assert board == {"vela": 2, "orin": 1}
    assert await a.scoreboard(REF) == {"vela": 2, "orin": 1}  # read-back matches
    # every change is a chronicled SCORE event (auditable)
    scores = await chron.events("duel", kind=EventKind.SCORE)
    assert len(scores) == 3 and scores[0].payload["issued_by"] == "themis"


async def test_penalize_flags_and_deducts(arb):
    a, chron = arb
    await a.score(REF, "vela", 5, "good play")
    board = await a.penalize(REF, "vela", 2, "revealed move early")
    assert board["vela"] == 3  # 5 - 2
    violations = await chron.events("duel", kind=EventKind.VIOLATION)
    assert len(violations) == 1 and violations[0].payload["reason"] == "revealed move early"


async def test_only_referee_may_score_penalize_flag_rule(arb):
    a, _ = arb
    for call in (
        a.score(PLAYER, "orin", 1, "x"),
        a.penalize(PLAYER, "orin", 1, "x"),
        a.flag(PLAYER, "orin", "x"),
        a.rule(PLAYER, "orin wins", ""),
    ):
        with pytest.raises(PermissionError):
            await call
    # but anyone may READ the scoreboard (it's public)
    assert await a.scoreboard(PLAYER) == {}


async def test_rule_records_verdict_with_final_scoreboard(arb):
    a, chron = arb
    await a.score(REF, "orin", 3, "won")
    await a.score(REF, "vela", 1, "consolation")
    res = await a.rule(REF, "orin wins 3-1", "best of five")
    assert res["outcome"] == "orin wins 3-1"
    verdicts = await chron.events("duel", kind=EventKind.VERDICT)
    assert verdicts[-1].payload["scoreboard"] == {"orin": 3, "vela": 1}


async def test_rule_gated_by_configured_min_rounds(arb):
    a, chron = arb
    order = ["vela", "orin"]
    # scenario requires 1 full round (min_rounds=1); still in round 1 -> rule() refused
    await chron.append_event("duel", EventKind.TURN,
                             {"round": 1, "order": order, "position": 0, "current": "vela",
                              "min_rounds": 1})
    res = await a.rule(REF, "vela wins", "")
    assert "error" in res and "too early" in res["error"]
    assert not await chron.events("duel", kind=EventKind.VERDICT)  # no verdict recorded
    # once a full round has completed (round 2), the verdict goes through
    await chron.append_event("duel", EventKind.TURN,
                             {"round": 2, "order": order, "position": 0, "current": "vela",
                              "min_rounds": 1})
    res = await a.rule(REF, "vela wins", "after a round")
    assert res["outcome"] == "vela wins"
    # a verdict is FINAL — a repeat rule() does not flip the outcome or record a second verdict
    res2 = await a.rule(REF, "orin wins", "changed my mind")
    assert res2["outcome"] == "vela wins" and "final" in res2.get("note", "")
    assert len(await chron.events("duel", kind=EventKind.VERDICT)) == 1


async def test_rule_not_gated_when_min_rounds_zero(arb):
    a, chron = arb
    # default policy (min_rounds=0): a sudden-death turns game may rule on move 1 — no guard
    await chron.append_event("sd", EventKind.TURN,
                             {"round": 1, "order": ["vela", "orin"], "position": 0,
                              "current": "vela", "min_rounds": 0})
    res = await a.rule(Identity("sd", "ref", True), "vela wins on move 1", "")
    assert res["outcome"] == "vela wins on move 1"


async def test_final_report_includes_scores_and_violations(arb):
    a, chron = arb
    await a.score(REF, "orin", 3, "won")
    await a.penalize(REF, "vela", 1, "late submission")
    await a.rule(REF, "orin wins", "")
    report = await chron.final_report("duel", title="The Duel")
    assert "orin wins" in report
    assert "## Scores" in report and "orin: 3" in report
    assert "## Violations" in report and "vela: late submission" in report


async def test_eliminate_is_tool_based_and_chronicled(arb):
    # session mechanics must never hinge on message parsing: the referee CALLS eliminate() and the
    # ejection lands as an ELIMINATION event the host enforces as physics.
    a, chron = arb
    res = await a.eliminate(REF, "juno", "voted out round 1")
    assert res["eliminated"] == "juno"
    evs = await chron.events("duel", kind=EventKind.ELIMINATION)
    assert evs[-1].payload == {"agent": "juno", "reason": "voted out round 1",
                               "issued_by": "themis"}


async def test_eliminate_sanitizes_markdown_and_normalizes(arb):
    # the model may pass '`Juno`' or ' JUNO ' — the event must carry the clean roster id
    a, chron = arb
    await a.eliminate(REF, "`Juno`")
    assert (await chron.events("duel", kind=EventKind.ELIMINATION))[-1].payload["agent"] == "juno"


async def test_eliminate_none_closes_a_round_with_nobody_out(arb):
    a, chron = arb
    res = await a.eliminate(REF, "none", "tie")
    assert res["eliminated"] is None
    assert (await chron.events("duel", kind=EventKind.ELIMINATION))[-1].payload["agent"] is None


async def test_only_the_referee_may_eliminate(arb):
    a, chron = arb
    with pytest.raises(PermissionError):
        await a.eliminate(PLAYER, "themis")
    assert await chron.events("duel", kind=EventKind.ELIMINATION) == []


async def test_concurrent_first_scores_do_not_lose_an_increment(arb):
    """The referee's board must not silently drop a point — issue #17.

    `_board()` fills a per-realm cache, and its only yield point is the chronicle read that sits
    between "is it cached?" and "store it". Two scores landing inside that window each built their
    own dict, and the later store discarded the earlier one's increment — while both still appended
    their SCORE event. The chronicle then held one more point than the board the referee ruled on.

    That is the rps-rv1 signature exactly: 3 SCORE events for orin against `scoreboard {orin: 2}` in
    the verdict — a 3-2 ledger under a 2-2 ruling, with nothing failing loudly.

    Both reads must yield before either stores, which is what a real async driver does under load.
    Delaying only ONE read does not reproduce it: the other call finishes first, and the stalled
    rebuild then picks that event up and self-corrects.

    Asserted against the number of score() calls rather than against the chronicle: sqlite's
    in-memory StaticPool shares a single connection across sessions, so concurrent commits
    interleaved with a read can lose rows there. Verified against live Postgres, which keeps all
    ten of ten — so no test here may assert an exact event count under concurrency."""
    import asyncio

    a, chron = arb
    real_events = chron.events

    async def slow_read(*args, **kwargs):
        await asyncio.sleep(0.05)          # every read yields, so both fills are in flight together
        return await real_events(*args, **kwargs)

    chron.events = slow_read  # type: ignore[method-assign]
    await asyncio.gather(
        a.score(REF, "orin", 1, "round R1"),
        a.score(REF, "orin", 1, "round R1"),   # the second delivery
    )
    chron.events = real_events  # type: ignore[method-assign]

    board = await a.scoreboard(REF)
    assert board["orin"] == 2.0, (
        f"board is {board['orin']} after two scores — a concurrent cache fill dropped an increment"
    )


async def test_many_concurrent_scores_all_land(arb):
    """Stronger form of the same guard: nothing is lost at any concurrency.

    The two-call case can pass by luck if the interleaving happens to serialise. Ten concurrent
    first-scores cannot: every one enters `_board()` before any fill completes, so a lock that
    hands late callers a fresh dict — rather than the one already stored — loses nine of them."""
    import asyncio

    a, chron = arb
    real_events = chron.events

    async def slow_read(*args, **kwargs):
        await asyncio.sleep(0.02)
        return await real_events(*args, **kwargs)

    chron.events = slow_read  # type: ignore[method-assign]
    await asyncio.gather(*(a.score(REF, "orin", 1, f"round R{i}") for i in range(10)))
    chron.events = real_events  # type: ignore[method-assign]

    board = await a.scoreboard(REF)
    assert board["orin"] == 10.0, f"board lost increments: {board['orin']} of 10"
