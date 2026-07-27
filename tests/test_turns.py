"""TurnManager: round-robin advance, one-message trigger, silence-timeout skip, wrap + status.

Bus + clock are injected, so the whole thing is deterministic — no Matrix, no wall-clock.
"""

import pytest

from agentrealm.chronicle import Chronicle, EventKind
from agentrealm.warden.turns import TurnManager, _short_name, _speech_text


class FakeTurnBus:
    def __init__(self):
        self.grants: list[str] = []
        self.announced: list[str] = []
        self.opened = 0
        self.mutes = 0

    async def grant(self, floor):
        self.grants.append(floor)

    async def mute_all(self):
        self.mutes += 1

    async def announce(self, floor, *, reminder=False, context=""):
        self.announced.append(floor)
        self.contexts = getattr(self, "contexts", [])
        self.contexts.append(context)
        if reminder:
            self.reminders = getattr(self, "reminders", 0) + 1

    async def notify_referee(self, text):
        self.cues = getattr(self, "cues", [])
        self.cues.append(text)

    async def open_all(self):
        self.opened += 1


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
async def chron():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    yield c
    await c.close()


def _mgr(chron, order=("pro", "con"), timeout=90.0, cue="round", drives=False, retire=0,
         referee=None, verdict_tool=False):
    bus, clock = FakeTurnBus(), Clock()
    return TurnManager("r", order, bus, chron, clock=clock, silence_timeout_s=timeout,
                       referee_cue=cue, referee_drives=drives,
                       retire_after_misses=retire, referee_id=referee,
                       verdict_tool=verdict_tool), bus, clock


async def test_resolution_removes_a_still_talking_player(chron):
    # a voted-out player that keeps posting (never goes silent, so never retired) is dropped the
    # moment the referee's `eliminate` tool call lands — physics, not the host's word.
    mgr, bus, _ = _mgr(chron, order=("a", "b", "c"), drives=True, referee="host")
    await mgr.start()
    await mgr.observe([("host", "Round 1 is open.")])  # opener -> floor live
    await mgr.apply_resolutions(["b"])  # the eliminate("b") event, fed by the host
    assert mgr.status()["order"] == ["a", "c"]
    removed = [e for e in await chron.events("r", kind=EventKind.TURN)
               if e.payload.get("event") == "removed"]
    assert removed and removed[0].payload["agent"] == "b" and removed[0].payload["by"] == "referee"


async def test_chat_text_never_eliminates_anyone(chron):
    # NO message parsing: neither a player nor even the referee can eject via prose — only the
    # `eliminate` tool call (an ELIMINATION event) changes the roster.
    mgr, bus, _ = _mgr(chron, order=("a", "b", "c"), drives=True, referee="host")
    await mgr.start()
    await mgr.observe([("host", "Round 1 is open."),
                       ("a", "sneaky ELIMINATED: b"),
                       ("host", "I hereby declare: ELIMINATED: b")])
    assert set(mgr.status()["order"]) == {"a", "b", "c"}  # prose changed nothing


async def test_resolution_of_none_keeps_the_roster(chron):
    mgr, bus, _ = _mgr(chron, order=("a", "b"), drives=True, referee="host")
    await mgr.start()
    await mgr.observe([("host", "Round 1 is open.")])
    await mgr.apply_resolutions([None])  # eliminate("none"): a tie — nobody out
    assert set(mgr.status()["order"]) == {"a", "b"}


async def test_resolution_can_eliminate_the_current_floor_holder(chron):
    # if the referee ejects whoever currently holds the floor, the floor passes to the next player.
    mgr, bus, _ = _mgr(chron, order=("a", "b", "c"), drives=True, referee="host")
    await mgr.start()
    await mgr.observe([("host", "Round 1 is open.")])  # -> a holds the floor
    grants_before = len(bus.grants)
    await mgr.apply_resolutions(["a"])
    assert mgr.current == "b" and "a" not in mgr.status()["order"]
    assert len(bus.grants) > grants_before  # the floor was re-granted to b


