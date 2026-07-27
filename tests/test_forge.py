"""Forge orchestration + Hermes adapter, exercised with a fake ContainerRuntime + Ledger."""

import pytest
import yaml

from agentrealm.core.schema import (
    AgentSpec,
    Budget,
    Environment,
    ModelRef,
    Project,
    ProjectMeta,
    ProjectSpec,
    SharedFolder,
)
from agentrealm.forge import Forge, MatrixCreds
from agentrealm.ledger import KeyStore, Ledger


class FakeRuntime:
    def __init__(self) -> None:
        self.networks: dict[str, bool] = {}  # name -> internal
        self.volumes: dict[str, dict[str, str]] = {}
        self.containers: dict[str, dict] = {}
        self.removed_networks: list[str] = []
        self._n = 0

    def create_network(self, name, *, internal):
        self.networks[name] = internal
        return name

    def connect_network(self, network, container):
        self.connected = getattr(self, 'connected', [])
        self.connected.append((network, container))

    def remove_network(self, name):
        self.removed_networks.append(name)
        self.networks.pop(name, None)

    def create_volume(self, name):
        self.volumes.setdefault(name, {})
        return name

    def seed_volume(self, name, files):
        self.volumes[name] = dict(files)

    def remove_volume(self, name):
        self.volumes.pop(name, None)

    def run_container(self, *, name, image, network, volumes, environment, command,
                      mem_limit=None, pids_limit=None, nano_cpus=None):
        self._n += 1
        cid = f"c{self._n}"
        self.containers[cid] = {
            "name": name, "image": image, "network": network, "volumes": volumes,
            "running": True, "command": command,
        }
        return cid

    def stop_container(self, container_id, *, timeout):
        self.containers[container_id]["running"] = False

    def remove_container(self, container_id):
        self.containers.pop(container_id, None)

    def container_logs(self, container_id, *, tail=4000):
        self.log_reads = getattr(self, "log_reads", [])
        self.log_reads.append(container_id)
        return f"fake logs for {container_id}"

    # --- what the reaper needs to SEE ---
    def list_containers(self, prefix):
        return {c["name"]: cid for cid, c in self.containers.items()
                if str(c["name"]).startswith(prefix)}

    def list_volumes(self, prefix):
        return [v for v in self.volumes if v.startswith(prefix)]

    def list_networks(self, prefix):
        return [n for n in self.networks if n.startswith(prefix)]



class FakeLiteLLM:
    def __init__(self) -> None:
        self._n = 0
        self.deleted: list[str] = []

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
        self.deleted.append(virtual_key)

    async def delete_model(self, model_name):
        self.deleted_models = getattr(self, 'deleted_models', [])
        self.deleted_models.append(model_name)


def _project() -> Project:
    def agent(aid, referee=False):
        extra = {"role": "referee", "rubric": "judge"} if referee else {}
        return AgentSpec(
            id=aid, persona=f"# {aid}",
            model=ModelRef(provider="azure", model="gpt-5.4-mini", api_key_ref="azure-main",
            input_cost_per_token=1e-7, output_cost_per_token=6e-7),
            budget=Budget(max_usd=1.0), **extra,
        )
    return Project(
        metadata=ProjectMeta(name="duel"),
        spec=ProjectSpec(
            guidelines="Be fair.",
            environment=Environment(shared_folder=SharedFolder(enabled=True)),
        ),
        agents=[agent("vela"), agent("orin"), agent("themis", referee=True)],
    )


def _matrix(ids):
    return {
        aid: MatrixCreds(
            homeserver="http://conduit:6167",
            user_id=f"@{aid}:realm.local",
            access_token=f"tok-{aid}",
            allowed_users=["@operator:realm.local"],
            commons_room="!commons:realm.local",
        )
        for aid in ids
    }


