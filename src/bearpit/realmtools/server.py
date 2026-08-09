"""Realmtools MCP server (#39) — exposes the sealed-submit mechanic to agents over MCP.

A streamable-HTTP MCP server (the transport Hermes uses) serving four tools backed by the
EscrowService. Identity is read per call from the request's bearer token (never a tool
argument), so an agent submits only as itself and only the referee can reveal/tally. Runs as a
service container attached to each realm network; agents reach it at
`http://pit-realmtools:9100/mcp` with their minted token.
"""

from __future__ import annotations

import inspect
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
from bearpit.core.tools import ToolProfile, tool_registry
from bearpit.forge.skills import BUILTIN_SKILLS
from bearpit.realmtools.arbiter import ArbiterService
from bearpit.realmtools.code import CodeService
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


# JSON Schema -> Python annotation, for the synthesised signatures below. Anything unrecognised
# stays `Any`, which advertises an unconstrained value rather than guessing wrong.
_JSON_PY: dict[str, Any] = {
    "string": str, "integer": int, "number": float,
    "boolean": bool, "array": list, "object": dict,
}


def _ctx_of(mcp: FastMCP) -> Any:
    """The current request's context, or None outside a request."""
    try:
        return mcp.get_context()
    except Exception:  # noqa: BLE001 - listing outside a request is not an error, just anonymous
        return None


def _granted_tool_body(profile: ToolProfile, granted: ToolCallService, secret: str) -> Any:
    """An MCP tool that records an intent for the host to perform.

    The signature is built from the profile's own JSON Schema rather than written by hand, because
    FastMCP derives the advertised schema from the signature — so this is what makes an agent see
    real, typed, individually-documented parameters (`query`, `count`) instead of one opaque
    `args` blob. Verified against the SDK before being relied on; a nested-blob call works too,
    and reads far worse to a model.
    """
    props: dict[str, Any] = profile.params.get("properties", {}) or {}
    required = set(profile.params.get("required", []) or [])
    params = [
        inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY,
            annotation=_JSON_PY.get(str(spec.get("type", "")), Any),
            default=inspect.Parameter.empty if name in required else None,
        )
        for name, spec in props.items()
    ]

    async def body(**kwargs: Any) -> dict[str, Any]:
        ctx = _ctx_of(granted_mcp[0]) if granted_mcp else None
        ident = _identity(ctx, secret) if ctx is not None else None
        if ident is None:
            _audit(f"{profile.name}()", None, "no valid realmtools token on this request")
            return {"error": "no valid realmtools token on this request"}
        args = {k: v for k, v in kwargs.items() if v is not None}
        try:
            result = await granted.call(ident, profile.name, args)
        except (PermissionError, ValueError) as exc:
            _audit(f"{profile.name}()", ident, str(exc))
            return {"error": str(exc)}
        _audit(f"{profile.name}()", ident, result=result)
        return result

    body.__signature__ = inspect.Signature(params)  # type: ignore[attr-defined]
    body.__annotations__ = {p.name: p.annotation for p in params}
    body.__doc__ = profile.description
    return body


# Set by build_app so a tool body can reach the live FastMCP for its request context. One server
# per process; a list rather than a bare global so the closure sees assignment.
granted_mcp: list[FastMCP] = []


def build_app(
    secret: str, *, chronicle: Chronicle | None = None, db_url: str | None = None
) -> Starlette:
    service = EscrowService(chronicle, secret=secret)
    arbiter = ArbiterService(chronicle)
    turns = TurnReader(chronicle)
    private = PrivateMessageService(chronicle)
    notes = NoteService(chronicle)
    coder = CodeService(chronicle)
    granted = ToolCallService(chronicle)

    lifespan = None
    if chronicle is None and db_url is not None:
        @asynccontextmanager
        async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
            # connect the Chronicle INSIDE the serving loop so its async engine binds correctly
            chron = await Chronicle.connect(db_url, create=True)
            service.set_chronicle(chron)
            arbiter.set_chronicle(chron)
            turns.set_chronicle(chron)
            private.set_chronicle(chron)
            notes.set_chronicle(chron)
            coder.set_chronicle(chron)
            granted.set_chronicle(chron)
            try:
                yield {}
            finally:
                await chron.close()

    mcp: FastMCP = (
        FastMCP("realmtools", lifespan=lifespan, transport_security=_TRANSPORT)
        if lifespan is not None
        else FastMCP("realmtools", transport_security=_TRANSPORT)
    )

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
    # Registered from the tool REGISTRY's metadata only. The profile's `handler` is never called
    # here and must not be: it needs the keystore and the internet, and this server is deliberately
    # given neither. All these bodies do is record the intent for the host to perform.
    granted_mcp.clear()
    granted_mcp.append(mcp)
    for profile in tool_registry().values():
        mcp.add_tool(
            _granted_tool_body(profile, granted, secret),
            name=profile.name, description=profile.description,
        )

    # An agent sees only the tools it holds. #51 established this is a UX control, not a security
    # one — a hidden tool still executes when named — so the grant is ALSO checked in the body
    # above. This exists so an agent does not waste a turn discovering a tool it cannot use (#41).
    _base_list_tools = mcp.list_tools

    async def list_tools_for_caller() -> list[MCPTool]:
        listed = await _base_list_tools()
        ident = _identity(_ctx_of(mcp), secret)
        grants = set(ident.grants) if ident else set()
        gated = set(tool_registry())
        return [t for t in listed if t.name not in gated or t.name in grants]

    # Overwrites FastMCP's own handler — the low-level server keeps one per request type and the
    # last registration wins. Verified against the SDK in #51, and guarded by the probes there:
    # this reaches through a private attribute, so an SDK upgrade is what would break it.
    mcp._mcp_server.list_tools()(list_tools_for_caller)  # type: ignore[no-untyped-call]

    app: Starlette = mcp.streamable_http_app()  # serves the MCP endpoint at /mcp
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