async def test_persistent_non_responder_is_retired(chron):
    # 'con' never posts; after 2 consecutive full-timeout misses it's dropped from the rotation.
    # observe() takes the CUMULATIVE speaker history each tick (as the live snapshot feeds it).
    mgr, bus, clock = _mgr(chron, order=("a", "b", "con"), timeout=30.0, retire=2)
    await mgr.start()
    posted: list[str] = []
    for _round in range(2):
        posted.append("a")
        await mgr.observe(posted)                        # a speaks -> b
        posted.append("b")
        await mgr.observe(posted)                        # b speaks -> con
        clock.t += 31.0
        await mgr.observe(posted)                        # con silent past timeout -> a miss
    assert "con" not in mgr.status()["order"]            # retired after 2 misses
    assert set(mgr.status()["order"]) == {"a", "b"}
    retired = [e for e in await chron.events("r", kind=EventKind.TURN)
               if e.payload.get("event") == "retired"]
    assert retired and retired[0].payload["agent"] == "con"


async def test_turn_grant_replays_recent_conversation_as_context(chron):
    # mention-gated agents can't see the discussion, so each turn grant must replay the recent
    # substantive messages (the among-us "no prior context" bug).
    mgr, bus, clock = _mgr(chron, order=("a", "b", "c"), timeout=90.0)
    await mgr.start()
    assert bus.contexts[0] == ""  # first grant: nothing has been said yet
    # a speaks (substantive) + a status heartbeat, then the floor moves to b
    said = "I suspect c — they've been quiet"
    await mgr.observe([("a", said)])
    await mgr.observe([("a", said), ("a", "📚 Reading skill")])
    ctx = bus.contexts[-1]  # the grant to b carries a's message, not the status line
    assert "a: I suspect c" in ctx and "Reading skill" not in ctx


async def test_tool_call_does_not_complete_a_turn(chron):
    # A leaked tool-call block (skill_view etc. surfacing as chat text) is the agent
    # trying to USE a tool, not its turn statement — it must NOT pass the floor. In among-us-7d4c69
    # Juno was ejected for "not voting" because its skill_view call was counted as its whole turn.
    # observe() takes the CUMULATIVE speaker history each tick, so grow the list.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), timeout=90.0)
    await mgr.start()
    granted = len(bus.grants)
    posted = [("a", '<call>\n{"name": "skill_view", "arguments": {"name": "x"}}\n</call>')]
    await mgr.observe(posted)
    assert len(bus.grants) == granted  # a tool call did NOT advance the floor
    posted.append(("a", '<tool_call>{"name":"skill_view"}</tool_call>\n\nawaiting result'))
    await mgr.observe(posted)
    assert len(bus.grants) == granted  # a tool call + trailing meta-prose is still not a turn
    posted.append(("a", "Here's my read. VOTE: b"))
    await mgr.observe(posted)  # a's real message finally completes the turn -> floor moves to b
    assert bus.grants[-1] == "b"
    # and the leaked tool-call text is never replayed to the next holder as "conversation"
    assert "skill_view" not in "".join(bus.contexts)


async def test_status_line_does_not_complete_a_turn(chron):
    # A Hermes operational status/heartbeat post (skill read, "working", error) must NOT pass the
    # floor before the agent actually speaks — the among-us bug where "📚 Reading skill" advanced
    # the floor before the player made its case.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), timeout=90.0)
    await mgr.start()
    granted = len(bus.grants)
    await mgr.observe([("a", "📚 Reading skill social-deduction")])
    assert len(bus.grants) == granted  # a status line did NOT advance the floor
    await mgr.observe([("a", "📚 Reading skill social-deduction"), ("a", "⏳ Working — 2 min")])
    assert len(bus.grants) == granted  # still just heartbeats -> still a's turn
    # a's real message finally completes the turn -> floor moves to b
    await mgr.observe([("a", "📚 …"), ("a", "⏳ …"), ("a", "Here's my read. VOTE: b")])
    assert bus.grants[-1] == "b"


async def test_a_responder_is_never_retired(chron):
    # a player that keeps responding never accrues misses, even at retire=1
    mgr, bus, clock = _mgr(chron, order=("a", "b"), timeout=30.0, retire=1)
    await mgr.start()
    posted: list[str] = []
    for _round in range(3):
        posted.append("a")
        await mgr.observe(posted)
        posted.append("b")
        await mgr.observe(posted)
    assert set(mgr.status()["order"]) == {"a", "b"}  # both still in


