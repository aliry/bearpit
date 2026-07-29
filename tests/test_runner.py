"""Runner end-to-end flow with fake IO components + real Warden/Ledger."""

from datetime import timedelta

from bearpit.chronicle import Chronicle, EventKind
from bearpit.core.schema import (
    AgentSpec,
    ModelRef,
    Project,
    ProjectMeta,
    ProjectSpec,
    TerminationCondition,
    TerminationKind,
)
from bearpit.forge import Forge
from bearpit.gatekeeper.runner import Runner
from bearpit.herald import Herald
from bearpit.ledger import KeyStore, Ledger
from bearpit.warden import RealmSnapshot, Warden, evaluate_termination


class FakeMatrix:
    def __init__(self):
        self.users = {}
        self.rooms = {}
        self.sent = []
        self._n = 0

    async def register_or_login(self, username, password):
        return self.users.setdefault(username, f"tok-{username}")

    async def create_room(self, token, name, invite):
        rid = f"!room{len(self.rooms)}:realm.local"
        self.rooms[rid] = {"invite": list(invite)}
        return rid

    async def invite(self, token, room_id, user_id):
        pass

    async def join(self, token, room_id):
        pass

    async def send(self, token, room_id, body, msgtype="m.text", mentions=None):
        self.sent.append((room_id, body))
        self.mentions = getattr(self, "mentions", [])
        self.mentions.append(mentions)
        return "$e"

    async def messages(self, token, room_id, limit=100):
        return []

    async def room_members(self, token, room_id):
        return list(self.rooms.get(room_id, {}).get("invite", []))

    async def set_power_levels(self, token, room_id, users, events_default):
        self.power = getattr(self, "power", [])
        self.power.append({"users": dict(users), "events_default": events_default})


class FakeRuntime:
    def __init__(self):
        self.containers = {}
        self.volumes = {}
        self.networks = {}
        self.removed = []
        self._n = 0

    def create_network(self, name, *, internal):
        self.networks[name] = internal
        return name

    def connect_network(self, network, container):
        self.connected = getattr(self, 'connected', [])
        self.connected.append((network, container))

    def remove_network(self, name):
        self.removed.append(name)

    def create_volume(self, name):
        self.volumes[name] = {}
        return name

    def seed_volume(self, name, files):
        self.volumes[name] = dict(files)

    def read_volume(self, name):
        return {}

    def remove_volume(self, name):
        self.volumes.pop(name, None)

    def run_container(self, *, name, image, network, volumes, environment, command,
                      mem_limit=None, pids_limit=None, nano_cpus=None):
        self._n += 1
        cid = f"c{self._n}"
        self.containers[cid] = {"running": True}
        return cid

    def stop_container(self, container_id, *, timeout):
        self.containers[container_id]["running"] = False

    def remove_container(self, container_id):
        self.containers.pop(container_id, None)

    def container_logs(self, container_id, *, tail=4000):
        self.log_reads = getattr(self, "log_reads", [])
        self.log_reads.append(container_id)
        return f"fake logs for {container_id}"



class FakeLiteLLM:
    def __init__(self):
        self._n = 0

    async def register_model(self, model_name, real_model, api_key, api_base=None,
                             input_cost_per_token=None, output_cost_per_token=None,
                             reasoning_effort=None):
        pass

    async def mint_key(self, alias, models, max_budget):
        self._n += 1
        return f"vk-{self._n}"

    async def key_spend(self, virtual_key):
        return 0.0, None

    async def key_tokens(self, virtual_key):
        return (0, 0)

    async def delete_key(self, virtual_key):
        pass

    async def delete_model(self, model_name):
        self.deleted_models = getattr(self, 'deleted_models', [])
        self.deleted_models.append(model_name)


