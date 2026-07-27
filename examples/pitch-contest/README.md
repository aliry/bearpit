# Pitch Contest — three founders, one judge, a scored verdict

Three founders — **Ada** (dev-tools), **Bram** (climate), **Cleo** (health) — each enter exactly
**one** startup pitch. A **Judge** scores every pitch on three criteria, reads the platform's
scoreboard, and issues a verdict that ends the contest.

It is the platform's smallest *judged* scenario: one turn round, one sealed submission each, nine
scoring calls, one verdict. If you want to see the Arbiter (score / scoreboard / rule) work
end-to-end without an eight-player social-deduction game around it, run this.

## What it proves

- **A referee that actually decides.** The winner is not narrated in chat — it is nine `score()`
  calls, a `scoreboard()` read, and a `rule()` verdict. Delete the tool calls and the contest
  produces nothing, which is exactly the point.
- **A round boundary as a trigger.** The judge is *reactive*: it does nothing until the turn engine
  tells it "round 1 is complete — every participant has now had the floor." That cue is the only
  deterministic moment at which all three pitches are known to exist.
- **Sealed entries.** Each founder's pitch is sealed under `pitch-1` as well as posted, so the judge
  scores the exact text the founder committed to, whatever the bus delivered.

## The mechanics (the exact contract)

| Who | On its turn / cue | Tool calls (nothing else counts) |
|---|---|---|
| ada, bram, cleo | one paragraph, once, then silent | `submit_sealed(round='pitch-1', payload='<pitch>')` |
| judge | only after the round-complete cue | `reveal(round='pitch-1')` → 9 × `score(agent, delta, reason)` → `scoreboard()` → `rule(outcome, reasons)` |

The round label is **exactly `pitch-1`** — in the guidelines, in all three founder personas, and in
the judge's rubric. A mismatch (`pitch1` vs `pitch-1`) yields an empty reveal.

Turns are **physics**: `one-at-a-time` / `one-message` / roster order, so each founder gets the floor
for exactly one message and no one can shout over the round. `min_rounds_before_verdict: 1` makes the
Arbiter *reject* a verdict until all three have had the floor — the judge cannot crown a winner
before anyone has pitched. `require_mention: false` so the judge ingests every commons message
instead of only messages that @mention it (the founders @mention it anyway, belt and braces).

Scoring is 1-5 on each of **clarity**, **market**, **originality** — three `score()` calls per
founder, nine in total, max 15 points each.

## How it ends (deterministic, with a floor)

1. `referee_verdict` — the judge calls `rule()`. The decided ending; `powers.verdict_ends_realm` is
   true.
2. `message` — a hardened fallback: the judge's single closing line `🏁 WINNER: <name> — <reason>`
   (emoji-anchored, case-insensitive regex, so a founder arguing about who should win cannot trip
   it).
3. `duration: 20m` — the wall-clock backstop.
4. `stall: 5m` — the floor. If the judge dies, starves, or narrates instead of calling `rule()`, the
   realm ends in five idle minutes rather than burning the full clock.

The judge is `large` (it does all the reasoning), holds `max_usd: 6.0` and `on_exhausted: starve` —
it is the only agent that can conclude the realm, so a budget blip must not kill it. The founders are
`small`, `$2`, `starve_then_kill`.

## A healthy transcript

```
system   Kickoff. Ada has the floor.
ada      ⚙️ mcp_realmtools_submit_sealed…            (free — does not use the turn)
ada      Dev-tools pitch, one paragraph. @judge
system   Bram has the floor.
bram     Climate pitch, one paragraph. @judge
system   Cleo has the floor.
cleo     Health pitch, one paragraph. @judge
system   (to judge) Round 1 is complete — every participant has now had the floor.
judge    ⚙️ reveal(pitch-1) → 3 sealed pitches
judge    ⚙️ score × 9 → scoreboard() → ada 12, bram 10, cleo 13
judge    ⚙️ rule(outcome='cleo wins - 13/15', reasons='…')     ← this ends the realm
judge    🏁 WINNER: Cleo — the only pitch with a moat a competitor cannot copy in a quarter.
```

Unhealthy: the judge posts "Winner: Ada, 13/15" and calls no tool (nothing was scored, nothing
ended); founders reply to each other's pitches (the round burns and their `$2` caps go with it); the
reveal comes back `n=0` (a round-label typo).

## Run it

```sh
uv run arealm validate examples/pitch-contest
uv run arealm up examples/pitch-contest
```

## Dials

Add a second round (`pitch-2`) for rebuttals; add a fourth founder; make the judge score on a
weighted rubric; or give the founders a shared folder and a real market-sizing task to compute with
`run_code`.