async def test_driving_referee_cue_is_directive_and_carries_the_round(chron):
    # a game-master referee (referee_drives) must resolve every round -> directive cue that
    # INCLUDES the round's player messages (so a mention-gated host still has the votes)
    mgr, bus, _ = _mgr(chron, drives=True)
    await mgr.start()
    r1 = [("pro", "I suspect con. VOTE: con"), ("con", "no, VOTE: pro")]
    await mgr.observe(r1[:1])
    await mgr.observe(r1)  # round 1 done
    cue = bus.cues[0]
    assert "Resolve the round NOW" in cue and "let another round run" not in cue
    assert "VOTE: con" in cue and "VOTE: pro" in cue  # the round's discussion rides in the cue
    # ...but the CUE ITSELF must not assume what the game is. It goes to every driving referee —
    # an auction clerk, a debate chair, an editor certifying a document — so it may not order them
    # to "count the VOTE lines" or eject "players". It used to, and sealed-auction's clerk had to
    # apologise for us in its own rubric: "the cue's stock wording talks about ejecting players and
    # counting votes. IGNORE that: this is an auction."
    assert "VOTE lines" not in cue and "ejected player" not in cue
    assert "still in the game" not in cue.lower()  # a document realm has no "game"
    assert "CALL the tool" in cue  # what the platform DOES guarantee

    # a reactive referee keeps the optional wording
    mgr2, bus2, _ = _mgr(chron, drives=False)
    await mgr2.start()
    await mgr2.observe(["pro"])
    await mgr2.observe(["pro", "con"])
    assert "let another round run" in bus2.cues[0]


async def test_cue_directs_the_verdict_tool_when_the_referee_holds_one(chron):
    # A referee with a realm-ending `rule` tool follows the cue in front of it: if the cue only
    # says "open the next round", it opens rounds forever and narrates the win in chat instead of
    # calling the tool (among-us-tele2, 9x). The cue must route the win-check at the tool call.
    async def play_round(mgr):
        posted = [("host", "Round 1 is open — make your case.")]  # opener resumes the floor
        await mgr.observe(posted)
        posted.append(("pro", "VOTE: con"))
        await mgr.observe(posted)
        posted.append(("con", "VOTE: pro"))
        await mgr.observe(posted)  # round 1 completes

    mgr, bus, _ = _mgr(chron, drives=True, referee="host", verdict_tool=True)
    await mgr.start()
    await play_round(mgr)
    cue = bus.cues[-1]
    assert "`rule`" in cue and "does not end anything" in cue
    assert "side has WON" not in cue  # not every realm has sides, or a winner
    # without the tool, the cue must NOT advertise it (an inert tool would just confuse)
    mgr2, bus2, _ = _mgr(chron, drives=True, referee="host", verdict_tool=False)
    await mgr2.start()
    await play_round(mgr2)
    assert "`rule`" not in bus2.cues[-1] and "open the next round" in bus2.cues[-1]


async def test_resolution_resumes_a_paused_round_even_with_no_ejection(chron):
    # at a round boundary the rotation waits for the referee's `eliminate` call; a tie resolution
    # (eliminate("none") -> None) must ALSO reopen the floor — the round is resolved either way.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), drives=True, referee="host")
    await mgr.start()
    posted = [("host", "Round 1 is open.")]
    await mgr.observe(posted)                       # -> grant a
    posted.append(("a", "case made. VOTE: b"))
    await mgr.observe(posted)
    posted.append(("b", "not me. VOTE: a"))
    await mgr.observe(posted)                       # round wraps -> paused on the referee
    assert mgr.status()["awaiting_referee"] == "round"
    await mgr.apply_resolutions([None])             # tie: nobody out, but the round IS resolved
    assert mgr.status()["awaiting_referee"] is None
    assert set(mgr.status()["order"]) == {"a", "b"} and bus.grants[-1] == "a"


async def test_mixed_tool_call_and_argument_is_speech_with_block_stripped(chron):
    # a message = leaked tool-call block + a real argument must COUNT as the turn (its VOTE line
    # is real), with the call debris stripped from what the referee/context sees.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), timeout=90.0)
    await mgr.start()
    body = '<call>\n{"name": "skill_view", "arguments": {"name": "x"}}\n</call>\n\n' \
           "I think b dodged every direct question today. VOTE: b"
    await mgr.observe([("a", body)])
    assert mgr.current == "b"  # the argument completed a's turn
    ctx = bus.contexts[-1]
    assert "dodged every direct question" in ctx and "skill_view" not in ctx


