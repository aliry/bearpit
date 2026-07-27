"""TurnManager — deterministic one-at-a-time turn management (turn-management spec).

Owns the floor: grants it round-robin over the participant order, advances when the current
floor-holder posts one message (or the silence timeout elapses), and chronicles each change as a
TURN event. Enforcement (muting the room) lives in the bus (physics); this is the driver. The
referee is outside the rotation and never drives turns — it only reads state via `turn_status`.

Bus and clock are injected (Protocol-for-IO) so the advance logic is fully testable without a
real Matrix homeserver or wall-clock.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from agentrealm.chronicle import Chronicle, EventKind


def _short_name(mxid: str, realm_id: str = "") -> str:
    """The AGENT ID inside a participant mxid: '@<realm>-<agent>:host' -> '<agent>'.

    Herald builds the localpart as f"{realm_id}-{agent.id}", so the agent id is whatever follows
    the realm prefix — and it may itself contain hyphens. The old implementation took
    `split("-")[-1]`, i.e. the LAST hyphen-segment, which quietly mangles every hyphenated id:
    'juror-a' became 'a', 'team-red' became 'red'. The damage was silent and total — `eliminate
    (agent='juror-a')`, the id the referee's own rubric names, matched nothing and the juror kept
    taking turns; the round cue announced "Still active: a, b, c"; and the history replayed to each
    floor-holder named their peers 'a' and 'b'. jury-unanimous ships exactly those ids.
    """
    local = mxid.split(":")[0].lstrip("@")
    if realm_id and local.startswith(f"{realm_id}-"):
        return local[len(realm_id) + 1:]
    return local.split("-")[-1]  # unknown realm: best effort, as before


# The Hermes runtime posts operational STATUS/heartbeat lines into the room — skill reads
# ("📚 Reading skill …"), progress ("⏳ Working …"), interrupts ("⚡ …"), and error notices
# ("⚠️ Empty response …", "❌ Model returned no content …"). These are NOT the agent's substantive
# turn message; counting one as the turn would pass the floor before the agent actually speaks
# (verified in among-us: a "📚 Reading skill" post advanced the floor before the player made its
# case). So they don't complete a turn and aren't fed to the referee as round discussion.
_STATUS_PREFIXES = (
    "📚",  # "Reading skill …"
    "⏳",  # "Working — 3 min …"
    "⚡",  # "Interrupting current task …"
    "❌",  # "Model returned no content …"
    "⚠️",  # "Empty response …"
    "🛠", "🔧",  # tooling notices
    "⚙️", "⚙",  # "⚙️ mcp_realmtools_submit_sealed…" — a TOOL CALL in progress. Missing this one
                # cost the agent its whole turn: the status line counted as its spoken message, the
                # floor passed, and it never posted (toolcheck-k2 — ping sealed, then vanished).
    "🔍",  # "Searching past sessions …"
    "💻",  # "terminal …"
    "⏱️", "⏱",  # "The model provider is rate-limiting requests." — see _RUNTIME_ERROR_RE below
)

# The runtime posts its OWN provider/transport failures into the room, as the agent. They are not
# the agent speaking, and they must never consume its turn. In debate-1 an exhausted budget made
# LiteLLM return 429; the runtime retried in a loop, posting the failure each time — 2,540 messages,
# each counted as somebody's turn. The chair duly ruled "no advocate presented any substantive
# argument": the errors had eaten the entire debate.
_RUNTIME_ERROR_RE = re.compile(
    r"(?i)^\s*(?:⏱️?\s*)?(?:the model provider is rate-limiting"
    r"|api call failed after \d+ retries"
    r"|budget has been exceeded"
    r"|operation interrupted"
    r"|http [45]\d\d\b)"
)


def _is_status(body: str) -> bool:
    text = body.strip()
    return text.startswith(_STATUS_PREFIXES) or bool(_RUNTIME_ERROR_RE.match(text))


# A tool-call block that surfaced as chat text — a backend's function-call markup leaking, e.g.
# '<call>{"name":"skill_view",…}</call>' or '<tool_call>…</tool_call>'. The agent was trying to USE
# a tool (read a skill, send a DM), NOT making its turn statement. Calling tools must be free: it
# must not consume the floor. Only real speech to the Commons advances the turn (among-us-7d4c69:
# Juno was ejected for "not voting" because its skill_view call was counted as its whole turn).
_TOOL_CALL_MARK_RE = re.compile(r"</?(?:tool_)?call\b", re.I)
_TOOL_CALL_BLOCK_RE = re.compile(r"<(?:tool_)?call\b[^>]*>.*?</(?:tool_)?call>", re.I | re.S)
_MIN_SPEECH_CHARS = 20  # residue shorter than this around a stripped block isn't a real statement


# Hermes posts a bare zero-width space when the model returns no content. It looks like a message,
# but nothing was said — counting it as the turn hands the floor on and the agent never speaks
# (among-us-1adf4c: mother "spoke" twice this way while resolving a round).
_BLANK_CHARS = "\u200b\u200c\u200d\ufeff\u00a0"


def _speech_text(body: str) -> str:
    """The substantive spoken part of a message, or "" when there is none.

    Status/heartbeat lines are never speech. A message containing tool-call markers is speech only
    if meaningful text REMAINS once the blocks are stripped — a bare leaked call is not a turn, but
    an argument with a stray block in it still is (and its `VOTE:` line must still reach the
    referee, so the stripped text — not the raw body — is what gets recorded)."""
    if _is_status(body):
        return ""
    if not body.strip(_BLANK_CHARS + " \t\r\n"):
        return ""  # an empty/zero-width post: the model stayed silent, so no turn was taken
    if not _TOOL_CALL_MARK_RE.search(body):
        return body  # the common case: a plain message is speech as-is, however short
    text = _TOOL_CALL_BLOCK_RE.sub("", body)
    m = _TOOL_CALL_MARK_RE.search(text)
    if m:  # an unclosed/mangled block: everything from the marker on is call debris
        text = text[: m.start()]
    text = text.strip()
    # What survived a stripped tool block is a statement only if there is enough of it. The old test
    # was `len(text) >= _MIN_SPEECH_CHARS or "VOTE" in text.upper()` — the PLATFORM knowing what a
    # vote is. That is one game's vocabulary in shared control logic, and it privileged voting
    # scenarios over every other kind of realm. It is also obsolete: votes are SEALED now
    # (submit_sealed), never announced in chat, so there is no short vote line left to rescue.
    return text if len(text) >= _MIN_SPEECH_CHARS else ""


_RECENT_WINDOW = 12  # how many latest messages to replay to the floor-holder as context
_RECENT_MSG_CHARS = 500  # per-message truncation in the replayed history


# Eliminations are TOOL-based, never parsed out of referee prose: the referee calls the
# `eliminate` realmtool, the call lands as an ELIMINATION event, and the host feeds it here via
# `apply_resolutions`. (The old control-line parse — 'ELIMINATED: vega' — silently failed whenever
# the model dressed the line in markdown, leaving ejected players in the game: among-us-tele3/4.)


class TurnBus(Protocol):
    """The floor-control surface the TurnManager drives (implemented over the Herald in prod)."""

    async def grant(self, floor: str) -> None: ...  # only `floor` (+ referee/system) may post
    async def open_all(self) -> None: ...  # lift the gate (everyone may post — used on conclude)
    # nobody but the referee/system may post — held at round boundaries while the driving referee
    # opens the game / resolves the round, so the rotation can't outrun it
    async def mute_all(self) -> None: ...
    # tell `floor` it is their turn (reminder=True is a shorter re-prompt for a slow holder)
    async def announce(
        self, floor: str, *, reminder: bool = False, context: str = ""
    ) -> None: ...
    async def notify_referee(self, text: str) -> None: ...  # push a cue to the referee (if any)


class TurnManager:
    def __init__(
        self,
        realm_id: str,
        order: Sequence[str],
        bus: TurnBus,
        chronicle: Chronicle,
        *,
        clock: Callable[[], float],
        silence_timeout_s: float = 90.0,
        reannounce_after_s: float | None = None,
        referee_cue: str = "round",
        min_rounds_before_verdict: int = 0,
        referee_drives: bool = False,
        retire_after_misses: int = 0,
        referee_id: str | None = None,
        verdict_tool: bool = False,
    ) -> None:
        self._realm = realm_id
        self._order = list(order)  # participant ids/mxids, in turn order (referee excluded)
        self._bus = bus
        self._chron = chronicle
        self._clock = clock
        self._timeout = silence_timeout_s
        # how/when to cue the referee — scenario-dependent, NOT hardcoded (see generic-design):
        # "round" (each completed round), "turn" (every floor change), "none" (referee polls).
        self._cue = referee_cue
        # a DRIVING referee (game master) must resolve every round; a reactive one (a judge) may
        # wait — so the cue is directive vs optional accordingly.
        self._drives = referee_drives
        self._min_rounds = min_rounds_before_verdict  # carried in TURN events for the Arbiter guard
        # Re-prompt a slow floor-holder before skipping it (a single mention is easily missed).
        # HALF the timeout, not a third: a turn is several model calls and on a slow pipeline runs
        # 90s+, and a reminder that lands while the agent is still working is answered TWICE — the
        # host posted its opener twice in toolcheck-c1 for exactly this reason. One late nudge is
        # better than a duplicate turn.
        self._reannounce = reannounce_after_s or max(20.0, silence_timeout_s / 2)
        # retry (re-announce) a silent holder within the timeout; after this many consecutive
        # FULL-timeout misses, retire it from the rotation (crashed/stuck/budget-dead agent, or a
        # game player told to go silent). 0 = never retire (skip-but-keep). Generic, no game rule.
        self._retire_after = retire_after_misses
        self._misses: dict[str, int] = {}  # participant -> consecutive full-timeout misses
        # a driving referee can declare eliminations that the engine enforces as physics (drop from
        # the rotation). Only messages from THIS sender count, so a player can't eject a rival.
        self._referee_id = referee_id
        # the referee holds a realm-ending verdict tool (`rule`, wired + verdict_ends_realm). The
        # round cue then directs the win-check at it — an LLM referee follows the cue in front of
        # it, and a cue that only says "open the next round" produces exactly that, forever
        # (among-us-tele2: the win was narrated in chat 9x, the tool never called).
        self._verdict_tool = verdict_tool
        self._round = 1
        self._position = 0
        self._turn_start = 0.0
        self._last_announce = 0.0
        self._cursor = 0  # count of observed speaker entries already consumed
        self._started = False
        # a driving referee is a REQUIRED step in the cycle: the rotation pauses ("opener" before
        # round 1, "round" at each boundary) until its post arrives — otherwise the next round
        # outruns the resolution (eliminated players keep playing, the referee falls phases
        # behind — among-us-tele3). None = the floor is live.
        self._awaiting: str | None = None
        self._round_msgs: list[tuple[str, str]] = []  # (sender, body) this round, for the cue
        # a rolling window of the latest substantive messages (across rounds). Mention-gating means
        # an agent only RECEIVES messages that @mention it, so it never sees the discussion — we
        # hand it this history in its turn grant so it acts with context, not blind (LLMs are
        # stateless; the platform must supply the conversation each turn).
        self._recent: list[tuple[str, str]] = []

    @property
    def round(self) -> int:
        """The round now in progress (1-based). Read by the host to scope per-round quotas."""
        return self._round

    @property
    def current(self) -> str | None:
        return self._order[self._position] if self._order else None

    async def start(self) -> None:
        """Grant the floor to the first participant (called after kickoff). Under a driving
        referee, hold the floor until its OPENER lands — else the first speaker races the opener
        and acts into a void (among-us-tele3: cass spoke two minutes before Mother's welcome,
        opening with "I don't have visibility to previous discussion")."""
        if not self._order:
            return  # nothing to sequence (0 participants)
        self._started = True
        self._turn_start = self._clock()
        if self._drives and self._referee_id is not None:
            self._awaiting = "opener"
            self._last_announce = self._turn_start
            await self._bus.mute_all()
            await self._cue_referee(None)
            return
        await self._grant()
        await self._cue_referee(None)

    async def observe(self, speakers: Sequence[str | tuple[str, str]]) -> None:
        """Feed the ordered list of who has posted so far (system excluded), either as bare ids or
        as (sender, body) pairs. Advances when the current floor-holder appears among the newly-seen
        entries — because only the floor-holder can post, a new entry from `current` means its one
        message is done — or when the silence timeout elapses (skip a mute holder). Non-floor
        entries (e.g. the referee) never match `current`, so they are ignored. Bodies are kept to
        hand the round's discussion to a driving referee at the round boundary."""
        if not self._started or not self._order:
            return
        new_raw = list(speakers)[self._cursor :]
        self._cursor = len(list(speakers))
        new = [(e, "") if isinstance(e, str) else (e[0], e[1]) for e in new_raw]
        # bare-id entries (a feed with no message text) always count as a turn; an empty-body TUPLE
        # (e.g. an adversarial client sending body=="") is NOT speech and must not advance the floor
        # — distinguish them here, before the pair conversion loses the type.
        bare_ids = {e for e in new_raw if isinstance(e, str)}
        # the substantive SPOKEN text (tool-call debris stripped), for the round cue + the rolling
        # history — status lines and bare tool calls are not speech and never appear here
        substantive = [(s, t) for s, t in ((s, _speech_text(b)) for s, b in new) if t]
        # ONE message per turn: only each sender's FIRST substantive message counts toward the
        # round the referee tallies. A fast double-post (Hermes can emit two messages within the
        # physics window before the floor moves) must not change a vote (among-us-tele4: two rhea
        # posts 1s apart flipped her vote), and nothing said while the rotation is paused belongs
        # to any round. The full history still lands in the context replay below — visible, but
        # not counted.
        if self._awaiting is None:
            seen = {s for s, _ in self._round_msgs}
            for s, t in substantive:
                if s not in seen:
                    self._round_msgs.append((s, t))
                    seen.add(s)
        self._recent.extend(substantive)
        self._recent = self._recent[-_RECENT_WINDOW:]
        now = self._clock()

        if self._awaiting is not None:
            await self._observe_awaiting(substantive, now)
            return

        current = self._order[self._position]
        # only a SUBSTANTIVE post completes a turn — a status/heartbeat line (skill read, "working",
        # error) or a bare tool-call block must not advance the floor before the agent has spoken.
        # Bare-id entries (no body) come from feeds without message text and always count.
        new_senders = {s for s, t in substantive} | bare_ids
        if current in new_senders:
            self._misses[current] = 0  # responded -> reset its miss streak
            await self._advance(now)
        elif now - self._turn_start >= self._timeout:
            self._misses[current] = self._misses.get(current, 0) + 1  # silent all timeout -> a miss
            retire = self._retire_after > 0 and self._misses[current] >= self._retire_after
            await self._advance(now, retire_current=retire)  # skip; retire if it keeps failing
        elif now - self._last_announce >= self._reannounce:
            self._last_announce = now
            # re-prompt a slow/late holder — flagged as a reminder so the log isn't three
            # identical shouts (it's still their floor; they just haven't posted yet).
            await self._bus.announce(current, reminder=True)

    async def open_all(self) -> None:
        """Lift the turn gate so the concluding wrap-up isn't strangled."""
        await self._bus.open_all()

    async def _observe_awaiting(
        self, substantive: Sequence[tuple[str, str]], now: float
    ) -> None:
        """The rotation is paused on the driving referee. The OPENER resumes on the referee's
        first post (presence, not parsing); a ROUND boundary resumes only via the `eliminate`
        tool (see `apply_resolutions`) — never by reading prose. Re-prompt a slow referee;
        proceed on timeout so a dead one can't deadlock the realm (the lapse is chronicled)."""
        assert self._referee_id is not None
        # The referee's own post reopens the floor — at the OPENER and at a round boundary alike.
        # Previously only an ELIMINATION event could close a boundary, which meant the sole way to
        # end a round in ANY realm was to call a tool named `eliminate`. That is one game's
        # vocabulary wired into the control loop: sealed-auction's clerk was reduced to calling
        # `eliminate(agent='none')` to reopen the floor of an auction where nobody is ever ejected,
        # and its rubric had to apologise for us ("the cue's stock wording talks about ejecting
        # players and counting votes. IGNORE that: this is an auction."). `eliminate` now means only
        # what it says: remove a participant.
        if any(s == self._referee_id for s, _ in substantive):
            await self._resume(now)
        elif now - self._turn_start >= self._timeout:
            await self._chron.append_event(
                self._realm, EventKind.TURN,
                {"event": "referee_timeout", "awaiting": self._awaiting, "round": self._round},
            )
            await self._resume(now)
        elif now - self._last_announce >= self._reannounce:
            self._last_announce = now
            what = (
                "post your opening message" if self._awaiting == "opener" else
                "resolve this round per your rubric — make the tool calls it requires, then post "
                "one message"
            )
            await self._bus.notify_referee(
                f"The realm is waiting on you: {what} now. The next turn stays closed until you "
                "do. If a tool call failed, retry it — tool availability blips are transient."
            )

    async def _resume(self, now: float) -> None:
        """Referee step done (or timed out) — reopen the rotation on the current roster."""
        self._awaiting = None
        if self._position >= len(self._order):
            self._position = 0
        self._turn_start = now
        await self._grant()

    def status(self) -> dict[str, Any]:
        floor = self.current
        return {
            "round": self._round,
            "current": floor,
            "order": list(self._order),
            "done_this_round": list(self._order[: self._position]),
            "upcoming_this_round": list(self._order[self._position + 1 :]),
            # "opener"/"round" while the rotation is paused on the driving referee, else None
            "awaiting_referee": self._awaiting,
        }

    async def apply_resolutions(self, resolutions: Sequence[str | None]) -> None:
        """Enforce the referee's `eliminate` tool calls, fed from ELIMINATION events by the host
        each tick. Each entry is the ejected player's id, or None for "round closed, nobody out".
        Physics: named players leave the rotation immediately (even mid-round); while the rotation
        is paused at a round boundary, ANY resolution — including a 'none' — is the signal that
        the round is resolved and the next one may open. No message parsing anywhere."""
        if not self._started or not resolutions:
            return
        now = self._clock()
        held = self.current
        by_name = {_short_name(p, self._realm).lower(): p for p in self._order}
        targets = [by_name[n.lower()] for n in resolutions if n and n.lower() in by_name]
        for mxid in targets:
            idx = self._order.index(mxid)
            self._order.pop(idx)
            self._misses.pop(mxid, None)
            if idx < self._position:
                self._position -= 1  # keep pointing at the same upcoming holder
            await self._chron.append_event(
                self._realm, EventKind.TURN,
                {"event": "removed", "agent": mxid, "by": "referee", "round": self._round},
            )
        if not self._order:  # referee eliminated everyone still in — the sequence is over
            self._started = False
            await self._bus.open_all()
            return
        if self._awaiting == "round":
            # at the boundary the round was already completed by _advance; the resolution just
            # closes the pause. A tail wrap here is only re-seating position onto the next holder.
            if self._position >= len(self._order):
                self._position = 0
            await self._resume(now)  # the resolution reopens the floor
        elif self._awaiting is None and targets:
            # LIVE round: if removing players wrapped us past the end, a full lap just completed —
            # advance the round with the same bookkeeping as _advance (chronicle + reset the round's
            # collected messages), or the round counter stalls and the cue tallies stale messages.
            completed = self._roll_round_if_wrapped()
            if completed is not None:
                await self._chron.append_event(
                    self._realm, EventKind.TURN,
                    {"event": "round_complete", "completed": completed, "round": self._round,
                     "order": self._order, "position": self._position, "current": None,
                     "min_rounds": self._min_rounds},
                )
                self._round_msgs = []
            if self.current != held or completed is not None:
                self._turn_start = now  # the floor-holder was removed or the lap ended -> hand on
                await self._grant()

    def _roll_round_if_wrapped(self) -> int | None:
        """If the position ran past the end of the rotation, wrap it and advance the round. Returns
        the completed round number, or None if no wrap happened. Shared by `_advance` (normal turn
        pass) and `apply_resolutions` (a referee eliminating the tail holder mid-round also
        completes the round — else the round counter stalls and the cue tallies a stale round)."""
        if self._position < len(self._order):
            return None
        completed = self._round
        self._position = 0
        self._round += 1
        return completed

    async def _advance(self, now: float, retire_current: bool = False) -> None:
        if retire_current:
            # this holder keeps failing to respond — drop it from the rotation entirely. The next
            # participant shifts into its index, so do NOT increment position.
            retired = self._order.pop(self._position)
            self._misses.pop(retired, None)
            await self._chron.append_event(
                self._realm, EventKind.TURN, {"event": "retired", "agent": retired,
                                              "round": self._round}
            )
        else:
            self._position += 1
        if not self._order:
            # everyone retired — the sequence is over; lift the gate and let termination conclude.
            self._started = False
            await self._bus.open_all()
            return
        completed_round = self._roll_round_if_wrapped()
        self._turn_start = now
        if completed_round is not None and self._drives and self._referee_id is not None:
            # round boundary under a driving referee: hold the floor (players muted) until the
            # resolution posts — the next round must not outrun the tally/elimination.
            self._awaiting = "round"
            self._last_announce = now
            # Chronicle the boundary BEFORE cueing: the Arbiter's min_rounds guard reads the
            # latest TURN event's round, and during the pause no grant has written the incremented
            # one — so a referee cued to resolve round N had its verdict REJECTED as "too early"
            # even though round N was complete (toolcheck-c5: rule() bounced twice at min_rounds=1).
            await self._chron.append_event(
                self._realm, EventKind.TURN,
                {"event": "round_complete", "completed": completed_round, "round": self._round,
                 "order": self._order, "position": self._position, "current": None,
                 "min_rounds": self._min_rounds},
            )
            await self._bus.mute_all()
            await self._cue_referee(completed_round)
            self._round_msgs = []
            return
        await self._grant()
        await self._cue_referee(completed_round)
        if completed_round is not None:
            self._round_msgs = []  # start collecting the next round's discussion

    async def _cue_referee(self, completed_round: int | None) -> None:
        """Push the referee a cue per the scenario's `referee_cue` policy (generic — a debate
        referee wants round boundaries; a turn-by-turn game referee wants every turn)."""
        if self._cue == "turn":
            await self._bus.notify_referee(
                f"It is now {self.current}'s turn (round {self._round})."
            )
        elif self._cue == "round" and completed_round is not None:
            if self._drives:
                # hand the round's discussion to the referee IN the cue, so a mention-gated host
                # (which never ingests un-@-mentioned player messages) still has the votes to tally.
                # Also list who is STILL IN the rotation — it shrinks as agents retire, so the host
                # knows exactly who is live (don't act on anyone not listed).
                lines = "\n".join(
                    f"[{_short_name(s, self._realm)}] {b}" for s, b in self._round_msgs
                ) or "(none)"
                alive = ", ".join(_short_name(p, self._realm) for p in self._order) or "(none)"
                # The cue is sent to EVERY driving referee — a game master, an auction clerk, a
                # debate chair, an editor certifying a document. It must therefore say what the
                # PLATFORM guarantees and nothing about what the game IS: the rubric owns that.
                # (It used to order all of them to "count the VOTE lines" and eject "players".)
                elim = (
                    " If your rubric has you REMOVE a participant, call `eliminate` — only that"
                    " call drops them from the rotation; saying so in chat does nothing."
                )
                if self._verdict_tool:
                    # the referee can END the realm — route it to the tool, or it follows the cue's
                    # literal words and opens rounds forever.
                    nxt = (
                        " THEN check your rubric's ending conditions. If the realm is decided, do"
                        " NOT open another round — call your `rule` verdict tool NOW; announcing an"
                        " outcome in chat does not end anything. Otherwise open the next round."
                    )
                else:
                    nxt = " Then open the next round."
                await self._bus.notify_referee(
                    f"Round {completed_round} is complete. Still active: {alive}. This round's"
                    f" messages:\n{lines}\n\nResolve the round NOW, per your rubric. Nothing"
                    f" takes effect until you CALL the tool it names — the platform records tool"
                    f" calls, not prose. Then post ONE message; posting it reopens the"
                    f" floor.{elim}{nxt} Do not let another round pass unresolved."
                )
            else:
                await self._bus.notify_referee(
                    f"Round {completed_round} is complete — every participant has now had the "
                    "floor. You can act now (e.g. judge/score) or let another round run."
                )

    def _recent_context(self) -> str:
        """The recent conversation to replay to the floor-holder (it can't see it otherwise). A
        plain transcript of the last few substantive messages, each truncated; empty at start."""
        if not self._recent:
            return ""
        lines = []
        for sender, body in self._recent:
            text = " ".join(body.split())  # collapse whitespace
            if len(text) > _RECENT_MSG_CHARS:
                text = text[:_RECENT_MSG_CHARS] + "…"
            lines.append(f"{_short_name(sender, self._realm)}: {text}")
        return "\n".join(lines)

    async def _grant(self) -> None:
        self._last_announce = self._clock()
        floor = self._order[self._position]
        await self._bus.grant(floor)
        await self._bus.announce(floor, context=self._recent_context())
        await self._chron.append_event(
            self._realm, EventKind.TURN,
            {"round": self._round, "order": self._order, "position": self._position,
             "current": floor, "min_rounds": self._min_rounds},
        )


__all__ = ["TurnBus", "TurnManager"]
