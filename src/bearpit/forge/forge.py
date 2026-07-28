"""Forge — materialize a validated Project into a running realm (M2, §5).

Creates the per-realm private network and shared volume, then for each agent: mints its
virtual key (Ledger), binds the adapter with its credential + Matrix identity, and
provisions + starts it. Teardown stops agents, removes their keys, and cleans up the
network/volume. Room creation and the agents' Matrix identities come from Herald (M3);
Forge receives them via `matrix_creds`.
"""

from __future__ import annotations

import contextlib
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from bearpit.core.interfaces import AgentHandle, RealmContext
from bearpit.core.plugins import hooks_for
from bearpit.core.schema import EgressTier, Project
from bearpit.forge.adapters.hermes.adapter import HermesAdapter
from bearpit.forge.adapters.hermes.config import MatrixCreds, RealmtoolsCreds
from bearpit.forge.container import ContainerRuntime
from bearpit.ledger import Ledger
from bearpit.realmtools.tokens import mint_token


def orphan_containers(found: dict[str, str], active: Collection[str]) -> dict[str, str]:
    """Of `found` (name -> container id), those belonging to NO realm in `active`.

    Ownership is decided by PREFIX MATCH against live realm ids — never by parsing the container
    name. A realm id and an agent id may both contain hyphens ('jury-1' + 'juror-a'), so
    `'realm-jury-1-juror-a'.rpartition('-')` yields the realm 'jury-1-juror', and the reaper would
    spare an orphan or, far worse, kill a live agent. (The identical mistake in `_short_name`
    silently broke eliminate() for every hyphenated id.)

    Shared by `Forge.reap_orphans` and the `pit reap` command so the two cannot drift — they
    already had: the CLI destroyed everything matching `realm-` while its own docstring promised it
    spared running realms.
    """
    prefixes = tuple(f"realm-{r}-" for r in active)
    return {n: c for n, c in found.items() if not n.startswith(prefixes)}


@dataclass
class RealmHandles:
    """What Forge returns after provisioning a realm: the network, shared volume, and the
    per-agent handles (for Warden to stop/collect later)."""

    realm_id: str
    network: str
    shared_volume: str | None
    agents: dict[str, AgentHandle] = field(default_factory=dict)
    # agent id -> its realmtools bearer token, for callers that must MASK it (`core.redact`).
    # The token lives in the agent's own container env, so anything the agent prints can contain
    # it — and it is the credential that calls eliminate()/tally() and reads sealed submissions AS
    # that agent, which makes it the worst of the three to let reach an append-only log.
    agent_tokens: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RealmtoolsConfig:
    """Where the Realmtools MCP server is and the secret to mint agent tokens with."""

    url: str  # in-cluster MCP endpoint, e.g. http://pit-realmtools:9100/mcp
    secret: str
    container: str  # container name to attach to the realm network


