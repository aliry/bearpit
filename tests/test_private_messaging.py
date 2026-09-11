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


def _live(chron, herald, side_channels, creds, machine=None):
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
        side_channels=side_channels, creds=creds, machine=machine,
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


# --- the machine's wake stamps: the host DELIVERS them, it never advances the machine -----------
WAKE_TEXT = "the machine is waiting on you. Call `game_state`."


def _machine_rec(wake, terminal=()):
    return {
        "version": 1,
        "declaration": {
            "roles": {"ref": {"members": "referee"}, "player": {"members": "participants"}},
            "states": ["a", "b"], "initial": "a", "terminal": list(terminal),
            "transitions": {"go": {"from": "a", "to": "b", "by": "ref"}}, "wake": wake,
        },
        "members": {"ref": ["ref"], "player": ["alice", "bob"]},
        "roster": ["alice", "bob"], "referee": "ref",
    }


def _wakes(mx):
    return [(room, body, mentions) for (_, room, body, mentions) in mx.sent if WAKE_TEXT in body]


def _woken(mx):
    return [m for (_, _, mentions) in _wakes(mx) for m in mentions]  # mxids, in send order


async def _game(chron, payload, ts_ms=None):
    await chron.append_event("g1", EventKind.GAME, payload, ts_ms=ts_ms)


async def test_wake_stamps_become_one_mention_per_target_and_actor_wakes_collapse():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix()
    herald = await _herald(mx)
    live = _live(chron, herald, {}, _creds("alice", "bob", "ref"),
                 machine=_machine_rec([{"role": "actor"}]))
    await _game(chron, {"op": "act", "actor": "alice", "wake": ["alice"]})
    await _game(chron, {"op": "act", "actor": "bob", "wake": ["bob"]})
    await _game(chron, {"op": "act", "actor": "bob", "wake": ["ref", "bob"]})
    await live()
    sent = _wakes(mx)
    mentioned = sorted(m for (_, _, ms) in sent for m in ms)
    # alice's actor-wake is stale — the pointer moved to bob — so only bob and ref are woken
    assert mentioned == ["@g1-bob:realm.local", "@g1-ref:realm.local"]
    await live()
    assert len(_wakes(mx)) == len(sent)  # delivered exactly once
    await chron.close()


async def test_after_s_nudges_the_role_once_per_stall_measured_from_the_last_game_event():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix()
    herald = await _herald(mx)
    t = {"now": 0.0}
    live = _live(chron, herald, {}, _creds("alice", "bob", "ref"),
                 machine=_machine_rec([{"role": "ref", "after_s": 240}]))
    live._clock = lambda: t["now"]
    await _game(chron, {"op": "act", "wake": []}, ts_ms=0)
    await live()
    assert _wakes(mx) == []
    t["now"] = 250
    await live()
    assert len(_wakes(mx)) == 1
    t["now"] = 500
    await live()
    assert len(_wakes(mx)) == 1  # not again until a new event re-arms it
    await _game(chron, {"op": "act", "wake": []}, ts_ms=500_000)
    t["now"] = 800
    await live()
    assert len(_wakes(mx)) == 2
    await chron.close()


async def test_game_events_count_as_activity_and_the_snapshot_carries_machine_state():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix()
    herald = await _herald(mx)
    t = {"now": 0.0}
    live = _live(chron, herald, {}, _creds("alice", "ref"),
                 machine=_machine_rec([], terminal=["b"]))
    live._clock = lambda: t["now"]
    snap = await live()
    assert snap.machine_state == "a"
    t["now"] = 100
    await _game(chron, {"op": "act", "to": "b", "wake": []})
    snap = await live()
    # a move IS agent activity: `stall` must not fire mid-hand
    assert snap.idle_s == 0.0
    assert snap.machine_state == "b"
    await chron.close()


async def test_snapshot_reports_machine_terminal_reached_once_the_machine_gets_there():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix()
    herald = await _herald(mx)
    live = _live(chron, herald, {}, _creds("alice", "ref"),
                 machine=_machine_rec([], terminal=["b"]))
    snap = await live()
    assert snap.machine_state == "a"
    assert snap.machine_terminal_reached is False
    await _game(chron, {"op": "act", "to": "b", "wake": []})
    snap = await live()
    assert snap.machine_state == "b"
    assert snap.machine_terminal_reached is True
    await chron.close()


async def test_after_s_escalates_rule_by_rule_and_a_new_event_re_arms_every_rule():
    """Each wake rule keeps its own fired-flag: a declaration may nudge the referee at 240s and
    escalate to the players at 600s, and one shared flag would let the first rule mute the second.
    A sibling of the test above rather than a continuation of it — the second declaration needs
    its own snapshot and chronicle."""
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    mx = FakeMatrix()
    herald = await _herald(mx)
    t = {"now": 0.0}
    live = _live(chron, herald, {}, _creds("alice", "bob", "ref"),
                 machine=_machine_rec([{"role": "ref", "after_s": 240},
                                       {"role": "player", "after_s": 600}]))
    live._clock = lambda: t["now"]
    await _game(chron, {"op": "act", "wake": []}, ts_ms=0)
    await live()
    assert _woken(mx) == []
    t["now"] = 250
    await live()
    assert _woken(mx) == ["@g1-ref:realm.local"]  # only the quick rule is due
    t["now"] = 650
    await live()
    # the slow rule escalates to the players; the quick rule does not fire a second time
    assert _woken(mx) == ["@g1-ref:realm.local", "@g1-alice:realm.local", "@g1-bob:realm.local"]
    await _game(chron, {"op": "act", "wake": []}, ts_ms=650_000)
    t["now"] = 900
    await live()
    assert _woken(mx)[3:] == ["@g1-ref:realm.local"]  # the move re-armed every rule
    await chron.close()
