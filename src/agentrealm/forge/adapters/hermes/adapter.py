"""Hermes runtime adapter (M2, ADR-001) — implements core.RuntimeAdapter.

Materializes an agent's HERMES_HOME (via the config renderer) into a named volume and runs
the pinned Hermes image on the realm's network. The whole config surface is set at
provision; after `start`, the agent is a black box (principle 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from agentrealm.core.interfaces import AgentArtifacts, AgentHandle, RealmContext
from agentrealm.core.schema import AgentSpec
from agentrealm.forge.adapters.hermes.config import (
    MatrixCreds,
    RealmtoolsCreds,
    render_hermes_home,
)
from agentrealm.forge.container import ContainerRuntime
from agentrealm.forge.skills import skill_files
from agentrealm.ledger import AgentCredential

HERMES_IMAGE = "hermes-agent:v2026.7.1"  # pinned (execution rule #4)
_HERMES_HOME = "/opt/data"


@dataclass
class HermesAdapter:
    """A RuntimeAdapter backed by a ContainerRuntime. Extra provisioning inputs (the agent's
    resolved credential + Matrix identity) are passed via `bind` since the RuntimeAdapter
    Protocol's provision(spec, realm) signature is intentionally minimal."""

    runtime: ContainerRuntime
    image: str = HERMES_IMAGE
    _creds: dict[str, AgentCredential] = field(default_factory=dict)
    _matrix: dict[str, MatrixCreds] = field(default_factory=dict)
    _ctx: dict[str, tuple[list[str], str | None, str | None, bool, dict[str, str]]] = field(
        default_factory=dict)
    _realmtools: dict[str, RealmtoolsCreds] = field(default_factory=dict)

    def bind(
        self,
        agent_id: str,
        cred: AgentCredential,
        matrix: MatrixCreds,
        *,
        roster: list[str] | None = None,
        guidelines: str | None = None,
        restrictions: str | None = None,
        realmtools: RealmtoolsCreds | None = None,
        allow_side_channels: bool = True,
        dm_rooms: dict[str, str] | None = None,
    ) -> None:
        """Supply the per-agent inputs the minimal provision() signature can't carry."""
        self._creds[agent_id] = cred
        self._matrix[agent_id] = matrix
        self._ctx[agent_id] = (roster or [], guidelines, restrictions, allow_side_channels,
                               dm_rooms or {})
        if realmtools is not None:
            self._realmtools[agent_id] = realmtools

    def provision(self, spec: AgentSpec, realm: RealmContext) -> AgentHandle:
        cred = self._creds[spec.id]
        matrix = self._matrix[spec.id]
        roster, guidelines, restrictions, side_channels, dm_rooms = self._ctx[spec.id]
        files = render_hermes_home(
            spec, cred, matrix, roster=roster, guidelines=guidelines, restrictions=restrictions,
            realmtools=self._realmtools.get(spec.id), allow_side_channels=side_channels,
            dm_rooms=dm_rooms, shared_folder=bool(realm.shared_folder),
        )
        # seed the role default (agent-basics / referee-basics) + any declared builtin skills
        files.update(skill_files(spec))
        # Namespaced under `realm-` like the container + shared volume, so the orphan reaper can
        # scan `realm-*` and never enumerate an operator's unrelated volumes (a bare
        # {realm}--{agent} name meant scanning ALL volumes and matching "contains --", which
        # force-removed things it did not own).
        volume = f"realm-{realm.realm_id}--{spec.id}"
        self.runtime.create_volume(volume)
        self.runtime.seed_volume(volume, files)

        mounts = {volume: _HERMES_HOME}
        if realm.shared_folder:
            mounts[realm.shared_folder] = "/realm/shared"
        # C11: only UID/GID need to be container env — the whole config surface is the volume.
        container = self.runtime.run_container(
            # host-safety caps (realm boundary): a runaway/adversarial agent can't exhaust the host.
            mem_limit="4g", pids_limit=512, nano_cpus=2_000_000_000,  # 2 CPUs
            name=f"realm-{realm.realm_id}-{spec.id}",
            image=self.image,
            network=realm.network,
            volumes=mounts,
            environment={"HERMES_UID": "10000", "HERMES_GID": "10000"},
            command=["gateway", "run"],
        )
        return AgentHandle(agent_id=spec.id, container_id=container, home_volume=volume)

    def start(self, handle: AgentHandle) -> None:
        # run_container already starts detached; nothing further to do for Hermes.
        return None

    def stop(self, handle: AgentHandle, grace: timedelta) -> None:
        if handle.container_id:
            self.runtime.stop_container(handle.container_id, timeout=int(grace.total_seconds()))

    def collect(self, handle: AgentHandle) -> AgentArtifacts:
        return AgentArtifacts(home_snapshot=handle.home_volume)
