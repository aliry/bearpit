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