async def test_driving_start_waits_for_the_referee_opener(chron):
    # referee_opens: the first player must not race the host's opening post (among-us-tele3: cass
    # spoke into a void two minutes before Mother's welcome). The floor stays muted until the
    # opener lands; the first grant then carries the opener as context.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), drives=True, referee="host")
    await mgr.start()
    assert bus.grants == [] and bus.mutes == 1  # nobody has the floor yet
    posted = [("a", "hello? I will start: VOTE: b")]  # out-of-turn noise must not resume
    await mgr.observe(posted)
    assert bus.grants == []
    posted.append(("host", "Welcome aboard. Round 1 is open — make your case, end with VOTE:."))
    await mgr.observe(posted)
    assert bus.grants == ["a"]  # opener arrived -> floor opens with the first player
    assert "Round 1 is open" in bus.contexts[-1]  # who gets the opener replayed as context


async def test_round_wrap_pauses_and_the_referees_own_post_reopens_the_floor(chron):
    """At a round boundary the rotation pauses on the referee: the next round must not outrun the
    resolution, and a participant removed this round never gets the next floor (among-us-tele3:
    rounds raced ahead and juno kept playing after ejection).

    The referee's own POST is what reopens the floor — which is the contract we give every referee
    ("do your tool work, then post ONE message; posting it reopens the floor"). It used to be that
    ONLY an ELIMINATION event could close a boundary, so the only way to end a round in ANY realm
    was to call a tool named `eliminate`: sealed-auction's clerk was reduced to calling
    eliminate(agent='none') to close a round of an auction where nobody is ever ejected."""
    mgr, bus, clock = _mgr(chron, order=("a", "b"), drives=True, referee="host")
    await mgr.start()
    posted = [("host", "Round 1 is open.")]
    await mgr.observe(posted)                       # -> grant a
    posted.append(("a", "I have made my case at length."))
    await mgr.observe(posted)                       # -> grant b
    posted.append(("b", "And here is my reply, at length."))
    await mgr.observe(posted)                       # round wraps -> PAUSE on the referee
    assert mgr.status()["awaiting_referee"] == "round" and bus.mutes == 2
    grants_before = list(bus.grants)

    # a PARTICIPANT talking during the pause does not reopen anything
    posted.append(("a", "extra chatter while the referee is resolving the round"))
    await mgr.observe(posted)
    assert bus.grants == grants_before and mgr.status()["awaiting_referee"] == "round"

    # the REFEREE's post does
    posted.append(("host", "Round 1 resolved. Round 2 is open."))
    await mgr.observe(posted)
    assert mgr.status()["awaiting_referee"] is None
    assert bus.grants[-1] == "a"  # floor reopens at the top of the rotation


async def test_eliminate_removes_a_participant_and_also_closes_a_paused_round(chron):
    # `eliminate` means only what it says: remove a participant. It still closes a paused boundary
    # (a referee may resolve entirely by tool call and never post), and the removed participant
    # must not receive the next floor.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), drives=True, referee="host")
    await mgr.start()
    posted = [("host", "Round 1 is open.")]
    await mgr.observe(posted)
    posted.append(("a", "I have made my case at length."))
    await mgr.observe(posted)
    posted.append(("b", "And here is my reply, at length."))
    await mgr.observe(posted)
    assert mgr.status()["awaiting_referee"] == "round"

    await mgr.apply_resolutions(["a"])              # eliminate("a") -> a is out, floor resumes at b
    assert mgr.status()["awaiting_referee"] is None
    assert mgr.status()["order"] == ["b"] and bus.grants[-1] == "b"


async def test_awaiting_referee_reminds_then_times_out(chron):
    # a slow referee is re-prompted; a dead one must not deadlock the realm — after the silence
    # timeout the rotation proceeds and the lapse is chronicled.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), drives=True, referee="host", timeout=90.0)
    await mgr.start()                               # awaiting the opener
    clock.t = 50.0
    await mgr.observe([])                           # past the re-announce window -> remind the host
    assert any("waiting on you" in c for c in bus.cues)
    clock.t = 91.0
    await mgr.observe([])                           # past the timeout -> proceed anyway
    assert bus.grants == ["a"]
    lapses = [e for e in await chron.events("r", kind=EventKind.TURN)
              if e.payload.get("event") == "referee_timeout"]
    assert lapses and lapses[0].payload["awaiting"] == "opener"


