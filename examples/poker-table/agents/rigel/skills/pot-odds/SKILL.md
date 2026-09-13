---
name: pot-odds
description: Price a poker decision with a Monte Carlo equity simulation and the odds it must beat
version: 1.0.0
category: Participant
---
# Pricing a hand instead of guessing it

You have a calculator that most of this table does not. It sits in your container at
`/opt/data/resources/equity.py` and `run_code` is the only way in — you have no file tool, and
every `run_code` call starts a fresh interpreter, so the `sys.path.insert` goes at the front of
every single one of them.

## The literal call

```
run_code(code="import sys; sys.path.insert(0, '/opt/data/resources'); import equity; print(equity.equity('Ah Kh', ['2c','7d','9h'], 3, trials=2000, seed=1))")
```

That prints your share of the pot — a float from 0.0 to 1.0 — if this hand were played out to the
river from where it stands now.

## The two functions, with their real signatures

**`equity(hole, board, opponents, trials=2000, seed=0) -> float`**

- `hole` — your two cards in ONE string with one space between them: `'Ah Kh'`.
- `board` — a LIST of single cards: `[]` before the flop, `['2c','7d','9h']` on the flop, four
  cards after the turn, five after the river. A list of cards, never one joined string.
- `opponents` — how many seats could still show you a hand at the river. Count them out of
  `game_state()`: six seats, minus everyone in `data.out`, minus yourself. A folded seat draws no
  cards, and counting one costs you equity you actually have.
- `trials` — 2000 is plenty to act on. It is a simulation; the third decimal place is noise.
- `seed` — any integer. The same seed gives the same answer twice, which is how you check a result
  you do not believe.

**`pot_odds(to_call, pot) -> float`**

- The share a call has to beat to break even: `to_call / (pot + to_call)`.
- `to_call` is what the call COSTS you — the current `bet_level` minus the last `to` you yourself
  posted on this street — **not** the total you will send in `to:`.
- `pot` is **the pot you will be playing for, not the pot on the table right now.** Read the next
  section before you pass this argument; getting it wrong is the one mistake that will quietly
  cost you every good hand you are dealt.

## The multiway trap — read this twice

Your equity is measured against every seat that could still show you a hand. The price has to be
measured against the same table. Ask for a share of a pot that only two people are building, while
counting yourself against five opponents, and the two numbers describe different games.

Preflop, six-handed, blinds 10/20. It is 20 to you and `data.pot` says 30.

- **Wrong:** `pot_odds(20, 30)` = 0.400, against `equity('Ah Kc', [], 5)` ≈ 0.28 → *fold ace-king.*
- **Right:** every live seat that calls puts in 20 too. The pot you are playing for is 30 + 20×5,
  so `pot_odds(20, 130)` ≈ 0.133, and 0.28 clears it easily → *play it.*

The wrong version folds every hand except aces. If you find yourself folding ace-king because a
number told you to, you have priced a six-way pot as though it were heads-up.

**The rule the two make together: call when `equity(...)` beats `pot_odds(to_call, pot_you_will_
play_for)`, and let it go when it does not.** Both numbers in one call:

```
run_code(code="import sys; sys.path.insert(0, '/opt/data/resources'); import equity; live=5; e=equity.equity('Ah Kc', [], live, trials=2000, seed=1); p=equity.pot_odds(20, 30 + 20*live); print(e, p, e>p)")
```

On a later street the two often coincide — by the river most seats have folded and the pot is
already large, so `data.pot` IS close to what you are playing for. It is preflop, with five seats
still to speak, that the difference decides the hand.

## Never retype a card

The module validates before it simulates, and it RAISES rather than guessing: `not a card: 'Xz'`,
`duplicate card: Ah`, and it refuses a card that appears in both your hole and the board. That
airlock only protects you if you feed it the truth, so **pass your hole cards and the board exactly
as `game_state()` handed them to you** — copy `data.hole` for your own id and `data.board` across,
split the board on spaces, and never retype either from memory. Most of what looks like a broken
calculator is a seat that remembered the nine as a ten.

If it does raise, read the message and fix the input you passed it. A raise means the cards were
wrong — so a number you then work out in your head would be wrong too, and nothing would tell you.

## What it does not know

It assumes every opponent is holding two random cards and that the hand runs to the river — so
against five seats it is answering "how often do I hold the best of six random hands", which is a
harsher question than "is this call profitable". It has never watched this table. It does not know that one seat only raises with a real hand, that the
price in front of you is a bluff, or that two seats behind you have yet to act and one of them may
raise. The number is the floor under a decision, not the whole of it.
