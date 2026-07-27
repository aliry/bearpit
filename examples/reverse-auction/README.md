# reverse-auction

A sealed-bid **reverse auction** (procurement auction) for a single supply contract: three
suppliers each seal ONE integer price, the buyer reveals them all at once, and the **lowest** bid
wins. Each supplier has a *different, private* cost floor, so nobody knows how far a rival can
actually go — that asymmetry is the game.

## Roster

| agent    | role        | model  | what it does |
|----------|-------------|--------|--------------|
| `buyer`  | referee     | large  | Opens the auction, reveals the bids, tallies `low-bid`, awards the contract with `rule()`. Never bids. |
| `athena` | participant | small  | Supplier. Private cost floor **38**. |
| `boreas` | participant | small  | Supplier. Private cost floor **45**. |
| `cassia` | participant | small  | Supplier. Private cost floor **52**. |

The floors live only in each supplier's own `persona.md`. No supplier knows another's floor.

## The mechanic

- **One round. Its label is exactly `final`.** Suppliers seal with
  `submit_sealed(round="final", payload="47")`; the buyer reveals/tallies the same label. There is
  no second round and no re-bid — a seal is immutable.
- **The payload is the bare integer and nothing else.** `"47"` — never `"47 credits"`, `"$47"` or
  `"I bid 47"`. `low-bid` parses the payload with `int()`, and `tally` *reveals before it tallies*,
  so one chatty payload would burn the round permanently.
- **Nobody says a number in the commons.** The bid is sealed so that a rival cannot undercut it by
  one; public posts are posturing only.
- **Ruleset: `low-bid`** (`spec.mechanics`) — the deterministic tally picks the minimum bid.
- **Tie-break:** tied lowest bids go to the supplier whose id comes **first alphabetically**. The
  built-in tally returns `kind="tie"` and no winner, so the buyer's rubric resolves it explicitly —
  a tie left alone would end the realm with no contract awarded.

## Turns

`spec.turns` is on (`one-at-a-time`, physics-enforced) with `referee_opens: true`:

1. The buyer posts the RFQ (the round label, the payload format, the tie-break) and stops.
2. Each supplier gets the floor once, in roster order (`athena`, `boreas`, `cassia`): it **seals
   its bid first** (tool calls are free and don't consume a turn), then posts one public message.
3. The system cues the buyer at the round boundary — this is its deterministic wake-up; it never
   has to poll or guess when the bids are in.

## How the realm ends

The buyer's procedure at the round cue (its `rubric` is the authoritative version):

```
reveal_status(round="final")   # who has sealed? never reveal blind — reveal is ONE-SHOT
  -> pending non-empty: @mention the stragglers to seal; a persistent non-sealer is
     disqualified with eliminate(agent=<id>)
reveal(round="final")          # the actual numbers
tally(round="final", ruleset="low-bid")
rule(outcome="<supplier> wins the contract at <price>", reasons="...")   # MANDATORY
"🏁 CONTRACT AWARDED - <supplier> at <price>"                            # one closing line
```

**Only `rule()` ends the realm.** Announcing a winner in chat awards nothing. `spec.termination`,
in order of preference:

1. `referee_verdict` — the buyer's `rule()` call (the only *decided* ending).
2. `message` — hardened fallback: `(?i)🏁\s*contract awarded` on the commons.
3. `duration` — 60m wall-clock backstop.
4. `stall` — 20m of silence (e.g. the buyer dies) ends the realm rather than idling to the duration.

## Run it

```sh
uv run arealm validate examples/reverse-auction
uv run arealm up examples/reverse-auction
```
