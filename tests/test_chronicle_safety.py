"""What an agent may write into the chronicle, and what happens to what slips past.

Two characters end a realm in ways nothing else in the suite covers, and they do it for opposite
reasons: a NUL is refused by a Postgres text column, and an unpaired surrogate is *accepted*
everywhere and then cannot be encoded into any HTTP response. The second is the dangerous one —
it stores, it round-trips, and the damage only appears when someone opens the console.

The tests below drive the two halves of the guard: the ingress that can tell the agent, and the
response encoder that cannot but must not fall over either.
"""
from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from bearpit.chronicle import Chronicle, EventKind
from bearpit.chronicle.safety import MAX_VALUE_CHARS, oversized, unstorable
from bearpit.gatekeeper.api import SafeJSONResponse, create_app
from bearpit.realmtools.machine_service import MachineService
from bearpit.realmtools.service import Identity

# Built at runtime. A test file that describes a surrogate must not contain one either — writing
# `"\ud800"` in source makes the module itself unencodable, which is how this was first found.
SURROGATE = chr(0xD800)
NUL = "a" + chr(0) + "b"

DECL = {
    "roles": {"dealer": {"members": "referee"}, "player": {"members": "participants"}},
    "states": ["waiting", "street"], "initial": "waiting",
    "actor": {"over": "player", "skip": ["acted"]},
    "data": {"pot": {"visibility": "public"}, "acted": {"visibility": "public", "type": "set"}},
    "transitions": {
        "deal": {"from": "waiting", "to": "street", "by": "dealer",
                 "effects": [{"set_actor": "$args.first"}]},
    },
}
MACHINE = {"version": 1, "declaration": DECL,
           "members": {"dealer": ["dealer"], "player": ["a", "b"]},
           "roster": ["a", "b"], "referee": "dealer"}
DEALER = Identity("r", "dealer", True, roster=("a", "b"))


# ---------------------------------------------------------------- the value guard

def test_ordinary_values_pass_including_non_ascii():
    """The guard must not become a reason to strip legitimate text. Emoji are a single code
    point in Python, not a surrogate pair, and must survive untouched."""
    assert unstorable({"to": "60", "note": "héllo \U0001F600 世界"}) is None
    assert unstorable([1, 2.5, True, None, {"a": ["b"]}]) is None


def test_a_nul_is_refused_anywhere_in_the_structure():
    assert "NUL" in (unstorable({"to": NUL}) or "")
    assert "NUL" in (unstorable([[{"deep": NUL}]]) or "")


def test_an_unpaired_surrogate_is_refused_anywhere_in_the_structure():
    assert "surrogate" in (unstorable({"to": SURROGATE}) or "")
    assert "surrogate" in (unstorable([[{"deep": SURROGATE}]]) or "")


def test_a_bad_key_is_refused_and_named_as_a_key():
    why = unstorable({SURROGATE: "fine"}) or ""
    assert "key" in why and "surrogate" in why


def test_depth_is_bounded_so_the_walk_cannot_be_a_weapon():
    assert unstorable(json.loads("[" * 40 + "]" * 40)) is not None
    assert unstorable(json.loads("[" * 5 + "]" * 5)) is None


def test_oversized_reports_the_limit_it_enforced():
    assert oversized(MAX_VALUE_CHARS) is None
    assert str(MAX_VALUE_CHARS) in (oversized(MAX_VALUE_CHARS + 1) or "")


# ---------------------------------------------------------------- the ingress

@pytest.fixture
async def chron():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await c.append_event("r", EventKind.MACHINE, MACHINE)
    yield c
    await c.close()


def _svc(chron):
    async def lookup(realm: str, round_id: str) -> set[str]:
        return set()
    return MachineService(chron, escrow_lookup=lookup)


async def test_act_refuses_an_unstorable_arg_and_chronicles_nothing(chron):
    """The agent is told, and the realm is left exactly as it was. An exception here would be
    worse than the refusal: it reaches the model as a tool failure with nothing to act on."""
    svc = _svc(chron)
    out = await svc.act(DEALER, "deal", {"first": "a", "note": SURROGATE})
    assert "surrogate" in out["error"] and out["error"].startswith("args")
    assert await chron.events("r", kind=EventKind.GAME) == []


async def test_act_refuses_an_oversized_arg(chron):
    svc = _svc(chron)
    out = await svc.act(DEALER, "deal", {"first": "a", "pad": "x" * (MAX_VALUE_CHARS + 1)})
    assert str(MAX_VALUE_CHARS) in out["error"]
    assert await chron.events("r", kind=EventKind.GAME) == []


async def test_set_refuses_an_unstorable_value_key_or_owner(chron):
    svc = _svc(chron)
    assert "surrogate" in (await svc.set(DEALER, "pot", SURROGATE))["error"]
    assert "NUL" in (await svc.set(DEALER, "pot", NUL))["error"]
    assert (await svc.set(DEALER, SURROGATE, 1))["error"].startswith("key")
    assert (await svc.set(DEALER, "pot", 1, SURROGATE))["error"].startswith("owner")


async def test_a_legal_act_still_works(chron):
    """The guard must be invisible to every write that was always fine."""
    svc = _svc(chron)
    out = await svc.act(DEALER, "deal", {"first": "a"})
    assert out["state"] == "street"


# ---------------------------------------------------------------- the response encoder

def test_the_encoder_serves_a_surrogate_instead_of_raising():
    """Starlette's own render raises on this; ours must not, and must stay valid JSON."""
    with pytest.raises(UnicodeEncodeError):
        json.dumps({"v": SURROGATE}, ensure_ascii=False).encode("utf-8")
    body = SafeJSONResponse(content={"v": SURROGATE}).render({"v": SURROGATE})
    assert json.loads(body)["v"] == SURROGATE


def test_ordinary_payloads_are_byte_identical_to_starlettes():
    """The fallback must cost nothing when it is not needed — same bytes, same order."""
    from fastapi.responses import JSONResponse
    payload = {"b": 1, "a": ["x", "héllo \U0001F600"], "n": None}
    assert SafeJSONResponse(content=payload).render(payload) == \
        JSONResponse(content=payload).render(payload)


class _FakeManager:
    runs: dict = {}
    max_active = 6

    def active(self):
        return []


async def test_a_realm_holding_a_surrogate_is_still_readable_over_the_api():
    """The whole point. A surrogate stores fine, so without the encoder fallback this realm's
    endpoint answers 500 for good — and the chronicle is append-only, so it never recovers."""
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await c.append_event("wedged", EventKind.GAME,
                         {"op": "act", "caller": "vega", "args": {"to": SURROGATE}})
    app = create_app(chron=c, manager=_FakeManager())
    with TestClient(app) as client:
        r = client.get("/api/realms/wedged/events")
    assert r.status_code == 200
    # The value survives intact: escaped on the wire, a surrogate again once parsed.
    assert r.json()["events"][0]["payload"]["args"]["to"] == SURROGATE
    await c.close()
