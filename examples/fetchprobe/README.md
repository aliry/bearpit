# fetchprobe

**One agent, one job: call `web_fetch` once and repeat what it returned.**

A diagnostic in the shape of `toolcheck` — run it when a granted tool looks like it is not working,
before blaming the model.

```sh
pit up examples/fetchprobe
```

## Why the URL matters

It fetches `https://uuid.rocks/json`, which returns a **freshly generated UUID**. That is the whole
design: the agent cannot know it, guess it, or remember it, so the only way to have the value is to
have actually fetched it.

The first version of this probe used `https://example.com/` and was useless — the agent posted
"# Example Domain" without calling anything, because that page is the most memorable document on
the internet. A probe whose answer the model already knows cannot tell a fetch from a recollection.

## Reading the result

The message is not the evidence. Check the record:

```sh
docker logs pit-realmtools 2>&1 | grep "by=<realm>/prober"
# tool=web_fetch() by=<realm>/prober ok
```

or the chronicle, for `tool_call` / `tool_result` events on that realm. A plausible UUID with no
`tool_call` event means the model invented it — which is exactly the failure this probe exists to
catch, and exactly what #73 turned out to be.

## What it found

Run against the platform as it was, this probe surfaced two defects that a five-agent research
scenario had only hinted at:

- **`wire_tools` ignored grants.** A scenario with no referee, no mechanic and no turns was never
  connected to the Realmtools server at all — so a granted tool had nothing on the other end.
- **The Copilot allowlist was hardcoded.** `--available-tools` named the fifteen standing verbs, so
  a granted tool was explicitly excluded from what the model was permitted to call.

Both are fixed. The probe stays, because the next tool to go quietly missing will look the same.