async def test_provision_and_teardown_realm():
    runtime = FakeRuntime()
    ledger = Ledger(_ks(), FakeLiteLLM(), "http://litellm:4000")
    forge = Forge(runtime, ledger)
    project = _project()
    creds = _matrix([a.id for a in project.agents])

    handles = await forge.provision_realm(
        "r1", project, creds,
        bus_homeserver="http://conduit:6167", proxy_url="http://litellm:4000",
        commons_room="!commons:realm.local",
    )

    # per-realm network is internal (model_only default), one container per agent, shared vol
    assert runtime.networks["realm-r1"] is True
    assert len(runtime.containers) == 3
    assert handles.shared_volume == "realm-r1-shared"
    # shared volume is seeded (-> chowned to the Hermes uid) so agents can write to /realm/shared
    assert "README.txt" in runtime.volumes["realm-r1-shared"]
    # each agent got its own HERMES_HOME volume seeded with a config carrying its virtual key
    vela_home = runtime.volumes["realm-r1--vela"]
    cfg = yaml.safe_load(vela_home["config.yaml"])
    assert cfg["model"]["api_key"].startswith("vk-")  # C10: minted key injected into config
    assert "MATRIX_ACCESS_TOKEN=tok-vela" in vela_home[".env"]
    # every container is attached to the realm network and mounts the shared folder
    for c in runtime.containers.values():
        assert c["network"] == "realm-r1"
        assert "realm-r1-shared" in c["volumes"]

    await forge.teardown_realm(handles)
    assert runtime.containers == {}  # all removed
    assert "realm-r1" in runtime.removed_networks
    assert runtime.volumes == {}  # homes + shared removed
    assert len(ledger._keys) == 0  # virtual keys deleted


async def test_bus_and_proxy_attached_to_realm_network():
    runtime = FakeRuntime()
    forge = Forge(runtime, Ledger(_ks(), FakeLiteLLM(), "http://litellm:4000"))
    project = _project()
    creds = _matrix([a.id for a in project.agents])
    await forge.provision_realm(
        "r3", project, creds, bus_homeserver="http://arealm-conduit:6167",
        proxy_url="http://arealm-litellm:4000", commons_room="!c",
        attach_containers=("arealm-conduit", "arealm-litellm"),
    )
    # both service containers were joined to the realm network so agents can resolve them
    assert ("realm-r3", "arealm-conduit") in runtime.connected
    assert ("realm-r3", "arealm-litellm") in runtime.connected


