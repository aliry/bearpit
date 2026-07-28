"""Herald: rate limiter + bus provisioning + mirror (with a fake Matrix client)."""

import pytest

from bearpit.chronicle import Chronicle
from bearpit.core.schema import (
    AgentRole,
    AgentSpec,
    ModelRef,
    PrivateMessaging,
    Project,
    ProjectMeta,
)
from bearpit.herald import Herald, RateLimiter, TokenBucket


# --- rate limiter -----------------------------------------------------------
def test_token_bucket_burst_then_blocks_then_refills():
    b = TokenBucket(rate=1.0, capacity=3.0)
    assert [b.allow(0.0) for _ in range(4)] == [True, True, True, False]  # burst 3, then empty
    assert b.allow(0.5) is False  # only 0.5 token refilled
    assert b.allow(1.0) is True  # a full token back after 1s


def test_rate_limiter_is_per_sender():
    rl = RateLimiter(rate=0.0, capacity=1.0)  # 1 token each, no refill
    assert rl.allow("@a", 0.0) is True and rl.allow("@a", 0.0) is False
    assert rl.allow("@b", 0.0) is True  # @b has its own bucket


# --- bus provisioning -------------------------------------------------------
class FakeMatrix:
    def __init__(self) -> None:
        self.users: dict[str, str] = {}  # username -> token
        self.rooms: dict[str, dict] = {}
        self.sent: list[tuple[str, str]] = []  # (room, body)
        self._room_events: dict[str, list] = {}
        self._n = 0

    async def register_or_login(self, username, password):
        self._n += 1
        tok = self.users.setdefault(username, f"tok-{username}")
        return tok

    async def create_room(self, token, name, invite):
        rid = f"!room{len(self.rooms)}:realm.local"
        self.rooms[rid] = {"name": name, "invite": list(invite), "creator": token}
        return rid

    async def invite(self, token, room_id, user_id):
        self.rooms[room_id]["invite"].append(user_id)

    async def join(self, token, room_id):
        self.rooms.setdefault(room_id, {"invite": []})
        self.rooms[room_id].setdefault("joined", []).append(token)

    async def room_members(self, token, room_id):
        # treat everyone invited as joined (enough to test the readiness gate)
        return list(self.rooms.get(room_id, {}).get("invite", []))

    async def send(self, token, room_id, body, msgtype="m.text", mentions=None):
        self.sent.append((room_id, body))
        self.mentions = getattr(self, "mentions", [])
        self.mentions.append(mentions or [])
        return f"$ev{len(self.sent)}"

    async def messages(self, token, room_id, limit=100):
        return self._room_events.get(room_id, [])

    async def set_power_levels(self, token, room_id, users, events_default):
        self.power_calls = getattr(self, "power_calls", [])
        self.power_calls.append({"room": room_id, "users": dict(users),
                                 "events_default": events_default})

    def seed_events(self, room_id, events):
        self._room_events[room_id] = events


def _project():
    def agent(aid):
        model = ModelRef(provider="azure", model="m", api_key_ref="azure-main")
        return AgentSpec(id=aid, model=model)
    return Project(metadata=ProjectMeta(name="duel"), agents=[agent("vela"), agent("orin")])


async def test_provision_bus_registers_system_first_and_scopes_users():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="http://conduit:6167")
    await herald.ensure_system("syspw")
    assert "system" in mx.users  # system registered before any agent

    bus = await herald.provision_bus("duel1", _project())
    # realm-scoped usernames (no cross-realm collision), and MXIDs reflect them
    assert "duel1-vela" in mx.users and "duel1-orin" in mx.users
    vela = bus.creds["vela"]
    assert vela.user_id == "@duel1-vela:realm.local"
    assert vela.commons_room == bus.commons_room
    # allowlist = system + operator + the peer (never itself)
    assert "@system:realm.local" in vela.allowed_users
    assert "@operator:realm.local" in vela.allowed_users
    assert "@duel1-orin:realm.local" in vela.allowed_users
    assert "@duel1-vela:realm.local" not in vela.allowed_users
    # commons room was created by the system account and everyone invited
    room = mx.rooms[bus.commons_room]
    assert set(room["invite"]) == {"@duel1-vela:realm.local", "@duel1-orin:realm.local"}


def _pm_agent(aid, pm=None, role=AgentRole.PARTICIPANT):
    return AgentSpec(id=aid, role=role,
                     model=ModelRef(provider="azure", model="m", api_key_ref="azure-main"),
                     private_messaging=pm or PrivateMessaging())


async def _provision(project, realm="g1"):
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    await herald.ensure_system("pw")
    return mx, await herald.provision_bus(realm, project)


