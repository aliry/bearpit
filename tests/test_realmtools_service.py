"""Realmtools tokens + EscrowService (#39): identity can't be spoofed, role gates, hidden moves."""

import pytest

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.notes import NoteService
from bearpit.realmtools.service import (
    EscrowService,
    Identity,
    SealedError,
    read_turn_status,
)
from bearpit.realmtools.tokens import mint_token, verify_token

SECRET = "platform-secret"


# --- tokens -----------------------------------------------------------------
def test_token_roundtrip_and_role():
    t = mint_token("duel", "vela", is_referee=False, secret=SECRET)
    assert verify_token(t, SECRET) == ("duel", "vela", False, (), ())
    r = mint_token("duel", "themis", is_referee=True, secret=SECRET, roster=["vela", "orin"])
    assert verify_token(r, SECRET) == ("duel", "themis", True, ("vela", "orin"), ())


def test_grants_survive_the_round_trip_in_a_stable_order(): 
    """Tool grants ride in the token, extending what `is_referee` already does: authority is in
    the signed token, never in a tool argument (ADR-004 §4). Sorted on mint, so one grant set
    always produces one token — a token that varied with dict order would be a nightmare to
    compare across runs."""
    a = mint_token("duel", "vela", is_referee=False, secret=SECRET,
                   grants=["web_search", "web_fetch"])
    b = mint_token("duel", "vela", is_referee=False, secret=SECRET,
                   grants=["web_fetch", "web_search"])
    assert a == b
    assert verify_token(a, SECRET) == ("duel", "vela", False, (), ("web_fetch", "web_search"))


def test_a_token_minted_before_grants_existed_still_verifies():
    """The deploy hazard this guards: token code runs in BOTH the host Forge and the realmtools
    container, and they are separate deployments. A hard cutover would mean any skew between them
    reads as an auth failure — the least debuggable symptom available. A 4-field token is a
    grantless one."""
    import base64

    from bearpit.realmtools import tokens as tokmod

    payload = ":".join(("duel", "vela", "player", "vela,orin"))          # the pre-change shape
    body = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    old_token = f"{body}.{tokmod._sign(payload, SECRET)}"
    assert verify_token(old_token, SECRET) == ("duel", "vela", False, ("vela", "orin"), ())


def test_the_grants_field_cannot_be_edited_without_breaking_the_signature():
    """The whole security property. If a grant could be added by hand, the token would be a
    suggestion rather than an authority, and every tool check downstream would be theatre."""
    import base64

    honest = mint_token("duel", "vela", is_referee=False, secret=SECRET, grants=["web_fetch"])
    body, sig = honest.split(".", 1)
    payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
    forged_payload = payload.replace("web_fetch", "net_open")
    forged_body = base64.urlsafe_b64encode(forged_payload.encode()).decode().rstrip("=")
    assert verify_token(f"{forged_body}.{sig}", SECRET) is None


def test_token_rejects_tamper_and_wrong_secret():
    t = mint_token("duel", "vela", is_referee=False, secret=SECRET)
    assert verify_token(t, "other-secret") is None  # wrong signing secret
    # flip the identity body -> signature no longer matches
    body, sig = t.split(".", 1)
    forged = mint_token("duel", "orin", is_referee=False, secret=SECRET).split(".")[0] + "." + sig
    assert verify_token(forged, SECRET) is None
    assert verify_token("garbage", SECRET) is None


# --- escrow service ---------------------------------------------------------
@pytest.fixture
async def svc():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    yield EscrowService(c), c
    await c.close()


async def test_submit_is_attributed_to_token_identity_not_a_claim(svc):
    service, _ = svc
    vela = Identity("duel", "vela", False)
    orin = Identity("duel", "orin", False)
    await service.submit(vela, "1", "rock")
    # status shows WHO sealed (from their tokens), not the payload
    assert await service.status(vela, "1") == {"submitted": ["vela"], "pending": []}
    await service.submit(orin, "1", "paper")
    assert set((await service.status(vela, "1"))["submitted"]) == {"vela", "orin"}


async def test_roster_aware_status_shows_pending_before_any_submit(svc):
    """The referee's token carries the roster, so reveal_status lists who is still pending even
    before anyone submits — the fix for referees concluding 'nobody submitted' too early."""
    service, _ = svc
    themis = Identity("duel", "themis", True, roster=("vela", "orin"))
    # referee checks first, before any player has sealed
    assert await service.status(themis, "1") == {"submitted": [], "pending": ["orin", "vela"]}
    await service.submit(Identity("duel", "vela", False, roster=("vela", "orin")), "1", "rock")
    assert await service.status(themis, "1") == {"submitted": ["vela"], "pending": ["orin"]}


