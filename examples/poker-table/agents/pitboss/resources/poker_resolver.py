"""Poker Table — the dealer's arithmetic (deck commitment, hand ranking, side pots).

Runs inside the Pitboss's container via run_code. Cards are two characters: rank in
`23456789TJQKA`, suit in `cdhs` — `"Ah"`, `"Td"`, `"2c"`. A hole string is two cards separated by
one space. stdlib only (hashlib, random, itertools).

The `to:` contract (repeated verbatim in the dealer rubric and all six personas):

Every `call`, `raise` and `all_in` carries `to:` — the total number of chips you will have put
into the pot on this street once this action stands, not the increment. If the price is 60 and
you have already put in 20, you call with `to: 60`. The resolver reads the last `to` each seat
posted on a street as that seat's contribution; an increment posted where a total belongs
corrupts the pot silently.

Odd-chip rule: when a pot does not divide evenly among its tied winners, the indivisible chip (or
chips) go to the first seat in the pot's eligible order, which is alphabetical.

Every card-bearing function is an airlock: card tokens are normalised (rank upper, suit lower —
an LLM's wrong-case typo is corrected, not rejected) and validated before anything is scored, and
`award` checks the whole table's hole cards plus the board together, since a card shared between
two seats never shows up inside a single seat's own hand.
"""

from __future__ import annotations

import hashlib
import itertools
import random
import re
from collections import Counter

_CARD_RE = re.compile(r"^[23456789TJQKA][cdhs]$")

RANKS = "23456789TJQKA"
SUITS = "cdhs"
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}

RANK_SINGULAR = {
    2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine",
    10: "ten", 11: "jack", 12: "queen", 13: "king", 14: "ace",
}
RANK_PLURAL = {
    2: "twos", 3: "threes", 4: "fours", 5: "fives", 6: "sixes", 7: "sevens", 8: "eights",
    9: "nines", 10: "tens", 11: "jacks", 12: "queens", 13: "kings", 14: "aces",
}

CATEGORY_NAMES = {
    0: "high card", 1: "pair", 2: "two pair", 3: "three of a kind", 4: "straight",
    5: "flush", 6: "full house", 7: "four of a kind", 8: "straight flush",
}


def commit(seed: str) -> str:
    """sha256 hexdigest of the seed — published before the deck is dealt."""
    return hashlib.sha256(seed.encode()).hexdigest()


def verify(seed: str, commitment: str) -> bool:
    """True if `seed` is the pre-image the dealer committed to."""
    return commit(seed) == commitment


def deck_for(seed: str) -> list[str]:
    """52 unique cards, deterministically shuffled from `seed`."""
    deck = [r + s for r in RANKS for s in SUITS]
    random.Random(seed).shuffle(deck)
    return deck


def deal(seed: str, seats: list[str]) -> dict:
    """Two cards per seat in seat order, no burns, then five board cards."""
    if 2 * len(seats) + 5 > 52:
        raise ValueError(
            f"deck cannot serve {len(seats)} seats plus a 5-card board (52-card deck)")
    deck = deck_for(seed)
    hole = {}
    i = 0
    for seat in seats:
        hole[seat] = f"{deck[i]} {deck[i + 1]}"
        i += 2
    board = deck[i:i + 5]
    return {"hole": hole, "board": board}


def _cards(tokens: list[str]) -> list[str]:
    """Normalise and validate a multiset of card tokens: strip whitespace, uppercase the rank,
    lowercase the suit (so `KH`, `kh` and `Kh` all become `Kh` — a wrong-case typo is the
    likeliest LLM mistake and must be corrected, not rejected), then require each token to match
    rank+suit exactly and the whole multiset to hold no duplicate card. Raises `ValueError` naming
    the offending token or the duplicate card."""
    normalised = []
    for tok in tokens:
        t = tok.strip()
        card = t[:1].upper() + t[1:].lower() if len(t) == 2 else t
        if not _CARD_RE.match(card):
            raise ValueError(f"not a card: {tok!r}")
        normalised.append(card)
    seen: set[str] = set()
    for card in normalised:
        if card in seen:
            raise ValueError(f"duplicate card: {card}")
        seen.add(card)
    return normalised


def _score5(cards: list[str]) -> tuple:
    """Score exactly five cards as (category, *tiebreakers), bigger is better."""
    ranks = sorted((RANK_VALUE[c[0]] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    is_flush = len(set(suits)) == 1

    unique_ranks = sorted(set(ranks), reverse=True)
    is_straight = False
    straight_high = 0
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]
        elif unique_ranks == [14, 5, 4, 3, 2]:          # the wheel: plays as a five-high straight
            is_straight = True
            straight_high = 5

    if is_straight and is_flush:
        return (8, straight_high)

    counts = Counter(ranks)
    groups = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    pattern = [n for _, n in groups]

    if pattern == [4, 1]:
        return (7, groups[0][0], groups[1][0])
    if pattern == [3, 2]:
        return (6, groups[0][0], groups[1][0])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, straight_high)
    if pattern == [3, 1, 1]:
        kickers = sorted((groups[1][0], groups[2][0]), reverse=True)
        return (3, groups[0][0], *kickers)
    if pattern == [2, 2, 1]:
        pairs = sorted((groups[0][0], groups[1][0]), reverse=True)
        return (2, *pairs, groups[2][0])
    if pattern == [2, 1, 1, 1]:
        kickers = sorted((groups[1][0], groups[2][0], groups[3][0]), reverse=True)
        return (1, groups[0][0], *kickers)
    return (0, *ranks)


