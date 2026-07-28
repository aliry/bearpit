# Market Scan Duel — a refereed research race (2 analysts + a judge)

Two rival analysts, **Athena** (structure, signal, speed) and **Loki** (contrarian, second-order
effects), race to write the better market scan of **"Open-source vector databases, 2026"**. Each
files it as a real file in the shared folder. **The Judge** reads both files, scores them on a
fixed rubric, and rules a winner — and that verdict is what ends the realm.

## Why it is built this way

Three rules of the platform shape this scenario, and every one of them was learned the hard way:

- **The shared folder is only reachable through `run_code`.** An agent has no `write_file` tool.
  So the personas name the tool, the exact path, and the `os.makedirs` — because "work in your own
  folder" produces an agent that posts its report in chat and writes nothing at all.
- **Nothing is real until a tool is called.** A judge that posts "Athena wins" has decided nothing.
  The winner exists only if `score(...)` and `rule(...)` were actually called.
- **Somebody must adjudicate.** "Produce the better report" is not a mechanic unless an agent with
  a rubric reads both and rules. Without the judge, the realm simply stops and no winner exists
  anywhere in the chronicle.

The topic is **assigned**, not left to the analysts — two reports on two self-chosen subjects are
not comparable, and there is nothing to judge. Egress is `model_only` and both analysts are told,
plainly, that they have **no internet access**: they write from their own knowledge, and the
rubric's heaviest deduction is for confident fabrication.

## The roster

| Agent | Role | Model | Does |
|---|---|---|---|
| Athena | participant | medium | Writes `/realm/shared/athena/report.md` |
| Loki | participant | medium | Writes `/realm/shared/loki/report.md` |
| The Judge | **referee** | large | Polls the folder, scores both, calls `rule()` |

Always-on and parallel — no turns. Both analysts work simultaneously; `require_mention: false` so
they and the judge all see the commons traffic.

## The deliverable

Each analyst writes, **with `run_code`**, to exactly:

    /realm/shared/athena/report.md
    /realm/shared/loki/report.md

First line `STATUS: FINAL`; sections **Landscape / Key players / Second-order risks / Verdict**;
~400 words. Then the analyst posts `🏁 FINAL` in the commons — that is the judge's cue to come and
read the file, not a submission in itself. **A report posted in chat does not count.**

## Scoring

The judge scores each report on four criteria, one `score()` call each (eight in total), and reads
the authoritative total from `scoreboard()`:

| Criterion | Max | |
|---|---|---|
| structure | 5 | four sections, in order, ~400 words |
| rigour | 10 | calibrated claims; **confident fabrication is the heaviest deduction** |
| insight | 10 | non-obvious second-order effects, not a vendor listicle |
| clarity | 5 | a decision-maker could act on it |

Tiebreak on equal totals: whoever was `STATUS: FINAL` first (file mtime). Speed is the tiebreaker,
not the prize.

## How it ends

In order (first match wins):

1. **`referee_verdict`** — the judge calls `rule(outcome, reasons)`. The intended ending, and the
   only *decided* one.
2. **`message`** — a hardened fallback: `🏁 VERDICT` posted in the commons, in case the judge
   decided but its `rule()` call failed.
3. **`duration`** — 60m wall-clock backstop.
4. **`stall`** — 20m of total silence (a judge that dies mid-poll can't hang the realm to the wall
   clock). The judge posts a one-line "waiting on X" whenever it polls, so a live judge keeps the
   realm alive while it waits.

There is deliberately **no "both files are FINAL" termination**. It would fire the instant the
second report landed — *before* the judge had read either — and the realm would end with two
reports and no winner, which is exactly the defect this scenario is built to avoid. The judge's own
deadline rule is the safety net instead: after ten polls it judges with whatever exists, and a
missing report scores zero.

## Run it

```bash
# 1. bring up the platform stack
docker compose -f deploy/docker-compose.yaml up -d

# 2. register the credential handle (BYOK — your key, your spend; see the top-level README)

# 3. validate, then run
pit validate examples/market-scan-duel
pit up examples/market-scan-duel
```

Watch it with `pit tail <realm>`; inspect the reports and the verdict afterwards with
`pit archive <realm>`.

## Difficulty dials

Swap the topic in `spec.goals` (and in both personas) for any subject the models know cold. To make
it harder: drop both analysts to `small` and watch how much of the failure is simply "the model
never emitted a valid `run_code` call". To make it a research task rather than a recall task, set
`network_egress: allowlist` with an `egress_allowlist`, and tell the analysts to fetch inside
`run_code` with `urllib` — that is the only real path to live data on this platform.
