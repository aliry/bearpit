"""Sealed-submit escrow + tally rulesets (M10). This is the RPS/Exchange fix as code."""

import pytest

from agentrealm.chronicle import Chronicle
from agentrealm.realmtools import SealedError, SealedEscrow, TallyError, tally

# --- tally rulesets ---------------------------------------------------------
# RPS's rules live in the caller's config, never in the platform (Principle 10 / ADR-002).
_RPS = {"beats": {"rock": ["scissors"], "scissors": ["paper"], "paper": ["rock"]}}


def test_dominance():
    assert tally("dominance", {"vela": "scissors", "orin": "paper"}, _RPS).result == "vela"
    assert tally("dominance", {"vela": "rock", "orin": "paper"}, _RPS).result == "orin"
    assert tally("dominance", {"vela": "rock", "orin": "rock"}, _RPS).kind == "tie"
    # an invalid token forfeits; a valid token beats it; both invalid = tie
    assert tally("dominance", {"vela": "rock", "orin": "banana"}, _RPS).result == "vela"
    assert tally("dominance", {"vela": "kiwi", "orin": "banana"}, _RPS).kind == "tie"
    # an N-player rock/paper/scissors cycle has no dominator -> tie
    assert tally("dominance", {"a": "rock", "b": "paper", "c": "scissors"}, _RPS).kind == "tie"
    with pytest.raises(TallyError):
        tally("dominance", {"vela": "rock", "orin": "paper"})  # missing config[beats]


def test_high_bid():
    r = tally("high-bid", {"vela": "40", "orin": "62", "mira": "45"})
    assert r.result == "orin" and r.detail["winning_bid"] == 62
    assert tally("high-bid", {"vela": "50", "orin": "50"}).kind == "tie"  # top tie
    with pytest.raises(TallyError):
        tally("high-bid", {"vela": "not-a-number", "orin": "5"})


def test_vote_rulesets():
    votes = {"a": "yes", "b": "yes", "c": "no"}
    assert tally("plurality", votes).result == "yes"
    assert tally("majority", votes).result == "yes"  # 2/3 > half
    assert tally("majority", {"a": "yes", "b": "no"}).kind == "tie"  # no majority
    assert tally("unanimous", {"a": "yes", "b": "yes"}).result == "yes"
    assert tally("unanimous", votes).kind == "tie"
    assert tally("plurality", {"a": "x", "b": "y"}).kind == "tie"  # 1-1 tie


def test_low_bid_reverse_auction():
    r = tally("low-bid", {"vela": "40", "orin": "62", "mira": "45"})
    assert r.result == "vela" and r.detail["winning_bid"] == 40  # lowest wins
    assert tally("low-bid", {"vela": "50", "orin": "50"}).kind == "tie"


def test_unknown_ruleset():
    with pytest.raises(TallyError):
        tally("borda-count", {"a": "x"})


def test_register_ruleset_extension():
    from agentrealm.realmtools.tally import TallyResult, register_ruleset
    register_ruleset("custom:pv", lambda sub, config: TallyResult("custom:pv", "vela", "agent"))
    assert tally("custom:pv", {"vela": "x", "orin": "y"}).result == "vela"


def test_schema_ruleset_parity_with_tally():
    from agentrealm.core.schema import BUILTIN_RULESETS as schema_set
    from agentrealm.realmtools.tally import BUILTIN_RULESETS as tally_set
    assert schema_set == tally_set  # the two lists must never drift


# --- escrow -----------------------------------------------------------------
@pytest.fixture
async def chron():
    c = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    yield c
    await c.close()


async def test_seal_is_hidden_immutable_and_atomic_reveal(chron: Chronicle):
    escrow = SealedEscrow(chron, "r1", {"vela", "orin"})
    await escrow.submit("1", "vela", "rock")

    # status shows WHO sealed, never WHAT — peers can't see the payload
    st = escrow.status("1")
    assert st == {"submitted": ["vela"], "pending": ["orin"]}
    assert not escrow.all_in("1")

    # immutable after seal
    with pytest.raises(SealedError):
        await escrow.submit("1", "vela", "paper")
    # non-participants rejected
    with pytest.raises(SealedError):
        await escrow.submit("1", "intruder", "rock")

    await escrow.submit("1", "orin", "paper")
    assert escrow.all_in("1")
    revealed = await escrow.reveal("1")
    assert revealed == {"vela": "rock", "orin": "paper"}
    # deciding the round is a deterministic tally over the revealed pair
    assert tally("dominance", revealed, _RPS).result == "orin"

    # reveal is one-shot; nothing can be sealed into a revealed round
    with pytest.raises(SealedError):
        await escrow.reveal("1")
    with pytest.raises(SealedError):
        await escrow.submit("1", "vela", "scissors")


async def test_chronicle_holds_markers_then_payloads(chron: Chronicle):
    escrow = SealedEscrow(chron, "r1", {"vela", "orin"})
    await escrow.submit("1", "vela", "rock")
    await escrow.submit("1", "orin", "paper")
    # before reveal: only sealed markers, and they carry NO payload
    seals = await chron.events("r1", kind="sealed_submit")
    assert len(seals) == 2 and all("payload" not in e.payload for e in seals)
    await escrow.reveal("1")
    reveal_evs = await chron.events("r1", kind="reveal")
    assert reveal_evs[0].payload["submissions"] == {"vela": "rock", "orin": "paper"}
