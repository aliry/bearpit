"""MachineService: identity -> role, the per-realm lock, append-then-apply, replay-after-restart.

The two invariants that matter most are the ones the reviews called out: memory must never lead
the chronicle (append first), and two acts arriving together must serialise — the Arbiter #17
bug class was exactly a check-then-write interleaving across an await.
"""
from __future__ import annotations

import asyncio

import pytest

from bearpit.chronicle import Chronicle, EventKind
from bearpit.realmtools.machine import ReplayError
from bearpit.realmtools.machine_service import MachineService
from bearpit.realmtools.service import Identity

DECL = {
    "roles": {"dealer": {"members": "referee"}, "player": {"members": "participants"}},
    "states": ["waiting", "street"], "initial": "waiting",
    "actor": {"over": "player", "skip": ["acted"]},
    "data": {"pot": {"visibility": "public"}, "hole": {"visibility": "owner"},
             "acted": {"visibility": "public", "type": "set"},
             "hand": {"visibility": "public"}},
    "transitions": {
        "deal": {"from": "waiting", "to": "street", "by": "dealer",
                 "effects": [{"reset": "acted"}, {"set_actor": "$args.first"}]},
        "call": {"from": "street", "to": "same", "by": "player",
                 "guard": ["caller_is_actor", {"caller_not_in": "acted"}],
                 "effects": [{"add_to": {"key": "acted", "value": "$caller"}}, "advance_actor"]},
        "resolve": {"from": "street", "to": "waiting", "by": "dealer",
                    "guard": [{"escrow_complete":
                               {"round": "$data.hand", "over": "player", "minus": []}}]},
    },
    "wake": [{"role": "actor"}],
}
MACHINE = {"version": 1, "declaration": DECL,
           "members": {"dealer": ["dealer"], "player": ["a", "b"]},
           "roster": ["a", "b"], "referee": "dealer"}
# One transition, one state: enough to make a lot of log rows quickly.
PING_MACHINE = {
    "version": 1,
    "declaration": {"roles": {"dealer": {"members": "referee"}}, "states": ["s"], "initial": "s",
                    "transitions": {"ping": {"from": "s", "to": "same", "by": "dealer"}}},
    "members": {"dealer": ["dealer"]}, "roster": ["dealer"], "referee": "dealer",
}
# Every row of spec §3's participant log table, in one declaration.
TABLE_MACHINE = {
    "version": 1,
    "declaration": {
        **DECL,
        "data": {**DECL["data"], "secret": {"visibility": "referee"}},
        "transitions": {**DECL["transitions"],
                        "peek": {"from": "street", "to": "same", "by": "dealer",
                                 "log": "referee"}},
    },
    "members": {"dealer": ["dealer"], "player": ["a", "b"]},
    "roster": ["a", "b"], "referee": "dealer",
}
DEALER = Identity("r", "dealer", True, roster=("a", "b"))
A = Identity("r", "a", False)
B = Identity("r", "b", False)


@pytest.fixture
async def chron():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await c.append_event("r", EventKind.MACHINE, MACHINE)
    yield c
    await c.close()


def _svc(chron, sealed=None):
    async def lookup(realm: str, round_id: str) -> set[str]:
        return set((sealed or {}).get(round_id, ()))
    return MachineService(chron, escrow_lookup=lookup)


async def test_no_machine_declared_is_a_clean_answer_not_an_exception():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    svc = _svc(c)
    assert await svc.state(A) == {"error": "no machine declared"}
    assert (await svc.act(A, "call", {}))["error"] == "no machine declared"
    await c.close()


async def test_act_returns_the_callers_view_and_chronicles_the_event(chron):
    svc = _svc(chron)
    out = await svc.act(DEALER, "deal", {"first": "a"})
    assert out["state"] == "street" and out["actor"] == "a"
    ev = [e for e in await chron.events("r", kind=EventKind.GAME)]
    assert len(ev) == 1 and ev[0].payload["transition"] == "deal"
    # `deal` moves the pointer, so a is an ACTOR wake: the host may collapse it to the latest
    # event's actor, which it must never do to a role wake (review I1).
    assert ev[0].payload["wake"] == [] and ev[0].payload["wake_actor"] == ["a"]


async def test_rejections_are_chronicled_and_named(chron):
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    out = await svc.act(B, "call", {})
    assert out == {"error": "rejected", "check": "guard", "detail": "caller_is_actor"}
    rej = [e.payload for e in await chron.events("r", kind=EventKind.GAME)
           if e.payload["op"] == "reject"]
    assert rej and rej[0]["caller"] == "b" and rej[0]["check"] == "guard"


async def test_set_is_referee_only_and_writes_owner_entries(chron):
    svc = _svc(chron)
    assert (await svc.set(A, "pot", 1))["error"] == "rejected"
    reject = [e.payload for e in await chron.events("r", kind=EventKind.GAME)
              if e.payload["op"] == "reject"]
    assert reject and reject[0]["key"] == "pot"  # a refused write is still chronicled
    out = await svc.set(DEALER, "hole", "AhKh", owner="a")
    assert out["data"]["hole"] == {"a": "AhKh"}  # the dealer sees every owner entry
    assert (await svc.state(A))["data"]["hole"] == {"a": "AhKh"}
    assert (await svc.state(B))["data"]["hole"] == {}