async def test_referee_gates(svc):
    service, chron = svc
    themis = Identity("duel", "themis", True)
    vela = Identity("duel", "vela", False)
    await service.submit(vela, "1", "rock")

    # a referee cannot submit; a player cannot reveal or tally
    with pytest.raises(PermissionError):
        await service.submit(themis, "1", "scissors")
    with pytest.raises(PermissionError):
        await service.reveal(vela, "1")
    with pytest.raises(PermissionError):
        await service.tally(vela, "1", "dominance")


async def test_hidden_until_reveal_then_tally_emits_a_TALLY_not_a_verdict(svc):
    service, chron = svc
    themis = Identity("duel", "themis", True)
    vela, orin = Identity("duel", "vela", False), Identity("duel", "orin", False)
    await service.submit(vela, "1", "rock")
    await service.submit(orin, "1", "paper")

    # before reveal the chronicle holds only markers (no payloads)
    seals = await chron.events("duel", kind="sealed_submit")
    assert len(seals) == 2 and all("payload" not in e.payload for e in seals)

    # referee tallies -> reveals + scores deterministically, and records a TALLY (never a VERDICT)
    result = await service.tally(themis, "1", "dominance",
        {"beats": {"rock": ["scissors"], "scissors": ["paper"], "paper": ["rock"]}})
    assert result["result"] == "orin"  # paper beats rock
    tallies = await chron.events("duel", kind=EventKind.TALLY)
    assert tallies[-1].payload["outcome"] == "orin"
    # CRUCIALLY: scoring a round must not end the realm. `referee_verdict` termination fires on ANY
    # verdict event, so emitting one here decapitated every sealed-submit scenario the first time
    # its referee scored a round — before it could reveal the rest or call rule().
    assert await chron.events("duel", kind=EventKind.VERDICT) == []
    # immutable after seal
    with pytest.raises(SealedError):
        await service.submit(vela, "1", "scissors")


# --- turn status ------------------------------------------------------------
async def test_turn_status_reads_latest_and_shortens_ids(svc):
    _, chron = svc
    assert (await read_turn_status(chron, "duel"))["active"] is False  # turns off
    await chron.append_event("duel", EventKind.TURN, {
        "round": 2, "order": ["@duel-pro:realm.local", "@duel-con:realm.local"],
        "position": 1, "current": "@duel-con:realm.local"})
    s = await read_turn_status(chron, "duel")
    assert s["active"] is True and s["round"] == 2
    assert s["current"] == "con" and s["order"] == ["pro", "con"]  # mxids shortened to agent ids
    assert s["done_this_round"] == ["pro"] and s["upcoming_this_round"] == []


async def test_builtin_skills_are_served_as_mcp_prompts():
    # Agents ask the MCP server for skills by name (get_prompt("referee-basics") observed live);
    # an unknown prompt made FastMCP raise server-side. Every builtin skill must be registered
    # on the REAL server and answer with the canonical skill text.
    from bearpit.forge.skills import BUILTIN_SKILLS
    from bearpit.realmtools.server import build_app

    app = build_app("secret", db_url="sqlite+aiosqlite:///:memory:")
    mcp = app.state.mcp
    names = {p.name for p in await mcp.list_prompts()}
    assert set(BUILTIN_SKILLS) <= names
    got = await mcp.get_prompt("referee-basics")
    text = "".join(str(m.content.text) for m in got.messages)  # type: ignore[union-attr]
    assert "referee-basics" in text  # the skill body itself, not an error


def test_audit_never_logs_sealed_payloads():
    # `reveal` returns the SEALED submissions. The whole security property of sealed-submit is that
    # a move stays hidden until the referee reveals it — dumping it into the audit log would leak
    # every player's secret move to anyone tailing `docker logs`, in a competitive realm possibly
    # before the round resolves. The audit may say HOW MANY and WHOSE, never WHAT.
    from bearpit.realmtools.server import _result_shape

    revealed = {"ping": "mango", "pong": "papaya"}
    shape = _result_shape(revealed)
    assert "mango" not in shape and "papaya" not in shape  # no payloads, ever
    assert "n=2" in shape and "ping" in shape  # count + who sealed (already public via status)
    # and the diagnostic this exists for: empty vs never-called stays distinguishable
    assert "n=0" in _result_shape({}) and _result_shape(None) == "-"


