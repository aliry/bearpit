# Board Majority — a five-director board decides a $210M acquisition

The board of Corvid Instruments (industrial sensors, flat for three years) votes on acquiring
Halyard Systems (90 people, edge analytics) for **$210M** — all of Corvid's cash plus $80M of new
debt. One debate round, then a **sealed ballot**. The secretary opens the ballots, counts them, and
rules.

## The roster — five voters, one officer

| Agent | Role | Position |
|---|---|---|
| **dana** | director (votes) | **For.** Lost three tenders to rivals who shipped analytics. |
| **emil** | director (votes) | **Against.** Ran the last integration; 70% of the engineers left. |
| **faye** | director (votes) | **For.** Has an actual plan: combined product in seven months. |
| **gus** | director (votes) | **Swing.** Wants the Meridian-concentration and debt questions answered. |
| **hana** | director (votes) | **Swing.** Chairs audit; the only one who has read the debt covenant. |
| **secretary** | **referee — does NOT vote** | Runs the vote, opens the ballots, counts, rules. |

**Five votes, not six.** The referee cannot seal a ballot — the platform refuses it
(`EscrowService.submit` raises for a referee) and Forge builds the escrow roster by *excluding* the
referee. So the board is **odd on purpose**: three of five carries, and no tie is reachable.

Two firm yes, one firm no, two genuinely undecided — the **argument** decides this, not the roster.
Every director holds one fact nobody else has (a lost-tender debrief, a failed integration, a dated
shipping plan, a debt covenant), so the debate carries information instead of restating labels.

## The sealed round — the exact contract

- Round label: **`merger`** — exactly that string, everywhere (guidelines, all five personas, the
  secretary's rubric).
- Payload: **exactly the lowercase word `yes` or `no`.** Nothing else. No reasoning, no punctuation,
  no capitals.

  ```
  submit_sealed(round="merger", payload="yes")
  submit_sealed(round="merger", payload="no")
  ```

  This is pinned because the tally is a **raw string count** — `'Yes'`, `'yes — growth case'` and
  `'YES'` are three *different* options, and a vote spread across them silently evaporates.
- **Ballots are never announced in chat.** Sealed submission is the only way to get simultaneity;
  open sequential voting makes models bandwagon on whoever spoke first.

## The decision rule

> The motion carries **iff 3 or more of the 5 directors sealed `yes`.** Anything else — 2 yes, an
> abstention, a spoiled ballot — and it **fails for want of a majority**.

Stated as a threshold against the five *seats*, this is total: every possible state maps to
approved-or-rejected. There is no tie, no casting vote, and no state in which the secretary has to
improvise.

## How the meeting runs (turns)

One-at-a-time floor, roster order (dana → emil → faye → gus → hana), physics-enforced. The secretary
is a **driving referee** (`referee_opens: true`), so the rotation pauses at each round boundary and
the system cues it to resolve.

1. **Opening** — the secretary states the motion and the vote call, and the floor opens.
2. **Round 1 — DEBATE.** Each director gets one message. Nobody seals.
3. **Round-1 boundary** — the secretary opens the vote and calls `eliminate(agent="none")`.
   *This board ejects nobody:* in a turns realm with a driving referee, a resolution call is the only
   thing that reopens the floor, so `eliminate("none")` is simply the gavel that closes a round.
4. **Round 2 — THE VOTE.** Each director posts a one-line final position and seals its ballot on its
   own turn. Tool calls are free — sealing does not cost the director its message.
5. **Round-2 boundary** — the secretary resolves:
   `reveal_status(round="merger")` until `pending` is empty → `reveal(round="merger")` →
   count with `run_code` → **`rule(outcome, reasons)`** → one closing line.

`min_rounds_before_verdict: 2` makes "debate before you decide" structural, not advisory: a verdict
is refused until both rounds have completed.

### Why the secretary does not call `tally`

`tally()` writes a VERDICT event, and *any* verdict event fires the `referee_verdict` termination —
so a tally would end the realm on its own, with the raw one-word outcome (`yes`), and on a
no-majority result with the literal outcome `None`. That is not a decided ending. The secretary
therefore **reveals, counts with `run_code`, and calls `rule()`** — the only tool call that produces
a stated, recorded decision ("merger approved — 3-2").

## How the realm ends

In precedence order (first match wins):

1. **`referee_verdict`** — the secretary calls `rule(outcome, reasons)`. **This is the decided
   ending**, and the only one. Announcing the result in chat ends nothing.
2. **`message`** — a hardened fallback: `(?i)🏁\s*decision`, emoji-anchored so a director arguing
   about the vote cannot trip it. The secretary posts `🏁 DECISION — merger approved — 3-2` *after*
   `rule()`.
3. **`duration` 75m** — the wall clock.
4. **`stall` 25m** — the floor. The secretary is the single point of failure (a dead or starved
   referee freezes a paused rotation); stall guarantees the realm ends anyway instead of burning to
   the duration.

## Dials

Raise the price, or move Meridian's renewal inside the covenant test, and the swings go no. Give
Faye a 12-month plan instead of 7 and Hana's covenant objection stands. Drop Hana and you are back
to an even board — which is exactly the bug this scenario was fixed for.
