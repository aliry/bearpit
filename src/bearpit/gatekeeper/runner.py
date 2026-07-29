"""Runner — provision and run a realm end to end (M7 orchestration).

Ties the components into the "First Duel" flow: Herald mints the bus, Forge provisions
agents (Ledger mints their keys), Warden watches for termination and concludes. The live
snapshot assembler gathers termination inputs from the running realm each tick. The Runner
is component-agnostic (takes the collaborators) so it is fully testable with fakes; the CLI
wires the real implementations.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from bearpit.chronicle import Chronicle, EventKind
from bearpit.core.redact import Redactor
from bearpit.core.schema import Project
from bearpit.forge import Forge, RealmHandles
from bearpit.forge.container import ContainerRuntime
from bearpit.herald import BusProvision, Herald
from bearpit.herald.types import MatrixCreds
from bearpit.ledger import Ledger
from bearpit.warden import ConcludeResult, RealmSnapshot, TurnManager, Warden

SnapshotProvider = Callable[[], Awaitable[RealmSnapshot]]
SnapshotFactory = Callable[
    [str, RealmHandles, BusProvision, "TurnManager | None"], SnapshotProvider
]


class HeraldTurnBus:
    """Adapts the Herald's floor controls to the TurnManager's TurnBus, bound to one realm's
    commons + roster (so the TurnManager stays Matrix-agnostic and testable)."""

    def __init__(
        self, herald: Herald, room: str, participants: list[str], referee: str | None
    ) -> None:
        self._h = herald
        self._room = room
        self._participants = participants
        self._referee = referee

    async def grant(self, floor: str) -> None:
        await self._h.grant_floor(self._room, floor, self._participants, self._referee)

    async def open_all(self) -> None:
        await self._h.open_floor(self._room, self._participants, self._referee)

    async def mute_all(self) -> None:
        # floor=None -> every player muted; only the referee/system may post (round boundary)
        await self._h.grant_floor(self._room, None, self._participants, self._referee)

    async def announce(self, floor: str, *, reminder: bool = False, context: str = "") -> None:
        if reminder:
            text = ("Still your turn — you haven't POSTED yet (tool calls don't count; only a "
                    "posted message does). Post your one message now, or the floor passes to the "
                    "next speaker.")
        else:
            # Agents are mention-gated, so they don't see the discussion — replay it here so the
            # floor-holder acts with the conversation as context, not blind.
            preface = (f"Conversation so far (you only see this on your turn):\n{context}\n\n"
                       if context else "")
            # The turn grant is the LAST thing the agent reads, so it is the instruction it obeys.
            # A bare "post your one message NOW" therefore overrides every scenario that requires a
            # tool call first — the agent just posts and skips the tool (toolcheck-c1: players
            # never called submit_sealed). Say plainly that tools are free and the POST is what
            # ends the turn; this stays generic (a tool-less scenario simply has nothing to do
            # first).
            text = (preface +
                    "It is your turn — you have the floor.\n"
                    "1. FIRST do whatever your instructions require of a turn. Tool calls are FREE "
                    "and do NOT use up your turn — take as many as you need.\n"
                    "2. THEN post your ONE message to the Commons, building on the discussion "
                    "above.\n"
                    "Posting that message is what passes the floor — nothing else does, and no one "
                    "else can speak until you do.")
        await self._h.announce(self._room, text, mentions=[floor])

    async def notify_referee(self, text: str) -> None:
        if self._referee is None:
            return  # no referee to notify
        await self._h.announce(self._room, text, mentions=[self._referee])


@dataclass
class Runner:
    herald: Herald
    forge: Forge
    warden: Warden
    ledger: Ledger
    chronicle: Chronicle
    attach_containers: tuple[str, ...] = ()  # service containers to join each realm network

    async def run(
        self,
        realm_id: str,
        project: Project,
        snapshot_factory: SnapshotFactory,
        *,
        system_password: str,
        require_mention: bool = True,
        grace: timedelta = timedelta(seconds=10),
        interval_s: float = 5.0,
        max_ticks: int | None = None,
        run_config: dict[str, Any] | None = None,
    ) -> ConcludeResult:
        await self.herald.ensure_system(system_password)
        await self.chronicle.append_event(realm_id, EventKind.LIFECYCLE, {"event": "provisioning"})
        bus = await self.herald.provision_bus(realm_id, project, require_mention=require_mention)
        handles = await self.forge.provision_realm(
            realm_id, project, bus.creds,
            bus_homeserver=self.herald.homeserver,  # agent-facing (in-cluster) URL
            proxy_url=self.ledger.proxy_url,  # agent-facing (in-cluster) URL
            commons_room=bus.commons_room,
            attach_containers=self.attach_containers,
            side_channels=bus.side_channels,
        )
        await self.chronicle.append_event(
            realm_id, EventKind.LIFECYCLE,
            {"event": "running", "commons_room": bus.commons_room,
             # {room_id: "a · b"} so the UI can label private DM threads in the transcript
             "side_channels": {r: c["label"] for r, c in bus.side_channels.items()},
             # The configuration this run ACTUALLY used — models, turns, budgets, all RESOLVED.
             # It rides in the lifecycle event (not its own event) because realm_status reads the
             # LAST lifecycle payload as the realm's state; a separate "config" event would
             # masquerade as one. Captured here so it stays true for an archived realm.
             "config": run_config or {},
             # ...and the whole resolved project, so the run can be REPLAYED exactly — same models,
             # same personas, same budgets — even if the scenario file is edited afterwards or the
             # active provider is switched. Kept OUT of `config` because the UI polls that every
             # few seconds and this is large; it is read server-side by the re-run endpoint only.
             "project": _project_snapshot(project),
             # where it came from, so the SAME scenario can be relaunched against the CURRENT file
             "package": project.source,
             "require_mention": require_mention},
        )

        # Turn management (opt-in): the TurnManager drives a one-at-a-time floor. When on, the
        # kickoff must NOT broadcast (else every agent tries to post at once and hits the mute) —
        # the TurnManager addresses the first floor-holder instead.
        turn_manager = self._build_turn_manager(realm_id, project, bus)

        # kick off: wait for agents to boot + join (else the kickoff is sent before they sync
        # and is missed — verified live), then address them so they start.
        await self.herald.wait_for_agents(bus)
        await self.herald.kickoff(bus, project, broadcast=turn_manager is None)
        if turn_manager is not None:
            await turn_manager.start()

        snapshot = snapshot_factory(realm_id, handles, bus, turn_manager)

        referee = project.referee
        drives = project.spec.referee_opens and referee is not None

        async def _nudge() -> None:
            if drives:
                # A game-master referee drives the realm — poke ONLY it to post the next phase,
                # not the whole roster (broadcasting makes idle players trade acknowledgements).
                assert referee is not None
                # Say what the PLATFORM guarantees; leave what the realm IS to the rubric. This
                # used to tell every driving referee to "announce the hidden action and reveal it,
                # open the meeting, or call the vote" — three Among Us phases, delivered to an
                # auction clerk, a jury foreperson and an RPS judge at the exact moment the platform
                # thinks the realm is stuck, i.e. when a referee is most likely to take a literal
                # instruction literally.
                await self.herald.nudge(
                    bus,
                    "You drive this realm — continue now: take the next step your rubric calls "
                    "for. Only tool calls change anything; announcing something in chat does "
                    "not. Don't wait.",
                    mentions=[bus.creds[referee.id].user_id],
                )
            else:
                await self.herald.nudge(bus)

        on_conclude = turn_manager.open_all if turn_manager is not None else None
        return await self.warden.watch(
            realm_id, handles, bus.commons_room, project.effective_termination, snapshot,
            interval_s=interval_s, max_ticks=max_ticks, grace=grace,
            nudge=_nudge if project.spec.stall_nudge else None,  # per-project opt-out
            on_conclude=on_conclude,
        )

    def _build_turn_manager(
        self, realm_id: str, project: Project, bus: BusProvision
    ) -> TurnManager | None:
        if project.spec.turns is None:
            return None
        referee = project.referee
        ref_mxid = bus.creds[referee.id].user_id if referee is not None else None
        participants = [
            c.user_id for aid, c in bus.creds.items()
            if referee is None or aid != referee.id
        ]
        turn_bus = HeraldTurnBus(self.herald, bus.commons_room, participants, ref_mxid)
        turns = project.spec.turns
        # the referee can end the realm via its `rule` verdict tool only when the realmtools are
        # actually wired (provide_tools) AND its verdict concludes the realm — then the round cue
        # directs the win-check at the tool call (an unwired/inert tool must not be advertised).
        verdict_tool = bool(
            project.spec.provide_tools
            and referee is not None
            and referee.powers is not None
            and referee.powers.verdict_ends_realm
        )
        return TurnManager(
            realm_id, participants, turn_bus, self.chronicle,
            clock=time.time, silence_timeout_s=turns.silence_timeout_s,
            referee_cue=str(turns.referee_cue),
            min_rounds_before_verdict=turns.min_rounds_before_verdict,
            referee_drives=project.spec.referee_opens and referee is not None,
            retire_after_misses=turns.retire_after_misses,
            referee_id=ref_mxid,
            verdict_tool=verdict_tool,
        )


def _project_snapshot(project: Project) -> dict[str, Any]:
    """The resolved project, serialised for an exact replay.

    `resource_files` and `local_skills` are loader state (exclude=True), so a plain model_dump
    drops them — and a replayed realm would silently lose the reference files and hand-written
    skills the original agents were given. They are carried alongside."""
    data = project.model_dump(mode="json", by_alias=True)
    data["_loaded"] = {
        a.id: {"resource_files": a.resource_files, "local_skills": a.local_skills}
        for a in project.agents
        if a.resource_files or a.local_skills
    }
    return data


def project_from_snapshot(data: dict[str, Any]) -> Project:
    """Rebuild a project from `_project_snapshot`, restoring the loader state."""
    loaded = data.pop("_loaded", {}) if isinstance(data, dict) else {}
    project = Project.model_validate(data)
    for agent in project.agents:
        got = loaded.get(agent.id) or {}
        agent.resource_files = got.get("resource_files", {}) or {}
        agent.local_skills = got.get("local_skills", {}) or {}
    return project


class LiveSnapshot:
    """Assembles a RealmSnapshot from the running realm each tick: elapsed time, mirrored
    messages, shared-folder files, per-agent spend, referee verdict, and the manual stop flag."""

    def __init__(
        self,
        *,
        herald: Herald,
        ledger: Ledger,
        chronicle: Chronicle,
        runtime: ContainerRuntime,
        realm_id: str,
        commons_room: str,
        shared_volume: str | None,
        stop_flag: Callable[[], bool],
        clock: Callable[[], float] = time.time,
        turns: TurnManager | None = None,
        side_channels: dict[str, dict[str, object]] | None = None,
        creds: dict[str, MatrixCreds] | None = None,
        dm_quota: dict[str, int] | None = None,
        containers: dict[str, str] | None = None,
        agent_tokens: dict[str, str] | None = None,
        budget_policy: dict[str, tuple[str, float]] | None = None,
        participants: Sequence[str] | None = None,
    ) -> None:
        self._herald = herald
        self._ledger = ledger
        self._chron = chronicle
        self._runtime = runtime
        self._realm = realm_id
        self._commons = commons_room
        self._shared = shared_volume
        self._stop = stop_flag
        self._clock = clock
        self._start = clock()
        self._last_activity = self._start  # clock of the last new agent message (for `stall`)
        self._prev_msg_count = 0
        self._turns = turns
        # room_id -> {"members": [mxid,...], "label": "a · b"} for the platform-brokered DM rooms
        self._side_channels = side_channels or {}
        self._creds = creds or {}  # agent_id -> MatrixCreds (to post a DM AS its real sender)
        # agent_id -> realmtools bearer. Held ONLY so it can be masked out of recorded output.
        self._agent_tokens = agent_tokens or {}
        self._delivered_private: set[int] = set()  # PRIVATE event ids already delivered to Matrix
        # agent_id -> max private messages it may SEND per round (0/absent = unlimited)
        self._dm_quota = dm_quota or {}
        self._dm_sent: dict[tuple[str, int], int] = {}  # (agent, round) -> delivered so far
        self._dm_warned: set[tuple[str, int]] = set()  # (agent, round) already told it is spent
        # agent_id -> its OWN container. The map is the security boundary for `run_code`: the agent
        # is taken from the caller's verified token, never from a tool argument, so an agent can
        # only ever execute inside its own sandbox.
        self._containers = containers or {}
        self._executed: set[int] = set()  # EXEC event ids already run
        # agent_id -> (on_exhausted, grace_seconds). The BUDGET boundary (architecture §6) was only
        # half-built: LiteLLM refuses the call, but nothing ever acted on it. `Ledger.exhausted()`
        # even documents itself as "(Warden acts on these)" — and had no caller in production.
        self._budget_policy = budget_policy or {}
        self._exhausted_since: dict[str, float] = {}  # agent -> clock when its cap was first hit
        self._killed_broke: set[str] = set()
        self._applied_eliminations: set[int] = set()  # ELIMINATION event ids already enforced
        self._eliminated: set[str] = set()  # agent ids removed from the realm (containers stopped)
        # The non-referee roster, for the `no_active_participants` termination. Empty means "not
        # tracked", which can never trip the rule — an older caller that omits it keeps today's
        # behaviour rather than silently gaining a new way for its realms to end.
        self._participants: list[str] = list(participants or ())
        self._dm_dead_notified: set[tuple[str, str]] = set()  # (from,to) told the peer is gone
        # route a (from,to) pair to its DM room by the label's bare ids ("a · b")
        self._dm_route: dict[frozenset[str], str] = {}
        for room, info in self._side_channels.items():
            ids = [p.strip() for p in str(info.get("label", "")).split("·")]
            if len(ids) == 2 and all(ids):
                self._dm_route[frozenset(ids)] = room

    async def __call__(self) -> RealmSnapshot:
        # Deliver any queued private messages first (agents call send_private, which records a
        # PRIVATE event; realmtools can't post to Matrix, so the host delivers into the DM room AS
        # the sender). Doing it before the mirror means the message is captured this same tick.
        await self._deliver_private()
        await self._run_exec_requests()
        await self._herald.mirror(self._realm, self._commons, self._chron)
        # Also mirror the platform-brokered private DM rooms — the system is a member, so they are
        # captured into the Chronicle just like the commons (they carry their own room-id channel).
        for room in self._side_channels:
            await self._herald.mirror(self._realm, room, self._chron)
        # Label commons messages "commons" (termination conditions use that, not the room id),
        # and EXCLUDE the platform's own @system posts: the kickoff quotes the guidelines, which
        # mention the termination phrase (e.g. "post VERDICT:") — matching that would end the
        # realm on turn 0. Only agent messages count for termination + progress.
        non_system = [
            m for m in await self._chron.messages(self._realm, self._commons)
            if not m.sender.startswith("@system:")
        ]
        # Feed the turn manager the speakers so it can pass the floor when the current holder
        # posts (it consumes new entries via an internal cursor; the referee is ignored).
        if self._turns is not None:
            # Observe the messages FIRST, THEN apply eliminations. If a stray player post slips
            # through the mute race in the same tick as the referee's eliminate, observing first
            # consumes that post while the rotation is still PAUSED (so it is correctly ignored and
            # the cursor advances past it); applying the elimination then resumes the floor. The old
            # order resumed first, so the just-past post leaked into the NEW round and stole a turn.
            await self._turns.observe([(m.sender, m.body) for m in non_system])
        # Enforce the referee's `eliminate` calls (tool-based, never parsed from prose) at the
        # CONTAINER boundary — in every turn mode. An eliminated agent must FULLY leave the realm:
        # dropped from the turn rotation (below, when turns are on) AND its container stopped, so it
        # can no longer post, DM, or run code. The turn mute alone left the side-channel open —
        # among-us-cb70f7 ejected an impostor in R3 and it kept conferring privately with its
        # partner. Each ELIMINATION event is enforced exactly once.
        new_elims = [
            e for e in await self._chron.events(self._realm, kind=EventKind.ELIMINATION)
            if e.id not in self._applied_eliminations
        ]
        if new_elims:
            self._applied_eliminations.update(e.id for e in new_elims)
            for agent in {str(e.payload.get("agent"))
                          for e in new_elims if e.payload.get("agent")}:
                self._eliminated.add(agent)
                await self._stop_agent_container(agent)
            if self._turns is not None:
                await self._turns.apply_resolutions(
                    [e.payload.get("agent") for e in new_elims]
                )
        messages = [("commons", m.body) for m in non_system]
        # idle time for the `stall` termination: reset the clock whenever a new agent message lands;
        # if the realm goes quiet (e.g. a player stops responding and the rest wait), idle grows.
        # PRIVATE DM traffic counts too: two impostors conferring in their side-channel while the
        # Commons stays quiet are actively working, and stalling them out mid-plot (then claiming
        # "no agent message") was a real bug. Every PRIVATE event is agent activity.
        now = self._clock()
        private_count = len(await self._chron.events(self._realm, kind=EventKind.PRIVATE))
        activity = len(non_system) + private_count
        if activity > self._prev_msg_count:
            self._prev_msg_count = activity
            self._last_activity = now
        spend = await self._ledger.poll_spend(self._realm, self._chron)
        await self._enforce_budgets(spend)
        contents = self._runtime.read_volume(self._shared) if self._shared else {}
        verdicts = await self._chron.events(self._realm, kind=EventKind.VERDICT)
        verdict = str(verdicts[-1].payload.get("outcome")) if verdicts else None
        # Who could still act? A participant whose container has been stopped — killed for budget or
        # eliminated by the referee — cannot. The referee is excluded by construction: it is alive
        # and funded in exactly the case this exists to catch, calling rounds into an empty room.
        gone = self._killed_broke | self._eliminated
        alive = [a for a in self._participants if a not in gone]
        return RealmSnapshot(
            elapsed_s=now - self._start,
            messages=messages,
            files=list(contents),
            file_contents=contents,
            spend=spend,
            verdict=verdict,
            idle_s=now - self._last_activity,
            manual_stop=self._stop(),
            participants=len(self._participants),
            participants_alive=len(alive),
        )

    async def _enforce_budgets(self, spend: dict[str, tuple[float, float | None]]) -> None:
        """Act on an agent that has spent its cap — the KILL half of the budget boundary.

        LiteLLM starves the key (it answers 429), but nothing stopped the agent, so the runtime kept
        retrying and POSTING each failure into the room. debate-1 drowned in 2,540 copies of "the
        model provider is rate-limiting requests" and the chair, seeing no arguments, ruled "no
        contest". A starving agent that cannot be silenced does not just die quietly — it takes the
        realm down with it.
        """
        # computed here rather than via Ledger.exhausted() so the check does not depend on the
        # ledger implementation (the same rule: cumulative spend has reached the key's cap)
        broke = [a for a, (used, cap) in spend.items() if cap is not None and used >= cap]
        now = self._clock()
        for agent in broke:
            policy, grace = self._budget_policy.get(agent, ("starve", 0.0))
            if agent in self._killed_broke:
                continue
            if agent not in self._exhausted_since:
                self._exhausted_since[agent] = now
                await self._chron.append_event(
                    self._realm, EventKind.LIFECYCLE,
                    {"event": "budget_exhausted", "agent": agent, "policy": policy,
                     "spend": round(spend[agent][0], 6), "cap": spend[agent][1]},
                )
            if policy == "starve":
                continue  # the scenario asked for it: the agent lives on, unable to call the model
            if policy == "starve_then_kill" and now - self._exhausted_since[agent] < grace:
                continue  # still inside its grace period — let it finish what it can
            container = self._containers.get(agent)
            if container is None:
                continue
            self._killed_broke.add(agent)
            with contextlib.suppress(Exception):  # a kill that fails must not break the tick
                await asyncio.to_thread(self._runtime.stop_container, container, timeout=5)
            await self._chron.append_event(
                self._realm, EventKind.LIFECYCLE,
                {"event": "agent_killed", "agent": agent, "reason": "budget exhausted",
                 "policy": policy},
            )

    async def _run_exec_requests(self) -> None:
        """Execute agents' queued `run_code` requests, each inside THAT agent's own container.

        realmtools records the intent and waits; the host owns Docker, so the host performs it —
        the same broker shape as `send_private`. realmtools never gets a Docker socket, because a
        socket in that small agent-facing server would turn any bug in it into host root."""
        if not self._containers:
            return
        for ev in await self._chron.events(self._realm, kind=EventKind.EXEC):
            if ev.id in self._executed:
                continue
            self._executed.add(ev.id)
            agent = str(ev.payload.get("agent", ""))
            code = str(ev.payload.get("code", ""))
            container = self._containers.get(agent)  # the CALLER'S container, and only ever that
            if not container or not code:
                await self._chron.append_event(
                    self._realm, EventKind.EXEC_RESULT,
                    {"id": ev.payload.get("id"), "agent": agent, "exit_code": None,
                     "output": "no container available for this agent"},
                )
                continue
            exit_code: int | None
            output: str
            try:
                exit_code, output = await asyncio.to_thread(
                    self._runtime.exec_python, container, code
                )
            except Exception as exc:  # a broken exec must answer, or the agent blocks for 90s
                exit_code, output = None, f"exec failed: {exc}"
            await self._chron.append_event(
                self._realm, EventKind.EXEC_RESULT,
                {"id": ev.payload.get("id"), "agent": agent,
                 "exit_code": exit_code, "output": self._redactor()(output)[:8000]},
            )

    def _redactor(self) -> Redactor:
        """Mask the credentials this platform minted for this realm.

        `run_code` runs inside the agent's own container, which holds its Matrix access token and
        its LiteLLM virtual key in plaintext. An agent that prints its environment — deliberately
        or incidentally — would otherwise write live, replayable credentials into an append-only
        log that is served over the API and included in exports.

        All THREE of an agent's credentials, because two out of three is a defence that only
        looks complete: its Matrix access token, its model-proxy virtual key, and its realmtools
        bearer — the last being the one that calls eliminate()/tally() and reads sealed
        submissions as that agent.

        Rebuilt per call rather than cached: agents are provisioned as the realm starts, so a
        snapshot taken once would miss whatever was minted afterwards."""
        return Redactor([
            *(c.access_token for c in self._creds.values()),      # Matrix
            *self._ledger.minted_keys(self._realm),               # model proxy
            *self._agent_tokens.values(),                         # realmtools
        ])

    async def _stop_agent_container(self, agent: str) -> None:
        """Stop an eliminated agent's container so it fully leaves the realm — no more posting, DMs
        or run_code. Best-effort and idempotent: the elimination stands even if the stop fails, and
        an already-gone container is fine. The container is STOPPED, not removed, so its logs
        survive for the flight recorder until realm teardown."""
        cid = self._containers.get(agent)
        if not cid:
            return
        try:
            await asyncio.to_thread(self._runtime.stop_container, cid, timeout=5)
            await self._chron.append_event(
                self._realm, EventKind.SYSTEM,
                {"event": "agent_stopped", "agent": agent, "reason": "eliminated"},
            )
        except Exception as exc:  # a teardown hiccup must never wedge the tick
            await self._chron.append_event(
                self._realm, EventKind.SYSTEM,
                {"event": "agent_stop_failed", "agent": agent, "detail": str(exc)},
            )

    async def _deliver_private(self) -> None:
        """Drain new PRIVATE events into their DM rooms. Each is posted AS the sender (so the mirror
        records the real author) and @mentions the recipient. The host is also the permission gate:
        a message with no DM room for its (from,to) pair is silently dropped — a pair only has a
        room when the roster's private_messaging allowed it."""
        if not self._dm_route:
            return
        for ev in await self._chron.events(self._realm, kind=EventKind.PRIVATE):
            if ev.id in self._delivered_private:
                continue
            self._delivered_private.add(ev.id)
            p = ev.payload
            frm, to, text = str(p.get("from", "")), str(p.get("to", "")), str(p.get("text", ""))
            # an eliminated agent is out of the realm: never deliver a DM it sent, and never one
            # addressed to it (its container is stopped). Tell a living sender once that its peer
            # has left, so it stops messaging the void.
            if frm in self._eliminated:
                continue
            if to in self._eliminated:
                room = self._dm_route.get(frozenset({frm, to}))
                if room is not None and (frm, to) not in self._dm_dead_notified:
                    self._dm_dead_notified.add((frm, to))
                    await self._herald.announce(
                        room, f"🚫 {to} has left the realm (eliminated) and cannot be reached."
                    )
                continue
            room = self._dm_route.get(frozenset({frm, to}))
            sender = self._creds.get(frm)
            if not (frm and to and text) or room is None or sender is None:
                continue  # unknown pair / missing sender creds / no channel — nothing to deliver
            # PER-ROUND QUOTA. Two always-on agents alone in a DM room acknowledge each other
            # forever — the Commons has floor control, a private room has none, so every delivery
            # provokes a reply (among-us-sim1: 25 messages of "Copy"/"Agreed" between the two
            # impostors in a single round, burning the round and the budget). The send is recorded
            # in the Chronicle either way (the operator still sees the ATTEMPT); it just stops
            # being DELIVERED once the sender is out of budget for the round.
            quota = self._dm_quota.get(frm, 0)
            if quota:
                rnd = self._turns.round if self._turns is not None else 0
                seen = self._dm_sent.get((frm, rnd), 0)
                if seen >= quota:
                    if (frm, rnd) not in self._dm_warned:
                        self._dm_warned.add((frm, rnd))
                        # No mentions: the notice must NOT ping anyone, or it provokes the very
                        # reply we are trying to stop. It lands in the room for the record.
                        await self._herald.announce(
                            room,
                            f"🔇 {frm} has used its {quota} private message(s) for this round. "
                            f"Further private messages are not delivered until the next round.",
                        )
                    continue
                self._dm_sent[(frm, rnd)] = seen + 1
            recipient = self._creds.get(to)
            mentions = [recipient.user_id] if recipient else None
            await self._herald.post_as(sender.access_token, room, text, mentions=mentions)