def _project():
    def agent(aid, referee=False):
        extra = {"role": "referee", "rubric": "j"} if referee else {}
        m = ModelRef(provider="azure", model="m", api_key_ref="azure-main")
        return AgentSpec(id=aid, model=m, **extra)
    return Project(
        metadata=ProjectMeta(name="duel"),
        spec=ProjectSpec(
            termination=[TerminationCondition(type="message", channel="commons", pattern="DONE")]
        ),
        agents=[agent("vela"), agent("orin"), agent("themis", referee=True)],
    )


async def test_runner_provisions_runs_watches_concludes():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    ks = KeyStore(KeyStore.generate_key())
    ks.put("azure-main", "REALKEY")
    herald = Herald(FakeMatrix(), server_name="realm.local", homeserver="http://conduit:6167")
    ledger = Ledger(ks, FakeLiteLLM(), "http://litellm:4000")
    forge = Forge(FakeRuntime(), ledger)
    warden = Warden(forge, herald, chron)
    runner = Runner(herald, forge, warden, ledger, chron)

    ticks = {"n": 0}

    def snapshot_factory(realm_id, handles, bus, turns):
        async def snap():
            ticks["n"] += 1
            # the closing message appears on the 2nd tick
            msgs = [("commons", "we are DONE")] if ticks["n"] >= 2 else []
            return RealmSnapshot(messages=msgs)
        return snap

    result = await runner.run(
        "duel1", _project(), snapshot_factory,
        system_password="pw", grace=timedelta(0), interval_s=0.0, max_ticks=10,
    )

    assert result.fired.kind.value == "message"
    # lifecycle went provisioning -> running -> concluding -> archived
    events = await chron.events("duel1", kind=EventKind.LIFECYCLE)
    assert [e.payload["event"] for e in events] == [
        "provisioning", "running", "concluding", "archived",
    ]
    assert "duel" in result.report
    await chron.close()


async def test_live_snapshot_labels_commons_so_message_termination_fires():
    from bearpit.core.schema import TerminationCondition
    from bearpit.gatekeeper.runner import LiveSnapshot
    from bearpit.warden import evaluate_termination

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    # a system kickoff that QUOTES the termination phrase must NOT count (else turn-0 end)
    await chron.record_message(
        "r", "!room:realm.local", "@system:realm.local", "…post DONE when done"
    )
    # the mirror records messages under the Matrix ROOM ID, not "commons"
    await chron.record_message("r", "!room:realm.local", "@r-vela:realm.local", "we are DONE")

    class _Herald:
        async def mirror(self, *a, **k):
            return 0

    class _Ledger:
        async def poll_spend(self, realm, chron):
            return {}

    class _Runtime:
        def read_volume(self, name):
            return {}

    snap_fn = LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="r", commons_room="!room:realm.local", shared_volume=None,
        stop_flag=lambda: False, clock=lambda: 0.0,
    )
    snap = await snap_fn()
    # only the agent message survives (relabeled); the @system kickoff is excluded
    assert snap.messages == [("commons", "we are DONE")]
    fired = evaluate_termination(
        [TerminationCondition(type="message", channel="commons", pattern="DONE")], snap
    )
    assert fired and fired.kind.value == "message"  # now matches (the bug: it never did)
    await chron.close()


async def test_live_snapshot_tracks_idle_for_stall():
    from bearpit.core.schema import TerminationCondition
    from bearpit.gatekeeper.runner import LiveSnapshot
    from bearpit.warden import evaluate_termination

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")

    class _Herald:
        async def mirror(self, *a, **k):
            return 0

    class _Ledger:
        async def poll_spend(self, realm, chron):
            return {}

    class _Runtime:
        def read_volume(self, name):
            return {}

    now = [0.0]
    snap_fn = LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="r", commons_room="!c:realm.local", shared_volume=None,
        stop_flag=lambda: False, clock=lambda: now[0],
    )
    # no agent messages yet -> idle grows from realm start
    now[0] = 120.0
    assert (await snap_fn()).idle_s == 120.0
    # an agent speaks -> idle resets on the tick that observes it
    await chron.record_message("r", "!c:realm.local", "@r-vela:realm.local", "hi")
    now[0] = 130.0
    assert (await snap_fn()).idle_s == 0.0
    # goes quiet again -> idle climbs from the last message
    now[0] = 400.0
    snap = await snap_fn()
    assert snap.idle_s == 270.0
    assert evaluate_termination(
        [TerminationCondition(type="stall", limit="4m")], snap  # 240s < 270s -> fires
    ).kind.value == "stall"
    await chron.close()