async def test_realmtools_wired_for_mechanic_project():
    import yaml as _yaml

    from agentrealm.core.schema import Mechanic
    from agentrealm.forge import RealmtoolsConfig
    from agentrealm.realmtools.tokens import verify_token

    runtime = FakeRuntime()
    rt = RealmtoolsConfig(
        url="http://arealm-realmtools:9100/mcp", secret="s3cr3t", container="arealm-realmtools"
    )
    forge = Forge(runtime, Ledger(_ks(), FakeLiteLLM(), "http://p"), realmtools=rt)
    project = _project()
    project.spec.mechanics = [Mechanic(kind="sealed-submit", ruleset="dominance")]
    creds = _matrix([a.id for a in project.agents])

    await forge.provision_realm(
        "duelm", project, creds, bus_homeserver="h", proxy_url="p", commons_room="!c",
        attach_containers=("arealm-conduit",),
    )
    # the realmtools container is attached to the realm network alongside the bus
    assert ("realm-duelm", "arealm-realmtools") in runtime.connected

    # each agent got the MCP server in its config + a token whose identity+role verify
    vela_cfg = _yaml.safe_load(runtime.volumes["realm-duelm--vela"]["config.yaml"])
    assert vela_cfg["mcp_servers"]["realmtools"]["url"] == rt.url

    # a referee-only project (no mechanic) STILL wires realmtools (Arbiter scoring tools)
    runtime2 = FakeRuntime()
    forge2 = Forge(runtime2, Ledger(_ks(), FakeLiteLLM(), "http://p"), realmtools=rt)
    proj2 = _project()  # has a referee (themis), no mechanic
    proj2.spec.mechanics = []
    await forge2.provision_realm(
        "reftest", proj2, _matrix([a.id for a in proj2.agents]),
        bus_homeserver="h", proxy_url="p", commons_room="!c",
    )
    assert ("realm-reftest", "arealm-realmtools") in runtime2.connected
    themis_cfg = _yaml.safe_load(runtime2.volumes["realm-reftest--themis"]["config.yaml"])
    assert "realmtools" in themis_cfg["mcp_servers"]
    vela_token = _token_from_env(runtime.volumes["realm-duelm--vela"][".env"])
    vela_id = verify_token(vela_token, "s3cr3t")
    assert vela_id[:3] == ("duelm", "vela", False)  # player
    assert "vela" in vela_id[3] and "themis" not in vela_id[3]  # roster = participants, no referee

    # provide_tools=false opts OUT — a message-based scenario gets NO realmtools (no idle tools)
    runtime3 = FakeRuntime()
    forge3 = Forge(runtime3, Ledger(_ks(), FakeLiteLLM(), "http://p"), realmtools=rt)
    proj3 = _project()  # has a referee
    proj3.spec.provide_tools = False
    await forge3.provision_realm(
        "notools", proj3, _matrix([a.id for a in proj3.agents]),
        bus_homeserver="h", proxy_url="p", commons_room="!c",
    )
    assert ("realm-notools", "arealm-realmtools") not in getattr(runtime3, "connected", [])
    themis3 = _yaml.safe_load(runtime3.volumes["realm-notools--themis"]["config.yaml"])
    assert "mcp_servers" not in themis3  # no tools wired at all
    themis_token = _token_from_env(runtime.volumes["realm-duelm--themis"][".env"])
    themis_id = verify_token(themis_token, "s3cr3t")
    assert themis_id[:3] == ("duelm", "themis", True)  # referee carries the same roster
    assert "vela" in themis_id[3]


async def test_realmtools_wired_for_private_messaging_only_project():
    # a purely collaborative project (no referee, no mechanic) still needs realmtools if any agent
    # can DM — that's how they get the send_private tool.
    from agentrealm.core.schema import PrivateMessaging
    from agentrealm.forge import RealmtoolsConfig

    runtime = FakeRuntime()
    rt = RealmtoolsConfig(
        url="http://arealm-realmtools:9100/mcp", secret="s3cr3t", container="arealm-realmtools"
    )
    forge = Forge(runtime, Ledger(_ks(), FakeLiteLLM(), "http://p"), realmtools=rt)
    proj = Project(
        metadata=ProjectMeta(name="dm"),
        spec=ProjectSpec(guidelines="Collaborate."),
        agents=[
            AgentSpec(id="alice",
                      model=ModelRef(provider="azure", model="m", api_key_ref="azure-main"),
                      private_messaging=PrivateMessaging(enabled=True)),
            AgentSpec(id="bob",
                      model=ModelRef(provider="azure", model="m", api_key_ref="azure-main")),
        ],
    )
    assert proj.referee is None and not proj.spec.mechanics  # neither legacy trigger applies
    await forge.provision_realm(
        "dmrealm", proj, _matrix([a.id for a in proj.agents]),
        bus_homeserver="h", proxy_url="p", commons_room="!c",
    )
    assert ("realm-dmrealm", "arealm-realmtools") in runtime.connected
    alice_cfg = yaml.safe_load(runtime.volumes["realm-dmrealm--alice"]["config.yaml"])
    assert "realmtools" in alice_cfg["mcp_servers"]  # send_private reachable


def _token_from_env(env_text: str) -> str:
    for line in env_text.splitlines():
        if line.startswith("REALMTOOLS_TOKEN="):
            return line.split("=", 1)[1]
    raise AssertionError("no REALMTOOLS_TOKEN in .env")


async def test_open_egress_is_not_internal():
    runtime = FakeRuntime()
    forge = Forge(runtime, Ledger(_ks(), FakeLiteLLM(), "http://p"))
    project = _project()
    project.spec.environment.network_egress = project.spec.environment.network_egress.OPEN
    creds = _matrix([a.id for a in project.agents])
    await forge.provision_realm("r2", project, creds, bus_homeserver="h", proxy_url="p",
                                commons_room="!c")
    assert runtime.networks["realm-r2"] is False  # open egress => not internal


