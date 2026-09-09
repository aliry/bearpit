"""send_private: the PrivateMessageService (records the intent) + the host delivery loop that
turns those PRIVATE events into real DM-room messages posted AS the sender."""

import pytest

from bearpit.chronicle import Chronicle, EventKind
from bearpit.herald import Herald
from bearpit.herald.types import MatrixCreds
from bearpit.realmtools import Identity, PrivateMessageService

A = Identity("g1", "alice", False)
B = Identity("g1", "bob", False)


@pytest.fixture
async def svc():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    yield PrivateMessageService(c), c
    await c.close()


# --- the service: an agent's send_private records a PRIVATE event, unspoofably from itself -------
async def test_send_records_a_private_event(svc):
    s, chron = svc
    out = await s.send(A, "bob", "meet me at the bridge")
    assert "bob" in out
    evs = await chron.events("g1", kind=EventKind.PRIVATE)
    assert len(evs) == 1
    p = evs[0].payload
    assert p == {"from": "alice", "to": "bob", "text": "meet me at the bridge"}


async def test_recipient_is_normalized(svc):
    # agents may address a peer as "Bob" or "@Bob" — normalize to the bare id the host routes on
    s, chron = svc
    await s.send(A, " Bob ", "hi")
    assert (await chron.events("g1", kind=EventKind.PRIVATE))[0].payload["to"] == "bob"


async def test_from_is_the_caller_not_an_argument(svc):
    # identity comes from the verified token — an agent can only send AS itself
    s, chron = svc
    await s.send(B, "alice", "hello")
    assert (await chron.events("g1", kind=EventKind.PRIVATE))[0].payload["from"] == "bob"


@pytest.mark.parametrize("to,msg,match", [
    ("", "hi", "recipient"),
    ("   ", "hi", "recipient"),
    ("bob", "  ", "non-empty"),
    ("bob", "", "non-empty"),
])
async def test_send_validates(svc, to, msg, match):
    s, _ = svc
    with pytest.raises(ValueError, match=match):
        await s.send(A, to, msg)


async def test_cannot_message_yourself(svc):
    s, _ = svc
    with pytest.raises(ValueError, match="yourself"):
        await s.send(A, "alice", "note to self")


# --- the host delivery loop: PRIVATE events -> DM-room messages, posted as the sender ------------
class FakeMatrix:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, tuple]] = []  # (token, room, body, mentions)

    async def register_or_login(self, username, password):
        return f"tok-{username}"

    async def send(self, token, room_id, body, msgtype="m.text", mentions=None):
        self.sent.append((token, room_id, body, tuple(mentions or ())))
        return f"$ev{len(self.sent)}"

    async def messages(self, token, room_id, limit=100):
        return []  # mirror finds nothing; we only assert on delivery here


async def _herald(mx):
    h = Herald(mx, server_name="realm.local", homeserver="h")
    await h.ensure_system("pw")  # mirror needs a system token
    return h


def _live(chron, herald, side_channels, creds):
    from bearpit.gatekeeper.runner import LiveSnapshot

    class _Ledger:
        async def poll_spend(self, realm, chron):
            return {}

    class _Runtime:
        def read_volume(self, name):
            return {}

    return LiveSnapshot(
        herald=herald, ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="g1", commons_room="!commons:realm.local", shared_volume=None,
        stop_flag=lambda: False, clock=lambda: 0.0,
        side_channels=side_channels, creds=creds,
    )


def _creds(*ids):
    return {i: MatrixCreds(
        homeserver="h", user_id=f"@g1-{i}:realm.local", access_token=f"tok-{i}",
        allowed_users=[], commons_room="!commons:realm.local", require_mention=True,
    ) for i in ids}


async def test_host_delivers_private_message_into_dm_room_as_sender():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix()
    herald = await _herald(mx)
    side = {"!dm:realm.local": {"members": ["@g1-alice:realm.local", "@g1-bob:realm.local"],
                                "label": "alice · bob"}}
    live = _live(chron, herald, side, _creds("alice", "bob"))

    await PrivateMessageService(chron).send(A, "bob", "psst — vote for me")
    await live()

    assert len(mx.sent) == 1
    token, room, body, mentions = mx.sent[0]
    assert token == "tok-alice"  # posted AS the sender, not the system
    assert room == "!dm:realm.local"
    assert body == "psst — vote for me"
    assert mentions == ("@g1-bob:realm.local",)  # recipient is @mentioned so it reaches them
    await chron.close()


async def test_host_does_not_redeliver_on_later_ticks():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix()
    herald = await _herald(mx)
    side = {"!dm:realm.local": {"members": ["@g1-alice:realm.local", "@g1-bob:realm.local"],
                                "label": "alice · bob"}}
    live = _live(chron, herald, side, _creds("alice", "bob"))

    await PrivateMessageService(chron).send(A, "bob", "one")
    await live()
    await live()  # second tick: the already-delivered event must not be posted again
    assert len(mx.sent) == 1

    await PrivateMessageService(chron).send(B, "alice", "two")  # a new one delivers
    await live()
    assert len(mx.sent) == 2
    assert mx.sent[1][0] == "tok-bob"  # bob's reply goes out as bob
    await chron.close()


async def test_host_drops_private_message_with_no_channel_for_the_pair():
    # permission gate is host-side: no DM room for (alice,carol) -> the message is silently dropped
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix()
    herald = await _herald(mx)
    side = {"!dm:realm.local": {"members": ["@g1-alice:realm.local", "@g1-bob:realm.local"],
                                "label": "alice · bob"}}
    live = _live(chron, herald, side, _creds("alice", "bob", "carol"))

    await PrivateMessageService(chron).send(A, "carol", "can't reach you")
    await live()
    assert mx.sent == []
    await chron.close()