async def test_runner_manual_stop():
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    ks = KeyStore(KeyStore.generate_key())
    ks.put("azure-main", "K")
    herald = Herald(FakeMatrix(), server_name="realm.local", homeserver="h")
    ledger = Ledger(ks, FakeLiteLLM(), "p")
    forge = Forge(FakeRuntime(), ledger)
    runner = Runner(herald, forge, Warden(forge, herald, chron), ledger, chron)

    def factory(realm_id, handles, bus, turns):
        async def snap():
            return RealmSnapshot(manual_stop=True)  # kill switch immediately
        return snap

    result = await runner.run(
        "r1", _project(), factory, system_password="pw",
        grace=timedelta(0), interval_s=0.0, max_ticks=3,
    )
    assert result.fired.kind.value == "manual"
    await chron.close()


async def test_runner_wires_turns_end_to_end():
    """A turns-enabled project: kickoff does NOT broadcast, the TurnManager grants the first
    floor (a power-level state event), and conclude lifts the gate."""
    from bearpit.core.schema import Turns

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    ks = KeyStore(KeyStore.generate_key())
    ks.put("azure-main", "K")
    mx = FakeMatrix()
    herald = Herald(mx, server_name="realm.local", homeserver="h")
    ledger = Ledger(ks, FakeLiteLLM(), "p")
    forge = Forge(FakeRuntime(), ledger)
    warden = Warden(forge, herald, chron)
    runner = Runner(herald, forge, warden, ledger, chron)

    proj = _project()
    proj = proj.model_copy(update={"spec": proj.spec.model_copy(update={"turns": Turns()})})

    def factory(realm_id, handles, bus, turns):
        async def snap():
            return RealmSnapshot(manual_stop=True)  # end immediately
        return snap

    await runner.run("deb1", proj, factory, system_password="pw",
                     grace=timedelta(0), interval_s=0.0, max_ticks=1)

    # a TURN event was chronicled (the first floor was granted)
    turns = await chron.events("deb1", kind="turn")
    assert turns and turns[0].payload["current"] == "@deb1-vela:realm.local"
    # the kickoff carried NO broad mention (turns address per-turn, not everyone at once)
    assert None in mx.mentions
    # power levels were set (initial mute + first grant + conclude open); gate lifted at the end
    assert mx.power[-1]["events_default"] == 0  # gate lifted on conclude


