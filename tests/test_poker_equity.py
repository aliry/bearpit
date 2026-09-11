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
