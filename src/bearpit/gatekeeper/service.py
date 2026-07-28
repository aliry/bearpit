"""Platform — wires the control-plane components and runs a realm. Shared by the CLI + API.

Everything the CLI's `up` did to stand up a realm, factored out so the API (and a RealmManager
for background runs) reuse the exact same wiring: control-plane clients on localhost, agent
creds carrying in-cluster URLs, the Realmtools MCP server attached when a mechanic/referee is
present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from bearpit.chronicle import Chronicle
from bearpit.core.providers import (
    pace_turns_for_provider,
    raise_budgets_for_flat_rate_provider,
    resolve_project,
)
from bearpit.core.runconfig import run_config
from bearpit.core.schema import Project, parse_duration
from bearpit.core.settings import Settings, load_settings
from bearpit.forge import DockerRuntime, Forge, RealmHandles, RealmtoolsConfig
from bearpit.gatekeeper.appstate import active_provider, providers_config
from bearpit.gatekeeper.runner import LiveSnapshot, Runner
from bearpit.herald import BusProvision, Herald, HttpMatrixClient
from bearpit.ledger import HttpLiteLLMClient, KeyStore, Ledger
from bearpit.warden import ConcludeResult, TurnManager, Warden


class ConfigError(RuntimeError):
    """A required runtime secret/config is missing."""


def stop_flag_path(realm_id: str) -> Path:
    d = Path.home() / ".bearpit" / "realms"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{realm_id}.stop"


def open_keystore() -> KeyStore:
    key = os.environ.get("BEARPIT_KEYSTORE_KEY")
    if not key:
        raise ConfigError("BEARPIT_KEYSTORE_KEY not set (Fernet key for the BYOK keystore)")
    path = Path.home() / ".bearpit" / "keystore.json"
    # 0700: this directory also holds realm stop-flags and flight logs. Default 0755 makes all of
    # it readable by every account on the machine for no benefit.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return KeyStore(key.encode(), path)


@dataclass
class Platform:
    settings: Settings
    chronicle: Chronicle
    runtime: DockerRuntime
    herald: Herald
    ledger: Ledger
    forge: Forge
    warden: Warden
    runner: Runner
    system_password: str

    async def run(
        self, realm_id: str, project: Project, *, require_mention: bool = True
    ) -> ConcludeResult:
        # Apply the active model-provider pipeline: resolve each agent's model_category to a real
        # model via the active provider's table, and pace turn windows for the slower pipeline.
        # Single launch chokepoint; manifests are never mutated on disk.
        provider = active_provider()
        providers = providers_config()
        project = resolve_project(project, provider, providers)
        project = pace_turns_for_provider(project, provider, providers)
        project = raise_budgets_for_flat_rate_provider(project, provider, providers)
        # NB: the stale-flag clear now happens in RealmManager.start(), synchronously, before
        # task is even scheduled — clearing it here (after start() returned) could erase a
        # fresh stop
        # that raced the launch.
        stop_path = stop_flag_path(realm_id)

        def factory(
            rid: str, handles: RealmHandles, bus: BusProvision, turns: TurnManager | None
        ) -> LiveSnapshot:
            return LiveSnapshot(
                herald=self.herald, ledger=self.ledger, chronicle=self.chronicle,
                runtime=self.runtime, realm_id=rid, commons_room=bus.commons_room,
                shared_volume=handles.shared_volume, stop_flag=stop_path.exists, turns=turns,
                side_channels=bus.side_channels, creds=bus.creds,
                dm_quota={a.id: a.private_messaging.max_per_round for a in project.agents
                          if a.private_messaging.max_per_round},
                containers={aid: h.container_id for aid, h in handles.agents.items()
                            if h.container_id},
                agent_tokens=handles.agent_tokens,
                budget_policy={
                    a.id: (str(a.budget.on_exhausted),
                           parse_duration(a.budget.grace_period)
                           if a.budget.grace_period else 0.0)
                    for a in project.agents
                },
            )

        return await self.runner.run(
            realm_id, project, factory,
            system_password=self.system_password, require_mention=require_mention,
            # snapshot what ACTUALLY runs — after model resolution, the turn floor, and the
            # flat-rate budget lift. The manifest is not what runs.
            run_config=run_config(project, provider, require_mention=require_mention),
        )

    async def close(self) -> None:
        await self.chronicle.close()


async def build_platform(settings: Settings | None = None) -> Platform:
    s = settings or load_settings()
    master_key = os.environ.get("LITELLM_MASTER_KEY")
    if not master_key:
        raise ConfigError("LITELLM_MASTER_KEY not set")
    system_pw = os.environ.get("BEARPIT_SYSTEM_PASSWORD")
    if not system_pw:
        raise ConfigError("BEARPIT_SYSTEM_PASSWORD not set (must be stable across runs)")

    chron = await Chronicle.connect(s.database_url, create=True)
    runtime = DockerRuntime()
    herald = Herald(
        HttpMatrixClient(s.matrix_homeserver),
        server_name=s.matrix_server_name, homeserver=s.matrix_homeserver_internal,
        operator=s.operator_user,
    )
    litellm = HttpLiteLLMClient(s.litellm_url, master_key)
    ledger = Ledger(open_keystore(), litellm, s.litellm_url_internal)
    rt_secret = os.environ.get("REALMTOOLS_SECRET")
    realmtools = (
        RealmtoolsConfig(
            url=s.realmtools_url_internal, secret=rt_secret, container=s.realmtools_container
        )
        if rt_secret
        else None
    )
    forge = Forge(
        runtime, ledger, realmtools=realmtools,
        # flight recorder: agent container logs survive teardown for post-mortems
        flight_logs_dir=Path.home() / ".bearpit" / "realms",
    )
    warden = Warden(forge, herald, chron)
    runner = Runner(
        herald, forge, warden, ledger, chron,
        attach_containers=(s.conduit_container, s.litellm_container),
    )
    return Platform(s, chron, runtime, herald, ledger, forge, warden, runner, system_pw)