async def test_turn_manager_verdict_tool_wiring():
    # The round cue advertises the referee's `rule` verdict tool ONLY when the realmtools are
    # wired (provide_tools) AND its verdict ends the realm — an unwired tool must not be advertised
    # (among-us originally had provide_tools=false, so `rule` never even reached the model).
    from bearpit.core.schema import RefereePowers, Turns
    from bearpit.herald.herald import BusProvision
    from bearpit.herald.types import MatrixCreds

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    ks = KeyStore(KeyStore.generate_key())
    ks.put("azure-main", "K")
    herald = Herald(FakeMatrix(), server_name="realm.local", homeserver="h")
    ledger = Ledger(ks, FakeLiteLLM(), "p")
    forge = Forge(FakeRuntime(), ledger)
    runner = Runner(herald, forge, Warden(forge, herald, chron), ledger, chron)

    def creds(aid):
        return MatrixCreds(homeserver="h", user_id=f"@r-{aid}:realm.local", access_token="t",
                           allowed_users=[], commons_room="!c")

    proj = _project()
    proj = proj.model_copy(update={"spec": proj.spec.model_copy(update={"turns": Turns()})})
    bus = BusProvision(commons_room="!c", creds={a.id: creds(a.id) for a in proj.agents})

    # default referee powers: the verdict does NOT end the realm -> the cue must not push the tool
    mgr = runner._build_turn_manager("r", proj, bus)
    assert mgr is not None and mgr._verdict_tool is False

    # verdict_ends_realm granted (provide_tools defaults true) -> the cue directs the tool
    ref = proj.referee
    assert ref is not None
    ref2 = ref.model_copy(update={"powers": RefereePowers(verdict_ends_realm=True)})
    proj2 = proj.model_copy(
        update={"agents": [ref2 if a.id == ref.id else a for a in proj.agents]})
    mgr2 = runner._build_turn_manager("r", proj2, bus)
    assert mgr2 is not None and mgr2._verdict_tool is True

    # but never when the scenario opted out of the realmtools entirely
    proj3 = proj2.model_copy(
        update={"spec": proj2.spec.model_copy(update={"provide_tools": False})})
    mgr3 = runner._build_turn_manager("r", proj3, bus)
    assert mgr3 is not None and mgr3._verdict_tool is False
    await chron.close()


async def test_live_snapshot_feeds_elimination_events_to_the_turns():
    # the referee's `eliminate` tool call lands as an ELIMINATION event; the snapshot layer must
    # enforce each one exactly once via turns.apply_resolutions — physics from a tool, not from
    # parsing referee prose.
    from bearpit.gatekeeper.runner import LiveSnapshot

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await chron.append_event("r", EventKind.ELIMINATION, {"agent": "juno", "issued_by": "mother"})
    await chron.append_event("r", EventKind.ELIMINATION, {"agent": None, "issued_by": "mother"})

    class _Herald:
        async def mirror(self, *a, **k):
            return 0

    class _Ledger:
        async def poll_spend(self, realm, chron):
            return {}

    class _Runtime:
        def read_volume(self, name):
            return {}

    class _Turns:
        def __init__(self):
            self.resolutions = []
            self.observed = 0

        async def apply_resolutions(self, entries):
            self.resolutions.append(list(entries))

        async def observe(self, speakers):
            self.observed += 1

    turns = _Turns()
    snap_fn = LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="r", commons_room="!room", shared_volume=None,
        stop_flag=lambda: False, clock=lambda: 0.0, turns=turns,  # type: ignore[arg-type]
    )
    await snap_fn()
    assert turns.resolutions == [["juno", None]]  # both calls enforced, in order
    await snap_fn()
    assert turns.resolutions == [["juno", None]]  # cursor: never re-applied
    assert turns.observed == 2
    await chron.close()


