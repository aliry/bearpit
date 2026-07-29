"""Warden: termination evaluation (every kind) + conclude/watch orchestration."""

from datetime import timedelta

from bearpit.chronicle import Chronicle, EventKind
from bearpit.core.schema import TerminationCondition, TerminationKind
from bearpit.warden import RealmSnapshot, Warden, evaluate_termination


def _cond(type_, **kw):
    return TerminationCondition(type=type_, **kw)


# --- pure termination evaluation --------------------------------------------
def test_duration():
    c = [_cond("duration", limit="1h")]
    assert evaluate_termination(c, RealmSnapshot(elapsed_s=3599)) is None
    fired = evaluate_termination(c, RealmSnapshot(elapsed_s=3600))
    assert fired and fired.kind == TerminationKind.DURATION


def test_stall_fires_on_idle():
    c = [_cond("stall", limit="5m")]
    # active recently -> no stall; quiet past the idle limit -> fire
    assert evaluate_termination(c, RealmSnapshot(idle_s=299)) is None
    fired = evaluate_termination(c, RealmSnapshot(idle_s=300))
    assert fired and fired.kind == TerminationKind.STALL and "idle" in fired.detail


def test_stall_requires_limit():
    import pytest
    with pytest.raises(ValueError, match="stall termination requires"):
        _cond("stall")


def test_file_glob_content_and_count():
    c = [_cond("file", path="shared/submissions/*/final.md",
               content_match="STATUS: FINAL", count=2)]
    snap = RealmSnapshot(
        files=["shared/submissions/vela/final.md", "shared/submissions/orin/final.md",
               "shared/notes.txt"],
        file_contents={
            "shared/submissions/vela/final.md": "…STATUS: FINAL",
            "shared/submissions/orin/final.md": "draft only",  # no content match
        },
    )
    assert evaluate_termination(c, snap) is None  # only 1 of 2 matches content
    snap.file_contents["shared/submissions/orin/final.md"] = "done STATUS: FINAL"
    assert evaluate_termination(c, snap).kind == TerminationKind.FILE


def test_message_pattern_on_channel():
    c = [_cond("message", channel="commons", pattern="MATCH OVER")]
    wrong_channel = RealmSnapshot(messages=[("dm", "MATCH OVER")])
    assert evaluate_termination(c, wrong_channel) is None
    fired = evaluate_termination(c, RealmSnapshot(messages=[("commons", "…MATCH OVER…")]))
    assert fired and fired.kind == TerminationKind.MESSAGE


def test_budget_scopes():
    any_c = [_cond("budget_exhausted", scope="any_agent")]
    all_c = [_cond("budget_exhausted", scope="all_agents")]
    total_c = [_cond("budget_exhausted", scope="realm_total")]
    partial = RealmSnapshot(spend={"vela": (0.06, 0.05), "orin": (0.01, 0.05)})
    assert evaluate_termination(any_c, partial).kind == TerminationKind.BUDGET_EXHAUSTED
    assert evaluate_termination(all_c, partial) is None
    everyone = RealmSnapshot(spend={"vela": (0.06, 0.05), "orin": (0.05, 0.05)})
    assert evaluate_termination(all_c, everyone).kind == TerminationKind.BUDGET_EXHAUSTED
    # realm_total: fires when SUM spend reaches SUM caps, even if no single agent is exhausted
    under = RealmSnapshot(spend={"vela": (0.04, 0.05), "orin": (0.04, 0.05)})  # 0.08 < 0.10
    over = RealmSnapshot(spend={"vela": (0.08, 0.05), "orin": (0.03, 0.05)})   # 0.11 >= 0.10
    assert evaluate_termination(total_c, under) is None
    assert evaluate_termination(total_c, over).kind == TerminationKind.BUDGET_EXHAUSTED
    # not meaningful if any agent is uncapped
    mixed = RealmSnapshot(spend={"vela": (99.0, 0.05), "orin": (0.0, None)})
    assert evaluate_termination(total_c, mixed) is None


def test_verdict_and_manual_always_available():
    v = evaluate_termination([_cond("referee_verdict")], RealmSnapshot(verdict="orin wins"))
    assert v and v.kind == TerminationKind.REFEREE_VERDICT
    # manual kill switch fires even when not declared
    fired = evaluate_termination([_cond("duration", limit="9h")], RealmSnapshot(manual_stop=True))
    assert fired and fired.kind == TerminationKind.MANUAL


