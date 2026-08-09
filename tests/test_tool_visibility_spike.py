"""SPIKE (#51): can the MCP tool LIST be filtered per caller, using the bearer token?

Realmtools serves every agent in a realm from one endpoint, and each agent authenticates with its
own signed token. ADR-004 grants tools per agent — so an agent must see only the tools it holds,
not merely be refused when it calls one it does not.

That distinction is the whole spike. #41 established that idle tools tempt agents into misusing
them and waste turns; a tool that appears in the list and then refuses is WORSE than one never
offered, because the agent spends a turn discovering it and may retry.

These are probes, not unit tests of shipped code — they answer "does the SDK let us do this?"
against the real protocol: a real Starlette app, real HTTP requests carrying real Authorization
headers, driven in-process over ASGITransport so there is no port to bind and nothing to poll.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Tool as MCPTool
from starlette.applications import Starlette

# what each caller is allowed to see; the probe's stand-in for a signed grant list
GRANTS = {"tok-analyst": {"web_search", "web_fetch"}, "tok-sealed": set()}
# Realmtools disables DNS-rebinding protection because agents reach it by container name; mirror
# that here or every request is a 421 that has nothing to do with what is being probed.
_TRANSPORT = TransportSecuritySettings(enable_dns_rebinding_protection=False)
UNGATED = {"remember"}  # every agent has this one, granted or not


def _build() -> tuple[Starlette, FastMCP]:
    mcp: FastMCP = FastMCP("spike", stateless_http=True, transport_security=_TRANSPORT)

    @mcp.tool()
    async def remember(note: str) -> str:
        """Ungated: every agent holds this."""
        return f"noted:{note}"

    @mcp.tool()
    async def web_search(query: str) -> str:
        """Gated by grant."""
        return f"results:{query}"

    @mcp.tool()
    async def web_fetch(url: str) -> str:
        """Gated by grant."""
        return f"body:{url}"

    def _grants() -> set[str]:
        """The caller's grants, from the Authorization header of the CURRENT request.

        `list_tools` takes no arguments, but the request context is a contextvar, so the HTTP
        request — and its headers — is reachable from inside it. This is the same route
        `realmtools._identity` already uses from inside a tool call.
        """
        try:
            ctx = mcp.get_context()
        except Exception:
            return set()
        rc = getattr(ctx, "request_context", None)
        request = getattr(rc, "request", None) if rc else None
        if request is None:
            return set()
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        return GRANTS.get(token, set())

    base_list_tools = mcp.list_tools

    async def filtered_list_tools() -> list[MCPTool]:
        grants = _grants()
        return [t for t in await base_list_tools()
                if t.name in UNGATED or t.name in grants]

    # Re-registering overwrites FastMCP's own handler (the low-level server keeps one handler per
    # request type, last registration wins). This is the seam the spike is testing.
    mcp._mcp_server.list_tools()(filtered_list_tools)

    return mcp.streamable_http_app(), mcp


@asynccontextmanager
async def _serving(app: Starlette):
    """Run the app's lifespan by hand.

    httpx's ASGITransport speaks HTTP only — it never sends the lifespan events, and the MCP
    session manager's task group is started there. Without this the first request dies with
    "Task group is not initialized", which reads like a bug in the probe rather than a missing
    startup.
    """
    async with app.router.lifespan_context(app):
        yield


def _client(app: Starlette, token: str | None = None) -> httpx.AsyncClient:
    """An httpx client that speaks to the app in-process, carrying the caller's bearer token."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    )


async def _tools_seen_by(app: Starlette, token: str) -> set[str]:
    """Caller must already hold `_serving(app)`: the session manager's run() is once-per-instance,
    so two connections share one lifespan rather than each starting their own."""
    async with (
        _client(app, token) as hc,
        streamable_http_client("http://localhost/mcp", http_client=hc) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        return {t.name for t in (await session.list_tools()).tools}


@pytest.mark.asyncio
async def test_two_callers_see_different_tool_lists():
    """The question the spike exists to answer."""
    app, _ = _build()
    async with _serving(app):
        # the same server, two callers — the difference must come from the token alone
        assert await _tools_seen_by(app, "tok-analyst") == {"remember", "web_search", "web_fetch"}
        assert await _tools_seen_by(app, "tok-sealed") == {"remember"}


@pytest.mark.asyncio
async def test_an_unknown_token_sees_only_ungated_tools():
    """Fails open to LESS, never more — a caller we cannot identify gets no grants."""
    app, _ = _build()
    async with _serving(app):
        assert await _tools_seen_by(app, "who-is-this") == {"remember"}


@pytest.mark.asyncio
async def test_filtering_the_list_does_not_gate_the_call():
    """The trap. Hiding a tool from the list does NOT stop a caller that knows its name, so
    invocation must be checked separately. Asserting this now stops someone reading the first
    test and concluding the list filter is the security boundary — it is a UX boundary."""
    app, _ = _build()
    async with (
        _serving(app),
        _client(app, "tok-sealed") as hc,
        streamable_http_client("http://localhost/mcp", http_client=hc) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        assert "web_search" not in {t.name for t in (await session.list_tools()).tools}
        result = await session.call_tool("web_search", {"query": "x"})
        text = "".join(getattr(c, "text", "") for c in result.content)
        assert "results:x" in text, (
            "the hidden tool still executed — list filtering is cosmetic, so the grant MUST "
            "also be enforced inside every tool body"
        )


@pytest.mark.asyncio
async def test_whether_dotted_tool_names_survive_the_protocol():
    """ADR-004 specifies `family.verb` names (`web.search`). If the SDK or the protocol rejects a
    dot, the naming convention has to change BEFORE anything is built on it."""
    mcp: FastMCP = FastMCP("dots", stateless_http=True, transport_security=_TRANSPORT)

    @mcp.tool(name="web.search")
    async def dotted(query: str) -> str:
        return f"ok:{query}"

    app = mcp.streamable_http_app()
    async with (
        _serving(app),
        _client(app) as hc,
        streamable_http_client("http://localhost/mcp", http_client=hc) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        names = {t.name for t in (await session.list_tools()).tools}
        assert "web.search" in names, f"dotted names do not survive listing: {names}"
        result = await session.call_tool("web.search", {"query": "q"})
        text = "".join(getattr(c, "text", "") for c in result.content)
        assert "ok:q" in text, "dotted names list but cannot be called"
