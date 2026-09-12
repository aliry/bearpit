"""Poker Table — a quant seat's equity calculator.

Ships alone inside the quant kit's container (Vega and Rigel each get a byte-identical copy), so
it is self-contained and carries its own compact hand evaluator rather than importing the
dealer's `poker_resolver.py` — the two files live in different containers and neither may depend
on the other. stdlib only (`random`, `itertools`, `collections`).

Cards are two characters: rank in `23456789TJQKA`, suit in `cdhs` — `"Ah"`, `"Td"`, `"2c"`. A hole
string is two cards separated by one space.

`equity` and `pot_odds` are airlocks: card tokens are normalised (rank upper, suit lower — a
wrong-case typo is corrected, not rejected) and validated, and a card cannot appear in both
`hole` and `board`, before any trial runs.
"""

from __future__ import annotations

import itertools
import random
import re
from collections import Counter

RANKS = "23456789TJQKA"
SUITS = "cdhs"
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}
FULL_DECK = [r + s for r in RANKS for s in SUITS]
_CARD_RE = re.compile(r"^[23456789TJQKA][cdhs]$")


def _cards(tokens: list[str]) -> list[str]:
    """Normalise and validate a multiset of card tokens: strip whitespace, uppercase the rank,
    lowercase the suit, then require each token to match rank+suit exactly and the whole
    multiset to hold no duplicate card. Raises `ValueError` naming the offending token or the
    duplicate card."""
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
    """Score exactly five cards as (category, *tiebreakers); bigger is better. 0=high card ...
    8=straight flush. The wheel (A-2-3-4-5) scores as a five-high straight, not ace-high."""
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
        elif unique_ranks == [14, 5, 4, 3, 2]:
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


def _best_of(cards: list[str]) -> tuple:
    """Best five-card score out of five or more cards."""
    return max(_score5(list(combo)) for combo in itertools.combinations(cards, 5))


def equity(hole: str, board: list[str], opponents: int,
           trials: int = 2000, seed: int = 0) -> float:
    """Monte Carlo win share for `hole` against `opponents` random hands, given the known
    `board` (0, 3, 4 or 5 cards). One trial deals every opponent's hole cards and the rest of the
    board from the deck minus all known cards; a win counts 1.0 and an n-way tie counts 1/n."""
    if trials < 1:
        raise ValueError(f"trials must be >= 1, got {trials}")
    if opponents < 0:
        raise ValueError(f"opponents must be >= 0, got {opponents}")
    if len(board) not in (0, 3, 4, 5):
        raise ValueError(f"board must have 0, 3, 4 or 5 cards, got {len(board)}")

    my_hole = _cards(hole.split())
    if len(my_hole) != 2:
        raise ValueError(f"hole must be exactly two cards, got {len(my_hole)}")
    board = _cards(board)
    _cards(my_hole + board)                # raises on a card shared between hole and board

    known = set(my_hole) | set(board)
    deck = [c for c in FULL_DECK if c not in known]
    to_draw = opponents * 2 + (5 - len(board))

    rng = random.Random(seed)
    share = 0.0
    for _ in range(trials):
        drawn = rng.sample(deck, to_draw)
        opp_holes = [drawn[i:i + 2] for i in range(0, opponents * 2, 2)]
        full_board = board + drawn[opponents * 2:]

        my_score = _best_of(my_hole + full_board)
        opp_scores = [_best_of(oh + full_board) for oh in opp_holes]
        best = max([my_score, *opp_scores])
        if my_score == best:
            tied = 1 + sum(1 for s in opp_scores if s == best)
            share += 1.0 / tied

    return share / trials


def pot_odds(to_call: int, pot: int) -> float:
    """The win share a call must clear to break even: to_call / (pot + to_call)."""
    if pot < 0:
        raise ValueError(f"pot must be >= 0, got {pot}")
    if to_call <= 0:
        return 0.0
    return to_call / (pot + to_call)
