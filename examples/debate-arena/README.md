# Debate Arena — two debaters, two rounds, one judge

**The motion:** *"Remote-first is better than office-first for startups."*
PRO argues for it, CON argues against it, and the Judge decides.

## The shape of the realm

The debate is **two rounds of turn-taken exchange**, then a verdict.

| | Floor | What happens |
|---|---|---|
| **Round 1** | CON, then PRO | Opening cases — one message each. |
| *(round cue)* | Judge | `recall()` → `score()` both debaters → `remember()` → one short line. |
| **Round 2** | CON, then PRO | Rebuttals — each names the opponent's strongest point and dismantles it. |
| *(round cue)* | Judge | `score()` → `remember()` → `scoreboard()` → **`rule()`** → the closing line. |

Turns are **physics**: the system grants the floor to one debater at a time (roster order — CON
first, then PRO) and the room *refuses* off-turn posts. Tool calls are free and never consume a
turn, so a debater can `recall()`, `remember()` and `run_code()` as much as it likes before it
speaks.

Mentions are **off** (`require_mention: false`). That is what makes an exchange possible at all: it
is how CON hears PRO's argument, and how the Judge — who is never @mentioned by either debater —
sees the debate it is scoring. `min_rounds_before_verdict: 2` is what stops the Judge ruling on a
debate that has not happened yet: the `rule` tool refuses a verdict until both rounds are complete.

Side channels are **off** (`allow_side_channels: false`). Everything is argued on the public
record, which is the only record the Judge scores.

## How the realm ends

> **The Judge ends this realm by CALLING `rule(outcome=…, reasons=…)`.** Nothing else ends it.
> Announcing a winner in chat ends nothing.

`referee_verdict` is the first termination condition, and it fires on that tool call. Everything
else is a backstop:

- **`message`** — `(?i)🏁\s*verdict`, a *hardened fallback only*. Emoji-anchored and
  case-insensitive so a debater arguing *about* the verdict cannot trip it, and a Judge that writes
  "Verdict:" instead of "VERDICT:" cannot miss it. The Judge posts `🏁 VERDICT - <winner> - …`
  *after* `rule()`, never instead of it.
- **`duration` 60m** — the wall-clock backstop.
- **`stall` 20m** — the floor. The Judge is the only agent that can end the realm; if it starves or
  dies, this ends the realm instead of burning the debaters' budgets until the clock runs out.

## Scoring

The Judge scores each debater 0–3 on **clarity**, **evidence**, and **rebuttal** (round 2 only)
each round, through `score(agent, delta, reason)` — so the running total lives in the platform's
scoreboard, not in the model's head. `scoreboard()` is the authoritative tally, and the Judge must
quote it in its `reasons`.

## Dials

Change the motion in `spec.goals` + `spec.guidelines` and in both debaters' personas. Longer debate:
raise `min_rounds_before_verdict` and add the extra rounds to the personas. Harder debate: give the
debaters `model_category: medium` and a bigger budget.