# --- conclude / watch orchestration -----------------------------------------
class FakeForge:
    def __init__(self):
        self.torn_down = False

    async def teardown_realm(self, handles, *, grace):
        self.torn_down = True


class FakeHerald:
    def __init__(self):
        self.announced = []

    async def announce(self, room, body):
        self.announced.append(body)
        return "$ev"


async def _chron():
    return await Chronicle.connect("sqlite+aiosqlite:///:memory:")


async def test_conclude_runs_the_sequence():
    forge, herald, chron = FakeForge(), FakeHerald(), await _chron()
    await chron.append_event("r", EventKind.VERDICT, {"outcome": "orin wins"})
    warden = Warden(forge, herald, chron)  # type: ignore[arg-type]
    from bearpit.warden import TerminationFired
    result = await warden.conclude(
        "r", handles=object(), commons_room="!c",  # type: ignore[arg-type]
        fired=TerminationFired(TerminationKind.MESSAGE, "MATCH OVER"),
        grace=timedelta(0), sleep=_noop,
    )
    assert forge.torn_down is True
    assert any("REALM_ENDING" in a for a in herald.announced)
    assert "orin wins" in result.report  # final report drew from the chronicle
    events = await chron.events("r", kind=EventKind.LIFECYCLE)
    assert {e.payload["event"] for e in events} == {"concluding", "archived"}
    await chron.close()


async def test_watch_polls_until_fired():
    forge, herald, chron = FakeForge(), FakeHerald(), await _chron()
    warden = Warden(forge, herald, chron)  # type: ignore[arg-type]
    calls = {"n": 0}

    async def snapshot():
        calls["n"] += 1
        # first two ticks: nothing; third: the closing message appears
        msgs = [("commons", "MATCH OVER")] if calls["n"] >= 3 else []
        return RealmSnapshot(messages=msgs)

    result = await warden.watch(
        "r", handles=object(), commons_room="!c",  # type: ignore[arg-type]
        conditions=[TerminationCondition(type="message", channel="commons", pattern="MATCH OVER")],
        snapshot=snapshot, interval_s=0.0, grace=timedelta(0), sleep=_noop,
    )
    assert calls["n"] == 3 and result.fired.kind == TerminationKind.MESSAGE
    assert forge.torn_down is True
    await chron.close()


async def test_watch_nudges_a_stalled_realm():
    forge, herald, chron = FakeForge(), FakeHerald(), await _chron()
    warden = Warden(forge, herald, chron)  # type: ignore[arg-type]
    nudges = {"n": 0}

    async def nudge():
        nudges["n"] += 1

    async def snapshot():
        return RealmSnapshot(messages=[])  # never progresses -> stalled

    await warden.watch(
        "r", handles=object(), commons_room="!c",  # type: ignore[arg-type]
        conditions=[TerminationCondition(type="duration", limit="9h")],
        snapshot=snapshot, interval_s=0.0, max_ticks=6, grace=timedelta(0), sleep=_noop,
        nudge=nudge, stall_after_s=1.0, max_nudges=2,
    )
    assert nudges["n"] == 2  # nudged, then capped at max_nudges
    await chron.close()


async def test_watch_does_not_nudge_when_progressing():
    forge, herald, chron = FakeForge(), FakeHerald(), await _chron()
    warden = Warden(forge, herald, chron)  # type: ignore[arg-type]
    nudges = {"n": 0}
    calls = {"n": 0}

    async def nudge():
        nudges["n"] += 1

    async def snapshot():
        calls["n"] += 1
        return RealmSnapshot(messages=[("c", f"m{i}") for i in range(calls["n"])])  # grows

    await warden.watch(
        "r", handles=object(), commons_room="!c",  # type: ignore[arg-type]
        conditions=[TerminationCondition(type="duration", limit="9h")],
        snapshot=snapshot, interval_s=0.0, max_ticks=6, grace=timedelta(0), sleep=_noop,
        nudge=nudge, stall_after_s=1.0,
    )
    assert nudges["n"] == 0  # progress every tick -> never stalled
    await chron.close()


