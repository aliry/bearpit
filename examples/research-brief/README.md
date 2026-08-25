# research-brief

**Three researchers take different angles on one topic, a critic checks every claim against a real
source, and an editor publishes the brief.**

```sh
pit up examples/research-brief
pit up examples/research-brief --param topic="What is the state of solid-state batteries?"
pit up examples/research-brief -p topic="Why did the Baltic Dry Index spike?" -p audience="a logistics analyst"
```

Both the topic and the audience are scenario parameters, so one scenario covers any research
question without editing the manifest.

## The roster

| agent | angle |
|---|---|
| **Evidence** | the numbers — figures, dates, magnitudes, who published them |
| **Context** | how we got here — the history and what changed recently |
| **Challenge** | the strongest case against the obvious answer |
| **Critic** | which claims were actually sourced, and which were remembered |
| **Editor** (referee) | commissions the work, publishes the brief, ends the realm |

All five hold `web_fetch`. `spec.tools` gives each researcher twelve calls and the Critic thirty — a verifier that re-checks everyone else's sources needs several times what any one of them spends, and the first run starved it mid-audit on a shared cap of eight.
so any public page is reachable — private, loopback and cloud-metadata addresses are refused by the
tool itself, and every fetch is chronicled with its redirect chain.

## How a claim gets made

Every factual claim carries **two** things: the URL, and a **verbatim quote** from what the tool
returned. The Critic then re-fetches that URL with `contains` set to the quote — if the words come
back, the claim is supported; if they do not, it is unsupported whatever URL it carries.

That is deliberate. A URL is cheap to write from memory; a quote is not, because you cannot produce
one without the page in front of you. And `web_fetch`'s `contains` argument makes checking it a
single call.

## The design problem this scenario is built around

A granted tool is not a used tool. An agent can hold `web_fetch`, be told to use it, be offered it
in its function list, and still answer from memory — sometimes while claiming it looked something
up (#73).

So the scenario makes that failure **visible instead of silent**:

- every claim must carry the URL it was fetched from, or the words `from memory, unverified`
- the **Critic** exists to sort one from the other, and to spot-check citations by opening them
- the Editor's brief must end with a *"What we could not verify"* section
- the verdict is `published` or `published — unverified`, and the abstract must say how many claims
  were sourced versus remembered

A run where nobody fetches anything is therefore still a *legible* run: it ends
`published — unverified`, and the transcript says exactly who never looked anything up. That is a
finding, not a silent failure.

## What to watch

- **Does anyone actually fetch?** The realmtools audit and the `tool_call` events in the chronicle
  are the ground truth — not what an agent says it did.
- **Does the Critic catch an unsourced claim?** That is the mechanism doing its job.
- **Does the Editor's verdict match reality?** If the transcript is full of unsourced claims and the
  outcome is `published`, the referee is being generous with itself.

## What the live runs showed

Five runs, and each one moved the bottleneck somewhere more interesting.

**Run 1** — zero fetches. Not the model's fault: the Forge never wired the tools server for a
grant, and the provider allowlist excluded the tool (#73). Fixed.

**Run 2** — 32 successful fetches, and still `published — unverified`. Agents fetched and then
wrote from memory anyway. That looked like a prompting problem and was mostly a tool problem: a
fetched article arrived as ~64,000 tokens of raw HTML, truncated mid-document. Nobody can quote
from that. `web_fetch` now returns prose, and `contains` returns just the passages around a phrase
(~300 tokens) — see #77.

**Run 3** — agents used `contains` and posted verbatim quotes; the Critic re-fetched and confirmed
five of them. Then it ran out of calls mid-audit, because all four agents shared one quota and a
verifier re-checks everyone else's sources. Quotas gained a per-agent override.

**Run 4** — one claim confirmed verbatim and one citation **failed** re-fetch, which is the
mechanism catching exactly what it exists to catch.

**Run 5** — the Critic now reports coverage explicitly:

    COVERAGE: checked 4 of 6 claims — unchecked: Context's IEA quote (no URL supplied to me),
    Evidence's DOE/LBNL figure.

so the editor can tell *"I checked and it failed"* from *"I never got to it"*, and a researcher who
posts a claim with no URL is named for it.

### Where it lands

The verdict is still `published — unverified`, and that is now an accurate report rather than a
failure: two claims re-confirmed, six carrying URLs and quotes that the Critic did not reach inside
its turns. Full verification of every headline figure is not yet reliable — the researchers post
faster than one verifier can re-check, and that is a finding about multi-agent review under a turn
budget, not a broken scenario.

What the scenario reliably delivers is the thing it was built for: **you always know which claims
were checked, which failed, and which nobody got to.**

## If nothing gets fetched
## If nothing gets fetched
## If nothing gets fetched

In rough order of what to try: raise the researchers' `model_category` to `large`; give the topic a
more obviously lookup-shaped question (a current figure beats a conceptual one); or narrow
`spec.tools.web_fetch` with an `allow` list so the model has an obvious first URL to reach for.