async def test_private_messaging_pre_creates_brokered_dm_rooms():
    # 'a' may DM peers (not the referee); a<->b room exists, no a<->referee room. System creates it.
    proj = Project(metadata=ProjectMeta(name="g"), agents=[
        _pm_agent("a", PrivateMessaging(enabled=True)), _pm_agent("b"),
        _pm_agent("host", role=AgentRole.REFEREE)])
    mx, bus = await _provision(proj)
    assert {c["label"] for c in bus.side_channels.values()} == {"a · b"}
    room = next(iter(bus.side_channels))
    assert set(mx.rooms[room]["invite"]) == {"@g1-a:realm.local", "@g1-b:realm.local"}
    assert any("PRIVATE channel" in body for r, body in mx.sent if r == room)  # opening ping


async def test_include_referee_adds_a_referee_dm():
    proj = Project(metadata=ProjectMeta(name="g"), agents=[
        _pm_agent("a", PrivateMessaging(enabled=True, include_referee=True)),
        _pm_agent("host", role=AgentRole.REFEREE)])
    _, bus = await _provision(proj, "g2")
    assert {c["label"] for c in bus.side_channels.values()} == {"a · host"}


async def test_no_dm_rooms_without_permission():
    _, bus = await _provision(_project(), "g3")  # default agents, private_messaging off
    assert bus.side_channels == {}


def test_include_referee_requires_enabled():
    with pytest.raises(ValueError, match="include_referee requires enabled"):
        PrivateMessaging(include_referee=True)


async def test_provision_bus_requires_system_and_valid_realm_id():
    herald = Herald(FakeMatrix(), server_name="realm.local", homeserver="h")
    with pytest.raises(RuntimeError):
        await herald.provision_bus("r1", _project())  # ensure_system not called
    await herald.ensure_system("pw")
    with pytest.raises(ValueError):
        await herald.provision_bus("Bad_Realm", _project())  # invalid realm id


async def test_open_side_channel_pings_members():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    await herald.ensure_system("pw")
    members = ["@duel1-vela:realm.local", "@duel1-themis:realm.local"]
    room = await herald.open_channel(
        "Vela ↔ Themis", members,
        opening_message="@duel1-vela please submit your move privately here.",
    )
    # the room exists with exactly the two members invited...
    assert set(mx.rooms[room]["invite"]) == set(members)
    # ...and the platform ADDRESSED them there (the #33 fix): an opening message was posted
    assert any("submit your move privately" in body for _, body in mx.sent)


async def test_kickoff_addresses_every_agent():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    await herald.ensure_system("pw")
    project = _project()
    bus = await herald.provision_bus("duel1", project)
    await herald.kickoff(bus, project)
    # the kickoff message mentions every agent (else they'd sit idle — proven live)
    last_mentions = mx.mentions[-1]
    assert set(last_mentions) == {"@duel1-vela:realm.local", "@duel1-orin:realm.local"}
    assert any("is now open" in body for _, body in mx.sent)


async def test_referee_opens_prompts_only_the_referee():
    from bearpit.core.schema import AgentRole, ProjectSpec
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    await herald.ensure_system("pw")

    def agent(aid, role=AgentRole.PARTICIPANT):
        return AgentSpec(id=aid, role=role,
                         model=ModelRef(provider="azure", model="m", api_key_ref="azure-main"))
    project = Project(metadata=ProjectMeta(name="g"),
                      agents=[agent("host", AgentRole.REFEREE), agent("p1"), agent("p2")],
                      spec=ProjectSpec(referee_opens=True))
    bus = await herald.provision_bus("game1", project)
    await herald.kickoff(bus, project)
    # only the host is prompted to begin; players are NOT mentioned (they wait for its cue)
    assert mx.mentions[-1] == ["@game1-host:realm.local"]
    assert any("you run this realm" in body for _, body in mx.sent)


async def test_nudge_re_addresses_all_agents():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    await herald.ensure_system("pw")
    bus = await herald.provision_bus("duel1", _project())
    await herald.nudge(bus)
    assert set(mx.mentions[-1]) == {"@duel1-vela:realm.local", "@duel1-orin:realm.local"}
    assert any("keep the task moving" in body for _, body in mx.sent)


async def test_nudge_can_target_specific_agents():
    # a game-master realm pokes ONLY the driving referee, not the whole roster (avoids ack storms)
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    await herald.ensure_system("pw")
    bus = await herald.provision_bus("duel1", _project())
    await herald.nudge(bus, "continue driving", mentions=["@duel1-vela:realm.local"])
    assert mx.mentions[-1] == ["@duel1-vela:realm.local"]
    assert any("continue driving" in body for _, body in mx.sent)


