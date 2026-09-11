"""The resolver ships inside the dealer's container, so it is loaded by path, exactly the way
tests/test_border_states_adjudicator.py loads its own scenario's module."""
import importlib.util
import pathlib
import random

import pytest

_SRC = pathlib.Path(__file__).parent.parent / (
    "examples/poker-table/agents/pitboss/resources/poker_resolver.py")


def _load():
    spec = importlib.util.spec_from_file_location("poker_resolver", _SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pr():
    return _load()


def test_the_commitment_binds_the_deck_before_it_is_dealt(pr):
    """The dealer publishes commit(seed) before dealing and the seed after settling, so any
    player can re-derive the exact deck. A commitment that did not bind the deck would make the
    whole ceremony theatre."""
    assert pr.verify("s3cret", pr.commit("s3cret"))
    assert not pr.verify("other", pr.commit("s3cret"))
    assert pr.deck_for("s3cret") == pr.deck_for("s3cret")
    assert pr.deck_for("s3cret") != pr.deck_for("other")
    deck = pr.deck_for("s3cret")
    assert len(deck) == 52 and len(set(deck)) == 52


def test_dealing_gives_every_seat_two_cards_and_nobody_shares_one(pr):
    out = pr.deal("s3cret", ["vega", "orion", "lyra"])
    assert sorted(out["hole"]) == ["lyra", "orion", "vega"]
    cards = [c for h in out["hole"].values() for c in h.split()] + out["board"]
    assert len(out["board"]) == 5
    assert len(cards) == len(set(cards)) == 11


@pytest.mark.parametrize("better,worse", [
    ("As Ks Qs Js Ts", "Ah Ad Ac Ks Kd"),      # straight flush > quads
    ("Ah Ad Ac As Kd", "Ah Ad Ac Ks Kd"),      # quads > full house
    ("Ah Ad Ac Ks Kd", "Ah Kh Qh Jh 9h"),      # full house > flush
    ("Ah Kh Qh Jh 9h", "Ah Kd Qc Js Td"),      # flush > straight
    ("Ah Kd Qc Js Td", "Ah Ad Ac Ks Qd"),      # straight > trips
    ("Ah Ad Ac Ks Qd", "Ah Ad Ks Kd Qc"),      # trips > two pair
    ("Ah Ad Ks Kd Qc", "Ah Ad Ks Qd Jc"),      # two pair > pair
    ("Ah Ad Ks Qd Jc", "Ah Kd Qc Js 9d"),      # pair > high card
])
def test_the_hand_ladder_is_in_the_right_order(pr, better, worse):
    assert pr.rank7(better.split()) > pr.rank7(worse.split())


def test_the_wheel_is_a_straight_but_the_lowest_one(pr):
    """A-2-3-4-5 plays as a five-high straight. An evaluator that reads the ace as high scores it
    as ace-high nothing, and a player who moved all-in on a made straight loses to air."""
    wheel = pr.rank7("Ah 2d 3c 4s 5h".split())
    assert wheel > pr.rank7("Ah Kd Qc Js 9d".split())          # beats high card
    assert wheel < pr.rank7("2h 3d 4c 5s 6h".split())          # lowest straight
    assert "straight" in pr.hand_name("Ah 2d 3c 4s 5h".split())


def test_seven_cards_are_scored_on_their_best_five(pr):
    assert pr.rank7("Ah Kh Qh Jh Th 2c 3d".split()) == pr.rank7("Ah Kh Qh Jh Th".split())


def test_a_split_pot_names_both_seats_in_one_tier(pr):
    """Two seats playing the same board split. The award must divide the pot, not hand it to
    whichever name sorted first."""
    board = "2c 7d 9h Jc Qs".split()
    hole = {"vega": "Ah Kh", "orion": "Ad Kd"}
    out = pr.award({"vega": 100, "orion": 100}, ["vega", "orion"], hole, board)
    assert out["ranking"][0] == ["orion", "vega"] or out["ranking"][0] == ["vega", "orion"]
    assert len(out["ranking"][0]) == 2
    assert out["awards"] == {"vega": 100, "orion": 100}


def test_a_short_all_in_cannot_win_the_side_pot(pr):
    """lyra is all-in for 50 into a pot two others build to 200 each. The main pot is 150 and
    lyra can win it; the 300 above it is a side pot only vega and orion are eligible for — even
    when lyra holds the best hand. Paying a short stack the whole pot is the classic bug."""
    contributions = {"lyra": 50, "vega": 200, "orion": 200}
    board = "2c 7d 9h Jc 3s".split()
    hole = {"lyra": "Ah As", "vega": "Kh Kd", "orion": "5c 4d"}   # lyra best, vega second
    out = pr.award(contributions, ["lyra", "vega", "orion"], hole, board)
    assert out["pots"][0] == {"amount": 150, "eligible": ["lyra", "orion", "vega"]}
    assert out["pots"][1] == {"amount": 300, "eligible": ["orion", "vega"]}
    assert out["awards"] == {"lyra": 150, "vega": 300}
    assert sum(out["awards"].values()) == sum(contributions.values())


def test_every_chip_is_awarded_whatever_the_shape(pr):
    """Chips must never be created or destroyed — the table's stacks are recomputed from this."""
    contributions = {"a": 35, "b": 200, "c": 200, "d": 75}
    board = "2c 7d 9h Jc 3s".split()
    hole = {"a": "Ah As", "b": "Kh Kd", "c": "Qc Qd", "d": "Jh Js"}
    out = pr.award(contributions, list(contributions), hole, board)
    assert sum(out["awards"].values()) == sum(contributions.values())


# --- Fix round 1: the caller is an LLM typing card strings into run_code. -------------------


def test_a_duplicate_card_in_the_multiset_is_rejected_not_scored(pr):
    """A duplicated ace must not silently become a phantom flush ranked above real hands."""
    with pytest.raises(ValueError, match="Ah"):
        pr.rank7("Ah Kh Ah 7h 9h Jc 3d".split())


def test_an_uppercase_suit_is_corrected_not_rejected(pr):
    """An LLM's wrong-case typo ('KH') is the likeliest mistake at the table — normalise it into
    the flush it actually is, don't destroy the flush by treating 'H' as a foreign suit."""
    assert pr.rank7("Ah KH Qh Jh 9h".split())[0] == 5   # flush, not high card


def test_an_invalid_token_names_itself_in_the_error(pr):
    with pytest.raises(ValueError, match="Ax"):
        pr.rank7("Ax Kh Qh Jh 9h".split())
    with pytest.raises(ValueError, match="Ahh"):
        pr.rank7("Ahh Kh Qh Jh 9h".split())


def test_fewer_than_five_cards_names_the_count(pr):
    with pytest.raises(ValueError, match="4"):
        pr.rank7("Ah Kh Qh Jh".split())
    with pytest.raises(ValueError, match="4"):
        pr.hand_name("Ah Kh Qh Jh".split())


def test_award_catches_a_card_shared_between_two_seats(pr):
    """A duplicate across two holes never shows up inside a single seat's rank7 call — award
    must check the whole table's cards together."""
    board = "2c 7d 9h Jc Qs".split()
    hole = {"vega": "Ah Kh", "orion": "Ah Kd"}
    with pytest.raises(ValueError, match="Ah"):
        pr.award({"vega": 100, "orion": 100}, ["vega", "orion"], hole, board)


def test_a_folded_top_contributor_rolls_into_the_pot_below(pr):
    """charlie overbets to 137 and folds; alpha and bravo see it through at 100 each. The top
    37-chip layer has no live eligible seat — that's dead money, and it joins the pot below
    rather than vanishing or raising."""
    contributions = {"alpha": 100, "bravo": 100, "charlie": 137}
    board = "2c 7d 9h Jc 3s".split()
    hole = {"alpha": "Ah As", "bravo": "Kh Kd"}
    out = pr.award(contributions, ["alpha", "bravo"], hole, board)
    assert out["pots"] == [{"amount": 337, "eligible": ["alpha", "bravo"]}]
    assert sum(out["awards"].values()) == sum(contributions.values())


def test_dead_money_with_no_earlier_pot_raises_a_named_error(pr):
    """Every contributor is out of the hand and no earlier layer exists to absorb their chips —
    a bare max() traceback tells the dealer nothing mid-hand."""
    with pytest.raises(ValueError, match="eligible"):
        pr.pots({"alpha": 50}, [])


def test_a_negative_contribution_is_rejected_not_paid_out(pr):
    """The dealer validates amounts upstream; the resolver must not invent chips when it doesn't."""
    with pytest.raises(ValueError, match="alpha"):
        pr.pots({"alpha": -1000, "bravo": 10, "charlie": 10}, ["alpha", "bravo", "charlie"])


def test_deal_refuses_more_seats_than_the_deck_can_serve(pr):
    with pytest.raises(ValueError, match="24"):
        pr.deal("s3cret", [str(i) for i in range(24)])


def test_award_property_folded_top_contributors_never_raise_and_chips_conserve(pr):
    """A few thousand random side-pot shapes, some with a folded seat as the top contributor —
    zero raises, exact chip conservation."""
    rng = random.Random(20260911)
    board = "2c 7d 9h Jc 3s".split()
    for _ in range(3000):
        n = rng.randint(2, 5)
        seats = [f"s{i}" for i in range(n)]
        contributions = {s: rng.randint(10, 300) for s in seats}
        n_live = rng.randint(1, n)
        live = seats[:n_live]
        deck = [r + s for r in "23456789TJQKA" for s in "cdhs" if r + s not in board]
        rng.shuffle(deck)
        hole = {s: f"{deck[2 * i]} {deck[2 * i + 1]}" for i, s in enumerate(live)}
        try:
            out = pr.award(contributions, live, hole, board)
        except ValueError as e:
            assert "eligible" in str(e), f"unexpected raise for {contributions}, {live}: {e}"
            continue
        assert sum(out["awards"].values()) == sum(contributions.values())
