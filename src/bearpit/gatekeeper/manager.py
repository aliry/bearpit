"""RealmManager — runs realms as background asyncio tasks for the API (§5, Gatekeeper).

`pit up` runs one realm and blocks; the API needs to start a realm and return immediately,
then let it run to conclusion in the background. The manager owns those tasks: start, list,
stop. It caps how many run at once — unbounded realms exhaust the host and take the model proxy
down with them (observed live). It reuses the shared Platform wiring so behavior matches the CLI.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field

from bearpit.chronicle import EventKind
from bearpit.core.schema import Project
from bearpit.gatekeeper.service import Platform, stop_flag_path


class CapacityError(RuntimeError):
    """Too many realms are already running."""


@dataclass
class RealmRun:
    realm_id: str
    task: asyncio.Task[None]
    report: str | None = None
    error: str | None = None


@dataclass
class RealmManager:
    platform: Platform
    max_active: int = 6  # host capacity guard — more than this starves the machine
    runs: dict[str, RealmRun] = field(default_factory=dict)

    def start(
        self,
        realm_id: str,
        project: Project,
        *,
        require_mention: bool = True,
        parameters: dict[str, str] | None = None,
        allow_provider_fallback: bool = False,
    ) -> None:
        """Launch a realm as a background task. Non-blocking; poll status via the Chronicle.
        Raises CapacityError if `max_active` realms are already running."""
        if realm_id in self.runs and not self.runs[realm_id].task.done():
            raise ValueError(f"realm {realm_id!r} is already running")
        if len(self.active()) >= self.max_active:
            raise CapacityError(
                f"{self.max_active} realms already running — stop one before starting another"
            )

        async def _run() -> None:
            try:
                result = await self.platform.run(
                    realm_id, project, require_mention=require_mention, parameters=parameters,
                    allow_provider_fallback=allow_provider_fallback,
                )
                self.runs[realm_id].report = result.report
            except Exception as exc:  # keep the failure on the run record + mark the realm failed
                self.runs[realm_id].error = f"{type(exc).__name__}: {exc}"
                with contextlib.suppress(Exception):
                    await self.platform.chronicle.append_event(
                        realm_id, EventKind.LIFECYCLE, {"event": "failed", "detail": str(exc)}
                    )

        # Clear any STALE stop flag from a prior run of this id BEFORE the task is scheduled — so a
        # fresh stop that races the launch cannot be silently deleted. Previously Platform.run did
        # this inside the background task, which had not run yet when start() returned; a stop
        # arriving in that window wrote the flag, then the task's unlink erased it, and the kill
        # switch was lost. Clearing it here, synchronously, closes the window.
        stop_flag_path(realm_id).unlink(missing_ok=True)
        task = asyncio.create_task(_run(), name=f"realm-{realm_id}")
        self.runs[realm_id] = RealmRun(realm_id=realm_id, task=task)

    def stop(self, realm_id: str) -> None:
        """Signal the kill switch; the running realm's snapshot picks it up and concludes."""
        stop_flag_path(realm_id).write_text("stop")

    def active(self) -> list[str]:
        return [rid for rid, run in self.runs.items() if not run.task.done()]

    async def shutdown(self) -> None:
        for run in self.runs.values():
            if not run.task.done():
                self.stop(run.realm_id)
        await asyncio.gather(
            *(run.task for run in self.runs.values()), return_exceptions=True
        )