async def test_double_post_cannot_change_a_vote(chron):
    # Hermes can slip a second message into the physics window before the floor moves
    # (among-us-tele4: two rhea posts 1s apart flipped her vote 2-1). Only each player's FIRST
    # substantive message reaches the referee's round cue — the tally input is deterministic.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), drives=True, referee="host")
    await mgr.start()
    posted = [("host", "Round 1 is open.")]
    await mgr.observe(posted)
    # a's turn: two messages land in the same tick — the vote flip must NOT count
    posted += [("a", "my read stands. VOTE: b"), ("a", "wait, changing my mind! VOTE: a")]
    await mgr.observe(posted)
    posted.append(("b", "defending myself. VOTE: a"))
    await mgr.observe(posted)  # round wraps -> cue to the host
    cue = bus.cues[-1]
    assert "my read stands. VOTE: b" in cue
    assert "changing my mind" not in cue  # the second message never reaches the tally


async def test_pause_chatter_does_not_leak_into_the_next_round(chron):
    # anything said while the rotation is paused on the referee belongs to NO round — it must not
    # surface in the next round's cue as if it were a turn.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), drives=True, referee="host")
    await mgr.start()
    posted = [("host", "Round 1 is open.")]
    await mgr.observe(posted)
    posted.append(("a", "VOTE: b"))
    await mgr.observe(posted)
    posted.append(("b", "VOTE: a"))
    await mgr.observe(posted)                     # round 1 wraps -> paused
    assert mgr.status()["awaiting_referee"] == "round"
    posted.append(("a", "psst, while the host counts: VOTE: b again!"))
    await mgr.observe(posted)                     # pause chatter
    await mgr.apply_resolutions([None])           # tie -> round 2 opens
    posted.append(("a", "round two thoughts. VOTE: b"))
    await mgr.observe(posted)
    posted.append(("b", "VOTE: a"))
    await mgr.observe(posted)                     # round 2 wraps -> cue
    cue = bus.cues[-1]
    assert "Round 2 is complete" in cue
    assert "round two thoughts" in cue and "psst" not in cue


async def test_start_grants_first_floor_and_chronicles(chron):
    mgr, bus, _ = _mgr(chron)
    await mgr.start()
    assert bus.grants == ["pro"] and bus.announced == ["pro"]
    turns = await chron.events("r", kind=EventKind.TURN)
    assert turns[-1].payload == {"round": 1, "order": ["pro", "con"], "position": 0,
                                 "current": "pro", "min_rounds": 0}


async def test_one_message_advances_to_next(chron):
    mgr, bus, _ = _mgr(chron)
    await mgr.start()
    await mgr.observe(["pro"])  # the floor-holder posted -> pass
    assert bus.grants == ["pro", "con"] and mgr.current == "con"


async def test_silence_timeout_skips_a_mute_holder(chron):
    mgr, bus, clock = _mgr(chron, timeout=30.0)
    await mgr.start()
    await mgr.observe([])  # no one spoke, no time passed -> stay
    assert mgr.current == "pro"
    clock.t = 31.0
    await mgr.observe([])  # timeout elapsed -> skip pro
    assert mgr.current == "con" and bus.grants == ["pro", "con"]


async def test_reannounces_a_slow_holder_before_skipping(chron):
    # re-announce at HALF the timeout (45s here) — a nudge that lands while a slow CLI turn is
    # still running gets answered twice (toolcheck-c1: the host posted its opener twice).
    mgr, bus, clock = _mgr(chron, timeout=90.0)
    await mgr.start()
    assert bus.announced == ["pro"]
    clock.t = 40.0
    await mgr.observe([])  # before the re-announce window -> still silent, no nudge
    assert bus.announced == ["pro"]
    clock.t = 50.0
    await mgr.observe([])  # slow but not timed out -> re-prompt, still pro's floor
    assert bus.announced == ["pro", "pro"] and mgr.current == "pro"
    clock.t = 91.0
    await mgr.observe([])  # past the timeout now -> skip to con
    assert mgr.current == "con"


