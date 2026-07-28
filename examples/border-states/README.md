# Border States — five powers, three years, one map

Five rival powers share a continent of eleven supply centres. Each holds two at the start. Win by
controlling **six**, or by holding the most when **Year 3** ends.

Three years is short on purpose. It is long enough for an alliance to form, pay off, and be
betrayed, and too short to win by sitting still — a power that defends its two home centres finishes
last. The pressure to move is the scenario.

## What it exercises

The most mechanically demanding realm here after `cygnus-crew`, and the only one with a
**deterministic adjudicator**:

- **Simultaneous sealed orders.** Every power submits its season's moves through the sealed-submit
  mechanic. Nobody sees another's orders until the reveal, so a betrayal genuinely cannot be
  detected in advance — that is physics, not an instruction to the model.
- **A referee that computes rather than judges.** The Cartographer resolves each season by running a
  shipped program: an adjudicator implementing Diplomacy-style movement, support, and standoff
  rules. It never negotiates, never favours a power, and never improvises an outcome. Its narration
  reports what the program returned.
- **Alliance and betrayal through private messaging.** Powers negotiate in private channels, so a
  deal is real information the others do not have.
- **Strict one-at-a-time turn order** across a six-agent roster, season after season.

## The powers

| Agent | Role |
| --- | --- |
| `cartographer` | Referee. Adjudicates each season, announces results, calls the ending. |
| `corvane` | Power |
| `ferrant` | Power |
| `sablerock` | Power |
| `verdance` | Power |
| `wrenmark` | Power |

Each power's `persona.md` gives it a distinct temperament — who it trusts first, when it defects,
what it does with a lead. They are written to disagree.

## Running it

```bash
pit validate examples/border-states
pit up examples/border-states
```

Expect roughly three in-game years of several seasons each. A five-way draw is a legitimate result:
with balanced powers and a short horizon, no one reaching six centres is a real outcome, not a
stalled realm.

## On the rules

The movement rules are the classic Diplomacy adjudication family — supports cut, standoffs bounce,
dislodged units retreat. The map, the eleven centres, the five powers and every persona here are
original to this scenario.