async def test_private_messages_are_capped_per_round():
    """Two always-on agents alone in a DM room ACK each other forever: the Commons has floor
    control, a private room has none, so every delivery provokes a reply (among-us-sim1 — the two
    impostors traded 25 messages of "Copy"/"Agreed" inside one round, burning the round and the
    budget). The quota is PHYSICS: past its budget the host simply stops DELIVERING. The attempt is
    still chronicled, so the operator can still see what the agent tried to say."""
    from bearpit.chronicle import EventKind
    from bearpit.gatekeeper.runner import LiveSnapshot
    from bearpit.herald.types import MatrixCreds

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")

    class _Herald:
        def __init__(self):
            self.posted, self.announced = [], []

        async def post_as(self, token, room, body, mentions=None):
            self.posted.append((room, body))
            return "$id"

        async def announce(self, room, body, mentions=None):
            self.announced.append((room, body, mentions))
            return "$id"

        async def mirror(self, realm_id, room_id, chronicle):
            return 0

    class _Ledger:
        async def spend(self, realm_id):
            return {}

        def minted_keys(self, realm_id):
            return []

    class _Runtime:
        def read_volume(self, name):
            return {}

    class _Turns:
        round = 1

    herald, turns = _Herald(), _Turns()
    def _mc(name, token):
        return MatrixCreds(
            homeserver="http://conduit:6167", user_id=f"@{name}", access_token=token,
            allowed_users=[], commons_room="!c",
        )

    creds = {"cass": _mc("cass", "t1"), "vega": _mc("vega", "t2")}
    snap = LiveSnapshot(
        herald=herald, ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="r", commons_room="!c", shared_volume=None, stop_flag=lambda: False,
        turns=turns, creds=creds,
        side_channels={"!dm": {"members": ["@cass", "@vega"], "label": "cass · vega"}},
        dm_quota={"cass": 2},
    )
    for n in range(4):
        await chron.append_event(
            "r", EventKind.PRIVATE, {"from": "cass", "to": "vega", "text": f"copy {n}"})
    await snap._deliver_private()
    assert len(herald.posted) == 2  # the 3rd and 4th are never delivered
    assert any("used its 2 private message" in body for _r, body, _m in herald.announced)
    # the notice must NOT @mention anyone, or it provokes the very reply it exists to stop
    assert all(m is None for _r, _b, m in herald.announced)

    turns.round = 2  # a new round refreshes the budget
    await chron.append_event(
        "r", EventKind.PRIVATE, {"from": "cass", "to": "vega", "text": "new round"})
    await snap._deliver_private()
    assert len(herald.posted) == 3
    await chron.close()


async def test_run_code_executes_in_the_CALLERS_OWN_container_only():
    """`run_code` is brokered: realmtools records the intent (it has no Docker — a socket in that
    small agent-facing server would turn any bug in it into host root), and the HOST performs it.
    The agent comes from the caller's VERIFIED TOKEN, never from a tool argument, so the container
    map is the security boundary: an agent can only ever execute inside its own sandbox."""
    from bearpit.chronicle import EventKind
    from bearpit.gatekeeper.runner import LiveSnapshot

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")

    class _Herald:
        async def mirror(self, realm_id, room_id, chronicle):
            return 0

    class _Ledger:
        async def spend(self, realm_id):
            return {}

        def minted_keys(self, realm_id):
            return []

    class _Runtime:
        def __init__(self):
            self.calls = []

        def read_volume(self, name):
            return {}

        def exec_python(self, container_id, code, *, timeout_s=30, user="10000"):
            self.calls.append((container_id, code))
            return 0, "Cass 4, Orin 3, SKIP 2\n"

    runtime = _Runtime()
    snap = LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=runtime,
        realm_id="r", commons_room="!c", shared_volume=None, stop_flag=lambda: False,
        containers={"mother": "cid-mother", "cass": "cid-cass"},
    )
    await chron.append_event(
        "r", EventKind.EXEC, {"id": "req1", "agent": "mother", "code": "print('tally')"})
    await snap._run_exec_requests()

    # ran in MOTHER's container — never anyone else's
    assert runtime.calls == [("cid-mother", "print('tally')")]
    results = await chron.events("r", kind=EventKind.EXEC_RESULT)
    assert results[-1].payload["id"] == "req1"
    assert results[-1].payload["exit_code"] == 0
    assert "Cass 4" in results[-1].payload["output"]

    # an agent with no container gets an ANSWER, not a 90-second hang
    await chron.append_event(
        "r", EventKind.EXEC, {"id": "req2", "agent": "ghost", "code": "print(1)"})
    await snap._run_exec_requests()
    assert len(runtime.calls) == 1  # nothing executed
    assert "no container" in (await chron.events("r", kind=EventKind.EXEC_RESULT))[-1].payload[
        "output"]
    await chron.close()


