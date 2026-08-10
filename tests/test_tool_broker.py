"""The tool broker (#54, ADR-004 §3): grant enforcement, quotas, and the host performing the call.

realmtools records the intent and waits; the host — which holds the keystore and the only route
to the internet — performs it and answers. Same shape as `run_code`, for the same reason: a
credential in that small agent-facing server would be a credential leaked by any bug in it.

The failure modes matter more than the happy path. An unanswered call costs an agent its whole
wait, so a tool that is merely broken must cost one call and never the realm.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from bearpit.chronicle import Chronicle, EventKind
from bearpit.core import tools as toolmod
from bearpit.core.tools import ToolProfile
from bearpit.realmtools.service import Identity
from bearpit.realmtools.tokens import mint_token
from bearpit.realmtools.toolcall import ToolCallService

SECRET = "s" * 40


@pytest.fixture
async def chron():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    yield c
    await c.close()


def _who(*grants: str) -> Identity:
    return Identity(realm_id="r1", agent_id="analyst", is_referee=False, grants=grants)


async def _tick(_seconds: float) -> None:
    """A poll delay that costs no wall-clock — for the case where nobody ever answers."""
    return None


async def _answer(chron: Chronicle, *, ok: bool = True, result: str = "results!",
                  error: str = "") -> None:
    """Play the host: answer the outstanding intent (the one carrying args — quota fixtures seed
    bare TOOL_CALL rows that were never really made)."""
    calls = [e for e in await chron.events("r1", kind=EventKind.TOOL_CALL)
             if e.payload.get("args") is not None]
    await chron.append_event("r1", EventKind.TOOL_RESULT, {
        "id": calls[-1].payload["id"], "agent": calls[-1].payload["agent"],
        "tool": calls[-1].payload["tool"], "ok": ok, "result": result, "error": error,
    })


async def _call_with_host(
    chron: Chronicle, svc: ToolCallService, who: Identity, tool: str, args: dict[str, Any],
    *, ok: bool = True, result: str = "results!", error: str = "",
) -> dict[str, Any]:
    """Make the call, and let the host answer through the service's own poll hook.

    Deterministic on purpose. An earlier version raced a concurrent `host()` task against the
    poll loop and passed or failed depending on which got to SQLite first — a test that reports
    a timeout that never happened is worse than no test. Driving the answer from the hook means
    the answer is always in place before the next poll, every run.
    """
    answered = False

    async def sleep(_seconds: float) -> None:
        nonlocal answered
        if not answered:
            answered = True
            await _answer(chron, ok=ok, result=result, error=error)

    return await svc.call(who, tool, args, sleep=sleep)


# --- the grant is enforced HERE, not by hiding the tool ----------------------------------------
async def test_an_ungranted_tool_is_refused(chron):
    """#51 proved a hidden tool still executes when named. This is the check that actually
    enforces ADR-004; the list filter only stops an agent wasting a turn."""
    svc = ToolCallService(chron)
    with pytest.raises(PermissionError, match="web_search"):
        await svc.call(_who("web_fetch"), "web_search", {"query": "x"})
    assert await chron.events("r1", kind=EventKind.TOOL_CALL) == [], "an intent was still recorded"


async def test_an_agent_with_no_grants_at_all_is_refused(chron):
    svc = ToolCallService(chron)
    with pytest.raises(PermissionError, match="you have none"):
        await svc.call(_who(), "web_search", {"query": "x"})


async def test_a_granted_call_is_recorded_and_answered(chron):
    svc = ToolCallService(chron)
    out = await _call_with_host(chron, svc, _who("web_search"), "web_search",
                                {"query": "otters"}, result="otters are mustelids")
    assert out["result"] == "otters are mustelids"
    recorded = (await chron.events("r1", kind=EventKind.TOOL_CALL))[0].payload
    assert recorded["agent"] == "analyst" and recorded["tool"] == "web_search"
    assert recorded["args"] == {"query": "otters"}


async def test_a_failed_call_returns_the_error_rather_than_raising(chron):
    """A broken tool costs one call. Raising into the tool body would surface to the agent as a
    protocol error instead of something it can read and route around."""
    svc = ToolCallService(chron)
    out = await _call_with_host(chron, svc, _who("web_search"), "web_search", {"query": "x"},
                                ok=False, error="upstream is down")
    assert out == {"error": "upstream is down"}


async def test_a_call_nobody_answers_times_out_with_a_readable_message(chron):
    svc = ToolCallService(chron)
    out = await svc.call(_who("web_search"), "web_search", {"query": "x"},
                         wait_s=1.0, sleep=_tick)
    assert "did not answer in time" in out["error"]


async def test_oversized_arguments_are_refused_before_anything_is_recorded(chron):
    svc = ToolCallService(chron)
    with pytest.raises(ValueError, match="too long"):
        await svc.call(_who("web_search"), "web_search", {"query": "x" * 20000})
    assert await chron.events("r1", kind=EventKind.TOOL_CALL) == []


# --- quotas ------------------------------------------------------------------------------------
async def _with_policy(chron: Chronicle, policy: dict[str, Any]) -> ToolCallService:
    await chron.append_event("r1", EventKind.TOOL_MANIFEST, {
        "version": 1,
        "tools": {name: {"available": True, "description": "d", "params": {"type": "object"},
                         "policy": block}
                  for name, block in policy.items()},
        "grants": {"analyst": list(policy)},
    })
    return ToolCallService(chron)


async def test_the_quota_is_read_from_the_run_record_and_enforced(chron):
    svc = await _with_policy(chron, {"web_search": {"max_calls_per_agent": 2}})
    for _ in range(2):
        await chron.append_event("r1", EventKind.TOOL_CALL,
                                 {"id": "x", "agent": "analyst", "tool": "web_search"})
    out = await svc.call(_who("web_search"), "web_search", {"query": "x"})
    assert out["quota_exhausted"] is True
    assert "2/2" in out["error"], "the agent should be told where it stands, not just refused"


async def test_the_quota_is_per_agent_not_per_realm(chron):
    """A shared counter would let one agent spend a peer's budget — and in a competitive realm
    that is a move, not an accident."""
    svc = await _with_policy(chron, {"web_search": {"max_calls_per_agent": 1}})
    await chron.append_event("r1", EventKind.TOOL_CALL,
                             {"id": "x", "agent": "rival", "tool": "web_search"})
    out = await _call_with_host(chron, svc, _who("web_search"), "web_search", {"query": "x"})
    assert "result" in out


async def test_a_call_that_failed_still_counts_against_the_quota(chron):
    """Counted from TOOL_CALL, not TOOL_RESULT: a call that failed still consumed the thing the
    quota exists to bound, and counting only successes would make a flapping tool free."""
    svc = await _with_policy(chron, {"web_search": {"max_calls_per_agent": 1}})
    await chron.append_event("r1", EventKind.TOOL_CALL,
                             {"id": "a", "agent": "analyst", "tool": "web_search"})
    await chron.append_event("r1", EventKind.TOOL_RESULT,
                             {"id": "a", "agent": "analyst", "tool": "web_search", "ok": False})
    assert (await svc.call(_who("web_search"), "web_search", {"q": "x"}))["quota_exhausted"]


async def test_no_policy_means_no_quota(chron):
    """A scenario that says nothing about a tool gets today's behaviour: unlimited. A default cap
    would silently change what an existing manifest does."""
    svc = ToolCallService(chron)
    for _ in range(50):
        await chron.append_event("r1", EventKind.TOOL_CALL,
                                 {"id": "x", "agent": "analyst", "tool": "web_search"})
    out = await _call_with_host(chron, svc, _who("web_search"), "web_search", {"q": "x"})
    assert "result" in out


# --- the host performs it ----------------------------------------------------------------------
def _snapshot(chron: Chronicle, tool_config: dict[str, Any] | None = None):
    from bearpit.gatekeeper.runner import LiveSnapshot

    class _Herald:
        async def read(self, *a, **k):
            return []

    class _Ledger:
        async def poll_spend(self, realm, chron):
            return {}

        def minted_keys(self, realm):
            return []

    class _Runtime:
        def read_volume(self, name):
            return {}

    return LiveSnapshot(
        herald=_Herald(), ledger=_Ledger(), chronicle=chron, runtime=_Runtime(),
        realm_id="r1", commons_room="!c:realm.local", shared_volume=None,
        stop_flag=lambda: False, clock=lambda: 0.0, tool_config=tool_config or {},
    )


def _profile(name: str = "web_search", handler: Any = None, **kw: Any) -> ToolProfile:
    async def ok(args: dict[str, Any], config: dict[str, Any], ctx: Any) -> Any:
        return f"searched {args.get('query')} with {config.get('region', 'default')}"

    return ToolProfile(name=name, label="L", description="d",
                       params={"type": "object", "properties": {"query": {"type": "string"}}},
                       handler=handler or ok, **kw)


@pytest.fixture
def registry(monkeypatch):
    def install(*profiles: ToolProfile) -> None:
        class _EP:
            name = "fake"

            def load(self):
                class _P:
                    def tools(self):
                        return profiles
                return _P()

        monkeypatch.setattr(toolmod, "_entry_points",
                            lambda g: [_EP()] if g == toolmod.TOOL_GROUP else [])
        toolmod.reset_tool_cache()

    yield install
    toolmod.reset_tool_cache()


async def test_the_host_runs_the_handler_and_answers(chron, registry):
    registry(_profile(cost_per_call_usd=0.005))
    await chron.append_event("r1", EventKind.TOOL_CALL,
                             {"id": "q1", "agent": "analyst", "tool": "web_search",
                              "args": {"query": "otters"}})
    await _snapshot(chron, {"web_search": {"region": "uk"}})._run_tool_requests()

    results = await chron.events("r1", kind=EventKind.TOOL_RESULT)
    assert len(results) == 1
    p = results[0].payload
    assert p["ok"] is True and p["id"] == "q1" and p["agent"] == "analyst"
    assert p["result"] == "searched otters with uk", "the realm's tool policy never reached it"
    assert p["cost_usd"] == 0.005


async def test_a_handler_that_raises_fails_only_that_call(chron, registry):
    async def angry(args, config, ctx):
        raise RuntimeError("kaboom")

    registry(_profile(handler=angry))
    await chron.append_event("r1", EventKind.TOOL_CALL,
                             {"id": "q1", "agent": "analyst", "tool": "web_search", "args": {}})
    await _snapshot(chron)._run_tool_requests()   # must not raise

    p = (await chron.events("r1", kind=EventKind.TOOL_RESULT))[0].payload
    assert p["ok"] is False and "kaboom" in p["error"]


async def test_a_handler_that_hangs_is_timed_out_and_answered(chron, registry, monkeypatch):
    import asyncio

    async def hang(args, config, ctx):
        await asyncio.sleep(3600)

    monkeypatch.setattr("bearpit.gatekeeper.runner._TOOL_TIMEOUT_S", 0.05)
    registry(_profile(handler=hang))
    await chron.append_event("r1", EventKind.TOOL_CALL,
                             {"id": "q1", "agent": "analyst", "tool": "web_search", "args": {}})
    await _snapshot(chron)._run_tool_requests()

    p = (await chron.events("r1", kind=EventKind.TOOL_RESULT))[0].payload
    assert p["ok"] is False and "timed out" in p["error"]


async def test_a_granted_tool_missing_from_THIS_host_is_answered_not_dropped(chron, registry):
    """The grant was checked against the token, not this machine: a manifest can grant a tool the
    host does not have installed. Silence would cost the agent its full wait for nothing."""
    registry()  # empty registry
    await chron.append_event("r1", EventKind.TOOL_CALL,
                             {"id": "q1", "agent": "analyst", "tool": "web_search", "args": {}})
    await _snapshot(chron)._run_tool_requests()

    p = (await chron.events("r1", kind=EventKind.TOOL_RESULT))[0].payload
    assert p["ok"] is False and "not installed" in p["error"]


async def test_a_non_string_result_is_serialised_rather_than_lost(chron, registry):
    async def structured(args, config, ctx):
        return {"results": [{"title": "Otters", "url": "https://example.org"}]}

    registry(_profile(handler=structured))
    await chron.append_event("r1", EventKind.TOOL_CALL,
                             {"id": "q1", "agent": "analyst", "tool": "web_search", "args": {}})
    await _snapshot(chron)._run_tool_requests()

    p = (await chron.events("r1", kind=EventKind.TOOL_RESULT))[0].payload
    assert p["ok"] is True and "Otters" in p["result"]


async def test_each_call_is_performed_exactly_once(chron, registry):
    """The drain runs every tick. A search billed twice per tick would be an expensive bug."""
    calls = {"n": 0}

    async def counting(args, config, ctx):
        calls["n"] += 1
        return "ok"

    registry(_profile(handler=counting))
    await chron.append_event("r1", EventKind.TOOL_CALL,
                             {"id": "q1", "agent": "analyst", "tool": "web_search", "args": {}})
    snap = _snapshot(chron)
    await snap._run_tool_requests()
    await snap._run_tool_requests()
    await snap._run_tool_requests()
    assert calls["n"] == 1


# --- end to end, through the real MCP protocol -------------------------------------------------
# These now run with an EMPTY tool registry in the server process, which is the whole point of #65:
# the agent-facing container describes granted tools from the manifest and needs no plugin at all.
def _manifest(*names: str, described: dict[str, Any] | None = None) -> dict[str, Any]:
    tools = {n: {"available": True, "description": f"The {n} tool",
                 "params": {"type": "object", "properties": {"query": {"type": "string"}},
                            "required": ["query"]},
                 "policy": {}} for n in names}
    if described:
        tools.update(described)
    return {"version": 1, "tools": tools, "grants": {}}


@asynccontextmanager
async def _server(manifests: dict[str, dict[str, Any]]) -> Any:
    """A real realmtools app with NO tool plugins installed, seeded with per-realm manifests."""
    from bearpit.realmtools.server import build_app

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    for realm, payload in manifests.items():
        await chron.append_event(realm, EventKind.TOOL_MANIFEST, payload)
    app = build_app(SECRET, chronicle=chron)
    async with app.router.lifespan_context(app):
        yield app
    await chron.close()


@asynccontextmanager
async def _as(app: Any, token: str) -> Any:
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                          headers={"Authorization": f"Bearer {token}"}) as hc,
        streamable_http_client("http://localhost/mcp", http_client=hc) as (r, w, _),
        ClientSession(r, w) as session,
    ):
        await session.initialize()
        yield session


async def test_an_agent_sees_and_calls_only_the_tools_it_holds(registry):
    """The whole chain with an EMPTY registry in this process: token -> manifest -> list -> call."""
    registry()  # nothing installed, deliberately
    analyst = mint_token("r1", "analyst", is_referee=False, secret=SECRET, grants=["web_search"])
    sealed = mint_token("r1", "sealed", is_referee=False, secret=SECRET)

    async with _server({"r1": _manifest("web_search", "web_fetch")}) as app:
        async with _as(app, analyst) as session:
            names = {t.name for t in (await session.list_tools()).tools}
            assert "web_search" in names
            assert "web_fetch" not in names, "a tool this agent does not hold must not be listed"
            assert "remember" in names, "the built-in verbs must be unaffected"

        async with _as(app, sealed) as session:
            names = {t.name for t in (await session.list_tools()).tools}
            assert "web_search" not in names and "remember" in names
            # hidden is not the boundary (#51): naming it directly must still be refused
            out = await session.call_tool("web_search", {"query": "x"})
            text = "".join(getattr(c, "text", "") for c in out.content)
            assert "Unknown tool" in text or "not one of your tools" in text


async def test_a_granted_tool_advertises_the_manifests_typed_parameters(registry):
    """The schema an agent sees comes from the record, not from an installed package."""
    registry()
    token = mint_token("r1", "analyst", is_referee=False, secret=SECRET, grants=["web_search"])
    described = {"web_search": {"available": True, "description": "Search the web",
                                "params": {"type": "object",
                                           "properties": {"query": {"type": "string"},
                                                          "count": {"type": "integer"}},
                                           "required": ["query"]},
                                "policy": {}}}
    async with _server({"r1": _manifest(described=described)}) as app, _as(app, token) as session:
        tool = next(t for t in (await session.list_tools()).tools if t.name == "web_search")
        assert tool.description == "Search the web"
        assert set(tool.inputSchema.get("properties", {})) == {"query", "count"}
        assert tool.inputSchema.get("required") == ["query"]


async def test_two_realms_on_one_server_see_only_their_own_tools(registry):
    """realmtools is shared by every realm. With per-realm descriptions read from data, realm A
    must never be described by realm B's manifest — the realm id comes from the signed token."""
    registry()
    a = mint_token("ra", "analyst", is_referee=False, secret=SECRET, grants=["web_search"])
    b = mint_token("rb", "analyst", is_referee=False, secret=SECRET, grants=["web_search"])

    async with _server({
        "ra": _manifest(described={"web_search": {"available": True, "description": "REALM A",
                                                  "params": {"type": "object"}, "policy": {}}}),
        "rb": _manifest(described={"web_search": {"available": True, "description": "REALM B",
                                                  "params": {"type": "object"}, "policy": {}}}),
    }) as app:
        async with _as(app, a) as s:
            assert next(t for t in (await s.list_tools()).tools
                        if t.name == "web_search").description == "REALM A"
        async with _as(app, b) as s:
            assert next(t for t in (await s.list_tools()).tools
                        if t.name == "web_search").description == "REALM B"


