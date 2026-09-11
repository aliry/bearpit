# RPS Machine — rps-duel's referee ribbon as a declared state machine

Same match as [`rps-duel`](../rps-duel): Orin and Vela play ten rounds of rock-paper-scissors,
sealed and revealed together so neither can react to the other's hand. What's different is Themis.
In rps-duel her reveal/score/next procedure lives only in her persona's prose. Here it is a
declared `state-machine` mechanic — `sealing → revealed → scored → sealing` (or `→ done` on the
last round) — enforced by the platform, not just written down and hoped for.

This is the genericity proof for the game-state-machine feature: the same game, the same sealed
escrow, the same players, with the referee's sequencing moved out of prose and into a machine the
platform itself checks. Sealing still runs through the sealed-submit mechanic — players still call
`submit_sealed` on their own turn via the `turns` engine, exactly as in rps-duel. No wake rules are
declared; `turns` handles the players' seal-on-cue and the machine handles only Themis's ribbon, so
the two attention systems never overlap.

Each round, Themis writes which round the machine is watching (`game_set('hand', 'R<N>')`), then
fires `reveal` — refused until both players have sealed, in which case she fires `void` instead and
scores nobody — reveals and scores the round with the same `reveal()`/`score()` calls rps-duel
uses, records the score on the machine, and fires `score` (skipped on a `void` round, which already
landed in `scored`). She then either advances to the next round (`next`) or, on `R10`, fires
`finish`, which moves the machine into its terminal state and ends the realm through
`machine_terminal` — the same rubric, running on a rail the platform itself enforces.

## Run it

```sh
uv run pit up examples/rps-machine
```
