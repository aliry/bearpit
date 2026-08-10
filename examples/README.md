# Example realms

Each folder here is a **portable project package**: a `project.json` plus an `agents/<id>/` folder
for every agent (its `agent.json` and `persona.md`). No secrets live in a package — a model
references a credential *handle* (e.g. `azure-main`) that the runner resolves from its keystore at
run time, so a package is safe to share.

## The realms

Sorted roughly by how much of the platform each one exercises.

| Package | Agents | Shape | Ends when |
| --- | --- | --- | --- |
| [`toolcheck`](./toolcheck) | 3 | Diagnostic | The umpire confirms every tool answered — run this first if something looks wrong |
| [`fetchprobe`](./fetchprobe) | 1 | Diagnostic | One agent, one fetch, an unguessable answer — run this when a granted tool looks broken |
| [`param-relay`](./param-relay) | 3 | Diagnostic, parameters | Shows `${name,default,description}` end to end — the worked example for ADR-003 |
| [`fact-race`](./fact-race) | 3 | Research, asymmetric tools | One analyst can look things up and one cannot — the worked example for ADR-004 |
| [`research-brief`](./research-brief) | 5 | Research, tools, review | Three angles, a citation critic, and an edited brief on any topic you pass in |
| [`rps-duel`](./rps-duel) | 3 | Competitive, hidden move, refereed | The referee rules the match after 10 rounds |
| [`sealed-auction`](./sealed-auction) | 4 | Sealed bids | The clerk reveals the bids and declares a winner |
| [`reverse-auction`](./reverse-auction) | 4 | Procurement, undercutting | The buyer awards the contract |
| [`debate-arena`](./debate-arena) | 3 | Adversarial, judged | The judge rules after closing statements |
| [`turns-debate`](./turns-debate) | 4 | Strict turn order, judged | The chair rules |
| [`pitch-contest`](./pitch-contest) | 4 | Competitive, scored | The judge scores every pitch |
| [`council-vote`](./council-vote) | 5 | Hidden votes, governance | The chair tallies a sealed vote |
| [`board-majority`](./board-majority) | 6 | Coalition-building, voting | The secretary records a majority |
| [`jury-unanimous`](./jury-unanimous) | 4 | Deliberation to consensus | The foreperson reports a unanimous verdict |
| [`split-the-pot`](./split-the-pot) | 4 | Negotiation, side deals | The banker records an agreed split |
| [`relay-story`](./relay-story) | 4 | Cooperative, turn-taking | The editor accepts the finished story |
| [`beacon-brief`](./beacon-brief) | 3 | Cooperative, shared folder, **no referee** | All three sign off on the shared brief |
| [`triad-build`](./triad-build) | 5 | Cooperative, free-for-all, shared folder | The editor accepts the assembled design |
| [`market-scan-duel`](./market-scan-duel) | 3 | Research race, free-for-all | Both analysts file a final report |
| [`border-states`](./border-states) | 6 | Diplomacy-style, alliances, betrayal | The cartographer adjudicates the final year |
| [`cygnus-crew`](./cygnus-crew) | 9 | Social deduction, hidden roles, sealed votes | The game-master declares a faction win |

**`cygnus-crew` is the hardest**: nine always-on agents, a private world model, sealed simultaneous
actions *and* sealed votes each round, faction-private messaging, and a referee that ends the realm
by tool call. **`toolcheck` is the cheapest** — use it to confirm a fresh install works.

## Run one

You need the platform stack up and one credential handle registered. From the repository root:

```bash
# 1. bring up the stack (fill in deploy/.env first — see the top-level README)
docker compose -f deploy/docker-compose.yaml up -d

# 2. register the credential these resolve to (BYOK — your key, your spend)
pit keys add openrouter-main --provider openrouter \
  --api-base https://openrouter.ai/api/v1 --api-key sk-or-...

# 3. validate first — it costs nothing and catches most authoring mistakes
pit validate examples/toolcheck

# 4. run it to a verdict
pit up examples/toolcheck
```

`pit up` provisions the realm, runs it to its termination condition, then prints the final
report. Inspect a run with `pit status <realm>`, `pit tail <realm>`, and
`pit archive <realm>`.

Pick the pipeline these resolve against on the **Settings** page (or `PUT /api/settings/provider`).
Agents declare a capability *tier*, never a model, so the same package runs on any provider.

## Writing your own

Copy the closest package and edit it. Two things are worth reading first:

- **`docs/scenario-contract.md`** — 18 invariants, each one paid for by a failed live run. A
  scenario that violates them tends to fail in ways that look like platform bugs.
- The **`toolcheck`** package — the smallest complete example of every moving part.

`pit validate <path>` checks structure, and the scenario editor in the web UI validates as you
type.
