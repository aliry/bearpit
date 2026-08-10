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

All five hold `web_fetch`, capped at eight calls each by `spec.tools`. There is no host allowlist,
so any public page is reachable — private, loopback and cloud-metadata addresses are refused by the
tool itself, and every fetch is chronicled with its redirect chain.

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

## Two live runs, before and after the platform fix

**Run 1 (`brief-1`)** — five agents, ~$2.70. **Zero `web_fetch` calls.** That turned out not to be
the model's fault at all: the Forge never wired the Realmtools server for a grant, and the provider's
tool allowlist excluded anything beyond the fifteen standing verbs. The tool could not be called by
anyone (#73, fixed).

**Run 2 (`brief-2`)**, after the fix — **32 fetches, all successful**, by four of the five agents,
against real sources: IEA electricity reports, an OWID page, a `science.org` DOI that answered a
genuine 403. The plumbing works.

**The editor still ruled `published — unverified`**, on the grounds that no figure in the brief was
traceable to a page a researcher had actually read. That is now a *scenario* question rather than a
platform one, and it is the interesting one: agents fetched, and then wrote their claims from
memory anyway rather than from what came back. Some of it is real — the IEA pages are heavy and a
403 is a 403 — and some of it is a citation habit the prompts have not yet instilled.

So the mechanism did its job twice, for two different reasons. In run 1 it correctly reported a
platform that could not fetch; in run 2 it correctly reported researchers who fetched and did not
cite. A confident brief would have been wrong both times.

## If nothing gets fetched
## If nothing gets fetched

In rough order of what to try: raise the researchers' `model_category` to `large`; give the topic a
more obviously lookup-shaped question (a current figure beats a conceptual one); or narrow
`spec.tools.web_fetch` with an `allow` list so the model has an obvious first URL to reach for.
