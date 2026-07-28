# council-vote

A five-seat city council picks the site of the new HQ. Four councillors — **mira** (miami),
**nils** (the swing vote), **otto** (denver), **vera** (austin) — each take the floor once, argue
in the open, and vote in **secret**. The **chair** (referee) opens the ballots together, counts
them with code, and ends the realm with a verdict.

Ballots are sealed for a reason: public sequential voting makes LLMs bandwagon onto whoever spoke
first. Here nobody learns anything about anyone's vote until every vote is cast.

## The contract (do not let these drift apart)

| Thing | Exact value |
|---|---|
| Sealed round label | `'ballot'` — the literal string, in `submit_sealed` **and** in `reveal`/`reveal_status` |
| Ballot payload | exactly one of `austin` \| `denver` \| `miami` — lowercase, one word, no punctuation, no reasoning |
| Player call | `submit_sealed(round='ballot', payload='austin')`, on their own turn (a tool call is free — it does not use up the turn) |
| Chair's resolution | `reveal(round='ballot')` → `run_code` (count) → **`rule(outcome='<city>', reasons='<counts>')`** |
| What ends the realm | the chair's `rule()` call. Nothing else decides anything — announcing a winner in chat ends nothing |

The payload vocabulary is load-bearing: `plurality` counts the raw strings, so `Austin`,
`austin` and `austin — for the talent pool` would be three different cities.

## Shape of the realm

- **Turns** (`one-at-a-time`, roster order): mira → nils → otto → vera, one message each. Speaking
  order is part of the game — nils, the swing vote, seals early and must decide on the cases made
  by the time the floor reaches him.
- **Driving chair** (`referee_opens: true`): the chair posts the opening, the rotation runs, and
  the floor pauses at the round boundary until the chair resolves — so nobody debates into a void
  and no round outruns the count.
- The chair deliberately does **not** call `tally`: `tally` records a verdict of its own (which
  would end the realm before a tie could be broken, and on a tie records no decision at all). It
  reveals, counts with `run_code`, and rules.

## Expected transcript

1. `chair` — "The council is in session. On the table: austin, denver, miami…"
2. `mira`, `nils`, `otto`, `vera` — one argument each; each seals `submit_sealed(round='ballot', …)`
   on its own turn (the seal never appears in chat).
3. `chair` — `reveal_status` (all four in) → `reveal` → `run_code` counts → `rule(outcome='austin',
   reasons='plurality: austin 2, denver 1, miami 1')` → `🏁 DECISION — austin (austin 2, denver 1,
   miami 1)`.

A good run ends in **5–10 minutes** with one `reveal` event, one `verdict` event, and the realm's
`outcome` set to a city.

## Edge cases, decided in advance

- **Tie** (e.g. 2–2): the **chair has the casting vote** — it picks from the tied cities only,
  citing the argument that carried it, and rules. The council never adjourns undecided (a tally
  that returns "no winner" is not an ending).
- **Somebody never seals:** the chair does *not* reveal (revealing closes the round permanently and
  locks out anyone still pending). It calls `eliminate(agent='none')` — here that only means
  "round closed, nobody ejected; reopen the floor" — names the stragglers, and resolves at the next
  round-complete cue regardless; a councillor who never sealed forfeits their ballot.
- **Chair fails to call `rule()`:** the `🏁 DECISION` message termination catches it
  (emoji-anchored + case-insensitive, so a councillor arguing about the vote cannot trip it), and
  below that sit `stall: 5m` and `duration: 25m`.

## Run it

```sh
uv run pit up examples/council-vote
curl -H "Authorization: Bearer $(cat ~/.bearpit/api-token)" \
  :8000/api/realms/<id>/events?kind=verdict   # the decided ending
```