async def test_state_pages_the_log_with_a_cursor_and_filters_rows(chron):
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    await svc.set(DEALER, "hole", "x", owner="a")
    await svc.act(A, "call", {})
    first = await svc.state(B, log_limit=10)
    ops = [(r["op"], r.get("transition") or r.get("key")) for r in first["log"]]
    assert ops == [("act", "deal"), ("act", "call")]  # b never sees a's hole write
    nxt = await svc.state(B, since=first["next_since"])
    assert nxt["log"] == []


async def test_escrow_completion_is_looked_up_per_call(chron):
    svc = _svc(chron, sealed={"H1": {"a", "b"}})
    await svc.act(DEALER, "deal", {"first": "a"})
    await svc.set(DEALER, "hand", "H1")
    out = await svc.act(DEALER, "resolve", {})
    assert out["state"] == "waiting"


async def test_two_acts_interleaved_across_the_append_await_serialise(chron):
    """The Arbiter #17 bug class: without the lock, both callers pass `caller_is_actor` on the
    same pre-state and both writes land."""
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    real_append = chron.append_event

    async def slow_append(*a, **k):
        await asyncio.sleep(0.01)
        return await real_append(*a, **k)
    chron.append_event = slow_append  # type: ignore[method-assign]
    r1, r2 = await asyncio.gather(svc.act(A, "call", {}), svc.act(A, "call", {}))
    outcomes = sorted(("error" in r1, "error" in r2))
    assert outcomes == [False, True], (r1, r2)  # exactly one lands
    acts = [e for e in await chron.events("r", kind=EventKind.GAME) if e.payload["op"] == "act"]
    assert len(acts) == 2  # deal + one call


async def test_a_restart_replays_only_events_after_the_latest_machine_record(chron):
    """Realm ids are reused; an older run's events must not leak into this one."""
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    await svc.act(A, "call", {})
    # a second run of the same realm id: new MACHINE record, fresh state
    await chron.append_event("r", EventKind.MACHINE, MACHINE)
    fresh = _svc(chron)
    assert (await fresh.state(A))["state"] == "waiting"
    # and a restart mid-run rebuilds what the run had reached
    await fresh.act(DEALER, "deal", {"first": "b"})
    again = _svc(chron)
    assert (await again.state(A))["actor"] == "b"


async def test_a_malformed_game_row_fails_the_realm_loudly(chron):
    """A row missing `transition`/`caller` raises a bare KeyError inside `eng.replay` — the
    service must turn that (and a genuine `ReplayError`) into a clearly-labelled `ReplayError`,
    not let a stack trace or a silent wrong-state leak out."""
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    await chron.append_event("r", EventKind.GAME, {"op": "act"})  # no transition/caller
    fresh = _svc(chron)
    with pytest.raises(ReplayError):
        await fresh.state(A)
    fresh2 = _svc(chron)
    with pytest.raises(ReplayError):
        await fresh2.act(A, "call", {})


async def test_a_stored_declaration_the_current_schema_refuses_fails_the_realm_loudly(chron):
    """The MACHINE record is validated on every cold load. Launch validation grows stricter over
    time (a guard that once passed now needs a key), so a declaration that was legal when it was
    chronicled can be refused by the schema that reloads it — that is chronicle damage from the
    service's point of view and must surface as `ReplayError`, never as a raw pydantic
    ValidationError whose text carries transition and key names to the caller."""
    import copy

    bad = copy.deepcopy(MACHINE)
    bad["declaration"]["transitions"]["deal"]["guard"] = [{"data_set_full": {"over": "player"}}]
    await chron.append_event("r", EventKind.MACHINE, bad)
    with pytest.raises(ReplayError):
        await _svc(chron).state(A)


async def test_a_cold_read_never_clobbers_a_concurrent_acts_applied_state(chron):
    """A `state()`/`declaration()` cold load takes no lock. If its chronicle read is slow, an
    `act()` can load, cache, append and apply first — the late-arriving cold load must never
    win the cache with its stale pre-append snapshot (`_get`'s `setdefault`, not unconditional
    assignment)."""
    svc = _svc(chron)
    real_events = chron.events
    first_game_call = True

    async def slow_events(realm_id, kind=None):
        nonlocal first_game_call
        if kind == EventKind.GAME and first_game_call:
            first_game_call = False
            snapshot = await real_events(realm_id, kind=kind)  # captured BEFORE the act runs
            await asyncio.sleep(0.05)
            return snapshot
        return await real_events(realm_id, kind=kind)
    chron.events = slow_events  # type: ignore[method-assign]

    state_task = asyncio.create_task(svc.state(A))
    await asyncio.sleep(0)
    await svc.act(DEALER, "deal", {"first": "a"})
    await state_task

    assert (await svc.state(A))["state"] == "street"
    second = await svc.act(DEALER, "deal", {"first": "a"})
    assert second == {"error": "rejected", "check": "from",
                       "detail": "'deal' is not available from state 'street'"}

    chron.events = real_events  # type: ignore[method-assign]
    fresh = _svc(chron)
    assert (await fresh.state(A))["state"] == "street"  # replays without error