async def test_a_crashing_exec_still_answers_the_waiting_agent():
    # realmtools blocks waiting for EXEC_RESULT; if the host threw and never replied, the agent
    # would stall for the full 90s timeout on every call.
    from bearpit.chronicle import EventKind
    from bearpit.gatekeeper.runner import LiveSnapshot

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")

    class _Herald:
        async def mirror(self, realm_id, room_id, chronicle):
            return 0

    class _Ledger:
        async def spend(self, realm_id):
            return {}

        def minted_keys(self, realm_id):
            return []

    class _Runtime:
        def read_volume(self, name):
            return {}

        def exec_python(self, container_id, code, *, timeout_s=30, user="10000"):
            raise RuntimeError("docker is on fire")

    snap = LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="r", commons_room="!c", shared_volume=None, stop_flag=lambda: False,
        containers={"juno": "cid-juno"},
    )
    await chron.append_event("r", EventKind.EXEC, {"id": "r1", "agent": "juno", "code": "x"})
    await snap._run_exec_requests()
    out = (await chron.events("r", kind=EventKind.EXEC_RESULT))[-1].payload
    assert out["exit_code"] is None and "exec failed" in out["output"]
    await chron.close()


async def test_an_agent_that_spends_its_budget_is_actually_killed():
    """The BUDGET boundary (architecture §6) was only half-built. LiteLLM starved the key — it
    answered 429 — but nothing ever acted on that, so the runtime retried forever and POSTED each
    failure into the room. debate-1 drowned in 2,540 copies of "the model provider is rate-limiting
    requests", and the chair, seeing no arguments at all, ruled "no contest". `Ledger.exhausted()`
    even documented itself as "(Warden acts on these)" and had no caller in production."""
    from bearpit.chronicle import EventKind
    from bearpit.gatekeeper.runner import LiveSnapshot

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")

    class _Herald:
        async def mirror(self, realm_id, room_id, chronicle):
            return 0

    class _Ledger:
        async def poll_spend(self, realm_id, chronicle):
            return {"broke": (2.0, 2.0), "fine": (0.5, 5.0)}   # broke has hit its cap exactly

    class _Runtime:
        def __init__(self):
            self.stopped = []

        def read_volume(self, name):
            return {}

        def stop_container(self, cid, *, timeout=5):
            self.stopped.append(cid)

    now = [0.0]
    runtime = _Runtime()
    snap = LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=runtime,
        realm_id="r", commons_room="!c", shared_volume=None, stop_flag=lambda: False,
        clock=lambda: now[0],
        containers={"broke": "cid-broke", "fine": "cid-fine"},
        budget_policy={"broke": ("starve_then_kill", 60.0), "fine": ("starve_then_kill", 60.0)},
    )

    await snap()                       # exhaustion noticed, but the grace period has not elapsed
    assert runtime.stopped == []
    events = await chron.events("r", kind=EventKind.LIFECYCLE)
    assert any(e.payload.get("event") == "budget_exhausted" for e in events)

    now[0] = 30.0
    await snap()
    assert runtime.stopped == []       # still inside the 60s grace

    now[0] = 61.0
    await snap()
    assert runtime.stopped == ["cid-broke"]   # ...and only the broke one
    killed = [e for e in await chron.events("r", kind=EventKind.LIFECYCLE)
              if e.payload.get("event") == "agent_killed"]
    assert killed and killed[-1].payload["agent"] == "broke"

    now[0] = 120.0
    await snap()
    assert runtime.stopped == ["cid-broke"]   # never killed twice
    await chron.close()


