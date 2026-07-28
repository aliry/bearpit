# Jury (unanimous) — three jurors, one arson case, up to three sealed ballots

**The case:** *State v. Merrick Vance* — arson of the Harborview warehouse. The state says Vance
burned his own warehouse to collect on the stock insured inside. Convict only **beyond reasonable
doubt**.

The point of the scenario is **pooled hidden information**: the public exhibits (in
`spec.guidelines`) are genuinely balanced, and each juror privately noticed **one** detail the other
two missed — each of which cuts *against* that juror's own instinct:

| Juror | Prior | The detail only they noticed |
|---|---|---|
| **Juror A** | leans guilty | The card that opened the door at 23:41 was **#4471** — the card Vance reported **lost**. His replacement, #4488, was never used that night. |
| **Juror B** | reasonable doubt | The insured stock was **moved out on 12 March**. Vance would have collected $400k on stock that was not in the building. |
| **Juror C** | swing vote | The fire marshal only wrote *"consistent with"* a wiring fault — the cabinet **passed inspection on 2 March**, and he **could not exclude a deliberate short**. |

A juror who sits on its detail makes the deliberation worthless. A jury that pools all three has a
genuinely hard, genuinely decidable case — and either verdict, or a hung jury, is a legitimate
outcome.

## The shape of the realm

Turn-based, **one message per juror per round**, driven by the Foreperson (`referee_opens: true`).

| | Floor | What happens |
|---|---|---|
| *(opener)* | Foreperson | Frames the charge + the standard, and states the ballot procedure. The rotation is **held** until it posts. |
| **Round 1** | juror-a → juror-b → juror-c | Each argues, then seals `submit_sealed(round='ballot-1', payload='guilty'\|'not-guilty')`. |
| *(round cue)* | Foreperson | `recall()` → `reveal_status('ballot-1')` → `reveal('ballot-1')` → `run_code()` to count → `remember()` → **`rule()`** if unanimous, else report the split and `eliminate(agent='none')` to open ballot 2. |
| **Round 2** | jurors | …seal `ballot-2`. Same boundary procedure. |
| **Round 3** | jurors | …seal `ballot-3`. Still split ⇒ **hung jury** (`rule(outcome='hung jury', …)`). |

### The two contracts that make it work

1. **The round label.** Ballot *N* is sealed under `round='ballot-N'` — `ballot-1`, `ballot-2`,
   `ballot-3`. **The ballot number is the turn round number** (`turn_status()` returns it), so a
   juror can always derive its label without waiting to be told.
2. **The payload.** Exactly one lowercase token: `guilty` or `not-guilty`. The `unanimous` tally is
   *exact string equality* — `Guilty`, `not guilty`, or `VERDICT: guilty` from a juror that dressed
   it up would read as three different votes and **hang a jury that actually agreed**. The token is
   stated verbatim in `spec.guidelines`, in every juror persona, and in the Foreperson's rubric.

**Multi-ballot is the whole design.** A sealed submission is immutable, so a single-ballot jury
whose members start with opposing priors can *never* converge — the first juror to seal has locked
its vote before anyone argued. Jurors may change their mind **between** ballots, never within one.

The Foreperson reports **only the split** (`2-1`), never who voted which way — a juror who knows
who is where votes with the majority instead of the evidence. Side channels are **off**: everything
is argued on the record.

## How the realm ends

> **The Foreperson ends this realm by CALLING `rule(outcome=…, reasons=…)`.** Nothing else ends it.
> Announcing a verdict in chat ends nothing.

`referee_verdict` is the first termination condition and fires on that tool call
(`powers.verdict_ends_realm: true`). Everything else is a backstop:

- **`message`** — `(?i)🏁\s*verdict`, a *hardened fallback only*: emoji-anchored and
  case-insensitive so a juror arguing *about* the verdict cannot trip it. The Foreperson posts
  `🏁 VERDICT - <outcome> - …` **after** `rule()`, never instead of it. Jurors are forbidden the 🏁
  character.
- **`duration` 60m** — the wall-clock backstop (three ballots of turn-taken play, 180s silence
  timeout).
- **`stall` 20m** — the floor. The Foreperson is the only agent that can end the realm; if it
  starves or dies, this ends the realm rather than burning an hour on an empty room. (8m, not 5m:
  with a 180s silence timeout, two consecutive slow turns are legitimately ~6 minutes of quiet.)

`min_rounds_before_verdict: 1` stops a verdict before ballot 1 has actually been cast.

## Two engine facts this scenario is built around

Both were found by reading the runtime, and both will silently ruin a "cleaner-looking" rewrite:

- **The Foreperson never calls `tally()`.** `tally()` records a platform `VERDICT` event, and the
  Warden's `referee_verdict` condition fires on the latest VERDICT event of *any* kind
  (`gatekeeper/runner.py`: `verdict = str(verdicts[-1].payload.get("outcome"))`). A `tally()` on
  ballot 1 would therefore **end the trial immediately** — with no reasons, and with the outcome
  `"None"` whenever the jury is split. So the ballots are counted with `reveal()` + `run_code()`
  (exact, deterministic, and never in the model's head), and the realm ends **only** on `rule()`.
- **`eliminate(agent='none')` is how a ballot closes.** Under a driving referee the TurnManager
  freezes the rotation at every round boundary and only reopens it on an ELIMINATION event (or a
  timeout). Nobody is ever ejected from this jury — the call is purely the "this ballot is
  resolved, open the next one" signal. Without it the jury room sits mute for the silence timeout
  at every boundary.

## Run it

```sh
pit validate examples/jury-unanimous
pit up examples/jury-unanimous
```

Watch for: all three jurors sealing on their own turn (`sealed_submit` markers), the Foreperson's
`reveal` at each boundary, the split-only reports, and a single final `rule()`.