def _ks() -> KeyStore:
    ks = KeyStore(KeyStore.generate_key())
    ks.put("azure-main", "REALKEY", api_base="https://x/v1")
    return ks


async def test_teardown_archives_agent_logs_as_flight_recorder(tmp_path):
    # post-mortems repeatedly died on "the containers are gone" — teardown must save each agent
    # container's log tail (Hermes' MCP/tool/model diagnostics) before removing it.
    runtime = FakeRuntime()
    ledger = Ledger(_ks(), FakeLiteLLM(), "http://litellm:4000")
    forge = Forge(runtime, ledger, flight_logs_dir=tmp_path)
    project = _project()
    handles = await forge.provision_realm(
        "r9", project, _matrix([a.id for a in project.agents]),
        bus_homeserver="http://conduit:6167", proxy_url="http://litellm:4000",
        commons_room="!commons:realm.local",
    )
    await forge.teardown_realm(handles)
    for agent in project.agents:
        log = tmp_path / "r9" / "logs" / f"{agent.id}.log"
        assert log.exists() and "fake logs" in log.read_text()
    # default construction (no dir) stays silent — tests/embedders don't write to $HOME
    assert Forge(runtime, ledger)._flight_logs_dir is None


def _seed(runtime, containers, volumes=(), networks=()):
    for cid, name in containers.items():
        runtime.containers[cid] = {"name": name, "running": True}
    for v in volumes:
        runtime.volumes[v] = {}
    for n in networks:
        runtime.networks[n] = True


def test_reaper_destroys_agents_whose_realm_is_gone():
    """Teardown only runs when a realm CONCLUDES. If the platform dies — a crash, a kill -9, a
    reboot — the realm loop dies with it and nothing ever stops the agents. Three from
    `relayclaude4` were found alive 22 HOURS after their realm was gone, each still holding a live
    model key, with no budget enforcement, no termination and nobody watching: the container
    boundary failing OPEN. Anything running that the platform does not own is an orphan."""
    runtime = FakeRuntime()
    _seed(runtime,
          {"c1": "realm-relayclaude4-wren", "c2": "realm-relayclaude4-sage",
           "c3": "realm-among-us-live-mother"},                      # c3 is LIVE
          volumes=["realm-relayclaude4--wren", "realm-among-us-live--mother",
                   "realm-relayclaude4-shared"],
          networks=["realm-relayclaude4", "realm-among-us-live"])

    killed = Forge(runtime, Ledger(_ks(), FakeLiteLLM(), 'http://p')).reap_orphans(active=["among-us-live"])

    assert sorted(killed) == ["realm-relayclaude4-sage", "realm-relayclaude4-wren"]
    assert "c3" in runtime.containers                    # the live agent is untouched
    assert "c1" not in runtime.containers and "c2" not in runtime.containers
    assert "realm-relayclaude4" in runtime.removed_networks
    assert "realm-among-us-live" not in runtime.removed_networks     # the live network survives
    assert "realm-relayclaude4--wren" not in runtime.volumes
    assert "realm-relayclaude4-shared" not in runtime.volumes
    assert "realm-among-us-live--mother" in runtime.volumes          # the live volume survives


def test_reaper_never_kills_a_live_agent_whose_id_contains_a_hyphen():
    """Ownership is decided by PREFIX MATCH against the live realm ids, never by parsing the name.
    A realm id AND an agent id may both contain hyphens, so 'realm-jury-1-juror-a'.rpartition('-')
    yields the realm 'jury-1-juror' — and a parsing reaper would spare an orphan or, far worse, kill
    a RUNNING agent. (The identical mistake in `_short_name` silently broke eliminate() for every
    hyphenated id.)"""
    runtime = FakeRuntime()
    _seed(runtime, {"live1": "realm-jury-1-juror-a",       # live realm 'jury-1', agent 'juror-a'
                    "live2": "realm-jury-1-foreperson",
                    "dead1": "realm-jury-0-juror-a"})      # a DIFFERENT, dead realm

    killed = Forge(runtime, Ledger(_ks(), FakeLiteLLM(), 'http://p')).reap_orphans(active=["jury-1"])

    assert killed == ["realm-jury-0-juror-a"]
    assert "live1" in runtime.containers and "live2" in runtime.containers  # both jurors survive
    assert "dead1" not in runtime.containers


