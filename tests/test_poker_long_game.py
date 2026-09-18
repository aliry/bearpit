"""A scenario's duration cap must accommodate the hand count it advertises.

`poker-table` offers `hands` up to 50 and capped the realm at 5 hours. A 20-hand run was therefore
guaranteed to be cut off — and was: poker-b reached hand 11 and concluded `duration — reached 5h`
with no verdict. The parameter and the termination condition disagreed, and nothing checked that
they agreed.

Measured pace: roughly 25-30 minutes per hand with six seats on a CLI-backed provider. The cap has
to clear the DEFAULT hand count with real margin; the far end of the parameter's range is the
operator's problem, but the default must never be unreachable.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from bearpit.core import load_package

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
# From five chronicled runs: the slowest averaged ~30 min/hand under provider contention.
MINUTES_PER_HAND = 30


def _minutes(limit: str) -> int:
    m = re.fullmatch(r"(\d+)([hm])", limit.strip())
    assert m, f"unparseable duration limit {limit!r}"
    n, unit = int(m.group(1)), m.group(2)
    return n * 60 if unit == "h" else n


@pytest.fixture
def poker():
    return load_package(EXAMPLES / "poker-table")


def test_the_cap_covers_every_hand_count_the_scenario_offers(poker):
    """The parameter's own ceiling must be reachable. Advertising 50 hands under a cap that fits
    24 is the same defect as the 5h cap that killed a 20-hand run: the manifest promises a run it
    cannot finish, and the operator only finds out five hours in."""
    duration = next((t.limit for t in poker.spec.termination if str(t.type) == "duration"), None)
    assert duration, "poker-table has no duration cap"
    hands = poker.spec.parameters.get("hands")
    assert hands is not None, "poker-table no longer declares a `hands` parameter"
    need = int(hands.max) * MINUTES_PER_HAND
    assert _minutes(duration) >= need, (
        f"the cap is {duration} but the parameter offers up to {int(hands.max)} hands, which "
        f"needs about {need} minutes at {MINUTES_PER_HAND} min/hand")


def test_the_cap_clears_a_long_game_too(poker):
    """The case that actually failed. 20 hands is the long-run test the scenario must support."""
    duration = next(t.limit for t in poker.spec.termination if str(t.type) == "duration")
    assert _minutes(duration) >= 20 * MINUTES_PER_HAND, (
        f"the cap is {duration}; a 20-hand game needs about {20 * MINUTES_PER_HAND} minutes. "
        f"poker-b died at hand 11 of 20 on exactly this.")


def test_a_stall_still_ends_a_wedged_realm_quickly(poker):
    """Raising the wall-clock cap must not remove the thing that catches a genuinely stuck realm —
    otherwise a wedge burns the full 12 hours instead of 30 minutes."""
    stall = next((t.limit for t in poker.spec.termination if str(t.type) == "stall"), None)
    assert stall and _minutes(stall) <= 60, f"stall guard is {stall!r}; it must stay tight"


def test_spend_is_bounded_whatever_the_clock_says(poker):
    """The other half of raising the cap: budgets, not the clock, are what bound the cost."""
    total = sum(a.budget.max_usd for a in poker.agents if a.budget and a.budget.max_usd)
    assert total <= 150, f"total agent budget is ${total}; a long run could cost that much"
    assert all(a.budget and a.budget.max_usd for a in poker.agents), (
        "an agent with no budget cap can spend without limit for the whole 12 hours")
