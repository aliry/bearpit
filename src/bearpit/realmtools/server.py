"""Realmtools MCP server (#39) — exposes the sealed-submit mechanic to agents over MCP.

A streamable-HTTP MCP server (the transport Hermes uses) serving four tools backed by the
EscrowService. Identity is read per call from the request's bearer token (never a tool
argument), so an agent submits only as itself and only the referee can reveal/tally. Runs as a
service container attached to each realm network; agents reach it at
`http://pit-realmtools:9100/mcp` with their minted token.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.prompts import Prompt
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool as MCPTool
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from bearpit.chronicle import Chronicle
from bearpit.forge.skills import BUILTIN_SKILLS
from bearpit.realmtools.arbiter import ArbiterService
from bearpit.realmtools.code import CodeService
from bearpit.realmtools.manifest import ManifestReader
from bearpit.realmtools.notes import NoteService
from bearpit.realmtools.private import PrivateMessageService
from bearpit.realmtools.service import (
    EscrowService,
    Identity,
    SealedError,
    TallyError,
    TurnReader,
)
from bearpit.realmtools.tokens import verify_token
from bearpit.realmtools.toolcall import ToolCallService

ToolContext = Context[Any, Any, Any]

# The server is reached by container name on an isolated realm network, so the SDK's default
# DNS-rebinding protection (localhost-only Host) rejects agents with 421 — disable it here.
_TRANSPORT = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# Per-call audit trail on stdout (docker logs) — who called which tool and how it ended. The
# chronicle records EFFECTS (events); this records ATTEMPTS, incl. auth failures and errors that
# never produce an event — exactly what post-mortems were missing.
_audit_log = logging.getLogger("realmtools.audit")


def _result_shape(result: Any) -> str:
    """A CONTENT-FREE summary of what a tool returned.

    Never log the payloads themselves: `reveal` hands back the sealed submissions, and the whole
    security property of sealed-submit is that a move stays hidden until the referee reveals it.
    Dumping it to stdout would leak every player's secret move to anyone tailing `docker logs`
    (and in a competitive realm, before the round even resolves).

    The shape is all the diagnostics ever needed anyway: the bug this was built for was "did
    `reveal` return nothing, or was it never called at all?" — which `n=2` vs `n=0` answers exactly,
    without disclosing 'mango'."""
    if result is None:
        return "-"
    if isinstance(result, dict):
        return f"n={len(result)} keys={sorted(result)!r}" if result else "n=0 (empty)"
    if isinstance(result, (list, tuple, set)):
        return f"n={len(result)}"
    return "ok"


def _audit(
    tool: str, ident: Identity | None, error: str | None = None, result: Any = None
) -> None:
    who_s = f"{ident.realm_id}/{ident.agent_id}" if ident else "unauthenticated"
    if error is None:
        _audit_log.info("tool=%s by=%s ok %s", tool, who_s, _result_shape(result))
    else:
        _audit_log.warning("tool=%s by=%s error=%s", tool, who_s, error)


def _identity(ctx: ToolContext, secret: str) -> Identity | None:
    rc = getattr(ctx, "request_context", None)
    request = getattr(rc, "request", None) if rc else None
    if request is None:
        return None
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    verified = verify_token(token, secret) if token else None
    return Identity(*verified) if verified else None


def _ctx_of(mcp: FastMCP) -> Any:
    """The current request's context, or None outside a request."""
    try:
        return mcp.get_context()
    except Exception:  # noqa: BLE001 - listing outside a request is not an error, just anonymous
        return None


