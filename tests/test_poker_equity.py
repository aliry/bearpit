import importlib.util
import pathlib
import time

import pytest

_SRC = pathlib.Path(__file__).parent.parent / (
    "examples/poker-table/agents/vega/resources/equity.py")


def _load():
    spec = importlib.util.spec_from_file_location("equity", _SRC)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def eq():
    return _load()


def test_the_two_seats_ship_the_same_calculator(eq):
    a = pathlib.Path(__file__).parent.parent / (
        "examples/poker-table/agents/vega/resources/equity.py")
    b = pathlib.Path(__file__).parent.parent / (
        "examples/poker-table/agents/rigel/resources/equity.py")
    assert a.read_bytes() == b.read_bytes()


def test_aces_crush_and_rags_do_not(eq):
    """Preflop heads-up, AA wins about 85% and 72o about 35%. Wide bands: this pins that the
    simulation is answering the question asked, not that it is precise."""
    assert 0.80 <= eq.equity("Ah Ad", [], 1, trials=3000, seed=7) <= 0.90
    assert 0.28 <= eq.equity("7h 2d", [], 1, trials=3000, seed=7) <= 0.42


def test_more_opponents_is_less_equity(eq):
    one = eq.equity("Ah Kh", [], 1, trials=3000, seed=7)
    five = eq.equity("Ah Kh", [], 5, trials=3000, seed=7)
    assert one > five


def test_a_made_flush_on_the_river_is_almost_always_good(eq):
    assert eq.equity("Ah Kh", "2h 7h 9h Jc 3d".split(), 1, trials=1500, seed=7) >= 0.85


def test_the_same_seed_gives_the_same_answer(eq):
    assert eq.equity("Ah Kh", [], 2, trials=800, seed=3) == eq.equity(
        "Ah Kh", [], 2, trials=800, seed=3)


def test_pot_odds_are_the_share_a_call_must_win(eq):
    assert eq.pot_odds(50, 150) == pytest.approx(0.25)     # call 50 to win 200
    assert eq.pot_odds(0, 100) == 0.0


def test_it_answers_fast_enough_to_be_used_in_a_turn(eq):
    """An agent calls this inside one turn. A calculator that takes a minute is one nobody uses."""
    start = time.monotonic()
    eq.equity("Ah Kh", ["2c", "7d", "9h"], 3, trials=2000, seed=1)
    assert time.monotonic() - start < 5.0


# --- Fix round 1: the caller is an LLM typing card strings into run_code. -------------------


def test_a_card_shared_between_hole_and_board_is_rejected(eq):
    """Today this silently returns 0.9935 — a card counted twice inflates equity."""
    with pytest.raises(ValueError, match="Ah|Kh"):
        eq.equity("Ah Kh", "Ah Kh 9h".split(), 1)


def test_a_repeated_hole_card_is_rejected(eq):
    """Today `equity('Ah Ah', [], 1)` silently returns 0.8705 for a hand that cannot exist."""
    with pytest.raises(ValueError, match="Ah"):
        eq.equity("Ah Ah", [], 1)


def test_suit_case_is_normalised_here_too(eq):
    """The same wrong-case typo the resolver corrects, not rejects."""
    assert 0.0 <= eq.equity("Ah KH", [], 1, trials=200, seed=1) <= 1.0


def test_a_six_card_board_is_rejected(eq):
    """Today a 6-card board drives to_draw negative and equity() returns 1.0 unconditionally."""
    with pytest.raises(ValueError, match="board"):
        eq.equity("Ah Kh", "2c 3c 4c 5c 6c 7c".split(), 1)


def test_zero_trials_is_rejected(eq):
    """Today trials=0 is a bare ZeroDivisionError."""
    with pytest.raises(ValueError, match="trials"):
        eq.equity("Ah Kh", [], 1, trials=0)


def test_negative_opponents_is_rejected(eq):
    with pytest.raises(ValueError, match="opponents"):
        eq.equity("Ah Kh", [], -1)


def test_pot_odds_rejects_a_negative_pot(eq):
    """Today pot_odds(50, -50) is a bare ZeroDivisionError."""
    with pytest.raises(ValueError, match="pot"):
        eq.pot_odds(50, -50)
