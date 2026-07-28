# RPS Duel — a 10-round sealed-move match (2 players + a referee)

Orin and Vela play ten rounds of rock-paper-scissors. Themis referees: she reveals each round,
scores it, and rules the match. She never plays.

The point of the scenario is the *seal*. Both moves are chosen blind and revealed together, so
neither player can react to the other's hand — the only read you get on your opponent is the
history Themis publishes after every round.

## The round protocol

The match runs on the platform's turn engine (`turns` + `referee_opens`), so it has a driver:

1. **Themis opens the match** — one post, then she goes quiet. The rotation is held until she does.
2. **Each player takes the floor for exactly one message.** Before speaking, they seal:

   ```
   submit_sealed(round='R<N>', payload='rock' | 'paper' | 'scissors')
   ```

   Tool calls are free — only the message ends the turn. Both players seal on their own turn, which
   is what makes the moves simultaneous: neither one can see the other's.
3. **At the round boundary** the system cues Themis. She (and only she) calls
   `reveal(round='R<N>')`, works out the winner, calls `score(<winner>, 1, 'round R<N>')`, reads
   `scoreboard()`, and posts ONE line that **always names both actual moves**:

   ```
   Round R3: orin=rock, vela=scissors — orin takes it. Score: orin 2, vela 1. Round R4 is open …
   Round R4: orin=paper, vela=paper — draw, nobody scores. Score: orin 2, vela 1. …
   ```

   Naming the moves is the whole point: they were sealed, so **Themis's report is the only way
   either player ever learns what the other threw**. A bare "R3 resolved: draw" tells them nothing
   and leaves them guessing at a game whose entire content is reading the opponent. She reports the
   moves on draws and void rounds too.

   Posting that message is what reopens the floor for the next round. Nobody is ever ejected from a
   rock-paper-scissors match, so Themis never calls `eliminate` at all.

### The labels are a contract

The rounds are the **literal strings `R1` … `R10`** — not `1`, not `round-1`, not `r1`.
`submit_sealed(round=…)` and `reveal(round=…)` key on the exact same string, and a `reveal` closes
its label **forever**. A mismatch means an empty reveal and a round that can never be played.
The label is stated in the project guidelines, in Themis's rubric, and in both personas; keep them
identical if you edit one.

## How the match ends

`rule()`. After round R10 — and not before (`turns.min_rounds_before_verdict: 10` makes that
physics) — Themis calls:

```
rule(outcome='orin wins the match 6-4', reasons='R1 orin, R2 vela, R3 draw, …')
```

That call is the realm's `referee_verdict` termination: it is the *only* designed ending. She then
posts one closing line, `🏁 MATCH OVER — orin 6-4`, which exists as a **fallback** message
termination (emoji-anchored, case-insensitive) in case the tool call never lands. Announcing a
winner in chat ends nothing on its own.

Backstops: a 60m `duration` and a 20m `stall` (if Themis dies, the realm concludes instead of
burning the players' budgets in silence).

## Edge cases the referee is told to handle

- **A missing or junk seal** — `reveal` returns fewer than 2 submissions: the round is **void**,
  nobody scores, and the next round opens immediately. She never stalls waiting for a late seal.
- **A draw** — identical moves: nobody scores.
- **Never `tally()`** — it writes a platform VERDICT event, which would conclude the realm on the
  first round. Rounds are scored with `reveal()` + `score()`; only `rule()` ends the match.

## Run it

```sh
uv run pit up examples/rps-duel
```