async def test_an_agent_can_only_recall_its_OWN_notes():
    # The scratchpad is private reasoning: an agent reads back only what IT wrote. Scoping comes
    # from the verified token, never from an argument, so there is nothing for a caller to forge.
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    notes = NoteService(chron)
    juno = Identity("r1", "juno", False, ())
    cass = Identity("r1", "cass", False, ())

    await notes.remember(juno, "Cass claimed Electrical in R2 but Mira placed her in MedBay.")
    await notes.remember(cass, "Bus Vega next round if the heat lands on me.")

    assert await notes.recall(juno) == [
        "Cass claimed Electrical in R2 but Mira placed her in MedBay."]
    assert await notes.recall(cass) == ["Bus Vega next round if the heat lands on me."]
    await chron.close()


async def test_the_audit_log_never_records_the_note_text():
    # A note is an agent's private reasoning — the impostor's kill plan lives here. The audit
    # records that a note was TAKEN, never what it said (same rule as sealed payloads).
    from bearpit.realmtools.server import _result_shape
    assert "Bus Vega" not in _result_shape({"ok": "Bus Vega next round"})


async def test_run_code_waits_for_the_hosts_result_and_returns_it():
    # The agent-side half of the broker: record the request, wait for the host's EXEC_RESULT,
    # hand back stdout. realmtools never touches Docker.
    from bearpit.chronicle import EventKind
    from bearpit.realmtools.code import CodeService

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    coder = CodeService(chron)
    mother = Identity("r1", "mother", True, ())

    async def fake_sleep(_s):
        # stand in for the host's tick: answer the pending request the first time we are polled
        pending = await chron.events("r1", kind=EventKind.EXEC)
        for ev in pending:
            await chron.append_event("r1", EventKind.EXEC_RESULT, {
                "id": ev.payload["id"], "agent": "mother", "exit_code": 0, "output": "Cass 4\n"})

    res = await coder.run(mother, "print('Cass 4')", sleep=fake_sleep)
    assert res["exit_code"] == 0 and "Cass 4" in str(res["output"])
    # the request carries the CALLER's id, taken from the token — never a tool argument
    req = (await chron.events("r1", kind=EventKind.EXEC))[-1].payload
    assert req["agent"] == "mother"
    await chron.close()


async def test_run_code_gives_up_rather_than_hanging_forever():
    from bearpit.realmtools.code import CodeService

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    coder = CodeService(chron)

    async def fake_sleep(_s):
        return None  # the host never answers

    res = await coder.run(Identity("r1", "juno", False, ()), "print(1)",
                          wait_s=0.5, sleep=fake_sleep)
    assert "did not finish" in str(res["error"])
    await chron.close()


async def test_turn_status_separates_the_open_round_from_the_round_to_resolve():
    """A referee cued at a boundary resolves the round that just FINISHED. But the manager
    chronicles the INCREMENTED round there (the Arbiter's min_rounds guard reads it), so `round`
    already says N+1. A referee keying its reveal off `round` would call reveal('R2') for moves the
    players sealed as 'R1' and get an empty round back — the sealed mechanic silently doing nothing,
    which is exactly the class of failure that is hardest to see."""
    from bearpit.realmtools.service import read_turn_status

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await chron.append_event("r1", EventKind.TURN, {
        "round": 1, "order": ["@r1-a:h", "@r1-b:h"], "position": 0, "current": "@r1-a:h"})
    st = await read_turn_status(chron, "r1")
    assert st["round"] == 1 and st["last_completed_round"] == 0  # nothing finished yet

    # round 1 wraps: the manager writes the INCREMENTED round with the completed one alongside
    await chron.append_event("r1", EventKind.TURN, {
        "event": "round_complete", "completed": 1, "round": 2,
        "order": ["@r1-a:h", "@r1-b:h"], "position": 0, "current": None})
    st = await read_turn_status(chron, "r1")
    assert st["round"] == 2               # the round now open
    assert st["last_completed_round"] == 1  # the round the referee must reveal + score
    await chron.close()