async def test_round_wraps_and_increments(chron):
    mgr, bus, _ = _mgr(chron)
    await mgr.start()
    await mgr.observe(["pro"])  # -> con
    await mgr.observe(["pro", "con"])  # con posted -> wrap to pro, round 2
    assert mgr.current == "pro"
    assert mgr.status()["round"] == 2
    assert bus.grants == ["pro", "con", "pro"]
    # default cue="round": the referee is pushed exactly once, when round 1 wraps
    assert len(bus.cues) == 1 and "Round 1 is complete" in bus.cues[0]


async def test_cue_each_turn_notifies_referee_every_advance(chron):
    mgr, bus, _ = _mgr(chron, order=("a", "b"), cue="turn")
    await mgr.start()  # cue on the first grant too
    assert len(bus.cues) == 1 and "a's turn" in bus.cues[0]
    await mgr.observe(["a"])  # -> b
    assert len(bus.cues) == 2 and "b's turn" in bus.cues[1]


async def test_cue_none_never_notifies_referee(chron):
    mgr, bus, _ = _mgr(chron, order=("a", "b"), cue="none")
    await mgr.start()
    await mgr.observe(["a"])
    await mgr.observe(["a", "b"])  # a full round
    assert getattr(bus, "cues", []) == []  # the referee polls on its own; no push


async def test_non_floor_speaker_is_ignored(chron):
    mgr, bus, _ = _mgr(chron)
    await mgr.start()
    await mgr.observe(["judge"])  # the referee spoke, not the floor-holder -> no advance
    assert mgr.current == "pro" and bus.grants == ["pro"]


async def test_status_reports_done_and_upcoming(chron):
    mgr, _, _ = _mgr(chron, order=("a", "b", "c"))
    await mgr.start()
    await mgr.observe(["a"])  # -> b
    s = mgr.status()
    assert s["current"] == "b" and s["done_this_round"] == ["a"]
    assert s["upcoming_this_round"] == ["c"] and s["order"] == ["a", "b", "c"]


async def test_empty_order_is_a_noop(chron):
    mgr, bus, _ = _mgr(chron, order=())
    await mgr.start()
    await mgr.observe(["x"])
    assert bus.grants == [] and mgr.current is None


async def test_open_all_lifts_the_gate(chron):
    mgr, bus, _ = _mgr(chron)
    await mgr.start()
    await mgr.open_all()
    assert bus.opened == 1


async def test_hermes_tool_call_status_line_does_not_consume_the_turn(chron):
    # Hermes posts "⚙️ mcp_realmtools_submit_sealed…" while a tool call runs. That is a STATUS
    # line, not speech — but it was missing from the filter, so it counted as the agent's whole
    # turn: ping sealed its word, the floor passed, and it never posted its message
    # (toolcheck-k2). Every Hermes status glyph must be non-speech.
    mgr, bus, _ = _mgr(chron, order=("a", "b"), timeout=90.0)
    await mgr.start()
    granted = len(bus.grants)
    posted = []
    for status in ("⚙️ mcp_realmtools_submit_sealed...", "🔍 Searching past sessions",
                   "💻 terminal", "📚 Reading skill social-deduction"):
        posted.append(("a", status))
        await mgr.observe(posted)
        assert len(bus.grants) == granted, f"{status!r} must not pass the floor"
    posted.append(("a", "APPLE -> MANGO"))
    await mgr.observe(posted)              # the real message finally completes the turn
    assert bus.grants[-1] == "b"
    assert "submit_sealed" not in "".join(bus.contexts)  # nor is status replayed as conversation


async def test_round_boundary_chronicles_the_incremented_round_for_the_verdict_guard(chron):
    # The Arbiter's min_rounds guard reads the LATEST turn event's round. During the boundary
    # pause no grant has been written yet, so the guard saw the OLD round and rejected a
    # perfectly-timed verdict as "too early" (toolcheck-c5: rule() bounced at min_rounds=1 right
    # when round 1 completed). The wrap must chronicle the incremented round before cueing.
    mgr, bus, clock = _mgr(chron, order=("a", "b"), drives=True, referee="host")
    await mgr.start()
    posted = [("host", "Round 1 is open.")]
    await mgr.observe(posted)
    posted.append(("a", "VOTE: b"))
    await mgr.observe(posted)
    posted.append(("b", "VOTE: a"))
    await mgr.observe(posted)                      # round 1 wraps -> paused on the referee
    evs = await chron.events("r", kind=EventKind.TURN)
    latest = evs[-1].payload
    assert latest["event"] == "round_complete" and latest["completed"] == 1
    assert latest["round"] == 2  # the guard now sees 2 > min_rounds=1 -> verdict allowed