async def test_mirror_records_every_message():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    await herald.ensure_system("pw")
    room = "!c:realm.local"
    # 5 rapid messages from one sender — ALL are chronicled (source of truth; no drops)
    mx.seed_events(room, [
        {"type": "m.room.message", "sender": "@spammer:realm.local", "event_id": f"$e{i}",
         "origin_server_ts": 1000, "content": {"body": f"msg{i}"}}
        for i in range(5)
    ])
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    n = await herald.mirror("r", room, chron)
    assert n == 5 and len(await chron.messages("r")) == 5  # nothing lost from the Chronicle
    await chron.close()


async def test_mirror_dedups_across_polls():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    await herald.ensure_system("pw")
    room = "!c:realm.local"
    mx.seed_events(room, [
        {"type": "m.room.message", "sender": "@vela:realm.local", "event_id": "$e1",
         "origin_server_ts": 1000, "content": {"body": "hello"}},
    ])
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    assert await herald.mirror("r", room, chron) == 1  # first poll records it
    assert await herald.mirror("r", room, chron) == 0  # second poll: already seen, no dup
    assert await herald.mirror("r", room, chron) == 0
    assert len(await chron.messages("r")) == 1  # exactly one row, not three
    await chron.close()


async def test_wait_for_agents_gates_on_membership():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    await herald.ensure_system("pw")
    bus = await herald.provision_bus("duel1", _project())

    async def _nosleep(_s):
        return None

    # everyone is 'in' (fake returns invited as members) -> ready immediately
    t = [0.0]
    ready = await herald.wait_for_agents(bus, sleep=_nosleep, clock=lambda: t[0])
    assert ready is True

    # if the room reports no members, it times out and returns False (caller kicks off anyway)
    mx.rooms[bus.commons_room]["invite"] = []
    ticks = iter([0.0, 0.0, 5.0, 10.0, 100.0])
    ready = await herald.wait_for_agents(
        bus, timeout_s=30, sleep=_nosleep, clock=lambda: next(ticks)
    )
    assert ready is False


# --- turns physics ----------------------------------------------------------
def _project_with_turns():
    from bearpit.core.schema import AgentRole, Turns

    def agent(aid, role=AgentRole.PARTICIPANT):
        model = ModelRef(provider="azure", model="m", api_key_ref="azure-main")
        kw = {"rubric": "judge fairly"} if role == AgentRole.REFEREE else {}
        return AgentSpec(id=aid, model=model, role=role, **kw)

    return Project(
        metadata=ProjectMeta(name="debate"),
        spec={"turns": Turns()},
        agents=[agent("pro"), agent("con"), agent("judge", AgentRole.REFEREE)],
    )


async def test_provision_bus_with_turns_mutes_room_from_start():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="http://conduit:6167")
    await herald.ensure_system("syspw")
    await herald.provision_bus("deb1", _project_with_turns())
    # a power-level state event was set: only the referee (+ implicit system) can post, no floor
    pl = mx.power_calls[-1]
    assert pl["events_default"] == 50
    users = pl["users"]
    assert "@system:realm.local" not in users  # creator is implicit infinite power in room v11+
    assert users["@deb1-pro:realm.local"] == 0 and users["@deb1-con:realm.local"] == 0
    assert users["@deb1-judge:realm.local"] == 50  # referee always speaks


async def test_grant_and_open_floor():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="http://conduit:6167")
    await herald.ensure_system("syspw")
    parts = ["@r-pro:realm.local", "@r-con:realm.local"]
    await herald.grant_floor("!c", "@r-pro:realm.local", parts, "@r-judge:realm.local")
    pl = mx.power_calls[-1]
    assert pl["events_default"] == 50
    assert pl["users"]["@r-pro:realm.local"] == 50  # floor-holder can post
    assert pl["users"]["@r-con:realm.local"] == 0   # the other participant is muted
    assert pl["users"]["@r-judge:realm.local"] == 50
    await herald.open_floor("!c", parts, "@r-judge:realm.local")
    assert mx.power_calls[-1]["events_default"] == 0  # gate lifted — everyone can post


async def test_kickoff_broadcast_false_omits_broad_mention():
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="http://conduit:6167")
    await herald.ensure_system("syspw")
    bus = await herald.provision_bus("deb2", _project_with_turns())
    await herald.kickoff(bus, _project_with_turns(), broadcast=False)
    assert mx.mentions[-1] == []  # no broad @mention when turns run the addressing