class Forge:
    def __init__(
        self, runtime: ContainerRuntime, ledger: Ledger,
        *, realmtools: RealmtoolsConfig | None = None,
        flight_logs_dir: Path | None = None,
    ) -> None:
        self._runtime = runtime
        self._ledger = ledger
        self._realmtools = realmtools
        # where teardown archives each agent container's log tail (the flight recorder);
        # None = archival off (the platform wiring passes ~/.bearpit/realms)
        self._flight_logs_dir = flight_logs_dir

    async def provision_realm(
        self,
        realm_id: str,
        project: Project,
        matrix_creds: dict[str, MatrixCreds],
        *,
        bus_homeserver: str,
        proxy_url: str,
        commons_room: str,
        attach_containers: tuple[str, ...] = (),
        side_channels: dict[str, dict[str, object]] | None = None,
    ) -> RealmHandles:
        env = project.spec.environment
        # egress=none/model_only => internal network (no outbound to the internet). The bus and
        # proxy containers are attached to it so agents can still reach them by name; the proxy
        # itself keeps its own internet path (its other network) as the sole controlled egress.
        internal = env.network_egress in (EgressTier.NONE, EgressTier.MODEL_ONLY)
        network = self._runtime.create_network(f"realm-{realm_id}", internal=internal)
        # From here on we are creating REAL resources — containers, volumes, keys. If provisioning
        # fails partway (agent k's Ledger/Docker/Matrix call throws), agents 0..k-1 are already
        # RUNNING, each holding a live capped key, with no Warden watching them. On a live
        # server the reaper only runs at startup, so they run unsupervised until the next
        # restart. So any
        # failure here tears down whatever was built before re-raising.
        partial: dict[str, AgentHandle] = {}
        try:
            return await self._provision_agents(
                realm_id, project, matrix_creds, network,
                bus_homeserver=bus_homeserver, proxy_url=proxy_url, commons_room=commons_room,
                attach_containers=attach_containers, side_channels=side_channels, out=partial,
            )
        except Exception:
            with contextlib.suppress(Exception):
                await self.teardown_realm(
                    RealmHandles(realm_id, network, self._shared_of(realm_id, project), partial)
                )
            raise

    def _shared_of(self, realm_id: str, project: Project) -> str | None:
        if project.spec.environment.shared_folder.enabled:
            return f"realm-{realm_id}-shared"
        return None

    async def _provision_agents(
        self,
        realm_id: str,
        project: Project,
        matrix_creds: dict[str, MatrixCreds],
        network: str,
        *,
        bus_homeserver: str,
        proxy_url: str,
        commons_room: str,
        attach_containers: tuple[str, ...],
        side_channels: dict[str, dict[str, object]] | None,
        out: dict[str, AgentHandle],
    ) -> RealmHandles:
        env = project.spec.environment
        attach = list(attach_containers)
        # Wire the Realmtools MCP server when the project declares a mechanic (players submit
        # sealed moves) OR has a referee (who needs the Arbiter score/scoreboard/rule tools).
        # A scenario that is purely message-based (open votes, message-termination) can opt OUT
        # via provide_tools=false — otherwise idle tools tempt agents into misusing them.
        needs_tools = (
            bool(project.spec.mechanics)
            or project.referee is not None
            or project.spec.turns is not None  # turns expose turn_status via the realmtools server
            # private DMs: an agent with the permission needs the send_private tool (the host can't
            # rely on the agent finding its DM room unaided — the tool is the reliable path).
            or any(a.private_messaging.enabled for a in project.agents)
        )
        wire_tools = needs_tools and self._realmtools is not None and project.spec.provide_tools
        if wire_tools:
            assert self._realmtools is not None
            attach.append(self._realmtools.container)
        for container in attach:
            self._runtime.connect_network(network, container)

        shared_volume: str | None = None
        if env.shared_folder.enabled:
            shared_volume = f"realm-{realm_id}-shared"
            self._runtime.create_volume(shared_volume)
            # a named volume is root-owned; seed a README so seed_volume chowns it to the Hermes
            # uid (10000) — otherwise agents (uid 10000) can't write to /realm/shared.
            readme = "Realm shared folder (/realm/shared). All agents read + write here."
            self._runtime.seed_volume(shared_volume, {"README.txt": readme})

        adapter = HermesAdapter(self._runtime)
        roster = [m.user_id for m in matrix_creds.values()]
        referee = project.referee
        # participant (non-referee) ids expected to submit — baked into each token so the escrow
        # can report who is still pending a sealed move (roster-aware reveal_status).
        escrow_roster = [a.id for a in project.agents if referee is None or a.id != referee.id]
        handles = out
        tokens: dict[str, str] = {}
        for agent in project.agents:
            realmtools_creds: RealmtoolsCreds | None = None
            token: str | None = None
            if wire_tools:
                assert self._realmtools is not None
                is_ref = referee is not None and agent.id == referee.id
                token = mint_token(
                    realm_id, agent.id, is_referee=is_ref,
                    secret=self._realmtools.secret, roster=escrow_roster,
                )
                realmtools_creds = RealmtoolsCreds(url=self._realmtools.url, token=token)
                tokens[agent.id] = token
            # Some providers want the agent's own realmtools token forwarded as the model's API
            # key, so the runtime on the far side can act AS this agent (e.g. to be given native
            # access to the realm's MCP tools under that identity). Most want nothing — the hook
            # defaults to None and the keystore credential is used (`core.plugins`).
            model = agent.require_model()
            override = hooks_for(model.provider).agent_request_key(model, token)
            cred = await self._ledger.provision_agent(
                realm_id, agent, api_key_override=override,
            )
            # this agent's private DM rooms: {room_id: peer_id} for every side-channel it's in.
            my_mxid = matrix_creds[agent.id].user_id
            dm_rooms: dict[str, str] = {}
            for room, ch in (side_channels or {}).items():
                if my_mxid in ch.get("members", []):  # type: ignore[operator]
                    peer = next((p for p in str(ch["label"]).split(" · ") if p != agent.id), "")
                    dm_rooms[room] = peer
            adapter.bind(
                agent.id,
                cred,
                matrix_creds[agent.id],
                roster=roster,
                guidelines=project.spec.guidelines,
                restrictions=project.spec.restrictions,
                allow_side_channels=env.allow_side_channels,
                realmtools=realmtools_creds,
                dm_rooms=dm_rooms,
            )
            ctx = RealmContext(
                realm_id=realm_id,
                network=network,
                model_base_url=proxy_url,
                bus_homeserver=bus_homeserver,
                commons_room=commons_room,
                shared_folder=shared_volume,
            )
            handle = adapter.provision(agent, ctx)
            handles[agent.id] = handle  # record BEFORE start(): if start() throws, the container
            #                             already exists and teardown must be able to reach it
            adapter.start(handle)

        return RealmHandles(realm_id, network, shared_volume, handles, tokens)

    async def teardown_realm(
        self, handles: RealmHandles, *, grace: timedelta = timedelta(seconds=10)
    ) -> None:
        # Cleanup is best-effort: a hiccup removing one resource must not strand the others,
        # nor abort the caller's conclude/archive sequence. Stopping agents is what matters.
        adapter = HermesAdapter(self._runtime)
        for agent_id, handle in handles.agents.items():
            with contextlib.suppress(Exception):
                adapter.stop(handle, grace)
            if handle.container_id:
                # flight recorder: archive the agent's stdout/stderr (Hermes' own MCP/tool/model
                # diagnostics) BEFORE the container is destroyed — post-mortems repeatedly died on
                # "the containers are gone" (among-us-tele4/5). Best-effort like the rest.
                if self._flight_logs_dir is not None:
                    with contextlib.suppress(Exception):
                        self._archive_agent_log(handles.realm_id, agent_id, handle.container_id)
                self._runtime.remove_container(handle.container_id)  # already best-effort
            if handle.home_volume:
                self._runtime.remove_volume(handle.home_volume)  # already best-effort
        with contextlib.suppress(Exception):
            await self._ledger.teardown(handles.realm_id)
        if handles.shared_volume:
            self._runtime.remove_volume(handles.shared_volume)
        self._runtime.remove_network(f"realm-{handles.realm_id}")

    def reap_orphans(self, active: Collection[str]) -> list[str]:
        """Destroy every agent container (and its volume/network) whose realm is NOT active.

        Teardown only runs when a realm CONCLUDES. If the platform dies — a crash, a kill -9, a
        reboot — the realm loop dies with it and nothing ever stops the agents. They kept running:
        three from `relayclaude4` were alive 22 hours after their realm was gone, each still holding
        a live model key, with no budget enforcement, no termination and nobody watching. That is
        the container boundary (architecture §6) failing OPEN.

        The platform is the only thing that may keep an agent alive, so anything running that it
        does not own is by definition an orphan. Called at startup (where `active` is empty, so it
        sweeps every leftover) and again on shutdown.

        Ownership is decided by PREFIX MATCH against the live realm ids — never by parsing the
        container name. A realm id and an agent id may both contain hyphens ('jury-1' + 'juror-a'),
        so 'realm-jury-1-juror-a'.rpartition('-') yields the realm 'jury-1-juror', and the reaper
        would spare an orphan or, far worse, kill a live agent. (The identical mistake in
        `_short_name` silently broke eliminate() for every hyphenated id.)
        """
        killed: list[str] = []
        found = self._runtime.list_containers("realm-")
        for name, cid in orphan_containers(found, active).items():
            with contextlib.suppress(Exception):  # a stubborn container must not stop the sweep
                self._runtime.stop_container(cid, timeout=5)
            with contextlib.suppress(Exception):
                self._runtime.remove_container(cid)
            killed.append(name)

        # the networks and volumes those agents were using, by the same prefix rule
        net_keep = tuple(f"realm-{r}" for r in active)
        for net in self._runtime.list_networks("realm-"):
            if net in net_keep:
                continue
            with contextlib.suppress(Exception):
                self._runtime.remove_network(net)
        # ONLY `realm-*` volumes are ours (home volumes realm-<realm>--<agent>, shared volumes
        # realm-<realm>-shared). Never enumerate with an empty prefix — that scanned every volume
        # on the daemon and force-removed any name that merely contained "--", destroying an
        # operator's unrelated data on startup.
        vol_keep = (tuple(f"realm-{r}--" for r in active)
                    + tuple(f"realm-{r}-shared" for r in active))
        for vol in self._runtime.list_volumes("realm-"):
            if vol.startswith(vol_keep):
                continue  # belongs to a realm that is genuinely running
            with contextlib.suppress(Exception):
                self._runtime.remove_volume(vol)
        return killed

    def _archive_agent_log(self, realm_id: str, agent_id: str, container_id: str) -> None:
        """Write the agent container's log tail to <flight_logs_dir>/<realm>/logs/<agent>.log —
        the only surviving copy of the runtime's own diagnostics once the container is removed."""
        assert self._flight_logs_dir is not None
        text = self._runtime.container_logs(container_id)
        out = self._flight_logs_dir / realm_id / "logs"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{agent_id}.log").write_text(text, encoding="utf-8")