async def test_watch_counts_files_and_spend_as_progress():
    # a quiet build realm: NO new messages, but shared files (then spend) advance -> not stalled
    forge, herald, chron = FakeForge(), FakeHerald(), await _chron()
    warden = Warden(forge, herald, chron)  # type: ignore[arg-type]
    nudges = {"n": 0}
    calls = {"n": 0}

    async def nudge():
        nudges["n"] += 1

    async def snapshot():
        calls["n"] += 1
        return RealmSnapshot(messages=[], files=[f"f{i}" for i in range(calls["n"])])

    await warden.watch(
        "r", handles=object(), commons_room="!c",  # type: ignore[arg-type]
        conditions=[TerminationCondition(type="duration", limit="9h")],
        snapshot=snapshot, interval_s=0.0, max_ticks=6, grace=timedelta(0), sleep=_noop,
        nudge=nudge, stall_after_s=1.0,
    )
    assert nudges["n"] == 0  # files advancing IS progress -> no false stall
    await chron.close()


def test_message_match_modes():
    # substring (default): matches within a larger body
    sub = [_cond("message", channel="commons", pattern="DONE")]
    assert evaluate_termination(sub, RealmSnapshot(messages=[("commons", "we are DONE now")]))
    # exact: only a full-string equal body fires
    ex = [_cond("message", channel="commons", pattern="DONE", match_mode="exact")]
    assert evaluate_termination(ex, RealmSnapshot(messages=[("commons", "we are DONE")])) is None
    assert evaluate_termination(ex, RealmSnapshot(messages=[("commons", "DONE")]))
    # regex
    rx = [_cond("message", channel="commons", pattern=r"score: \d+", match_mode="regex")]
    assert evaluate_termination(rx, RealmSnapshot(messages=[("commons", "final score: 42")]))
    assert evaluate_termination(rx, RealmSnapshot(messages=[("commons", "no number here")])) is None


def test_bad_regex_rejected_at_parse():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        TerminationCondition(type="message", pattern="[unclosed", match_mode="regex")
    with pytest.raises(ValidationError):
        TerminationCondition(type="message", pattern="x", match_mode="fuzzy")


async def _noop(_seconds: float) -> None:
    return None


def test_a_realm_whose_participants_are_all_gone_ends() -> None:
    """A realm nobody is left to play cannot make progress — issue #30.

    `stall` cannot catch this: it measures message quiet, and the referee is still alive and
    funded, calling rounds into an empty room. Every tick resets the clock, so the realm survived
    to its `duration` backstop. On rps-duel that was 25 further minutes and a chunk of real spend
    after both players hit their $2 cap and were killed exactly as designed."""
    # nothing declared: this must hold even for a project that never asked for it
    fired = evaluate_termination([], RealmSnapshot(participants=2, participants_alive=0))
    assert fired is not None
    assert fired.kind == TerminationKind.NO_ACTIVE_PARTICIPANTS
    assert "could act" in fired.detail


def test_one_survivor_keeps_the_realm_alive() -> None:
    """Elimination scenarios run down to a single survivor on purpose (cygnus-crew, among-us).

    The rule must key on ZERO able participants, never on "fewer than we started with"."""
    assert evaluate_termination([], RealmSnapshot(participants=5, participants_alive=1)) is None


def test_a_realm_that_does_not_track_participants_is_never_ended_by_this() -> None:
    """Both counters default to 0, so a snapshot from a caller that does not track a roster keeps
    exactly today's behaviour rather than silently gaining a new way to end."""
    assert evaluate_termination([], RealmSnapshot()) is None
    assert evaluate_termination([], RealmSnapshot(participants=0, participants_alive=0)) is None


def test_a_declared_verdict_still_wins_the_tick() -> None:
    """Checked last on purpose: if the referee rules in the same tick that the last player dies,
    the verdict is the more informative outcome and must name the ending."""
    fired = evaluate_termination(
        [TerminationCondition(type=TerminationKind.REFEREE_VERDICT)],
        RealmSnapshot(participants=2, participants_alive=0, verdict="orin wins"),
    )
    assert fired is not None and fired.kind == TerminationKind.REFEREE_VERDICT
