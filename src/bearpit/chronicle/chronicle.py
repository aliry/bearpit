"""The Chronicle — append-only event/message store + transcript & final report (M6, §14).

Every component writes here (Herald mirrors messages; Ledger writes spend; Warden writes
lifecycle; Arbiter writes score/verdict). The store is **append-only**: there is no update
or delete API, so a realm's history is always reconstructable — the #1 lesson of the
multi-agent failure literature (principle 7).
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from bearpit.chronicle.models import Base, Event, Message


def _now_ms() -> int:
    return int(time.time() * 1000)


class EventKind(StrEnum):
    """The well-known event kinds. `append_event` accepts any string, but using these keeps
    the store queryable and lets `final_report` aggregate."""

    LIFECYCLE = "lifecycle"  # realm/agent state changes
    SPEND = "spend"  # {agent, usd, tokens_in, tokens_out, model}
    SCORE = "score"  # {agent, delta, reason, issued_by}
    FILE = "file"  # shared-folder file event
    VIOLATION = "violation"  # referee flag
    VERDICT = "verdict"  # referee/outcome ruling {outcome, reasons} — the FINAL, realm-ending one
    # a per-round deterministic tally {round, ruleset, outcome, kind, detail}. Emphatically NOT a
    # VERDICT: `referee_verdict` termination fires on ANY verdict event, so recording a round tally
    # as one ENDED THE REALM the first time a referee scored a round — before it ever ruled.
    TALLY = "tally"
    SYSTEM = "system"  # announcements, injections
    TURN = "turn"  # turn-taking floor change {round, order, position, current}
    PRIVATE = "private"  # a queued private DM {from, to, text}; the host delivers it to Matrix
    # an agent's PRIVATE working note {agent, text} — its scratchpad/suspicion table. Only that
    # agent can read it back (`recall`); no other agent ever sees it. The operator does, because
    # everything is chronicled — which is exactly what makes an agent's real reasoning auditable.
    NOTE = "note"
    # an agent asked to run code IN ITS OWN CONTAINER {id, agent, code}. realmtools has no Docker
    # (deliberately — a socket there would turn any bug in it into host root); the HOST executes it,
    # exactly as it already brokers PRIVATE messages, and answers with EXEC_RESULT {id, exit, out}.
    EXEC = "exec"
    EXEC_RESULT = "exec_result"
    # an agent invoked a granted tool {id, agent, tool, args} (ADR-004). Same broker shape as EXEC
    # and for the same reason: realmtools holds no API keys, so the HOST — which holds the keystore
    # and the only internet route — performs the call and answers with TOOL_RESULT
    # {id, agent, tool, ok, result|error, cost_usd}. Both are chronicled, so what an agent looked
    # up (and what it cost) is part of the permanent record from day one.
    # a file the run produced {path, bytes, sha256} — or {path, missing: true} for a declared
    # deliverable that was never written (ADR-005). Metadata only: the bytes live beside the
    # flight logs, because an append-only store should not grow by the size of every artifact.
    OUTPUT = "output"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    # what agents were SHOWN: {version, tools:{name:{description,params,policy,...}}, grants}.
    # Realmtools describes a granted tool from this and nothing else, so no tool plugin has to be
    # installed in that container (#65). Descriptive, never authoritative — the signed token remains
    # the only thing that says what an agent may call. Its own kind, not run_config's `config`,
    # because `config` is served verbatim to a 2s console poll and read first-match.
    TOOL_MANIFEST = "tool_manifest"
    # a referee's `eliminate` tool call {agent|None, reason, issued_by}: agent None closes a round
    # with no ejection; a named agent is dropped from the turn rotation by the host (physics)
    ELIMINATION = "elimination"


class Chronicle:
    """Async handle to the append-only store. Construct via `connect`."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sf = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    async def connect(cls, url: str, *, create: bool = True) -> Chronicle:
        """Open the store at a SQLAlchemy async URL (postgresql+asyncpg / sqlite+aiosqlite)."""
        engine = create_async_engine(url)
        chron = cls(engine)
        if create:
            await chron.create_schema()
        return chron

    async def create_schema(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self._engine.dispose()

    # --- append (the only writes) --------------------------------------------
    async def append_event(
        self,
        realm_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        ts_ms: int | None = None,
    ) -> int:
        async with self._sf() as s:
            ev = Event(
                realm_id=realm_id, kind=kind, payload=payload or {}, ts_ms=ts_ms or _now_ms()
            )
            s.add(ev)
            await s.commit()
            return ev.id

    async def record_message(
        self,
        realm_id: str,
        channel: str,
        sender: str,
        body: str,
        attachments: Iterable[str] = (),
        ts_ms: int | None = None,
    ) -> int:
        async with self._sf() as s:
            m = Message(
                realm_id=realm_id, channel=channel, sender=sender, body=body,
                attachments=list(attachments), ts_ms=ts_ms or _now_ms(),
            )
            s.add(m)
            await s.commit()
            return m.id

    # --- read ----------------------------------------------------------------
    async def events(self, realm_id: str, kind: str | None = None) -> Sequence[Event]:
        q = select(Event).where(Event.realm_id == realm_id).order_by(Event.ts_ms, Event.id)
        if kind is not None:
            q = q.where(Event.kind == kind)
        async with self._sf() as s:
            return list((await s.scalars(q)).all())

    async def messages(self, realm_id: str, channel: str | None = None) -> Sequence[Message]:
        q = select(Message).where(Message.realm_id == realm_id).order_by(Message.ts_ms, Message.id)
        if channel is not None:
            q = q.where(Message.channel == channel)
        async with self._sf() as s:
            return list((await s.scalars(q)).all())

    async def realms(self) -> list[str]:
        """Distinct realm ids that have any chronicled events (most-recent first)."""
        q = select(Event.realm_id, func.max(Event.ts_ms).label("t")).group_by(
            Event.realm_id
        ).order_by(func.max(Event.ts_ms).desc())
        async with self._sf() as s:
            return [row[0] for row in (await s.execute(q)).all()]

    # --- exports -------------------------------------------------------------
    async def transcript(self, realm_id: str) -> str:
        """Chronological plain-text transcript of all messages in the realm."""
        return "\n".join(
            f"[{m.ts_ms}] {m.channel} · {m.sender}: {m.body}"
            for m in await self.messages(realm_id)
        )

    async def final_report(self, realm_id: str, title: str | None = None) -> str:
        """Markdown summary aggregated from the event stream: outcome, scores, spend, size."""
        evs = await self.events(realm_id)
        msgs = await self.messages(realm_id)

        spend: dict[str, float] = {}
        # agent -> (calls, usd). Tool spend is reported SEPARATELY from model spend, not folded
        # into it: the per-agent budget is a proxy key that meters models and cannot see a tool's
        # bill, so one combined number would imply an enforcement that does not exist (ADR-004 §6).
        tools: dict[str, tuple[int, float]] = {}
        score: dict[str, float] = {}
        violations: list[str] = []
        outcome: str | None = None
        concluded: tuple[str, str] | None = None  # (reason, detail) for non-verdict endings
        for e in evs:
            p = e.payload
            if e.kind == EventKind.SPEND and "agent" in p:
                spend[p["agent"]] = spend.get(p["agent"], 0.0) + float(p.get("usd", 0))
            elif e.kind == EventKind.TOOL_RESULT and "agent" in p:
                n, usd = tools.get(p["agent"], (0, 0.0))
                tools[p["agent"]] = (n + 1, usd + float(p.get("cost_usd", 0) or 0))
            elif e.kind == EventKind.SCORE and "agent" in p:
                score[p["agent"]] = score.get(p["agent"], 0.0) + float(p.get("delta", 0))
            elif e.kind == EventKind.VIOLATION and "agent" in p:
                violations.append(f"{p['agent']}: {p.get('reason', '')}")
            elif e.kind == EventKind.VERDICT:
                outcome = str(p.get("outcome", outcome))
            elif e.kind == EventKind.LIFECYCLE and p.get("event") == "concluding":
                concluded = (str(p.get("reason", "")), str(p.get("detail", "")))

        heading = title or f"Realm {realm_id}"
        # A referee verdict is the outcome when there is one; otherwise report HOW the realm ended
        # (file/message/duration/budget) — collaborative realms have no winner, just a conclusion.
        if outcome is not None:
            headline = f"**Outcome:** {outcome}"
        elif concluded is not None:
            reason, detail = concluded
            headline = f"**Concluded:** {reason}" + (f" — {detail}" if detail else "")
        else:
            headline = "**Outcome:** —"
        lines = [f"# {heading} — final report", "", headline, ""]
        if score:
            lines.append("## Scores")
            lines += [f"- {a}: {v:g}" for a, v in sorted(score.items(), key=lambda x: -x[1])]
            lines.append("")
        if violations:
            lines.append("## Violations")
            lines += [f"- {v}" for v in violations]
            lines.append("")
        lines.append("## Spend")
        if spend:
            lines += [f"- {a}: ${v:.4f}" for a, v in sorted(spend.items())]
            lines.append(f"- **total: ${sum(spend.values()):.4f}**")
        else:
            lines.append("- (no spend recorded)")
        if tools:
            lines += ["", "## Tool use"]
            lines += [f"- {a}: {n} call{'s' if n != 1 else ''}"
                      + (f", ${usd:.4f}" if usd else "")
                      for a, (n, usd) in sorted(tools.items())]
            total = sum(usd for _, usd in tools.values())
            if total:
                lines.append(f"- **total: ${total:.4f}**")
        lines += ["", f"_{len(msgs)} messages, {len(evs)} events chronicled._"]
        return "\n".join(lines)
