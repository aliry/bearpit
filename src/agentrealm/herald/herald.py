"""Herald — the Matrix bus provisioner + firehose mirror (M3, §7).

Provisions a realm's bus: registers the **system** account first (Conduit grants server
admin to the first account — never an agent, S2 finding), then one realm-scoped Matrix user
per agent, creates the commons room (system invites all agents), and returns the per-agent
`MatrixCreds` Forge injects. `mirror` reads a room's messages into the Chronicle (every
message — the Chronicle is the source of truth). Room ACLs are physics: an agent not invited to
a room cannot read or post in it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from agentrealm.chronicle import Chronicle
from agentrealm.core.schema import Project
from agentrealm.core.settings import DEFAULT_OPERATOR
from agentrealm.herald.matrix import MatrixClient
from agentrealm.herald.types import MatrixCreds

SYSTEM_USER = "system"
FLOOR_LEVEL = 50  # power level to post a message while turns are on (below it = muted)


@dataclass(frozen=True)
class BusProvision:
    commons_room: str
    creds: dict[str, MatrixCreds]  # agent_id -> creds
    # platform-brokered private DM rooms: room_id -> {"members": [mxid,...], "label": "a · b"}.
    # System is the creator (always a member), so these mirror into the Chronicle like the commons.
    side_channels: dict[str, dict[str, object]] = field(default_factory=dict)


class Herald:
    def __init__(
        self, client: MatrixClient, *, server_name: str, homeserver: str,
        operator: str = DEFAULT_OPERATOR,
    ) -> None:
        self._c = client
        self._server = server_name
        self._homeserver = homeserver
        self._operator = operator
        self._system_token: str | None = None
        self._mirrored: dict[str, set[str]] = {}  # room_id -> seen event ids (mirror dedup)

    @property
    def homeserver(self) -> str:
        return self._homeserver

    def _mxid(self, localpart: str) -> str:
        return f"@{localpart}:{self._server}"

    async def ensure_system(self, password: str) -> str:
        """Register/login the system account. Must be the first account on a fresh homeserver
        (first account = admin); never an agent (S2)."""
        self._system_password = password  # keyed into each agent's stable password (see below)
        self._system_token = await self._c.register_or_login(SYSTEM_USER, password)
        return self._system_token

    def _agent_password(self, localpart: str) -> str:
        """A per-agent password that is stable across runs of the same realm id. Keyed off the
        system secret so it is not guessable without it. (A random per-run password broke re-running
        an explicit realm id — the account already exists and the new password never matches.)"""
        base = getattr(self, "_system_password", None)
        if not base:
            # Unreachable today (ensure_system runs first), and it must stay that way: falling back
            # to a constant would make every agent password derivable by anyone who read this file.
            raise RuntimeError(
                "system password not set — call ensure_system() before provisioning agents"
            )
        return hmac.new(base.encode(), localpart.encode(), hashlib.sha256).hexdigest()

    async def provision_bus(
        self, realm_id: str, project: Project, *, require_mention: bool = True
    ) -> BusProvision:
        if self._system_token is None:
            raise RuntimeError("call ensure_system() before provision_bus()")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", realm_id):
            raise ValueError(f"realm_id {realm_id!r} must be lowercase alnum/dash")

        # realm-scoped usernames avoid cross-realm collisions; localpart has no leading '_' (C1)
        users: dict[str, tuple[str, str]] = {}  # agent_id -> (mxid, token)
        for agent in project.agents:
            localpart = f"{realm_id}-{agent.id}"
            # A STABLE password, derived from the system secret + the agent's mxid — NOT a fresh
            # random one each run. Matrix accounts are permanent (teardown never deletes them), so a
            # RE-RUN of an explicit realm id hit M_USER_IN_USE and the login fallback tried a brand-
            # new random password that could never match the original -> 403 -> the whole realm
            # "failed". A derived password lets register_or_login log back in on reuse.
            token = await self._c.register_or_login(localpart, self._agent_password(localpart))
            users[agent.id] = (self._mxid(localpart), token)

        invites = [mxid for mxid, _ in users.values()]
        commons = await self._c.create_room(
            self._system_token, name="Realm Commons", invite=invites
        )

        system_id, operator_id = self._mxid(SYSTEM_USER), self._mxid(self._operator)
        all_ids = [mxid for mxid, _ in users.values()]
        ref_id = project.referee.id if project.referee else None
        # A referee in a FREE-FOR-ALL realm must see everything, because nobody @mentions it and it
        # would otherwise never receive the debate/pitches/bids it exists to score. A referee in a
        # TURNS realm must NOT: the TurnManager already hands it the round transcript in its cue, so
        # lifting the gate only means it wakes on every single message, replies to each, and hammers
        # the proxy into rate-limiting (rps-1: Themis posted "⚡ Interrupting current task" and
        # duplicate round resolutions until the provider started refusing calls).
        ref_sees_all = project.referee is not None and project.spec.turns is None
        creds: dict[str, MatrixCreds] = {}
        for aid, (mxid, token) in users.items():
            peers = [u for u in all_ids if u != mxid]
            gated = require_mention and not (aid == ref_id and ref_sees_all)
            creds[aid] = MatrixCreds(
                homeserver=self._homeserver,
                user_id=mxid,
                access_token=token,
                allowed_users=[system_id, operator_id, *peers],
                commons_room=commons,
                require_mention=gated,
            )

        # Turns physics: mute the room from the start — only the referee + system may post until
        # the TurnManager grants the first floor. Absent `turns` = no gate (always-on default).
        referee = project.referee
        if project.spec.turns is not None:
            ref_mxid = users[referee.id][0] if referee is not None else None
            participants = [m for a, (m, _) in users.items()
                            if referee is None or a != referee.id]
            await self.grant_floor(commons, None, participants, ref_mxid)

        # Private DM rooms: platform-brokered (system creates + is a member, so they mirror). One
        # room per allowed pair, per each agent's private_messaging permission.
        side_channels = await self._provision_dm_rooms(project, users)
        return BusProvision(commons_room=commons, creds=creds, side_channels=side_channels)

    async def _provision_dm_rooms(
        self, project: Project, users: dict[str, tuple[str, str]]
    ) -> dict[str, dict[str, object]]:
        """Create a system-owned DM room for every pair the roster's private_messaging permits.
        A pair {A,B} gets a room if A may DM B (or vice-versa); the referee only when the initiator
        opted `include_referee`."""
        assert self._system_token is not None  # provision_bus checks this before calling
        ref_id = project.referee.id if project.referee else None
        by_id = {a.id: a for a in project.agents}
        pairs: set[frozenset[str]] = set()
        for a in project.agents:
            pm = a.private_messaging
            if not pm.enabled:
                continue
            # `peers` restricts WHO this agent may reach — the mechanism a hidden faction needs (the
            # impostors' room in among-us). Without it the permission is all-or-nothing and a
            # conspirator would also get a private line to every outsider. The referee stays gated
            # on include_referee either way, so a whisper channel is opt-in, not a side effect.
            allowed = set(pm.peers) if pm.peers else set(by_id)
            for other_id in by_id:
                if other_id == a.id:
                    continue
                if other_id == ref_id:
                    if not pm.include_referee:
                        continue
                elif other_id not in allowed:
                    continue
                pairs.add(frozenset({a.id, other_id}))
        channels: dict[str, dict[str, object]] = {}
        for pair in sorted(sorted(p) for p in pairs):  # deterministic order for tests
            id_a, id_b = pair
            ma, mb = users[id_a][0], users[id_b][0]
            room = await self._c.create_room(
                self._system_token, name=f"DM: {id_a} · {id_b}", invite=[ma, mb]
            )
            # POC #33: a room existing isn't enough — ping the members so their agents enter it.
            await self._c.send(
                self._system_token, room,
                f"🔒 This is the PRIVATE channel between {id_a} and {id_b} — only you two (and the "
                "operator) see it. To coordinate privately, reply RIGHT HERE in this room rather "
                "than in the Commons.",
                mentions=[ma, mb],
            )
            channels[room] = {"members": [ma, mb], "label": f"{id_a} · {id_b}"}
        return channels

    def _floor_users(
        self, participants: list[str], referee: str | None, floor: str | None
    ) -> dict[str, int]:
        # The room creator (system) is NEVER listed: in Matrix room v11+ (Conduit uses v12) the
        # creator has implicit infinite power and listing it is rejected ("Event is not
        # authorized"). So system can always post + change the floor; participants are gated here.
        levels: dict[str, int] = {}
        for p in participants:
            levels[p] = FLOOR_LEVEL if p == floor else 0
        if referee is not None:
            levels[referee] = FLOOR_LEVEL  # the referee is outside the rotation — always speaks
        return levels

    async def grant_floor(
        self, room_id: str, floor: str | None, participants: list[str], referee: str | None
    ) -> None:
        """Turns physics: only `floor` (+ referee + system) may post. `floor=None` mutes everyone
        but the referee/system (used before the first turn)."""
        if self._system_token is None:
            raise RuntimeError("system account not initialised")
        users = self._floor_users(participants, referee, floor)
        await self._c.set_power_levels(
            self._system_token, room_id, users, events_default=FLOOR_LEVEL
        )

    async def open_floor(self, room_id: str, participants: list[str], referee: str | None) -> None:
        """Lift the turn gate — everyone may post again (used on conclude so the wrap-up isn't
        strangled)."""
        if self._system_token is None:
            raise RuntimeError("system account not initialised")
        # events_default=0 + no explicit gates = everyone may post again (system stays implicit).
        await self._c.set_power_levels(self._system_token, room_id, {}, events_default=0)

    async def announce(
        self, room_id: str, body: str, mentions: list[str] | None = None
    ) -> str:
        """Post a system announcement/injection (e.g. REALM_ENDING, penalties). Pass `mentions`
        (mxids) to actually address agents — a plain-text ping won't reach them."""
        if self._system_token is None:
            raise RuntimeError("system account not initialised")
        return await self._c.send(self._system_token, room_id, body, mentions=mentions)

    async def post_as(
        self, token: str, room_id: str, body: str, mentions: list[str] | None = None
    ) -> str:
        """Post `body` into `room_id` as the account owning `token` (not the system). The host uses
        this to deliver an agent's queued `send_private` message into the peer's DM room AS the
        sender, so the mirror captures the real author. `mentions` (mxids) pings the peer."""
        return await self._c.send(token, room_id, body, mentions=mentions)

    async def wait_for_agents(
        self,
        bus: BusProvision,
        *,
        timeout_s: float = 90.0,
        interval_s: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> bool:
        """Poll the commons until every agent has JOINED (Hermes takes ~20-30s to boot + sync;
        a kickoff sent before they're listening is missed — verified live). Returns True if all
        joined before the timeout, else False (caller kicks off anyway, best-effort)."""
        if self._system_token is None:
            raise RuntimeError("system account not initialised")
        want = {c.user_id for c in bus.creds.values()}
        deadline = clock() + timeout_s
        while True:
            members = set(await self._c.room_members(self._system_token, bus.commons_room))
            if want <= members:
                return True
            if clock() >= deadline:
                return False
            await sleep(interval_s)

    async def kickoff(self, bus: BusProvision, project: Project, *, broadcast: bool = True) -> str:
        """Start the realm: post the goal + guidelines so agents engage (they join but sit idle
        until addressed — the POC's kickoff.sh role). With `broadcast=True` (default) it @mentions
        every agent; with turns enabled the caller passes `broadcast=False` so agents aren't all
        prompted at once (the TurnManager addresses the first floor-holder instead)."""
        parts = [f"The realm '{project.metadata.name}' is now open."]
        if project.spec.goals:
            parts.append("Goal: " + " ".join(project.spec.goals))
        if project.spec.guidelines:
            parts.append("Guidelines: " + project.spec.guidelines)
        referee = project.referee
        if project.spec.turns is not None:
            # The SYSTEM runs the turns, not the referee — say so, or the referee thinks it must
            # drive (and posts a redundant "opening" that collides with the first turn-grant).
            parts.append(
                "This realm takes turns: the SYSTEM gives each participant the floor one at a "
                "time and blocks out-of-turn posts — post only when the system says it is your "
                "turn."
            )
            if referee is not None and project.spec.referee_opens:
                # a game-master referee opens the game + judges each round; it never runs the turns
                parts.append(
                    f"{bus.creds[referee.id].user_id} — you host this realm. Post your opening now "
                    "per your rubric, then judge each round when the system cues you. Do NOT run "
                    "the turns or announce whose turn it is; the system does that."
                )
            elif referee is not None:
                parts.append(
                    f"{bus.creds[referee.id].user_id} is the referee — adjudicate per your "
                    "rubric. Do NOT run the turns or announce whose turn it is; the system does "
                    "that. Just watch and judge."
                )
        elif referee is not None and project.spec.referee_opens:
            # A game-master referee DRIVES the realm — prompt only it to begin, and don't address
            # the players (else they all intro at once and bury the host's opening in acks). They
            # wait for the host's first phase cue.
            parts.append(
                f"{bus.creds[referee.id].user_id} — you run this realm. Begin NOW per your rubric: "
                "post the opening phase yourself and drive it. The players wait on your cue; "
                "nothing else will prompt you, so do not wait."
            )
        elif referee is not None:
            parts.append(
                f"{bus.creds[referee.id].user_id} is the referee — adjudicate per your rubric."
            )
        parts.append("Begin.")
        if referee is not None and project.spec.referee_opens:
            mentions: list[str] | None = [bus.creds[referee.id].user_id]
        else:
            mentions = [c.user_id for c in bus.creds.values()] if broadcast else None
        return await self.announce(bus.commons_room, " ".join(parts), mentions=mentions)

    async def nudge(
        self, bus: BusProvision, message: str | None = None, *, mentions: list[str] | None = None
    ) -> str:
        """Re-address the agents when a realm stalls (mini-model coordination sometimes gets
        stuck 'waiting on' each other). A light poke, not a full re-kickoff. `mentions` targets
        specific agents (e.g. just the driving referee); default pokes everyone."""
        text = message or (
            "Reminder: keep the task moving. If you are waiting on someone, @mention them "
            "directly and state exactly what you need. If you are done, say so."
        )
        who = mentions if mentions is not None else [c.user_id for c in bus.creds.values()]
        return await self.announce(bus.commons_room, text, mentions=who)

    async def open_channel(
        self, name: str, members: list[str], *, opening_message: str | None = None
    ) -> str:
        """Create a private side-channel among `members` and (crucially) address them in it.

        POC finding #33: agents do NOT spontaneously enter side-channels — a room existing is
        not enough. So the platform posts an opening message that pings the members, pulling
        them in. Returns the room id."""
        if self._system_token is None:
            raise RuntimeError("system account not initialised")
        room = await self._c.create_room(self._system_token, name=name, invite=list(members))
        if opening_message:
            await self._c.send(self._system_token, room, opening_message, mentions=list(members))
        return room

    async def mirror(self, realm_id: str, room_id: str, chronicle: Chronicle) -> int:
        """Read a room's messages into the Chronicle, newest-first pagination flattened to
        chronological. Returns the count mirrored. EVERY message is recorded — the Chronicle is
        the source of truth (locked principle "everything is chronicled"), and agent speed is a
        legitimate advantage, so the mirror never drops. (A per-project message-rate limit, if a
        scenario wants one, belongs at the bus boundary, not here — it must not lose the record.)"""
        if self._system_token is None:
            raise RuntimeError("system account not initialised")
        events = await self._c.messages(self._system_token, room_id)
        events = sorted(
            (e for e in events if e.get("type") == "m.room.message"),
            key=lambda e: e.get("origin_server_ts", 0),
        )
        seen = self._mirrored.setdefault(room_id, set())
        mirrored = 0
        for e in events:
            event_id = str(e.get("event_id", ""))
            if event_id in seen:
                continue  # already chronicled — mirror is polled repeatedly; dedup by event id
            seen.add(event_id)
            ts = int(e.get("origin_server_ts", 0))
            sender = str(e.get("sender", ""))
            body = str(e.get("content", {}).get("body", ""))
            await chronicle.record_message(realm_id, room_id, sender, body, ts_ms=ts)
            mirrored += 1
        return mirrored