async def test_a_grant_with_no_manifest_entry_is_still_offered(registry):
    """The failure this design exists to remove is a grant going invisible. If the manifest cannot
    describe a tool, it is listed unconstrained rather than dropped — a guessed argument shape is
    recoverable, a tool the agent never sees is not."""
    registry()
    token = mint_token("r1", "analyst", is_referee=False, secret=SECRET, grants=["web_search"])
    empty = {"version": 1, "tools": {}, "grants": {}}
    async with _server({"r1": empty}) as app, _as(app, token) as session:
        tool = next(t for t in (await session.list_tools()).tools if t.name == "web_search")
        assert tool.inputSchema == {"type": "object"}


async def test_a_manifest_from_a_future_version_degrades_rather_than_breaks(registry):
    """Host and container are separate deployments (the tokens.py hazard). An unreadable manifest
    must cost precision, never the grant."""
    registry()
    token = mint_token("r1", "analyst", is_referee=False, secret=SECRET, grants=["web_search"])
    bad = {"version": 99, "tools": {"web_search": {"params": {"nope": 1}}}}
    async with _server({"r1": bad}) as app, _as(app, token) as session:
        names = {t.name for t in (await session.list_tools()).tools}
        assert "web_search" in names


# --- the writer and the reader must agree (#66) ------------------------------------------------
async def test_the_quota_the_platform_writes_is_the_quota_the_broker_reads(chron):
    """The test that was missing when quotas silently did nothing (#66), now against the manifest.

    Every other quota test seeds the payload, so they prove the READER works on input the WRITER
    may never produce. This one drives the real `grant_manifest()` into the chronicle exactly as
    `runner.py` writes it, and lets the real `policy()` read it back. No hand-written payload
    anywhere in the path, so the two halves cannot drift apart again without something going red.
    """
    from bearpit.core.schema import AgentSpec, Project, ProjectMeta, ProjectSpec
    from bearpit.core.tools import grant_manifest

    project = Project(
        metadata=ProjectMeta(name="p"),
        spec=ProjectSpec(tools={"web_search": {"max_calls_per_agent": 1}}),
        agents=[AgentSpec(id="analyst", tools=["web_search"])],
    )
    await chron.append_event("r1", EventKind.TOOL_MANIFEST, grant_manifest(project))

    svc = ToolCallService(chron)
    assert await svc.policy("r1", "web_search") == {"max_calls_per_agent": 1}, (
        "the platform's own manifest does not carry the policy the broker reads"
    )

    await chron.append_event("r1", EventKind.TOOL_CALL,
                             {"id": "a", "agent": "analyst", "tool": "web_search", "args": {}})
    out = await svc.call(_who("web_search"), "web_search", {"query": "x"})
    assert out["quota_exhausted"] is True, "the cap the scenario set did not bite"