def build_app(
    secret: str, *, chronicle: Chronicle | None = None, db_url: str | None = None
) -> Starlette:
    service = EscrowService(chronicle, secret=secret)
    arbiter = ArbiterService(chronicle)
    turns = TurnReader(chronicle)
    private = PrivateMessageService(chronicle)
    notes = NoteService(chronicle)
    coder = CodeService(chronicle)
    manifests = ManifestReader(chronicle)
    granted = ToolCallService(chronicle, manifests=manifests)

    def _wire(chron: Chronicle) -> None:
        for svc in (service, arbiter, turns, private, notes, coder, granted, manifests):
            svc.set_chronicle(chron)

    # NOT FastMCP's own lifespan. That one runs inside `app.run()`, which the streamable-http
    # manager calls PER SESSION — so connecting there built a new SQLAlchemy engine, and therefore
    # a new connection pool, for every agent session. Live that reached 206 new connections in an
    # hour under five concurrent realms and exhausted Postgres `max_connections`, taking down every
    # realm at once. It was also a correctness bug: these services are process-global, so each
    # session REPLACED the chronicle the others held, and one session ending could close the
    # connection another realm was mid-write on.
    #
    # Starlette's lifespan runs once per process and still inside the serving loop, which is the
    # property the old comment was reaching for.
    mcp: FastMCP = FastMCP("realmtools", transport_security=_TRANSPORT)

    # Serve the builtin skills as MCP prompts. Agents (Hermes maps skills<->MCP prompts) DO ask
    # for these by name — an unregistered name made FastMCP raise `ValueError: Unknown prompt`
    # server-side (observed live: get_prompt("referee-basics")). Registering them turns that
    # crash into a useful answer: the canonical skill text.
    def _skill_prompt(text: str) -> Callable[[], str]:
        def prompt() -> str:
            return text
        return prompt

    for skill_name, skill_text in BUILTIN_SKILLS.items():
        mcp.add_prompt(Prompt.from_function(
            _skill_prompt(skill_text), name=skill_name,
            description=f"The builtin '{skill_name}' skill, verbatim.",
        ))

    def who(ctx: ToolContext) -> Identity:
        ident = _identity(ctx, secret)
        if ident is None:
            _audit("(auth)", None, "no valid realmtools token on this request")
            raise PermissionError("no valid realmtools token on this request")
        return ident

    @mcp.tool()
    async def submit_sealed(round: str, payload: str, ctx: ToolContext) -> str:
        """Submit a hidden entry for a labeled round. Nobody — not even the referee — can see it
        until the referee reveals the round, and it cannot be changed once sealed.

        This is the ONLY way to submit something privately and simultaneously. Whatever the entry
        represents in this realm, your instructions say; use the exact round label they give you."""
        ident = _identity(ctx, secret)
        try:
            res = await service.submit(who(ctx), round, payload)
            _audit(f"submit_sealed(round={round!r})", ident)
            return res
        except (SealedError, PermissionError) as exc:
            _audit(f"submit_sealed(round={round!r})", ident, str(exc))
            return f"error: {exc}"

    @mcp.tool()
    async def reveal_status(round: str, ctx: ToolContext) -> dict[str, Any]:
        """See WHO has sealed a round — never WHAT. Returns {submitted, pending}.

        A referee should call this before `reveal`, every time: reveal is a one-way door."""
        ident = _identity(ctx, secret)
        try:
            res = dict(await service.status(who(ctx), round))
            _audit(f"reveal_status(round={round!r})", ident, result=res)
            return res
        except PermissionError as exc:
            _audit(f"reveal_status(round={round!r})", ident, str(exc))
            return {"error": str(exc)}

    @mcp.tool()
    async def turn_status(ctx: ToolContext) -> dict[str, Any]:
        """If this realm runs turns: whose turn it is now, the full order, who has already had the
        floor this round, and who is still to come. The system drives turns — you only read here.

        Two round numbers, and referees must not confuse them:
          `round`                 — the round now OPEN (players are speaking in it)
          `last_completed_round`  — the round that just FINISHED, i.e. the one you RESOLVE.
        When you are cued at a boundary, the round to reveal and score is `last_completed_round`.
        Returns {"active": false} when turns are off."""
        try:
            return dict(await turns.status(who(ctx).realm_id))
        except PermissionError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def send_private(to: str, message: str, ctx: ToolContext) -> str:
        """Send a PRIVATE 1:1 message to another agent by their id (e.g. "scout"). Only the two of
        you see it — it never appears in the Commons. You can only privately message peers you share
        a private channel with; if you don't, this returns an error. Prefer this tool over trying to
        post into a private room yourself."""
        try:
            return await private.send(who(ctx), to, message)
        except (ValueError, PermissionError) as exc:
            return f"error: {exc}"

    @mcp.tool()
    async def run_code(code: str, ctx: ToolContext) -> dict[str, Any]:
        """Run Python in YOUR OWN container and get back whatever you print.

        Use it whenever something must be EXACT rather than estimated: counting, tallying,
        comparing, checking a rule, parsing or cross-referencing data. Never do bookkeeping or
        arithmetic in your head when you can compute it here. If the realm has a shared folder it
        is at /realm/shared, and this is the only way to read or write it."""
        ident = _identity(ctx, secret)
        try:
            res = await coder.run(who(ctx), code)
            _audit("run_code", ident, result=res)  # never the code itself: it is the agent's own
            return res
        except (ValueError, PermissionError) as exc:
            _audit("run_code", ident, str(exc))
            return {"error": str(exc)}

    @mcp.tool()
    async def remember(note: str, ctx: ToolContext) -> str:
        """Write a PRIVATE note to yourself. Only you can ever read it back.

        You start every reply with NO memory of the last one, so anything you do not write down
        here is gone. Keep whatever you will need again: what you have already done, what others
        claimed, what you concluded and on what evidence."""
        ident = _identity(ctx, secret)
        try:
            res = await notes.remember(who(ctx), note)
            _audit("remember", ident)  # NEVER the note text: it is the agent's private reasoning
            return res
        except (ValueError, PermissionError) as exc:
            _audit("remember", ident, str(exc))
            return f"error: {exc}"

    @mcp.tool()
    async def recall(ctx: ToolContext) -> dict[str, Any]:
        """Read back everything you have privately noted, oldest first. Do this FIRST, before you
        act or speak — it is the only memory you have of what came before."""
        ident = _identity(ctx, secret)
        try:
            res = await notes.recall(who(ctx))
            _audit("recall", ident, result=res)
            return {"notes": res}
        except PermissionError as exc:
            _audit("recall", ident, str(exc))
            return {"error": str(exc)}

    @mcp.tool()
    async def reveal(round: str, ctx: ToolContext) -> dict[str, Any]:
        """Referee only: unseal every entry for a round, all at once.

        A ONE-WAY DOOR: the round closes, and anyone who has not sealed can never seal it. Call
        reveal_status first to see who is still pending."""
        ident = _identity(ctx, secret)
        try:
            res = dict(await service.reveal(who(ctx), round))
            _audit(f"reveal(round={round!r})", ident, result=res)
            return res
        except (SealedError, PermissionError) as exc:
            _audit(f"reveal(round={round!r})", ident, str(exc))
            return {"error": str(exc)}

    @mcp.tool()
    async def tally(
        round: str, ruleset: str, ctx: ToolContext, config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Referee only: reveal + score ONE round deterministically by a ruleset (dominance,
        high-bid, low-bid, plurality, majority, unanimous). Records the round's tally.

        For `dominance` (a rock-paper-scissors-shaped game), pass the beat relation yourself in
        `config={'beats': {'<token>': ['<tokens it beats>'], ...}}` — the platform ships no game's
        rules, so YOU supply them. e.g. config={'beats': {'rock':['scissors'],'scissors':['paper'],
        'paper':['rock']}}.

        This scores ONE round. It does NOT end the realm and it is NOT your final verdict — only
        `rule(outcome, reasons)` ends anything. Tally as many rounds as the realm has."""
        try:
            return await service.tally(who(ctx), round, ruleset, config)
        except (SealedError, TallyError, PermissionError) as exc:
            return {"error": str(exc)}

    # --- Arbiter: referee scoring + verdicts (the platform keeps the running score) ----------
    @mcp.tool()
    async def score(agent: str, delta: float, reason: str, ctx: ToolContext) -> dict[str, Any]:
        """Referee only: add (delta>0) or subtract (delta<0) points for an agent, with a reason.
        Returns the running totals. The platform holds the tally — use this and `scoreboard`
        rather than keeping numbers in your head, which drift."""
        ident = _identity(ctx, secret)
        try:
            res = {"scoreboard": await arbiter.score(who(ctx), agent, delta, reason)}
            _audit(f"score({agent!r},{delta})", ident, result=res)
            return res
        except PermissionError as exc:
            _audit(f"score({agent!r},{delta})", ident, str(exc))
            return {"error": str(exc)}

    @mcp.tool()
    async def penalize(agent: str, amount: float, reason: str, ctx: ToolContext) -> dict[str, Any]:
        """Referee only: record a rule violation AND subtract `amount` points for it."""
        try:
            return {"scoreboard": await arbiter.penalize(who(ctx), agent, amount, reason)}
        except PermissionError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def flag(agent: str, reason: str, ctx: ToolContext) -> dict[str, Any]:
        """Referee only: record a rule violation against an agent (no score change)."""
        try:
            return await arbiter.flag(who(ctx), agent, reason)
        except PermissionError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def scoreboard(ctx: ToolContext) -> dict[str, Any]:
        """The authoritative running totals the platform holds for this realm. Read them; never
        reconstruct them from memory."""
        try:
            return {"scoreboard": await arbiter.scoreboard(who(ctx))}
        except PermissionError as exc:
            return {"error": str(exc)}

    @mcp.tool()
    async def eliminate(agent: str, reason: str, ctx: ToolContext) -> dict[str, Any]:
        """Referee only: REMOVE a participant from the session. The system drops them from the
        turn rotation immediately — they can no longer act. Pass their exact agent id; pass 'none'
        to close the round with nobody removed.

        Only this call removes anyone: saying it in chat changes nothing. What removal MEANS here —
        an ejection, a disqualification, a resignation, a firing — is defined by your rubric."""
        ident = _identity(ctx, secret)
        try:
            res = await arbiter.eliminate(who(ctx), agent, reason)
            _audit(f"eliminate({agent!r})", ident, result=res)
            return res
        except PermissionError as exc:
            _audit(f"eliminate({agent!r})", ident, str(exc))
            return {"error": str(exc)}

    @mcp.tool()
    async def rule(outcome: str, reasons: str, ctx: ToolContext) -> dict[str, Any]:
        """Referee only: record the FINAL outcome of this realm, with your reasons.

        This is the only DECIDED ending: if your powers allow it, the realm concludes the moment you
        call it. Announcing an outcome in chat ends nothing. Call it once, when your rubric's ending
        condition is actually met."""
        ident = _identity(ctx, secret)
        try:
            res = await arbiter.rule(who(ctx), outcome, reasons)
            _audit(f"rule({outcome!r})", ident, result=res)
            return res
        except PermissionError as exc:
            _audit(f"rule({outcome!r})", ident, str(exc))
            return {"error": str(exc)}

    # --- granted tools (ADR-004) ---------------------------------------------------------------
    # Served entirely from the per-realm manifest the host wrote, and never registered in FastMCP's
    # tool manager. That is what lets this container hold no tool plugins: no third-party package,
    # no third-party dependency tree, no third-party import-time code in the agent-facing server.
    #
    # It also removes a hazard that per-realm registration would have created — the tool manager is
    # process-global and first-writer-wins, so two realms granting one name would have shared
    # whichever schema was registered first.
    _base_list_tools = mcp.list_tools

    async def _caller() -> Identity | None:
        return _identity(_ctx_of(mcp), secret)

    async def list_tools_for_caller() -> list[MCPTool]:
        """Built-in verbs, plus exactly the granted tools this caller holds.

        Grants come from the signed token; descriptions come from the manifest. #51 established
        that hiding a tool is not a security control — a hidden tool still executes when named —
        so this exists to stop an agent wasting a turn (#41), and `ToolCallService` enforces.
        """
        listed = list(await _base_list_tools())
        ident = await _caller()
        if ident is None:
            return listed
        for name in ident.grants:
            described = await manifests.describe(ident.realm_id, name)
            listed.append(MCPTool(
                name=name,
                description=str(described.get("description") or f"The {name} tool."),
                inputSchema=described.get("params") or {"type": "object"},
            ))
        return listed

    async def call_tool_for_caller(name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch: a granted tool goes to the broker, anything else to FastMCP's own handler."""
        ident = await _caller()
        if ident is not None and name in ident.grants:
            try:
                result = await granted.call(ident, name, dict(arguments or {}))
            except (PermissionError, ValueError) as exc:
                _audit(f"{name}()", ident, str(exc))
                return {"error": str(exc)}
            _audit(f"{name}()", ident, result=result)
            return result
        # Not granted (or no identity): fall through. A caller naming a tool it does not hold
        # lands here and gets "Unknown tool", which is the same answer as a typo — correct, since
        # it must not learn from the error message whether the tool exists.
        return await mcp.call_tool(name, arguments)

    mcp._mcp_server.list_tools()(list_tools_for_caller)  # type: ignore[no-untyped-call]
    mcp._mcp_server.call_tool(validate_input=False)(call_tool_for_caller)

    app: Starlette = mcp.streamable_http_app()  # serves the MCP endpoint at /mcp

    if chronicle is None and db_url is not None:
        inner = app.router.lifespan_context

        @asynccontextmanager
        async def with_chronicle(a: Starlette) -> AsyncIterator[None]:
            chron = await Chronicle.connect(db_url, create=True)
            _wire(chron)
            try:
                async with inner(a):
                    yield
            finally:
                await chron.close()

        app.router.lifespan_context = with_chronicle
    app.add_route("/health", lambda _r: JSONResponse({"ok": True}), methods=["GET"])
    app.state.mcp = mcp  # exposed for tests/diagnostics (prompt + tool registration)
    return app


# Short enough to type, long enough that a hand-picked value is not brute-forceable.
_MIN_SECRET_LEN = 32


def main() -> None:  # pragma: no cover - process entry point, exercised live
    import uvicorn

    secret = os.environ["REALMTOOLS_SECRET"]
    if len(secret) < _MIN_SECRET_LEN:
        raise SystemExit(
            f"REALMTOOLS_SECRET must be at least {_MIN_SECRET_LEN} characters — it is the HMAC key "
            "behind every agent identity token AND the material the submission seal is derived "
            "from. Generate one with: openssl rand -hex 32"
        )
    # No default: a hardcoded fallback DSN silently ignores whatever password the operator set,
    # and reverts them to a weak one they thought they had replaced.
    db_url = os.environ["BEARPIT_DATABASE_URL"]
    port = int(os.environ.get("REALMTOOLS_PORT", "9100"))
    app = build_app(secret, db_url=db_url)  # Chronicle connects in the serving loop (lifespan)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
