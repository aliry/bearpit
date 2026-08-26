# ADR-005: Realm outputs — a scenario declares the files its run produces

**Status:** Proposed · 2026-08-26
**Input:** An operator asked how to retrieve `brief.md` from a finished `beacon-brief` run; survey
of the shared-volume lifecycle in `forge.py`, the flight-recorder precedent, and `pit archive`.

## Context

Several scenarios produce a **file** as their entire deliverable. `beacon-brief` co-authors
`/realm/shared/brief.md`; `triad-build` assembles a design doc from four section files;
`market-scan-duel` judges two reports written to `/realm/shared/<agent>/report.md`. In each, the
commons is scaffolding and the file is the point — `beacon-brief` says so outright: *"a brief
pasted into chat counts for NOTHING."*

**That file is destroyed when the realm ends.** `Forge.teardown_realm` calls
`remove_volume(handles.shared_volume)`, and nothing reads it first. The verdict then describes a
document nobody can open.

Asked for a real run's brief, the only way to recover it was to scrape `run_code` traffic out of
the chronicle and reassemble the file from the code an agent happened to `print()` back. That
worked by luck — agents read files back to verify them — and it is archaeology, not a feature.

Today's surface, in full:

| | |
|---|---|
| shared volume | destroyed at teardown |
| `pit archive` | transcript + report only |
| `FILE` event kind | exists; nothing emits it |
| console | no artifact listing or download |

**The same problem was already solved once, for logs.** Immediately before destroying each agent
container, Forge writes its stdout/stderr to `~/.bearpit/realms/<realm>/logs/<agent>.log`, with the
comment: *"post-mortems repeatedly died on 'the containers are gone'."* The shared volume has the
identical shape and no equivalent rescue.

The capability is already present, too: `RuntimeAdapter.read_volume(name) -> {path: content}`
exists and runs every tick to evaluate the `file` termination condition. Only the policy is missing.

## Decision

**A scenario declares the files its run produces. The platform captures them before it destroys
the volume, writes them beside the flight logs, and records what it captured in the chronicle.**

### The declaration

```json
"spec": {
  "outputs": ["brief.md", "sections/*.md"]
}
```

Glob patterns, relative to the shared folder. Absent or empty means a realm produces no files,
which is true of most scenarios and must stay the default — the commons is the deliverable for a
debate, and capturing a seeded `README.txt` as an "output" would be noise.

Declared rather than inferred, for the reason the operator gave when asking for this: *"not always,
it depends on the scenario."* Capturing everything would also sweep up the platform's own seeded
README and whatever scratch files agents left behind.

### Captured at teardown, on the host

`Forge.teardown_realm` reads the volume and writes matching files to
`~/.bearpit/realms/<realm>/outputs/`, preserving relative paths, immediately before
`remove_volume`. It is the flight recorder's placement and its failure posture: **best-effort**,
wrapped so that a capture error can never wedge teardown. A realm that cannot save its output must
still release its containers, its network and its keys.

### Recorded in the chronicle as metadata, not content

Each captured file emits one `OUTPUT` event carrying **path, bytes and sha256 — never the body.**

This is the "everything is chronicled" principle satisfied at the right grain. The record answers
*what did this run produce* — which is what a report, a rerun comparison and an audit need — while
the bytes live where bytes belong. Putting bodies in the log would grow an append-only store by the
size of every artifact, and the console polls that store every few seconds.

A declared output that **was never written** emits `OUTPUT` with `missing: true`. That is a result,
not an error: `triad-build` has ended twice with four good section files and no assembled
`design.md`, and *"the deliverable was never written"* is precisely what its record should say.

### Text only, to begin with

`read_volume` already skips binary files and anything over 1 MB. That covers briefs, design docs,
reports and CSVs — every deliverable the shipped scenarios produce. A scenario needing an image or
a PDF is a real case and a later one; the event shape does not change when it arrives.

## Alternatives considered

**File bodies in the chronicle.** One source of truth, survives host loss, replays with the realm,
and would work unchanged for a cloud realm where there is no operator filesystem. Rejected for now
because it grows an append-only store by the size of every artifact and puts document bodies behind
a polling endpoint. The `OUTPUT` event is deliberately shaped so bodies can be added later without
changing what already exists.

**An agent-invoked `publish(path)` tool.** The most ADR-002-shaped option: the agent declares its
own deliverable, which allows interim artifacts and dynamic choices. Rejected on evidence. An agent
that simply does not call a tool it holds is the failure this codebase spent #73 diagnosing — and
here the cost is the deliverable itself, silently. The operator also framed this as a property of
the scenario rather than of the agent.

**Keep the volume and reap it later.** Trivial, and preserves everything including binaries.
Rejected: unbounded disk growth, Docker-local so it does not survive the platform moving anywhere,
and the answer to *"how do I get my file"* stays `docker run -v`, which is not an answer.

## Consequences

- `pit archive` becomes the one honest "give me everything from this run" command: transcript,
  report, and the files the run produced.
- The console can list and offer outputs, because the `OUTPUT` events tell it what exists without
  reading any bodies.
- Scenarios whose deliverable is a file gain a way to state that in the manifest. The scenario
  contract should say that a scenario whose goal names a file ought to declare it — the same shape
  as rule 20 for tools.
- A cloud realm will need a different sink for the bytes. The declaration and the event are
  unaffected; only the writer changes.
- This is filesystem-boundary work (architecture §6), not scenario logic: the platform already owns
  the volume's whole lifecycle, and this is reading it once more before deleting it.
