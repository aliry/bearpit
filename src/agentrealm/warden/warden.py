"""Warden — lifecycle + termination engine (M5, §11).

Owns the concluding sequence and the kill switch. `watch` polls a snapshot provider,
evaluates termination, and on the first match runs `conclude`: announce REALM_ENDING → a
grace period → actively STOP the agents (Forge teardown — the POC showed announcing the end
is not enough; the platform must stop containers) → archive → generate the final report.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta

from agentrealm.chronicle import Chronicle, EventKind
from agentrealm.core.schema import TerminationCondition, TerminationKind
from agentrealm.forge import Forge, RealmHandles
from agentrealm.herald import Herald
from agentrealm.warden.termination import (
    RealmSnapshot,
    TerminationFired,
    evaluate_termination,
)


@dataclass(frozen=True)
class ConcludeResult:
    fired: TerminationFired
    report: str


SnapshotProvider = Callable[[], Awaitable[RealmSnapshot]]


# consecutive snapshot failures the watch loop rides out before it concludes the realm
# as wedged (a Colima/LiteLLM blip is transient; a persistent failure means the realm is
# stuck and its containers must be torn down rather than left running unsupervised).
_MAX_TICK_ERRORS = 5
_log = logging.getLogger(__name__)


class Warden:
    def __init__(self, forge: Forge, herald: Herald, chronicle: Chronicle) -> None:
        self._forge = forge
        self._herald = herald
        self._chron = chronicle

    async def conclude(
        self,
        realm_id: str,
        handles: RealmHandles,
        commons_room: str,
        fired: TerminationFired,
        *,
        grace: timedelta = timedelta(seconds=10),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_conclude: Callable[[], Awaitable[None]] | None = None,
    ) -> ConcludeResult:
        await self._chron.append_event(
            realm_id, EventKind.LIFECYCLE,
            {"event": "concluding", "reason": fired.kind, "detail": fired.detail},
        )
        # Lift any turn gate FIRST so the wrap-up isn't strangled — agents must be able to post
        # their final outputs and the referee to announce during the grace period.
        if on_conclude is not None:
            await on_conclude()
        await self._herald.announce(
            commons_room, f"REALM_ENDING — {fired.kind}: {fired.detail}. Stand down."
        )
        await sleep(grace.total_seconds())  # let agents finalize outputs / referee score
        # Warden actively stops the agents — announcing the end is not enough (POC finding).
        # Teardown is best-effort; the archive + report must happen regardless (the run's value
        # is in the Chronicle, not the containers).
        try:
            await self._forge.teardown_realm(handles, grace=grace)
        except Exception as exc:  # never lose the report to a cleanup failure
            await self._chron.append_event(
                realm_id, EventKind.LIFECYCLE, {"event": "teardown_error", "detail": str(exc)}
            )
        await self._chron.append_event(realm_id, EventKind.LIFECYCLE, {"event": "archived"})
        report = await self._chron.final_report(realm_id, title=f"Realm {realm_id}")
        return ConcludeResult(fired=fired, report=report)

    async def watch(
        self,
        realm_id: str,
        handles: RealmHandles,
        commons_room: str,
        conditions: list[TerminationCondition],
        snapshot: SnapshotProvider,
        *,
        interval_s: float = 5.0,
        max_ticks: int | None = None,
        grace: timedelta = timedelta(seconds=10),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        nudge: Callable[[], Awaitable[None]] | None = None,
        stall_after_s: float = 150.0,
        max_nudges: int = 4,
        on_conclude: Callable[[], Awaitable[None]] | None = None,
    ) -> ConcludeResult:
        """Poll until a termination condition fires, then conclude. `max_ticks` bounds the loop
        (defensive); a manual stop in a snapshot ends it even if `manual` wasn't declared.

        If `nudge` is given, re-address the realm after `stall_after_s` of no new messages
        (mini-model coordination stalls) — up to `max_nudges` times, then let it time out."""
        tick = 0
        last_progress: tuple[int, int, float] | None = None
        silent_ticks = 0
        nudges = 0
        stall_ticks = max(1, int(stall_after_s / interval_s)) if interval_s > 0 else 1
        consec_errors = 0
        while max_ticks is None or tick < max_ticks:
            try:
                snap = await snapshot()
            except Exception as exc:  # noqa: BLE001 - one tick's hiccup must not kill the loop
                # A transient proxy/bus/Docker error in the snapshot must NOT tear the whole realm
                # down: that would leave every agent container running, unsupervised, with a live
                # model key. Ride out a few in a row (they are usually a Colima/LiteLLM blip), but
                # if the failures persist the realm is genuinely wedged — conclude it
                # deterministically rather than spin forever, so its containers are torn down.
                consec_errors += 1
                _log.warning("watch tick failed for %s (%d in a row): %s",
                                  realm_id, consec_errors, exc)
                if consec_errors >= _MAX_TICK_ERRORS:
                    return await self.conclude(
                        realm_id, handles, commons_room,
                        TerminationFired(TerminationKind.MANUAL, f"watch loop wedged: {exc}"),
                        grace=grace, sleep=sleep, on_conclude=on_conclude,
                    )
                tick += 1
                await sleep(interval_s)
                continue
            consec_errors = 0
            fired = evaluate_termination(conditions, snap)
            if fired is not None:
                return await self.conclude(
                    realm_id, handles, commons_room, fired, grace=grace, sleep=sleep,
                    on_conclude=on_conclude,
                )
            # Progress is ANY forward motion — new messages, new shared-folder files, or new spend
            # — not just commons chatter, so a quiet-but-working realm (a build, an analysis) is
            # not falsely nudged as stalled (generic across scenario shapes).
            progress = (len(snap.messages), len(snap.files),
                        round(sum(s for s, _ in snap.spend.values()), 6))
            if progress != last_progress:
                last_progress, silent_ticks = progress, 0
            else:
                silent_ticks += 1
                if nudge is not None and nudges < max_nudges and silent_ticks >= stall_ticks:
                    await self._chron.append_event(
                        realm_id, EventKind.SYSTEM, {"event": "nudge", "reason": "stalled"}
                    )
                    await nudge()
                    nudges += 1
                    silent_ticks = 0
            tick += 1
            await sleep(interval_s)
        # defensive fallback: the watch budget was exhausted without a match
        fired = TerminationFired(TerminationKind.MANUAL, "watch budget exhausted")
        return await self.conclude(
            realm_id, handles, commons_room, fired, grace=grace, sleep=sleep,
            on_conclude=on_conclude,
        )