async def test_starve_policy_leaves_the_container_alone():
    # `starve` is a real, declared choice: the agent lives on, unable to call the model. The
    # platform must honour it and NOT kill.
    from bearpit.gatekeeper.runner import LiveSnapshot

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")

    class _Herald:
        async def mirror(self, realm_id, room_id, chronicle):
            return 0

    class _Ledger:
        async def poll_spend(self, realm_id, chronicle):
            return {"a": (9.0, 9.0)}

    class _Runtime:
        def __init__(self):
            self.stopped = []

        def read_volume(self, name):
            return {}

        def stop_container(self, cid, *, timeout=5):
            self.stopped.append(cid)

    runtime = _Runtime()
    snap = LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=runtime,
        realm_id="r", commons_room="!c", shared_volume=None, stop_flag=lambda: False,
        clock=lambda: 10_000.0, containers={"a": "cid-a"},
        budget_policy={"a": ("starve", 0.0)},
    )
    await snap()
    assert runtime.stopped == []
    await chron.close()


async def test_exec_output_is_scrubbed_of_platform_minted_credentials():
    """An agent runs `env` inside its own container and pipes the lot back.

    That container holds its Matrix access token and its LiteLLM virtual key in plaintext, so
    without masking those land verbatim in an append-only log that is served over the API and
    included in exports — live, replayable credentials. The platform minted them, so it substitutes
    them on the way out (core.redact)."""
    from bearpit.chronicle import EventKind
    from bearpit.core.redact import MASK
    from bearpit.gatekeeper.runner import LiveSnapshot
    from bearpit.herald.types import MatrixCreds

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    token = "syt_dmVsYQ_QWERTYuiopASDFGHjkl_2xY9Zq"
    vkey = "sk-litellm-3f9a2c7e51b04d8ea6c1f0d2b7e4a913"

    class _Herald:
        async def mirror(self, realm_id, room_id, chronicle):
            return 0

    class _Ledger:
        async def spend(self, realm_id):
            return {}

        def minted_keys(self, realm_id):
            return [vkey]

    class _Runtime:
        def read_volume(self, name):
            return {}

        def exec_python(self, container_id, code, *, timeout_s=30, user="10000"):
            return 0, (
                f"HOME=/home/agent\nMATRIX_ACCESS_TOKEN={token}\n"
                f"MODEL_API_KEY={vkey}\nMATRIX_USER_ID=@vela:realm.local\n"
            )

    snap = LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="r", commons_room="!c", shared_volume=None, stop_flag=lambda: False,
        containers={"vela": "cid-vela"},
        creds={"vela": MatrixCreds(
            homeserver="http://hs", user_id="@vela:realm.local", access_token=token,
            allowed_users=[], commons_room="!c",
        )},
    )
    await chron.append_event(
        "r", EventKind.EXEC, {"id": "e1", "agent": "vela", "code": "import os; print(os.environ)"})
    await snap._run_exec_requests()

    output = (await chron.events("r", kind=EventKind.EXEC_RESULT))[-1].payload["output"]
    assert token not in output and vkey not in output   # neither credential survives
    assert output.count(MASK) == 2
    assert "HOME=/home/agent" in output                 # ordinary output is untouched
    assert "@vela:realm.local" in output                # an identity is not a credential
    await chron.close()