async def test_eliminated_agent_is_stopped_and_its_dm_channel_goes_dead_both_ways():
    """An ejected agent must FULLY leave the realm: its container is stopped and its private
    channel is dead in both directions. Regression for among-us-cb70f7, where an impostor ejected
    in R3 kept conferring with its partner in the side-channel (the turn mute never touched it)."""
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix()
    herald = await _herald(mx)
    side = {"!dm:realm.local": {"members": ["@g1-alice:realm.local", "@g1-bob:realm.local"],
                                "label": "alice · bob"}}

    stopped: list[str] = []

    class _Ledger:
        async def poll_spend(self, realm, chron):
            return {}

    class _Runtime:
        def read_volume(self, name):
            return {}

        def stop_container(self, container_id, *, timeout):
            stopped.append(container_id)

    from bearpit.gatekeeper.runner import LiveSnapshot
    live = LiveSnapshot(
        herald=herald, ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="g1", commons_room="!commons:realm.local", shared_volume=None,
        stop_flag=lambda: False, clock=lambda: 0.0,
        side_channels=side, creds=_creds("alice", "bob"),
        containers={"alice": "cA", "bob": "cB"},
    )

    # tick 1: bob is eliminated -> its container is stopped, once
    await chron.append_event("g1", EventKind.ELIMINATION, {"agent": "bob", "reason": "ejected"})
    await live()
    assert stopped == ["cB"]
    sys_events = await chron.events("g1", kind=EventKind.SYSTEM)
    assert any(e.payload.get("event") == "agent_stopped" and e.payload.get("agent") == "bob"
               for e in sys_events)

    # a second tick must not stop it again (each elimination enforced exactly once)
    await live()
    assert stopped == ["cB"]

    # tick: neither a DM FROM the eliminated bob nor one TO bob is delivered
    await PrivateMessageService(chron).send(B, "alice", "still here, partner?")  # from eliminated
    await PrivateMessageService(chron).send(A, "bob", "you there?")              # to eliminated
    await live()
    bodies = [body for _, _, body, _ in mx.sent]
    assert "still here, partner?" not in bodies
    assert "you there?" not in bodies
    assert any("left the realm" in b for b in bodies)  # alice is told once that bob is gone
    await chron.close()


class QuotaMatrix(FakeMatrix):
    """A DM room the agent posts into DIRECTLY, plus a record of every power-level change."""

    def __init__(self, room_events=None):
        super().__init__()
        self.room_events = room_events or {}
        self.power: list[tuple[str, dict, int]] = []  # (room, users, events_default)

    async def messages(self, token, room_id, limit=100):
        return self.room_events.get(room_id, [])

    async def set_power_levels(self, token, room_id, users, events_default):
        self.power.append((room_id, dict(users), events_default))


def _dm_event(n, sender, body):
    return {"type": "m.room.message", "event_id": f"$d{n}", "origin_server_ts": 1000 + n,
            "sender": sender, "content": {"msgtype": "m.text", "body": body}}


async def test_the_dm_quota_counts_what_an_agent_posts_in_the_room_itself():
    """The quota was enforced only on the `send_private` path. An agent is a member of its DM room
    and can post there with its own Matrix client — and the room's welcome message tells it to
    ("To coordinate privately, reply RIGHT HERE in this room"). So the limit guarded one of two
    sanctioned routes into the room it exists to protect.

    Live in camp-border-states round 1, `corvane` (quota 4): 4 delivered through the tool, 4 more
    correctly blocked with the 🔇 notice — and 6 posted straight into the DM rooms, unlimited. The
    content is exactly what the quota exists to stop: "Confirmed the pact", "Reaffirmed the pact",
    "Deal locked", "Deal reaffirmed" — one agreement restated four times. That realm spent $1.74 in
    a single round; a whole sealed-auction realm cost $0.15.

    Herald's own mirror docstring names the right boundary: "A per-project message-rate limit, if a
    scenario wants one, belongs at the bus boundary, not here — it must not lose the record." So
    the record is still chronicled in full; the agent is muted in the room by power levels, which
    a homeserver enforces against ANY client.
    """
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    room = "!dm:realm.local"
    alice = "@g1-alice:realm.local"
    mx = QuotaMatrix({room: [_dm_event(i, alice, f"reaffirming the pact #{i}") for i in range(3)]})
    herald = await _herald(mx)
    side = {room: {"members": [alice, "@g1-bob:realm.local"], "label": "alice · bob"}}
    live = _live(chron, herald, side, _creds("alice", "bob"))
    live._dm_quota = {"alice": 2}  # two per round; the agent posted three directly

    await live()

    # every message is still chronicled — the mirror must never drop the record
    assert len(await chron.messages("g1", room)) == 3
    # ...and the bus now refuses further posts from alice in that room
    mutes = [p for p in mx.power if p[0] == room and p[1].get(alice, 0) < 0]
    assert mutes, "an over-quota agent can still post directly into its DM room"
    await chron.close()


async def test_a_within_quota_agent_is_never_muted_in_its_dm_room():
    """The gate must only close on the agent that actually exceeded its allowance."""
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    room = "!dm:realm.local"
    alice = "@g1-alice:realm.local"
    mx = QuotaMatrix({room: [_dm_event(0, alice, "one quiet word")]})
    herald = await _herald(mx)
    side = {room: {"members": [alice, "@g1-bob:realm.local"], "label": "alice · bob"}}
    live = _live(chron, herald, side, _creds("alice", "bob"))
    live._dm_quota = {"alice": 4}

    await live()

    assert not [p for p in mx.power if p[1].get(alice, 0) < 0], "muted an agent inside its quota"
    await chron.close()