def test_agent_ids_with_hyphens_survive_the_mxid_round_trip():
    """Herald builds the localpart as f"{realm}-{agent.id}", so the agent id is whatever follows the
    realm prefix — and it may contain hyphens of its own. Taking `split("-")[-1]` mangled every
    hyphenated id: 'juror-a' became 'a'. The damage was silent and total — eliminate(agent=
    'juror-a'), the id the referee's OWN rubric names, matched nothing and the juror kept taking
    turns; the cue
    announced "Still active: a, b, c"; and the replayed history named everyone's peers 'a' and 'b'.
    jury-unanimous ships exactly those ids."""
    assert _short_name("@jury-1-juror-a:realm.local", "jury-1") == "juror-a"
    assert _short_name("@r1-team-red:realm.local", "r1") == "team-red"
    assert _short_name("@among-us-sim6-mother:realm.local", "among-us-sim6") == "mother"

    order = [f"@jury-1-juror-{c}:realm.local" for c in "abc"]
    by_name = {_short_name(p, "jury-1").lower(): p for p in order}
    assert by_name["juror-a"] == "@jury-1-juror-a:realm.local"  # eliminate('juror-a') now matches


def test_runtime_provider_errors_are_never_an_agents_turn():
    """The runtime posts its OWN provider failures into the room, as the agent. In debate-1 an
    exhausted budget made LiteLLM 429; the runtime retried in a loop and posted the failure each
    time — 2,540 messages, every one of them counted as somebody's turn. The chair duly ruled "no
    advocate presented any substantive argument": the errors had eaten the entire debate."""
    assert _speech_text("⏱️ The model provider is rate-limiting requests. Please wait.") == ""
    assert _speech_text("API call failed after 1 retries: HTTP 429: Budget exceeded!") == ""
    assert _speech_text("Operation interrupted: waiting for model response (35.5s).") == ""
    # ...while a real argument, which may well mention a rate limit, still is speech
    real = "I oppose the motion: our provider rate-limiting last quarter cost us three deadlines."
    assert _speech_text(real) == real


async def test_an_empty_body_post_does_not_advance_the_floor(chron):
    """A bare-id feed (no message text) always counts as a turn, but an empty-body TUPLE — an
    adversarial client sending body=="" — is not speech and must not pass the floor. The two were
    indistinguishable after the pair conversion, so "" advanced the floor and cost the holder its
    turn (the very skip the zero-width handling exists to prevent)."""
    mgr, bus, clock = _mgr(chron, order=("a", "b"), timeout=90.0)
    await mgr.start()
    granted = len(bus.grants)
    posted = [("a", "")]                       # the current holder posts an EMPTY body
    await mgr.observe(posted)
    assert len(bus.grants) == granted          # floor did NOT move
    posted.append(("a", "Here is my actual, substantive argument at length."))
    await mgr.observe(posted)
    assert len(bus.grants) > granted           # a real post DOES move it


async def test_eliminating_the_tail_holder_mid_round_completes_the_round(chron):
    """A referee (which is never muted and can call eliminate any time) removing the LAST holder of
    a live round wrapped the position to 0 without any round bookkeeping — the round counter stalled
    and the next cue tallied stale messages. Removing the tail now completes the round."""
    mgr, bus, clock = _mgr(chron, order=("a", "b", "c"), drives=False, referee="host")
    await mgr.start()
    posted = [("a", "case a, at some length"), ("b", "case b, at some length")]
    await mgr.observe(posted[:1])
    await mgr.observe(posted)   # floor advances a -> b -> c
    assert mgr.status()["round"] == 1
    # the referee ejects C, the tail holder, mid-round
    await mgr.apply_resolutions(["c"])
    # a full lap ended: the round advanced and a round_complete was chronicled
    assert mgr.status()["round"] == 2
    evs = [e.payload for e in await chron.events("r", kind=EventKind.TURN)]
    assert any(e.get("event") == "round_complete" and e.get("completed") == 1 for e in evs)
