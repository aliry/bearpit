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
    assert len(ev) == 1 and ev[0].payload["transition"] == "deal" and ev[0].payload["wake"] == ["a"]


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


async def test_paging_never_skips_a_visible_row(chron):
    """5 visible acts, paged 2 at a time: the concatenated pages must be exactly those 5 acts,
    in order — a page boundary landing mid-scan must not drop the rows after it."""
    decl = {
        "roles": {"dealer": {"members": "referee"}},
        "states": ["s"], "initial": "s",
        "transitions": {"ping": {"from": "s", "to": "same", "by": "dealer"}},
    }
    machine = {"version": 1, "declaration": decl,
               "members": {"dealer": ["dealer"]}, "roster": ["dealer"], "referee": "dealer"}
    await chron.append_event("p", EventKind.MACHINE, machine)
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