async def test_provisioning_failure_tears_down_what_it_built():
    """If provisioning throws partway (agent k's Docker/Ledger/Matrix call fails), agents 0..k-1 are
    already RUNNING, each holding a live capped key, with no Warden watching them. On a live server
    the reaper only runs at startup, so they would run unsupervised until the next restart. Any
    failure must tear down what it built."""
    import agentrealm.forge.forge as forge_mod

    runtime = FakeRuntime()
    real = forge_mod.HermesAdapter

    class Boom(real):  # type: ignore[misc, valid-type]
        n = {"c": 0}

        def start(self, handle):
            Boom.n["c"] += 1
            if Boom.n["c"] == 2:
                raise RuntimeError("docker fell over on agent 2")
            return super().start(handle)

    forge_mod.HermesAdapter = Boom
    try:
        forge = Forge(runtime, Ledger(_ks(), FakeLiteLLM(), "http://p"))
        project = _project()  # vela, orin, themis — the 2nd start() throws
        with pytest.raises(RuntimeError, match="docker fell over"):
            await forge.provision_realm(
                "r1", project, _matrix(["vela", "orin", "themis"]),
                bus_homeserver="h", proxy_url="p", commons_room="!c",
            )
    finally:
        forge_mod.HermesAdapter = real

    assert "realm-r1" in runtime.removed_networks       # the network it created is gone
    assert all(not c.get("running") for c in runtime.containers.values())  # nothing left running

async def test_forge_reports_the_realmtools_tokens_it_minted():
    """The Runner cannot mask what it never receives.

    Forge mints each agent's realmtools bearer and injects it into the container env; nothing
    downstream saw it, so `run_code` output containing it went into the append-only chronicle
    verbatim. RealmHandles now carries them — held only so they can be redacted."""
    from agentrealm.core.schema import AgentSpec, ModelRef, Project, ProjectMeta, ProjectSpec
    from agentrealm.forge import RealmtoolsConfig
    from agentrealm.herald.types import MatrixCreds

    def _model():
        return ModelRef(provider="azure", model="m", api_key_ref="azure-main",
                        input_cost_per_token=1e-7, output_cost_per_token=1e-7)

    project = Project(
        metadata=ProjectMeta(name="p"),
        spec=ProjectSpec(mechanics=[{"kind": "sealed-submit"}]),   # a mechanic wires the tools
        agents=[AgentSpec(id="vela", model=_model(), persona="x"),
                AgentSpec(id="orin", model=_model(), persona="y")],
    )
    creds = {
        a.id: MatrixCreds(homeserver="http://hs", user_id=f"@{a.id}:realm.local",
                          access_token=f"tok-{a.id}", allowed_users=[], commons_room="!c")
        for a in project.agents
    }
    runtime = FakeRuntime()
    ks = _ks()
    ks.put("azure-main", "REALKEY")
    forge = Forge(runtime, Ledger(ks, FakeLiteLLM(), "http://p"),
                  realmtools=RealmtoolsConfig(url="http://rt:9100/mcp", secret="s" * 40,
                                              container="arealm-realmtools"))
    handles = await forge.provision_realm(
        "r1", project, creds, bus_homeserver="http://hs", proxy_url="http://p",
        commons_room="!c",
    )

    assert set(handles.agent_tokens) == {"vela", "orin"}
    assert all(t for t in handles.agent_tokens.values())
    assert handles.agent_tokens["vela"] != handles.agent_tokens["orin"]   # per-agent, not shared
