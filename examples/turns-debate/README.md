# Turns Debate — three advocates, one chair, one decided winner

Three advocates argue for a startup HQ city — Ava/Austin, Mira/Miami, Dan/Denver — one at a time
under turn physics. A chair watches every round, scores each argument, and records the winner.

The smallest scenario that exercises the turn engine plus a *reactive* referee. It is deliberately
not a game: nobody is eliminated, nothing is sealed, and the only state that ever changes is the
scoreboard and the final verdict.

## The shape (do not "simplify" these away)

**A reactive chair, not a driving one.** `spec.referee_opens: false`. The advocates start on their
own; the chair only acts when the turn engine cues it at a round boundary. Setting `referee_opens:
true` would make the chair a *driving* referee — and under a driving referee the round boundary is
**held** (players muted) until an `eliminate()` call arrives. In a debate there is nobody to
eliminate, so every round would limp forward on the 150s referee timeout. Leave it false.

**`environment.require_mention: false`.** This is load-bearing. The default (`true`) means an agent
only ingests messages that @mention it — and the reactive round-complete cue does **not** carry the
round's transcript (only the driving-referee cue does). With mention-gating on, the chair would be
asked to judge a debate whose text it had never received, and would invent a winner from the city
names in its own persona. With it off, the chair reads the Commons directly. The advocates cannot
turn free-response into a chat storm: turn physics mutes everyone who is off the floor.

**~2 rounds.** `min_rounds_before_verdict: 1` blocks a verdict until a full round is in; the chair's
rubric tells it to decide after round 2 (round 2 is where the rebuttals land) and never past round 3.

**The verdict is a tool call.** `chair.powers.verdict_ends_realm: true` plus a `referee_verdict`
termination. The **only** decided ending is `rule(outcome, reasons)`. Prose changes nothing — a chair
that announces "Austin wins" in chat has ended nothing. The rubric says this in the imperative, and
`spec.guidelines` agrees with it; the `🏁 VERDICT` message termination is a **hardened fallback
only**, posted after the `rule` call has landed, never instead of it.

**The scoreboard is the platform's, not the model's.** Each cue is a fresh conversation, so the chair
`recall()`s first, `score()`s all three advocates on that round, `remember()`s one line each, and
reads `scoreboard()` before ruling. It never adds up rounds in its head. The advocates keep notes for
the same reason — nothing carries between turns but what they wrote down.

## Termination

| Condition | Why |
|---|---|
| `referee_verdict` | the decided ending — `chair.rule()` |
| `message` `(?i)🏁\s*verdict` | hardened fallback if the verdict event is missed |
| `duration` 60m | wall-clock backstop |
| `stall` 20m | the floor. If the chair starves or crashes, the realm ends instead of rotating advocates into a room nobody is judging. |

`turns.retire_after_misses: 2` drops a dead advocate from the rotation rather than burning a 150s
silence timeout on it every round.

## Run it

```sh
uv run pit validate examples/turns-debate
uv run pit up examples/turns-debate
```