async def test_sealed_state_and_scores_survive_a_realmtools_restart():
    """realmtools runs as a container the operator restarts (deploys). Both the escrow's sealed
    submissions and the Arbiter's scoreboard were held in PROCESS MEMORY only, so a restart
    mid-realm silently lost every sealed move and reset every score to zero — the referee would
    reveal an empty round and rule on a blank board. A fresh service rebuilds both from the
    chronicle."""
    from bearpit.realmtools.arbiter import ArbiterService

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    secret = "realm-secret"
    roster = ("vela", "orin")
    vela = Identity("r", "vela", False, roster)
    orin = Identity("r", "orin", False, roster)
    ref = Identity("r", "themis", True, roster)

    svc = EscrowService(chron, secret=secret)
    await svc.submit(vela, "R1", "rock")
    await svc.submit(orin, "R1", "paper")
    arb = ArbiterService(chron)
    await arb.score(ref, "vela", 3, "round win")

    # --- realmtools restarts: brand-new service objects, same chronicle ---
    svc2 = EscrowService(chron, secret=secret)
    revealed = await svc2.reveal(ref, "R1")
    assert revealed == {"vela": "rock", "orin": "paper"}   # the sealed payloads survived
    arb2 = ArbiterService(chron)
    assert await arb2.scoreboard(ref) == {"vela": 3.0}     # the score survived

    # and the sealed payload was NOT stored in the clear in the submit marker
    seals = await chron.events("r", kind="sealed_submit")
    assert seals
    assert all("rock" not in str(e.payload) and "paper" not in str(e.payload)
               for e in seals)
    await chron.close()


def test_the_server_surfaces_grants_on_the_identity_it_resolves():
    """The last link in the chain #54 consumes. Forge bakes grants in and `verify_token` returns
    them, but neither proves the SERVER hands them to a tool body — and `Identity(*verified)`
    unpacks positionally, so a field added in the wrong place would land silently in `roster`."""
    from types import SimpleNamespace

    from bearpit.realmtools.server import _identity

    token = mint_token("duel", "vela", is_referee=False, secret=SECRET,
                       roster=["vela", "orin"], grants=["web_fetch", "web_search"])
    ctx = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(headers={"authorization": f"Bearer {token}"})))

    ident = _identity(ctx, SECRET)
    assert ident is not None
    assert ident.agent_id == "vela"
    assert ident.roster == ("vela", "orin")
    assert ident.grants == ("web_fetch", "web_search")


def test_an_unsigned_caller_gets_no_identity_and_therefore_no_grants():
    from types import SimpleNamespace

    from bearpit.realmtools.server import _identity

    ctx = SimpleNamespace(request_context=SimpleNamespace(
        request=SimpleNamespace(headers={"authorization": "Bearer forged"})))
    assert _identity(ctx, SECRET) is None


async def test_the_chronicle_is_connected_once_per_process_not_once_per_session():
    """Realmtools leaked a Postgres connection pool per MCP session.

    FastMCP's *server* lifespan runs inside `app.run()`, which the streamable-http manager calls
    per session — not once per process. Connecting the Chronicle there created a fresh SQLAlchemy
    engine, and therefore a fresh pool, for every agent session. Live, that reached 206 new
    connections in an hour under five concurrent realms and exhausted `max_connections`, which
    takes down every realm at once.

    Worse than the leak: the services are process-global, so each session's lifespan REPLACED the
    chronicle the others were using, and closing one session could close the connection another
    realm was mid-write on.
    """
    from contextlib import asynccontextmanager

    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from bearpit.realmtools import server as srv

    connects = {"n": 0}
    real = Chronicle.connect

    async def counting(url: str, **kw: object) -> Chronicle:
        connects["n"] += 1
        return await real("sqlite+aiosqlite:///:memory:", **kw)  # type: ignore[arg-type]

    Chronicle.connect = counting  # type: ignore[method-assign]
    try:
        app = srv.build_app("s" * 40, db_url="sqlite+aiosqlite:///:memory:")
        token = mint_token("r1", "vela", is_referee=False, secret="s" * 40)

        @asynccontextmanager
        async def serving():
            async with app.router.lifespan_context(app):
                yield

        async with serving():
            for _ in range(3):
                async with (
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                      base_url="http://localhost",
                                      headers={"Authorization": f"Bearer {token}"}) as hc,
                    streamable_http_client("http://localhost/mcp", http_client=hc) as (r, w, _),
                    ClientSession(r, w) as session,
                ):
                    await session.initialize()
                    await session.list_tools()
    finally:
        Chronicle.connect = real  # type: ignore[method-assign]

    assert connects["n"] == 1, (
        f"connected {connects['n']} times for 3 sessions — one engine, and therefore one "
        f"connection pool, is leaked per session"
    )