async def test_a_huge_log_limit_is_clamped_not_honoured(chron):
    """`log_limit` comes straight from an agent's tool call. Unclamped, one agent asking for a
    billion rows builds that list in the realmtools process and ships it through the MCP session
    — a denial of service any player can spell (review M7). Clamped, never rejected: a caller
    that asks for too much gets the maximum, not an error it has to learn to handle."""
    from bearpit.realmtools.machine_service import LOG_LIMIT_MAX

    await chron.append_event("p", EventKind.MACHINE, PING_MACHINE)
    svc = _svc(chron)
    dealer = Identity("p", "dealer", True, roster=("dealer",))
    for _ in range(LOG_LIMIT_MAX + 5):
        await svc.act(dealer, "ping", {})
    page = await svc.state(dealer, log_limit=10**9)
    assert len(page["log"]) == LOG_LIMIT_MAX


async def test_set_chronicle_drops_the_locks_with_the_cached_state(chron):
    """`set_chronicle` is the re-wiring seam (the server calls it once per process, from the
    serving loop). It dropped `_live` and kept `_locks` — and an asyncio.Lock belongs to the loop
    it was created on, so a lock left over from a different loop is a RuntimeError on the next
    act, not a stale cache."""
    svc = _svc(chron)
    await svc.act(DEALER, "deal", {"first": "a"})
    assert svc._live and svc._locks  # noqa: SLF001 - the caches this seam must clear
    svc.set_chronicle(chron)
    assert svc._live == {} and svc._locks == {}  # noqa: SLF001


async def test_the_participant_log_view_matches_the_spec_table_through_the_service(chron):
    """Spec §3's participant log table, row by row, through the SERVICE seam rather than
    `log_row` in isolation — paging, the machine-event floor and the visibility filter all have
    to agree for a participant to read its own log correctly. Paged one row at a time, so a page
    boundary lands in the middle of the hidden run."""
    await chron.append_event("t", EventKind.MACHINE, TABLE_MACHINE)
    svc = _svc(chron)
    dealer, a, b = (Identity("t", "dealer", True, roster=("a", "b")),
                    Identity("t", "a", False), Identity("t", "b", False))
    start = (await svc.state(a))["next_since"]               # the cursor before anything happened
    await svc.act(dealer, "deal", {"first": "a"})            # act, public log
    await svc.set(dealer, "secret", "the deck order")        # set, referee key
    await svc.set(dealer, "hole", "2c7d", owner="b")         # set, owner key — b's
    await svc.act(dealer, "peek", {})                        # act, referee log
    await svc.set(dealer, "hole", "AhKh", owner="a")         # set, owner key — a's
    await svc.act(b, "call", {})                             # reject, public-log transition
    await svc.set(dealer, "pot", 40)                         # set, public key

    def rows(view):
        return [(r["op"], r.get("transition") or r.get("key"), r.get("owner")) for r in view]

    seen: list[tuple[str, str, str | None]] = []
    since = start
    while True:
        page = await svc.state(a, since=since, log_limit=1)
        if not page["log"]:
            break
        seen += rows(page["log"])
        since = page["next_since"]
    assert seen == [
        ("act", "deal", None),      # act on a public-log transition: shown
        ("set", "hole", "a"),       # set on an owner key: only to that owner...
        ("reject", "call", None),   # reject inherits its transition's log (public here)
        ("set", "pot", None),       # set on a public key: shown
    ]                               # `peek` (referee log) and `secret` (referee key): never
    assert rows((await svc.state(b, log_limit=50))["log"]) == [
        ("act", "deal", None), ("set", "hole", "b"), ("reject", "call", None),
        ("set", "pot", None),
    ]                               # b sees ITS OWN hole write and not a's
    assert len((await svc.state(dealer, log_limit=50))["log"]) == 7  # the referee sees every row


async def test_paging_never_skips_a_visible_row(chron):
    """5 visible acts, paged 2 at a time: the concatenated pages must be exactly those 5 acts,
    in order — a page boundary landing mid-scan must not drop the rows after it."""
    await chron.append_event("p", EventKind.MACHINE, PING_MACHINE)
    svc = _svc(chron)
    dealer = Identity("p", "dealer", True, roster=("dealer",))
    start = (await svc.state(dealer))["next_since"]  # before any pings: == machine_event_id
    for _ in range(5):
        await svc.act(dealer, "ping", {})
    since = start
    seen: list[str] = []
    while True:
        page = await svc.state(dealer, since=since, log_limit=2)
        if not page["log"]:
            break
        seen += [r["transition"] for r in page["log"]]
        since = page["next_since"]
    assert seen == ["ping"] * 5