async def test_all_three_of_an_agents_credentials_are_scrubbed():
    """Two out of three is a defence that only looks complete.

    An agent's container holds its Matrix access token, its model-proxy virtual key, AND its
    realmtools bearer. The last is the worst to leak — it is the credential that calls
    eliminate()/tally() and reads sealed submissions AS that agent — and it was the one the first
    version of this missed, because Forge mints it and the Runner never saw it."""
    from bearpit.chronicle import EventKind
    from bearpit.core.redact import MASK
    from bearpit.gatekeeper.runner import LiveSnapshot
    from bearpit.herald.types import MatrixCreds

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    matrix = "syt_dmVsYQ_QWERTYuiopASDFGHjkl_2xY9Zq"
    vkey = "sk-litellm-3f9a2c7e51b04d8ea6c1f0d2b7e4a913"
    rtok = "rt_eyJhZ2VudCI6InZlbGEiLCJyZWFsbSI6InIifQ.9f3c1e7a5b2d4086"

    class _Herald:
        async def mirror(self, realm_id, room_id, chronicle):
            return 0

    class _Ledger:
        async def spend(self, realm_id):
            return {}

        def minted_keys(self, realm_id):
            return [vkey]

    class _Runtime:
        def read_volume(self, name):
            return {}

        def exec_python(self, container_id, code, *, timeout_s=30, user="10000"):
            return 0, (f"MATRIX_ACCESS_TOKEN={matrix}\nMODEL_API_KEY={vkey}\n"
                       f"REALMTOOLS_TOKEN={rtok}\nHOME=/home/agent\n")

    snap = LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="r", commons_room="!c", shared_volume=None, stop_flag=lambda: False,
        containers={"vela": "cid-vela"},
        creds={"vela": MatrixCreds(
            homeserver="http://hs", user_id="@vela:realm.local", access_token=matrix,
            allowed_users=[], commons_room="!c",
        )},
        agent_tokens={"vela": rtok},
    )
    await chron.append_event("r", EventKind.EXEC, {"id": "e1", "agent": "vela", "code": "env"})
    await snap._run_exec_requests()

    out = (await chron.events("r", kind=EventKind.EXEC_RESULT))[-1].payload["output"]
    for secret in (matrix, vkey, rtok):
        assert secret not in out
    assert out.count(MASK) == 3
    assert "HOME=/home/agent" in out
    await chron.close()


async def test_the_snapshot_reports_participants_and_who_can_still_act():
    """The counters behind `no_active_participants` (#30) must reflect reality, tick by tick.

    The pure rule is only as good as the liveness the runner feeds it. Both ways a container is
    stopped have to count: killed for budget, and eliminated by the referee. The referee itself is
    excluded from the roster on purpose — in the reported case it was alive and funded, calling
    rounds into an empty room, which is exactly why nothing else caught this."""
    from bearpit.chronicle import EventKind
    from bearpit.gatekeeper.runner import LiveSnapshot

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")

    class _Herald:
        async def mirror(self, realm_id, room_id, chronicle):
            return 0

    class _Ledger:
        def __init__(self):
            self.spend = {"orin": (0.5, 2.0), "vela": (0.5, 2.0)}

        async def poll_spend(self, realm_id, chronicle):
            return self.spend

    class _Runtime:
        def read_volume(self, name):
            return {}

        def stop_container(self, cid, *, timeout=5):
            pass

    now = [0.0]
    ledger = _Ledger()
    snap = LiveSnapshot(
        herald=_Herald(), ledger=ledger, chronicle=chron, runtime=_Runtime(),
        realm_id="r", commons_room="!c", shared_volume=None, stop_flag=lambda: False,
        clock=lambda: now[0],
        containers={"orin": "cid-orin", "vela": "cid-vela", "themis": "cid-themis"},
        budget_policy={"orin": ("starve_then_kill", 0.0), "vela": ("starve_then_kill", 0.0)},
        participants=["orin", "vela"],          # themis referees; it is NOT a participant
    )

    s = await snap()
    assert (s.participants, s.participants_alive) == (2, 2)

    # orin burns its cap and is killed
    ledger.spend = {"orin": (2.0, 2.0), "vela": (0.5, 2.0)}
    now[0] = 10.0
    s = await snap()
    assert (s.participants, s.participants_alive) == (2, 1), "a killed agent cannot act"
    assert evaluate_termination([], s) is None, "one player left — the realm goes on"

    # the referee eliminates vela: the OTHER way a container stops
    await chron.append_event("r", EventKind.ELIMINATION, {"agent": "vela", "reason": "ejected"})
    now[0] = 20.0
    s = await snap()
    assert (s.participants, s.participants_alive) == (2, 0)
    fired = evaluate_termination([], s)
    assert fired is not None and fired.kind == TerminationKind.NO_ACTIVE_PARTICIPANTS
    await chron.close()
