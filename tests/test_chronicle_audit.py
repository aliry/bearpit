"""Integrity findings derived from a finished realm's own chronicle (#90).

Four archived realms hold verdicts scored on content no participant ever posted. The config defect
that caused it is fixed, but the runs stay in an append-only log, and nothing in them says the
verdict is void: every container was healthy, termination fired normally, and the verdict event is
well-formed. These checks report what the chronicle already shows, so a run broken this way — or
some other way that leaves the same fingerprint — is visible without anyone remembering #90.
"""
from __future__ import annotations

import pytest

from bearpit.chronicle import Chronicle, EventKind
from bearpit.chronicle.audit import audit_realm

pytestmark = pytest.mark.asyncio


async def _chron():
    return await Chronicle.connect("sqlite+aiosqlite:///:memory:")


async def _running(c, realm, *, turns=True, require_mention=True, agents=None):
    agents = agents or [
        {"id": "ada", "role": "participant"},
        {"id": "bram", "role": "participant"},
        {"id": "judge", "role": "referee"},
    ]
    await c.append_event(realm, EventKind.LIFECYCLE, {"event": "provisioning"})
    ref = next((a["id"] for a in agents if "referee" in str(a.get("role", "")).lower()), None)
    await c.append_event(realm, EventKind.LIFECYCLE, {
        "event": "running", "commons_room": "!c",
        "config": {
            "referee": ref, "agents": agents,
            "require_mention": require_mention,
            "turns": {"policy": "one-at-a-time"} if turns else None,
        },
    })


async def _say(c, realm, who, body, ts):
    await c.record_message(realm, "!c", f"@{realm}-{who}:realm.local", body, ts_ms=ts)


def _codes(findings):
    return {f.code for f in findings}


async def test_a_healthy_run_reports_nothing():
    """The check must be quiet on a sound realm, or it is noise nobody reads."""
    c = await _chron()
    await _running(c, "good")
    await _say(c, "good", "ada", "my pitch: we sell to hospitals", 100)
    await _say(c, "good", "bram", "mine undercuts them on price", 200)
    await c.append_event("good", EventKind.TURN, {"event": "round_complete", "completed": 1})
    await c.append_event("good", EventKind.VERDICT, {"outcome": "ada wins"})
    assert await audit_realm(c, "good") == []
    await c.close()


async def test_a_verdict_with_no_participant_message_is_reported():
    """pitch-contest-b62116: 77 messages, 31 of them interrupts, ZERO founder posts — and a verdict
    quoting three complete pitches with specific figures, invented wholesale."""
    c = await _chron()
    await _running(c, "pitch")
    for i in range(6):  # the judge talking to itself is not participation
        await _say(c, "pitch", "judge", "I stay silent until the round-complete cue", 100 + i)
    await c.append_event("pitch", EventKind.VERDICT, {"outcome": "cleo wins - 15/15"})
    f = await audit_realm(c, "pitch")
    assert "verdict_without_participation" in _codes(f)
    assert "judge" not in next(x.detail for x in f if x.code == "verdict_without_participation")
    await c.close()


async def test_a_round_that_completed_in_silence_is_named():
    """The referee scored rounds nobody spoke in. A round is reported by NUMBER so the finding
    points at the part of the transcript that is empty."""
    c = await _chron()
    await _running(c, "silent")
    await _say(c, "silent", "ada", "round one, here is my case", 100)
    await c.append_event("silent", EventKind.TURN,
                         {"event": "round_complete", "completed": 1}, ts_ms=150)
    await c.append_event("silent", EventKind.TURN,
                         {"event": "round_complete", "completed": 2}, ts_ms=250)
    await c.append_event("silent", EventKind.TURN,
                         {"event": "round_complete", "completed": 3}, ts_ms=350)
    f = await audit_realm(c, "silent")
    rounds = next(x for x in f if x.code == "rounds_without_participation")
    assert "2" in rounds.detail and "3" in rounds.detail
    assert "round 1" not in rounds.detail.lower()
    await c.close()


async def test_the_config_that_caused_it_is_named_directly():
    """`require_mention: false` ungates everyone, and that includes the referee: it is woken by
    every message and answers each one. Naming the cause turns 'this run looks wrong' into 'this
    is why'. The turn floor is an aggravator, not the discriminator — out-2 was damaged with
    `turns` off."""
    c = await _chron()
    await _running(c, "ungated", turns=False, require_mention=False)
    await _say(c, "ungated", "ada", "something", 100)
    assert "ungated_referee" in _codes(await audit_realm(c, "ungated"))
    await c.close()


async def test_a_transcript_that_is_mostly_repeats_is_reported():
    """Counting messages is not counting content. The runs in #90 carry 22-87 participant messages
    and 0-4 substantive ones; the rest are progress notices and agents insisting they already
    answered. Collapsing digits catches "2 min elapsed, iteration 1/90" and its twenty siblings."""
    c = await _chron()
    await _running(c, "loop")
    for i in range(20):
        body = f"Interrupting current task ({i} min elapsed, iteration {i}/90)"
        await _say(c, "loop", "ada", body, 100 + i)
    f = await audit_realm(c, "loop")
    assert "transcript_mostly_repeats" in _codes(f)
    assert "20 messages" in next(x.detail for x in f if x.code == "transcript_mostly_repeats")
    await c.close()


async def test_a_short_realm_is_not_judged_for_repeating_itself():
    """Below the floor a repeat or two proves nothing — camp-sealed-auction is a sound realm whose
    four participant messages collapse to two."""
    c = await _chron()
    await _running(c, "short")
    for i in range(4):
        await _say(c, "short", "ada", "sealed", 100 + i)
    assert "transcript_mostly_repeats" not in _codes(await audit_realm(c, "short"))
    await c.close()


async def test_a_varied_transcript_is_left_alone():
    """A realm where everyone says something different must stay quiet however much they say."""
    c = await _chron()
    await _running(c, "varied")
    for i in range(20):
        await _say(c, "varied", "ada", f"a distinct point about topic {chr(97 + i)}", 100 + i)
    assert "transcript_mostly_repeats" not in _codes(await audit_realm(c, "varied"))
    await c.close()


async def test_a_realm_with_no_referee_is_not_judged_for_a_missing_verdict():
    """The four beacon-brief runs shared the config but not the damage: no referee, so no loop
    driver and no verdict to be fabricated."""
    c = await _chron()
    await _running(c, "norefsolo", require_mention=False, agents=[
        {"id": "ada", "role": "participant"}, {"id": "bram", "role": "participant"}])
    await _say(c, "norefsolo", "ada", "working", 100)
    assert _codes(await audit_realm(c, "norefsolo")) == set()
    await c.close()


async def test_a_realm_that_never_started_is_not_audited():
    """No `running` event means no roster to reason about — report nothing rather than guess."""
    c = await _chron()
    await c.append_event("stub", EventKind.LIFECYCLE, {"event": "provisioning"})
    assert await audit_realm(c, "stub") == []
    await c.close()
