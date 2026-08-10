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

## The first live run

Run `brief-1`, topic *"How much electricity do data centres use, and is it growing?"*, audience
*"a policy analyst"*. Five agents, ~$2.70, concluded on the editor's verdict.

**Outcome: `published — unverified`.** Not one `web_fetch` call was made by anyone — while the same
agents used `recall`, `remember` and `run_code` from the same MCP server, repeatedly and
successfully, in the same run (#73).

The point is what the scenario did with that:

- each researcher disclosed it rather than inventing citations — *"I have no working `web_fetch` in
  this session, so nothing below is a fresh citation — from memory, unverified"*
- the Critic's audit came back, in the Editor's words, *"clean but empty"*
- the Editor pushed back once, named the exact fetch it wanted from each agent, and then published
  under the honest outcome
- the verdict records it plainly: *"Zero of the brief's factual claims were grounded in a fetched
  source"*

So the run is a **legible failure**, which is the design goal. A scenario that cannot yet reach the
web still produces an accurate account of the fact — rather than a confident brief resting on
remembered numbers.

## If nothing gets fetched

In rough order of what to try: raise the researchers' `model_category` to `large`; give the topic a
more obviously lookup-shaped question (a current figure beats a conceptual one); or narrow
`spec.tools.web_fetch` with an `allow` list so the model has an obvious first URL to reach for.