def _faction_project():
    """Two impostors who may DM only each other; two crew with no private messaging; a referee who
    may whisper every player."""
    from bearpit.core.schema import AgentRole

    def agent(aid, pm=None, role=AgentRole.PARTICIPANT):
        model = ModelRef(provider="azure", model="m", api_key_ref="azure-main")
        kw = {"rubric": "run the game"} if role == AgentRole.REFEREE else {}
        if pm is not None:
            kw["private_messaging"] = pm
        return AgentSpec(id=aid, model=model, role=role, **kw)

    return Project(
        metadata=ProjectMeta(name="among"),
        agents=[
            agent("cass", {"enabled": True, "peers": ["vega"]}),
            agent("vega", {"enabled": True, "peers": ["cass"]}),
            agent("juno"),
            agent("rhea"),
            agent("mother", {"enabled": True}, AgentRole.REFEREE),
        ],
    )


async def _channels(project):
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="http://conduit:6167")
    await herald.ensure_system("syspw")
    bus = await herald.provision_bus("r1", project)
    return {tuple(sorted(str(c["label"]).split(" \u00b7 "))) for c in bus.side_channels.values()}


async def test_peers_confines_a_faction_to_its_own_private_room():
    # The impostors share a room and get NO private line to any crewmate. Before `peers` the
    # permission was all-or-nothing: a conspirator who could DM anyone could DM everyone, so a
    # hidden faction could not exist without also handing the crew it deceives a private channel
    # to a conspirator.
    channels = await _channels(_faction_project())
    assert ("cass", "vega") in channels
    for crew in ("juno", "rhea"):
        assert not any(crew in pair and ("cass" in pair or "vega" in pair) for pair in channels)


async def test_a_referee_that_opts_in_gets_a_whisper_room_with_every_player():
    # The game-master's whisper channel: one 1:1 room per player, so Mother can tell each agent
    # only what THAT agent perceives. This is what makes public speech an unverifiable claim.
    channels = await _channels(_faction_project())
    assert {p for p in channels if "mother" in p} == {
        ("cass", "mother"), ("juno", "mother"), ("mother", "rhea"), ("mother", "vega"),
    }


async def test_the_referee_always_receives_the_commons_even_when_mention_gated():
    """`require_mention` means "you only receive messages that @mention you". That is right for a
    participant (it stops reply-loops) and catastrophic for a REFEREE: in a realm without turns
    nobody @mentions the judge, so it never receives the debate, the pitches or the bids it
    exists to score — and it either invents a verdict or never rules. A turns realm hides this
    (the TurnManager hands the referee the round transcript in its cue); a free-for-all realm
    does not."""
    from bearpit.core.schema import AgentRole

    def agent(aid, role=AgentRole.PARTICIPANT):
        model = ModelRef(provider="azure", model="m", api_key_ref="azure-main")
        kw = {"rubric": "score them"} if role == AgentRole.REFEREE else {}
        return AgentSpec(id=aid, model=model, role=role, **kw)

    project = Project(
        metadata=ProjectMeta(name="debate"),
        agents=[agent("pro"), agent("con"), agent("judge", AgentRole.REFEREE)],
    )
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="http://conduit:6167")
    await herald.ensure_system("syspw")
    bus = await herald.provision_bus("r1", project, require_mention=True)

    assert bus.creds["pro"].require_mention is True   # participants stay mention-gated
    assert bus.creds["con"].require_mention is True
    assert bus.creds["judge"].require_mention is False  # the judge must SEE the debate


async def test_a_referee_in_a_TURNS_realm_stays_mention_gated():
    """The exemption is only for free-for-all realms. In a turns realm the TurnManager already hands
    the referee the round transcript in its cue, so lifting the gate buys nothing — and costs a lot:
    the referee then wakes on EVERY message, replies to each, and hammers the model proxy until it
    rate-limits (rps-1: Themis posted "⚡ Interrupting current task" and duplicate round resolutions
    until the provider began refusing calls)."""
    from bearpit.core.schema import AgentRole, Turns

    def agent(aid, role=AgentRole.PARTICIPANT):
        model = ModelRef(provider="azure", model="m", api_key_ref="azure-main")
        kw = {"rubric": "score them"} if role == AgentRole.REFEREE else {}
        return AgentSpec(id=aid, model=model, role=role, **kw)

    project = Project(
        metadata=ProjectMeta(name="duel"),
        spec={"turns": Turns()},
        agents=[agent("orin"), agent("vela"), agent("themis", AgentRole.REFEREE)],
    )
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="http://conduit:6167")
    await herald.ensure_system("syspw")
    bus = await herald.provision_bus("r1", project, require_mention=True)
    assert bus.creds["themis"].require_mention is True