def rank7(cards: list[str]) -> tuple:
    """Score five or more cards on the best five of them; bigger is better."""
    cards = _cards(cards)
    if len(cards) < 5:
        raise ValueError(f"rank7 needs at least 5 cards, got {len(cards)}")
    return max(_score5(list(combo)) for combo in itertools.combinations(cards, 5))


def hand_name(cards: list[str]) -> str:
    """A human-readable name for the best five of `cards`, e.g. 'flush, ace high'."""
    cards = _cards(cards)
    if len(cards) < 5:
        raise ValueError(f"hand_name needs at least 5 cards, got {len(cards)}")
    best = max(_score5(list(combo)) for combo in itertools.combinations(cards, 5))
    category, *tiebreak = best

    if category in (0, 4, 5, 8):
        return f"{CATEGORY_NAMES[category]}, {RANK_SINGULAR[tiebreak[0]]} high"
    if category == 1:
        return f"pair of {RANK_PLURAL[tiebreak[0]]}"
    if category == 2:
        return f"two pair, {RANK_PLURAL[tiebreak[0]]} and {RANK_PLURAL[tiebreak[1]]}"
    if category == 3:
        return f"three of a kind, {RANK_PLURAL[tiebreak[0]]}"
    if category == 6:
        return f"full house, {RANK_PLURAL[tiebreak[0]]} full of {RANK_PLURAL[tiebreak[1]]}"
    if category == 7:
        return f"four of a kind, {RANK_PLURAL[tiebreak[0]]}"
    return CATEGORY_NAMES[category]


def pots(contributions: dict[str, int], live: list[str]) -> list[dict]:
    """Side-pot layers, main pot first. Walks the distinct contribution levels in ascending
    order; at each level every contributor (folded seats included — their money is in the pot)
    gives up min(level, contributed) minus what was already taken from them. A layer's eligible
    seats are the `live` seats that contributed at least that level.

    A layer with no eligible live seat is dead money (every contributor at that level folded):
    it rolls into the pot below rather than being dropped or awarded to nobody. If there is no
    pot below to absorb it, that is unresolvable and raises `ValueError`."""
    for seat, total in contributions.items():
        if total < 0:
            raise ValueError(f"negative contribution from {seat!r}: {total}")

    levels = sorted(set(contributions.values()))
    taken = dict.fromkeys(contributions, 0)
    result: list[dict] = []
    for level in levels:
        amount = 0
        for seat, total in contributions.items():
            share = min(level, total) - taken[seat]
            amount += share
            taken[seat] = min(level, total)
        if amount <= 0:
            continue
        eligible = sorted(seat for seat in live if contributions[seat] >= level)
        if not eligible:
            if not result:
                raise ValueError(
                    f"dead money at level {level} has no eligible seat and no earlier pot to "
                    "absorb it")
            result[-1]["amount"] += amount
            continue
        result.append({"amount": amount, "eligible": eligible})
    return result


def award(contributions: dict[str, int], live: list[str],
          hole: dict[str, str], board: list[str]) -> dict:
    """Settle a hand: side pots, chip awards, the full ranking (best tier first, ties grouped),
    and each live seat's hand name (see the module docstring for the odd-chip rule)."""
    seats_order = list(hole)
    flat = list(board) + [c for seat in seats_order for c in hole[seat].split()]
    normalised = _cards(flat)              # validates + dedupes across every seat and the board
    n_board = len(board)
    board = normalised[:n_board]
    idx = n_board
    hole = {}
    for seat in seats_order:
        hole[seat] = f"{normalised[idx]} {normalised[idx + 1]}"
        idx += 2

    all_pots = pots(contributions, live)
    scores = {seat: rank7(hole[seat].split() + board) for seat in live}
    names = {seat: hand_name(hole[seat].split() + board) for seat in live}

    order = sorted(live, key=lambda s: scores[s], reverse=True)
    ranking: list[list[str]] = []
    for seat in order:
        if ranking and scores[seat] == scores[ranking[-1][0]]:
            ranking[-1].append(seat)
        else:
            ranking.append([seat])
    for tier in ranking:
        tier.sort()

    awards: dict[str, int] = {}
    for pot in all_pots:
        best = max(scores[s] for s in pot["eligible"])
        winners = [s for s in pot["eligible"] if scores[s] == best]
        base, remainder = divmod(pot["amount"], len(winners))
        for i, seat in enumerate(winners):
            share = base + (remainder if i == 0 else 0)
            if share:
                awards[seat] = awards.get(seat, 0) + share

    return {"pots": all_pots, "awards": awards, "ranking": ranking, "names": names}
