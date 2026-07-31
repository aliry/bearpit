<!--
Keep it short. The goal is that a reviewer can tell WHAT changed, WHY, and HOW YOU KNOW it works,
without reading the diff first.
-->

## What and why

<!-- One or two paragraphs. Lead with the problem, not the patch. If it fixes an issue, say
     "Closes #N" so it links and closes automatically. -->

## How it was verified

<!-- The important section. "Tests pass" is not evidence on its own — this project has repeatedly
     shipped bugs past a green suite. Say what you actually ran:

       - the test that fails WITHOUT this change (a regression test that cannot fail proves nothing)
       - anything checked against a running realm, a real container, or live data
       - for a bug fix: the reproduction, and how you know it is the root cause rather than a symptom

     If something is unverified, say so plainly. That is more useful than a confident guess. -->

- [ ] `uv run ruff check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest`

## Anything a reviewer should push back on

<!-- Trade-offs you made, scope you deliberately left out, assumptions you are not sure about.
     Silence here reads as "nothing to question", so use it when that is not true. -->

<!--
Before you open this:

  * Read `docs/scenario-contract.md` if you touched a scenario or the control loop — every invariant
    in it was paid for by a failed live run.
  * `docs/architecture.md` §2 (design principles) and §6 (the four control boundaries) are locked
    decisions. Contradicting one needs an ADR in `docs/adr/`, not a PR comment.
-->
