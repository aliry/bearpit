# Split the Pot — a three-way coalition bargaining game (3 players + a banker)

Red, Gold and Blue divide a $100 pot. Nobody is paid unless **two of the three seal the same
split**, so every deal needs a partner — and any partner can be outbid before the seals open.

The interesting move is exclusion: any two players can cut a private side-deal that hands the third
almost nothing. The interesting risk is betrayal: a promise binds nobody, so the partner you bought
may seal a different split with the other rival.

## The mechanic (what actually runs)

| Thing | How it is wired |
|---|---|
| Commitment | `submit_sealed(round='R<N>-split', payload='SPLIT: red=40, gold=40, blue=20')` — **sealed, never posted in chat.** Splits are simultaneous, so there is nothing to bandwagon onto. |
| Round label | Exactly `R1-split`, `R2-split`, … — players read `N` from `turn_status()`; the Banker reveals the **same** string. |
| Payload format | `SPLIT: red=<n>, gold=<n>, blue=<n>` — always all three names, in that order, whole numbers summing to **exactly 100**. Anything else is VOID. |
| Side-deals | `send_private(to="gold", message="…")` — platform-brokered DM rooms, capped at **2 messages per round per player** (`private_messaging.max_per_round`). Agents never create rooms themselves. |
| Turns | One-at-a-time, one message per player per round (`turns`). Tool calls are free and do not consume a turn. |
| Ending | The **Banker** (referee, `verdict_ends_realm: true`) reveals the round's seals, checks them with `run_code`, and calls `rule(outcome=…, reasons=…)` the moment two or more players sealed the identical valid split. |

## How a round runs

1. Each player, on its turn: `recall()` → `turn_status()` → up to 2 × `send_private(...)` →
   `run_code` (does my split sum to 100?) → `submit_sealed('R<N>-split', …)` → **one** public
   message → `remember(...)`.
2. When the round completes, the system cues the Banker. It calls `reveal(round='R<N>-split')`,
   parses and counts the splits with `run_code` (never in its head), and:
   - **majority found** (≥2 identical valid splits) → `rule(...)`, then one closing line
     `🏁 DEAL DONE - red=.., gold=.., blue=..`;
   - **no majority** → one short line saying so (without publishing anyone's numbers), and round
     N+1 opens.

## Who ends the realm

The Banker's `rule()` call, and nothing else. Announcing a deal in chat ends nothing — the
platform records tool calls, not prose. `termination` also carries a hardened emoji-anchored
message fallback (`(?i)🏁\s*deal done`), a 60m duration and a 20m stall floor, so a dead referee
cannot hang the realm.

The Banker is **reactive**, not driving (`referee_opens: false`): it does not pause the rotation at
round boundaries, so it must never be given an `eliminate`-gated round loop. `min_rounds_before_verdict: 1`
stops it ruling before every player has spoken once.

## Design notes / dials

- **Why sealed splits rather than open agreement?** Open, sequential commitment makes LLMs
  bandwagon on whoever names a number first; a coalition game then collapses into "everyone agrees
  with Red". Sealing is the only way to get simultaneity — and it makes betrayal *mechanically*
  possible, which is the whole point of the scenario.
- **Why no ruleset on the mechanic?** The deal rule ("two identical valid triples") is checked by
  the Banker in `run_code`, which also voids splits that do not sum to 100.
- Dials: change the majority bar (3-of-3 unanimity is a far harder game); allow only one private
  message per round; hide the roster (`roster_visibility: anonymous`); or let the Banker read DMs
  (`powers.read_dms`) and penalize agents who break an explicit promise — turning betrayal from
  physics into law.
