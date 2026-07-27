# Sealed auction — three bidders, one contract, one sealed round

Three bidders (**Athena**, **Boreas**, **Cassia**) each seal a hidden bid for a single contract.
The **Clerk** (referee) waits until all three bids are in, reveals them at once, and awards the
contract to the highest bid.

It is **first-price**: the winner pays exactly what it bid. Each bidder has a *private value* —
Athena 140, Boreas 110, Cassia 160 — known only to itself. Winning above your value is a loss, so
the game is the shading trade-off: bid high enough to beat two rivals whose values you cannot see,
low enough that the win is still worth having. Boreas, who hates losing, is the one most likely to
"win" itself a loss.

## The contracts (every string here is load-bearing)

| Thing | Value |
|---|---|
| Round label | `final` (re-bid on a tie: `final-2`) |
| Payload | **bare digits only** — `'137'`. No words, no `$`, no commas, no decimals. |
| Bid range | whole numbers 1–200 |
| Tally ruleset | `high-bid` |

A bidder seals with `submit_sealed(round='final', payload='137')` and then posts **one** short line
saying it has sealed — **never the number**. Sealed submission is the only way to get
simultaneity; a number spoken in the commons simply hands the auction to whoever bids next.

The payload format is a contract because `high-bid` does `int(payload)` and raises on anything
else — `'$4,200'` or `'I bid 150'` makes the tally return an error and emit no verdict.

## How the round runs

Turns are on (`one-at-a-time`, physics-enforced) and the Clerk **opens** the realm
(`referee_opens: true`), so the sequence is deterministic:

1. The Clerk posts the opening call for bids. (Until it does, the floor stays shut.)
2. Each bidder gets the floor in turn: it seals its bid (tool calls are free — they do not use up
   the turn) and posts its one "sealed" line, which passes the floor on.
3. When all three have had the floor, the system **cues the Clerk** at the round boundary. This
   cue is the only thing that reliably wakes the referee — without turns there is no cue at all,
   and a clerk with no cue sleeps through a realm full of sealed bids.

## The Clerk's procedure (its rubric, in order)

1. `reveal_status(round='final')` — **mandatory, every cue, before anything else.**
2. If `pending` is non-empty: ping the stragglers, call `eliminate(agent='none', ...)` to reopen
   the floor for another round, and **stop**. Revealing early is irreversible: the round locks and
   every late bid is refused. (After two cues with someone still pending, it proceeds anyway, so a
   dead bidder cannot hang the realm.)
3. `reveal(round='final')` once `pending` is empty.
4. `run_code` to normalise the payloads and pick the top bid — never arithmetic in the head, and it
   salvages a malformed payload's digits.
5. **Tie** → no tally (a tallied tie records a *null* outcome and would end the auction with no
   winner). The Clerk opens the re-bid round `final-2`; all three bidders seal again. A second tie
   is broken alphabetically, so the auction always terminates with a decision.
6. Unique top bid → `tally(round=..., ruleset='high-bid')` for the platform's deterministic record,
   then immediately `rule(...)`.

## How the realm ends

`rule(outcome='<winner> wins the contract at <bid>', reasons='sealed bids: ...')`. The Clerk holds
`verdict_ends_realm: true` and the project declares a `referee_verdict` termination, so **that tool
call — and only that call — closes the auction.** Announcing a winner in chat ends nothing.

Backstops, in order: a hardened message fallback (`🏁 AUCTION CLOSED`, the Clerk's last line, posted
*after* it rules), a `5m` stall, and a `25m` duration.
