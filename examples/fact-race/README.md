# fact-race

**Two analysts, the same question, and only one of them can look things up.**

This is the worked example for per-agent tool grants (ADR-004). Scout holds `web.fetch` and is told
to answer by reading; Pundit holds nothing and must answer from memory; the Judge holds `web.fetch`
too, so it can verify rather than recall before ruling.

Before tool grants existed, every agent in a realm had the same capabilities, which made this
scenario impossible to express. The asymmetry *is* the experiment: does a research advantage
actually win, and does an agent that cannot look something up say so honestly or invent a number?

## Running it

```sh
pit up examples/fact-race
pit up examples/fact-race --param question="How tall is the Eiffel Tower?"
```

The question is a scenario parameter, so one scenario covers any factual question without editing
the manifest.

## What to watch

- **Does Scout actually fetch?** A granted tool an agent never calls is the interesting failure —
  the capability is there and the model did not reach for it.
- **Does Pundit stay honest?** Its persona tells it to flag stale knowledge. Claiming a source it
  cannot have read is the behaviour the Judge is asked to punish.
- **Does the Judge verify?** It is the referee, and it holds the tool precisely so its ruling rests
  on something checked rather than remembered.

## Policy

`spec.tools` caps `web.fetch` at four calls per agent and allows Wikipedia only, so the scenario is
bounded in both spend and blast radius. Both are realm-level: the scenario sets the policy, each
agent holds the grant.
