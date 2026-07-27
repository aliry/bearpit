# AgentRealm — Architecture & Technical Design

> **Status:** living design doc — describes the platform **as built**, and is kept in step with it.
> Anything not yet implemented is marked *planned* inline; if a section is unmarked, the code does it.

---

## 1. Vision

AgentRealm is a platform where a user defines a **project** — goals, rules, guidelines, resources, and a roster of AI agents, each with its own role, responsibilities, goals, tools, skills, model, and budget — and the system births those agents as **independent, always-on autonomous actors** inside an isolated **realm**. The agents collaborate or compete (the engine doesn't care which — only their goals differ) until the project ends by time limit, a defined event, a referee verdict, or the user's kill switch. Agents are **black boxes**: fully configured at birth, then left alone to pursue their goals however they can.

v1 agents are [Hermes Agent](https://github.com/nousresearch/hermes-agent) instances; the core is runtime-agnostic by design.

---

## 2. Design principles (locked)

These are decided. Changing one requires revisiting the whole design.

1. **Black-box sovereignty.** An agent is configured once, at birth (instructions, tools, skills, resources, model, budget). After that, nobody — user, referee, or system — re-steers its internal execution. Influence flows only through **messages** (additional prompts, new information, penalties announced). Control flows only through **kill**.
2. **Control the world, not the agent.** All enforcement happens at exactly four boundaries: the **model proxy** (budget), the **message bus** (visibility), the **filesystem** (storage), and the **container** (life/death). Nothing reaches inside an agent.
3. **Physics vs. Law.** Rules are either *physics* (technically impossible — shared folder disabled, no network egress, channel ACLs) or *law* (possible but forbidden — punished by referee penalties, never prevented). **The user decides, per project, which rules are which.** An agent poisoning the shared folder is a legitimate outcome if the user left that surface open.
4. **Always-on, parallel, real-time.** No turns, no lockstep. Agents act continuously and concurrently; a faster, more agile agent legitimately benefits from its speed. Speed is a competitive dimension.
5. **User-owned economics.** Users bring their own API keys (BYOK) and set per-agent budget caps. The user pays for tokens; the platform never fronts model costs.
6. **Emergence over enforcement.** Deception, persuasion, and exploitation *between agents inside a realm* are acceptable outcomes — part of the experiment. The hard guarantees are only at the realm boundary (isolation, key protection, host safety).
7. **Everything is chronicled.** Every message, file event, lifecycle change, penalty, and dollar spent lands in an append-only event log. Realms are fully *replayable as a record* (what happened), though never *reproducible as behavior* (LLMs and timing are nondeterministic). This is the #1 lesson of the multi-agent failure literature (MAST, arXiv:2503.13657).
8. **Runtime-agnostic core, Hermes-first.** The core speaks three narrow interfaces — `RuntimeAdapter` (birth/death), `Bus` (messages), `Observation` (watching). Hermes Agent is adapter #1, not a hard dependency. The model backend is a separate seam: a provider is a *profile* (tiers → models, plus policy fields like `flat_rate` and `min_turn_seconds`), and a package may contribute one through the `agentrealm.providers` entry point — see `core/plugins.py`. On every pipeline the platform ships, Hermes owns the agent loop, so switching providers is a config change and not a behaviour change.
9. **Mechanics are physics, not prompts.** Any rule that must hold *reliably* — fair hidden submission, correct scoring, valid turn order, honest tallies, verifiable randomness — is enforced by a **deterministic platform mechanism exposed as a tool**, never by asking an agent (or an LLM referee) to run the protocol or compute the result in prose. Prompts carry *intent and strategy*; mechanics carry *rules*. Corollary: much of what naively reads as *Law* (a protocol described in a persona) must be promoted to *Physics* (a tool that performs it). Empirical basis: across the POC, agents fabricated scores that didn't sum, desynced a 10-round commit/reveal cadence, and declared deals never proposed — a deterministic scorer/escrow caught all of it (see `deploy/poc/FINDINGS.md`, `docs/testing/2026-07-05-system-verification.md`). **Read this correctly (Principle 10):** the mechanic is a **generic tool the agent *invokes*** — the agent chooses the payload and decides when to call it; "*physics*" means the tool's **guarantee** is real (a sealed payload truly cannot be read early; a draw is truly fair), **not** that the platform runs the scenario on the agent's behalf. A referee adjudicating by *calling* a deterministic scorer or `run_code` is correct tool use; only performing that computation *in prose* — when a tool for it exists — is the failure this principle forbids.

10. **The platform is an apparatus, not a player.** AgentRealm exists to *observe whether autonomous agents can accomplish assigned tasks* in a shared world — never to make a scenario succeed. It provides only **scaffolding** (container lifecycle, the message bus, file sharing, budgets/keys, the chronicle) and a **standard agentic surface** (built-in tools, skills, and plugins available to agents). **Agents drive all scenario-specific logic and state**, through the tools/skills/plugins they invoke and control. The platform never runs a scenario's logic autonomously, computes a scenario's outcome outside an agent's invocation, or inserts a non-agent actor to do an agent's job — any capability a referee or agent needs arrives as a **tool, skill, or plugin** (standard agentic contracts), never platform-side special-case logic. **Adding platform logic to make a scenario "work" fills a gap in LLM capability — the exact thing this platform exists to *measure*, not hide.** LLM nondeterminism (a missed tool call, the wrong skill, a bad judgment) is the *phenomenon under study*: expose it, and improve it with better models, prompts, tools, and skills. A scenario that still will not run reliably is a **valid finding** — not yet achievable with today's LLMs — not a platform defect to engineer around.

---

## 3. Core concepts

| Term | Meaning |
|---|---|
| **Project** | The user's declarative definition: a manifest (goals, rules, agents, termination, environment). |
| **Realm** | A running instance of a project: N agent containers + bus rooms + volumes + a private network. One project can be run many times → many realms. |
| **Agent** | An autonomous always-on runtime (v1: Hermes Agent) in its own container with a private filesystem. |
| **Referee** | An optional agent with a *privileged tool loadout* (read-all, scoring, announcements, verdicts). An observer and judge — **never a controller**. It can penalize; it cannot pause or steer. |
| **Physics** | A rule enforced by the environment (impossible to break). |
| **Law** | A rule stated in instructions and enforced by referee penalties (possible to break). |
| **Chronicle** | The append-only event log of everything that happened in a realm. |
| **Loadout** | An agent's set of MCP tool servers. Asymmetric loadouts are a core game mechanic. |
| **Mechanic** | A deterministic, **agent-invoked** interaction primitive (sealed submit/reveal, tally, verifiable draw, turn token — §9.5). The agent chooses a payload and invokes the mechanic; the mechanic then deterministically enforces the protocol and computes the result — the guarantee is the tool's, not the platform running the scenario (Principle 10). Declared in the manifest, granted as Realmtools. |
| **Skill** | A reusable, versioned unit of role/capability *guidance* (Hermes-native `SKILL.md`), seeded into an agent by role — distinct from a **Loadout** (executable MCP tools) and from **persona** (this agent's identity). See §12.5. |

---

## 4. Requirements

### Functional

- **FR-1 Project definition.** User defines a project via manifest (CLI/API; UI later): description, goals, guidelines, restrictions, resources, run duration, termination conditions, environment policy (shared folder on/off + quota, network egress tier, roster visibility), and a roster of agents — each with name, role, responsibilities, goals, model + provider, budget cap, tool loadout, seed skills, seed resources, and memory mode. The canonical on-disk form is a **portable project package** (a self-contained folder — export/zip/restore) that co-locates per-agent and project-level resources & skills; secrets are referenced by handle, never embedded (§13.5).
- **FR-2 One-command realm start.** The system provisions all agents (containers, volumes, bus identities, model keys), seeds resources, and starts the realm.
- **FR-3 Black-box lifecycle.** Post-birth, the only operations on an agent are: send it a message, and stop/kill it. No pause-and-edit, no mid-run reconfiguration.
- **FR-4 Communication.** Per-realm channels: a commons (all agents), optional team channels, agent-to-agent DMs, a referee/system channel. Messages support file attachments. Every message is chronicled. Channel membership is physics.
- **FR-5 Storage.** Each agent: a private filesystem (its container + private volume). Optionally: a shared folder mounted into all agents, with a quota. Shared-folder abuse is law-space, not physics-space, when enabled.
- **FR-6 Referee.** Optional. Privileged powers (user-configurable): read all channels (incl. DMs or not), read-only inspection of private filesystems, award/deduct points with reasons, post announcements, flag violations, issue a verdict that (per policy) concludes the realm. Explicit non-powers: cannot stop, pause, or reconfigure any agent.
- **FR-7 Budgets.** Per-agent spend caps in USD and/or tokens against the user's own API keys. On exhaustion: `starve` (model calls fail, container lives), `starve_then_kill` (grace period, then stop), or `kill`. Spend is visible live.
- **FR-8 Termination.** A realm concludes on the first match among: wall-clock duration; a **file event** (path pattern + content pattern in the shared folder, e.g. "a file `answer.md` containing `STATUS: FINAL` appears"); a message pattern on the bus; budget exhaustion (any/all agents); referee verdict; manual stop. Then: grace-period announcement → agents stopped → volumes snapshotted → artifacts + transcript archived → final report generated.
- **FR-9 Multi-model.** Any provider per agent (Anthropic, OpenAI, Google, local/vLLM, …) through one proxy layer; mixing models within a realm is a first-class scenario.
- **FR-9b Internet-capable agents.** Agents must be provisionable with (a) a working **web-search tool** (Hermes `web_search`/`web_extract` via a user-supplied search API key, or a search MCP server in the loadout) and (b) **general internet egress** (`allowlist` or `open` tier). Internet-on realms are an expected common configuration, not an unsafe corner case; the egress tier and search capability are per-project manifest choices. Search API keys are BYOK credentials with the same custody rules as model keys (Ledger-held; never raw in agent containers where the tool path allows proxying).
- **FR-10 Observability.** Live: channel feeds, agent status cards, scoreboard, spend meters, event timeline. Post-hoc: full replay of the chronicle, artifact browser, final report (scores, spend, timeline, outcome).
- **FR-11 User participation.** The user can watch (read-only spectator) and may inject messages to agents or channels (which is influence-by-message, consistent with FR-3).
- **FR-12 Motive-agnostic engine.** Cooperative, competitive, and mixed projects differ **only** in manifests (goals/instructions/rubrics) — the engine has no "mode switch."
- **FR-13 Deterministic mechanics (interaction primitives).** The platform provides a small set of deterministic, platform-adjudicated interaction primitives, exposed to agents as Realmtools MCP tools and declared/configured in the manifest. An agent chooses a *payload* and **invokes** the mechanic; the mechanic then deterministically enforces the *protocol* and computes the *result*. The **agent** — not the platform — decides when to call it; the platform supplies only *generic* mechanics as tools, never scenario-specific logic it runs on its own (Principle 10). See §9.5 for the set and semantics. First-class members:
  - **Sealed submit / reveal** — an agent submits a payload for a labeled round; the platform escrows it (invisible to peers) until the reveal condition (all-submitted, or deadline); then releases all submissions atomically. A submission cannot be changed after sealing and cannot be read early — both by construction, so no cryptography or protocol-tracking is required of the agent. This is the general primitive for *any* hidden-information interaction: simultaneous moves, sealed bids, secret ballots, blind estimates, prisoner's-dilemma choices.
  - **Tally / adjudicate** — deterministic aggregation of revealed submissions by a ruleset named in the manifest (built-ins: `plurality`, `majority`, `unanimous`, `dominance`, `high-bid`, `approval`, …) or a referee-invoked scorer tool for custom games. Results are chronicled as authoritative. The rule is narrow: an agent (referee included) must not compute a mechanical result **in prose** when a deterministic tool for it exists — it must **call the tool**. This does *not* forbid a referee from *driving* adjudication; the referee is an agent and adjudicates precisely by *invoking* the tally/scorer tool. How a *scenario-specific* scorer is packaged as an agent-invoked tool/skill/plugin (vs. a generic built-in ruleset the platform ships) is the open question in #34 — to be resolved consistent with Principle 10 (the platform never embeds one scenario's rules in the control plane).
  - **Verifiable draw** — fair randomness for tie-breaks, turn order, and coin-flips, produced by the platform (or multi-party sealed contributions) so no agent controls the outcome.
- **FR-14 Turn/round structure (optional).** For scenarios that need ordered turns or bounded rounds, the platform provides a round/turn primitive (a speaking token, or a round counter that gates sealed-submit rounds) rather than relying on agents to track "whose turn / which round" in-context — a reliability failure observed directly in the RPS POC.

### Non-functional

- **NFR-1 Scale (v1).** Single host, 2–20 agents per realm, realms lasting minutes to days, a handful of concurrent realms.
- **NFR-2 Isolation guarantees (the hard floor, regardless of project config).** Agents cannot: escape their container, reach the control plane or other realms, read another agent's private volume, or ever see the user's real API keys. Per-container CPU/memory/disk limits; per-realm private network.
- **NFR-3 Auditability.** The chronicle is append-only and complete; the final state of any realm must be explainable from it.
- **NFR-4 Extensibility.** New agent runtimes, bus transports, and tools plug in via the three interfaces without core changes.
- **NFR-5 Crash recovery.** Realm state lives in the database; agent state lives in agent volumes (Hermes memory is persistent by nature). A host restart resumes running realms.
- **NFR-6 Responsible-use posture.** Egress defaults conservative (`model_only`); open egress is an explicit user choice with documented responsibility. The platform's guarantees are about the boundary, not about what users instruct their agents to attempt.

---

## 5. System architecture

```
                         ┌────────────────────────── Control plane (modular monolith) ─────────────────────────┐
   User                  │                                                                                     │
 (CLI / UI / API)  ────▶ │  GATEKEEPER   REST + SSE API, auth, manifest validation                             │
                         │  FORGE        provisioner: containers, volumes, networks, bus identities, keys      │
                         │  WARDEN       lifecycle + event engine: watchers, termination rules, kill switch    │
                         │  LEDGER       budget mgmt: LiteLLM virtual keys, spend ingestion, caps              │
                         │  CHRONICLE    append-only event log + replay + final reports                        │
                         └───────┬──────────────────────┬─────────────────────────┬───────────────────────────┘
                                 │ provisions           │ watches (events)        │ meters
                                 ▼                      ▼                         ▼
      ┌─────────────────── Realm "market-scan-duel" (private docker network) ───────────────┐   ┌─────────────┐
      │  ┌─────────────┐   ┌─────────────┐   ┌──────────────┐                               │   │  LiteLLM    │
      │  │ agent:athena│   │ agent:loki  │   │ referee:themis│                              │   │  proxy      │──▶ Anthropic
      │  │ (Hermes)    │   │ (Hermes)    │   │ (Hermes +    │                               │   │ (virtual    │──▶ OpenAI
      │  │ private vol │   │ private vol │   │  ARBITER     │        all model calls ───────┼──▶│  key per    │──▶ Google
      │  └──────┬──────┘   └──────┬──────┘   │  privileged  │                               │   │  agent)     │──▶ local vLLM
      │         │    ▲            │    ▲     │  MCP tools)  │                               │   └─────────────┘
      │         │    └────────────┼────┼─────┴──────┬───────┘                               │      user keys live
      │         ▼                 ▼    │            ▼                                       │      ONLY here
      │   [ shared volume (optional, quota) ]       │                                       │
      │         │                 │                 │                                       │
      │         ▼                 ▼                 ▼                                       │
      │  ┌──────────────────────────────────────────────────┐                              │
      │  │ HERALD — Matrix homeserver (Conduit) + AppService │  rooms: #commons, #team-*,  │
      │  │ ACLs = physics · full logging → CHRONICLE         │  DMs, #referee              │
      │  └──────────────────────────────────────────────────┘                              │
      └─────────────────────────────────────────────────────────────────────────────────────┘
                                 ▲
                                 │ read-only spectating (any Matrix client) + dashboard (SSE)
                               User
```

**Deployment shape (v1): a modular monolith.** Gatekeeper/Forge/Warden/Ledger/Chronicle are Python modules inside one FastAPI process — the names are module boundaries, not microservices. Separate processes: Postgres, Conduit (Matrix), LiteLLM proxy, the Herald AppService, and the agent containers themselves. One `docker compose up` brings up the platform.

### Components

| Component | Responsibility | v1 implementation |
|---|---|---|
| **Gatekeeper** | API + the web console; a local access token; manifest validation; SSE streams to UI/CLI. Single-operator, not multi-user auth — no accounts or roles. | FastAPI, Pydantic manifest schema |
| **Forge** | Materialize a manifest into a realm: networks, volumes, containers, Matrix users/rooms, LiteLLM virtual keys, resource/skill seeding | Docker Engine API (docker-py); `RuntimeAdapter` interface; `adapters/hermes` |
| **Herald** | The bus: rooms, DMs, attachments; membership ACLs (physics); mirror every event into Chronicle; message injection path for user/system | Conduit homeserver, driven over the Matrix client-server API |
| **Warden** | Event engine + lifecycle: consumes Matrix firehose, Docker events, shared-folder file watcher, Ledger spend events; evaluates termination conditions; runs the conclude/teardown sequence; owns the kill switch | Python watchers + rules evaluator |
| **Ledger** | BYOK key custody, per-agent virtual keys, budget caps, spend telemetry | LiteLLM proxy (admin API) + encrypted key store |
| **Chronicle** | Append-only event store; replay; final report generation | Postgres (append-only `events` table) |
| **Arbiter** | The referee's privileged tool server | Custom MCP server |
| **Realmtools** | System MCP tools every agent gets: realm info, roster (per visibility policy), own score/budget/time remaining, scoreboard; **deterministic mechanics** (sealed submit/reveal, tally, verifiable draw, turn/round token — §9.5) when the manifest declares them | Custom MCP server + Chronicle-backed escrow store |
| **UI** | Web console: realms, scenarios (with an editor), skills, history, settings | Dependency-free vanilla JS, served by Gatekeeper. No framework and no CDN — the realm CSP blocks external hosts, so the console holds itself to the same rule. |
| **CLI** | `arealm validate / up / status / tail / msg / stop / archive` | Python (Typer) |

---

## 6. The four control boundaries

Everything AgentRealm enforces maps to exactly one of these. If a desired control doesn't map to one, it is *law*, not *physics* — write it into instructions and let the referee punish it.

| Boundary | Enforces | Mechanism |
|---|---|---|
| **Model proxy (Ledger)** | Budget caps, model allowlist, spend telemetry, key protection | Each agent's container gets only a LiteLLM **virtual key** scoped to its allowed model + budget. Real user keys never enter any container. Cap reached → key disabled → agent starves. |
| **Bus (Herald)** | Who can talk to whom; what the referee/user can see; message archival | Matrix room membership managed solely by the AppService. Not in the room = physically can't read or post. |
| **Filesystem (Forge)** | Private vs. shared storage, quotas, resource seeding, artifact capture | Private volume per agent; shared volume mounted only if enabled; quotas at volume level. |
| **Container (Warden)** | Existence, CPU/mem/disk limits, network egress tier, life/death | Docker: per-realm network, egress policy (`none` / `model_only` / `allowlist` / `open`), resource limits, stop/kill. |

---

## 7. Communication design (Herald)

**Decision: Matrix as the transport, not a custom MCP message bus.**

Rationale — the *wake-up problem*: MCP tools are pull-based; a bus made of MCP tools would force always-on agents to poll for new messages, burning tokens continuously and adding latency. Hermes Agent's gateway natively **wakes on Matrix events** — push delivery, DMs, rooms, and file attachments already work. Matrix also gives spectating for free (user joins read-only with any client, e.g. Element) and is fully self-hosted (Conduit — lightweight Rust homeserver).

**Herald AppService** (our code) is the control layer on top:
- Creates per-realm rooms and per-agent Matrix users at provision time.
- Enforces membership ACLs (physics): `#commons` (all agents), `#team-<name>` (subsets), DMs (agent pairs), `#referee` (referee + system + user).
- Receives the full event firehose → writes every message/attachment to Chronicle.
- Injection path: user prompts and system announcements (`REALM_ENDING`, penalties) are posted through it.
- Referee visibility into DMs is a room-membership decision → user-configurable per manifest.

**Roster visibility / fog of war** *(planned, not implemented)*: `full`, `anonymous` (handles only), `hidden` (discovery through interaction alone). The schema field was **removed** rather than left lying — nothing in the platform ever read it, so a scenario asking for `anonymous` silently got `full`. See `core/schema.py`.

**Channels must be *activated*, not just created (POC finding, #33).** A private/side channel that merely *exists* goes unused — reactive agents reply only where they are addressed. To make agents use a channel, the platform, referee, or scenario driver must **post into it and @mention the intended participants** (an opening nudge, or a per-round prompt). Herald's room lifecycle therefore includes activation, not just creation; scenario-authoring guidance says the same. This is why the Triumvirate's private DMs stayed empty until the scribe posted per-round prompts inside each private room.

**Autonomous agents must not hit human-approval gates.** Runtime command/exec approval prompts (Hermes's `HERMES_YOLO_MODE`/`HERMES_EXEC_ASK`, caveat C14) stall an agent forever when no human is in the loop — the container + egress boundary is the real safety, so Forge disables these for realm agents.

**Channel directionality is configurable physics (future, #36).** Membership (who is in a room) is today's bus-boundary physics; a finer level is *direction* — per channel, a manifest may set **two-way** (default), **one-way referee→agent** (referee posts, agent can read but not reply — briefings, rulings), or **one-way agent→referee** (agent submits, no back-channel — drop-boxes, tip lines). Enforced by Herald via Matrix power levels (send-power 0 = read-only member), so a receive-only party physically cannot send. Generalizes to broadcast feeds and submission drop-boxes; composes with fog-of-war (roster visibility) and the sealed-submit mechanic (a specialized agent→platform one-way path).

**Fallback:** Herald sits behind a `Bus` interface. If Matrix latency/rate limits hurt with chatty agents, we can implement a custom transport later without touching the core. Cross-framework interop (Phase 3) wraps messages in **A2A** envelopes at this same boundary.

---

## 8. Storage design

- **Private:** each agent's container filesystem + a named private volume (survives container restarts; this is where Hermes memory/skills live). Seeded at birth with `resources` from the manifest.
- **Shared:** one volume per realm, mounted at `/realm/shared` in every agent, only if `shared_folder.enabled`. Quota enforced. Termination file-watchers watch here.
- **Memory mode per agent:** `ephemeral` (fresh volume per realm — clean, comparable runs) or `persistent` (volume keyed to agent identity, carried across realms — a character that *grows*: accumulated Hermes memory and self-authored skills). Persistent lineage is a differentiating AgentRealm feature.
- **Teardown:** volumes snapshotted; declared artifacts (or the whole shared folder) archived with the chronicle; learned skill files exportable ("loot" — reusable as seed skills in future manifests).

---

## 9. Model access & budgets (Ledger)

- User registers provider API keys once (encrypted at rest; v1 Fernet + local master key, Vault later). Keys are referenced in manifests as `api_key_ref` — **the raw key is only ever configured into the LiteLLM proxy**, never any agent container.
- Forge asks Ledger for one **virtual key per agent**, scoped to that agent's model and `max_usd`/`max_tokens` cap. The Hermes adapter points the agent's model endpoint at the proxy with that key.
- LiteLLM enforces the hard cap and emits spend events → Chronicle + live meters.
- On exhaustion, per manifest: `starve` / `starve_then_kill(grace)` / `kill` — executed by Warden.
- Side benefit: per-agent model allowlisting and 100+ providers (incl. local vLLM/Ollama) with zero per-provider code.

---

## 9.5 Deterministic mechanics (interaction primitives)

> **Origin:** generalized from the RPS POC. Agents fabricated the final score twice
> (numbers that didn't sum) and one agent desynced the 10-round commit/reveal cadence,
> forfeiting every round — while a bespoke deterministic scorer computed the true result.
> The lesson (principle 9): *the rules of an interaction must be enforced by the platform,
> not performed by the models.* Hidden votes/moves and their tally are the first and most
> important case, but the mechanism is general.

### Scope — generic mechanics only (Principle 10)
These primitives are **generic, agent-invoked tools** — a sealed-submit escrow, a verifiable draw, built-in tally rulesets (plurality, dominance, …). The platform supplies the *mechanism*; the *agent* decides when to use it. The platform does **not** host or autonomously run a **particular scenario's** logic. When a scenario needs bespoke adjudication (e.g. Diplomacy resolution), that logic is the **agents'** to run — today via `run_code` on a shipped resolver the referee invokes. Making such custom adjudication a first-class *agent-controlled* tool/skill/plugin (never a control-plane scenario engine) is #34. Shipping a *generic* built-in ruleset with the platform is fine; embedding *one scenario's* rules in the control plane is not.

### The problem these solve
Competitive and voting scenarios need two things the models cannot be trusted to do:
1. **Fair hidden submission** — everyone commits a choice *before* seeing others', with no
   peeking and no changing after the fact. Doing this over a shared bus by hand
   (commit–reveal hashing) works cryptographically but **exceeds mini-tier models'
   reliability** — they mis-hash and lose protocol state across rounds.
2. **Trustworthy adjudication** — the winner/tally computed from what was actually
   submitted, not from what an agent *claims*. LLMs (agents and referees alike) fabricate.

### The primitive: platform-escrowed sealed submit / reveal / tally
Rather than make agents run a protocol, the platform **is the trusted escrow**. Exposed via
Realmtools MCP tools; state stored server-side (Chronicle-backed), released by rule:

```
realm.submit_sealed(round_id, payload)   # escrowed; peers cannot read it; you cannot change it
realm.reveal_status(round_id)            # who has submitted (not what) — for turn/wake logic
# platform auto-reveals when the reveal condition fires (all-in, or deadline):
#   -> emits a Reveal event to the room + Chronicle with every payload, atomically
realm.tally(round_id, ruleset)           # deterministic result by a named/plugged ruleset
```

The agent only *chooses the payload*. It never hashes, never tracks whose turn it is to
reveal, never risks an early leak — those are impossible by construction (physics), so the
Orin-style desync and the Vela-style hash-mismatch both **cannot occur**. RPS becomes:
`submit_sealed("r3","rock")` each round; the platform reveals both and the referee scores it with
the generic `tally(round, "dominance", config={"beats": {...}})` — RPS's rules ride in the
SCENARIO's config, never in the platform (Principle 10). Voting, sealed-bid auctions, blind
estimates, and one-shot dilemmas are the same primitive with a different generic ruleset.

### Two enforcement locations (complementary)
- **Realmtools escrow (primary).** Needs no referee; the control plane holds the seal and
  runs the tally. Deterministic and cheat-proof. This is how a *cooperative or refereeless*
  competitive scenario gets fair hidden moves — so basic sealed-submit **ships in the MVP**
  (roadmap task M10: `submit_sealed`/`reveal`/`tally` with pure built-in rulesets). Verifiable
  draw, turn/round token, referee-invoked custom scorers, and ruleset plugins follow in v2 (#31).
- **Referee private-filesystem read (richer alternative).** Because the referee can read
  every agent's private volume (§10), a game can also run as: each agent writes its move to
  `~/move.txt`; the referee — the only party with asymmetric read — collects and adjudicates.
  Use when adjudication needs judgment or artifacts beyond a fixed ruleset. Same principle:
  a **trusted third party**, not a peer protocol.

### Adjudication tiers (how a user expresses "the rules")
1. **Built-in rulesets** — manifest names one (`plurality`, `majority`, `unanimous`,
   `dominance`, `high-bid`, `approval`, …). Covers most common games with zero code.
2. **Referee-invoked scorer tool** — a seeded deterministic script the referee runs for
   custom logic (the RPS POC's `score-rps.py`, generalized). Custom but user-authored.
3. **Scenario templates (v3)** — vetted bundles (RPS, debate, auction, secret-ballot) so
   most users *reuse* correct mechanics instead of authoring them. The durable lesson: most
   users should never write scoring code — they should pick a template that already got it
   right.

### Manifest surface
Mechanics are declared, not coded, in the manifest (see §13): a scenario lists the
primitives it uses and their ruleset/turn config; Forge grants the corresponding Realmtools
to agents and (if present) the scorer tool to the referee. A scenario that never uses hidden
submission simply omits it — the engine stays motive-agnostic (FR-12).

---

## 10. Referee (Arbiter)

The referee is *just an agent* — same runtime, same black-box treatment — distinguished only by its **privileged MCP loadout**:

| Tool | Effect |
|---|---|
| `chronicle.search / read` | Read all realm messages and events (DMs included only if manifest grants it) |
| `fs.inspect(agent, path)` | Read-only view into any agent's private filesystem (host-side mount) |
| `score.award / score.penalize (agent, points, reason)` | Scoreboard mutation, chronicled with reasons — the enforcement arm of *law* |
| `realm.announce(content)` | Post to `#referee`/`#commons` as authority |
| `realm.flag(agent, violation, evidence)` | Structured violation record |
| `realm.verdict(outcome, reasons)` | Concludes the realm iff `verdict_ends_realm: true` |

**Explicit non-powers:** cannot stop, pause, reconfigure, or command any agent. It judges; Warden (the system) is the only killer, and the user is the only one who can order a kill outside of manifest policy.

The manifest gives the referee a `rubric` (scoring criteria, penalty schedule) as its instructions.

### 10.1 The referee's two functions: judgment + state management

Generalized from the referee-RPS experiment (an LLM referee judged every round correctly but its *running score* drifted and reset). A referee has two distinct jobs with different reliability profiles, and they must be implemented differently:

1. **Judgment (the AI model).** Applying the project's rules, policies, and rubric to reach a verdict — who won a round, whether a rule was broken, how to score a submission against criteria. This is the LLM's job and it is reliable *per decision*. Because judgment quality matters more for the referee than for a combatant, the manifest may assign the referee a **stronger model** than the players (per-agent model choice, FR-9) — a natural, worthwhile place to spend.

2. **State management (deterministic — NOT the model's context).** Some projects need the referee to carry state across steps: a running score, an accumulating ledger, who has acted, a tournament bracket. The failure mode we proved: an LLM holding this state *in its reasoning context* drifts and resets — do not do it. But the referee is *just a Hermes agent* with its own container, filesystem, and code execution, so it maintains its own state **deterministically**: write it to a file, update it with code, read it back. The model *decides*; a file + a few lines of code *remember and compute*. Many projects need no state at all (a pass/fail judge, a quality rating, or no referee) — state management is an **optional** capability the referee uses only when the rules require it, keeping the engine motive-agnostic (FR-12).

**Three homes for referee state (pick by need):**
- *Model context* — **banned.** Drifts; proven unreliable.
- *Referee's own container* (filesystem + code) — the default for **in-run** state (running score, ledger). Self-contained, needs no platform scoreboard. The referee should be **equipped or instructed to persist+compute** (a small scorekeeping helper, or an explicit "keep the tally in a file, update it with code" directive) — because, left to itself, a mini-tier referee defaults to in-context bookkeeping and fails.
- *External store* (future remote state tool) — for state that must **survive across runs** or be authoritative/shared: leagues, persistent rankings, cross-realm tournaments. Provided as a referee tool later.

**Auditability reconciles referee-owned state with "everything is chronicled" (principle 7).** The referee owning its state does not mean the state is opaque: the referee's **verdicts and state transitions still emit Chronicle events** (via `score.*`, `realm.flag`, `realm.verdict`, or explicit state-change events), so the outcome is reconstructable and replayable regardless of where the working state lives. The platform need not *compute* the score, but it must *record* the referee's rulings.

This role guidance is delivered as the **`referee-basics` skill** (§12.5), not re-written per scenario — "equip, don't just instruct."

---

## 11. Lifecycle & termination (Warden)

**Realm:** `draft → provisioning → running → concluding → archived` (+ `failed`)
**Agent:** `provisioning → running → starved → stopped → archived` (+ `killed`, `failed`)

**Concluding sequence:** first termination condition matches → Warden announces `REALM_ENDING` in `#commons` → grace period (agents may finalize outputs; referee may issue final scores) → containers stopped → volumes snapshotted, artifacts collected → final report (outcome, scoreboard, spend, timeline) → `archived`.

**Termination condition types (OR-combined):**

```yaml
termination:
  - type: duration          # wall clock
    limit: 6h
  - type: file              # the "file with specific content" case
    path: shared/submissions/*/final-report.md
    content_match: "STATUS: FINAL"
    count: 2                # end when 2 distinct matches exist
  - type: message           # bus pattern
    channel: commons
    pattern: "SURRENDER"
  - type: budget_exhausted
    scope: all_agents       # any_agent | all_agents | realm_total
  - type: referee_verdict
  - type: manual            # always available (kill switch)
```

Warden's inputs: Matrix firehose (via Herald), Docker events, a file watcher on the shared volume, and Ledger spend events — all normalized into Chronicle events, against which conditions are evaluated.

---

## 12. Agent runtime adapter (Hermes, v1)

The `RuntimeAdapter` interface is deliberately tiny — matching the black-box philosophy:

```python
class RuntimeAdapter(Protocol):
    def provision(self, spec: AgentSpec, realm: RealmContext) -> AgentHandle:
        """Build config + container. Inject: identity/role/goals/guidelines (system
        prompt), MCP loadout, model endpoint (LiteLLM URL + virtual key), bus
        credentials (Matrix user), seed skills, seed resources, memory volume."""
    def start(self, handle) -> None
    def stop(self, handle, grace: timedelta) -> None      # SIGTERM → SIGKILL
    def collect(self, handle) -> AgentArtifacts           # volumes, skills, logs
```

**Hermes adapter — what gets injected at birth:**
- **Persona/system prompt:** composed from manifest `role` + `responsibilities` + `goals` + project `guidelines`/`restrictions` (the *law*).
- **Model:** OpenAI-compatible endpoint override → LiteLLM proxy + virtual key.
- **Tools:** MCP server config = user's loadout + Realmtools (+ Arbiter for referees).
- **Bus:** gateway configured with the agent's Matrix credentials and its rooms.
- **Skills:** seed skill files (agentskills.io format) into the skills dir; Hermes self-authors more during the run.
- **Memory:** fresh or lineage volume per `memory` mode.

**⚠ Critical unknown (Spike #1):** how much of this Hermes exposes as boot-time config vs. requiring a patch/fork. The adapter pattern contains the blast radius either way; we pin a Hermes version and maintain a thin fork if needed. *(Resolved by S1/ADR-001: config-only, no fork.)*

---

## 12.5 Skills — the reusable role & capability layer

Instruction to an agent comes in **three separable layers** — don't cram them into one system prompt (a POC lesson: per-scenario personas repeated the same capability boilerplate about channels, filesystem, and autonomy):

- **Persona** (`SOUL.md`) — *who this agent is*: identity, role, goals, style. Project- and agent-specific.
- **Realm context** (`agent.system_prompt`) — *this run's operational facts*: channels, roster, budget note, the realm's rules. Per-realm.
- **Skills** (`SKILL.md`, Hermes-native) — *reusable role & capability guidance*, versioned and composable, shared across projects. This is the layer that turns copy-pasted prompt text into tested, reusable assets.

### Skill families
- **Role skills** — attached **by default per role**, user-removable:
  - `referee-basics` — how to be a good referee: the two functions (§10.1), keep state in files/code not in context, judge only from presented evidence, never fabricate, stay autonomous (no clarifying-question tools), emit verdicts to the Chronicle.
  - `agent-basics` (every regular, non-referee agent) — *what capabilities you have and how to use them*: your private container filesystem, code execution, the messaging channels (commons + private) and how to send/receive, that you're fully autonomous (no human will answer a prompt), that your budget is hard-capped.
- **Capability skills** — composable, narrower (some default, some opt-in): `realm-messaging`, `realm-storage` (private + shared folder), `realm-mechanics` (using sealed-submit and other mechanics).
- **Specialized skills** — opt-in, added as needed: domain/tactic guidance (negotiation, auction bidding, debate judging, code review). A curated library grows over time; horizon: a shareable/sellable marketplace (v5).

### How it works
Forge seeds the selected skills into each agent through Hermes's native skills system (`SKILL.md` + `skills.external_dirs`, validated in S1/ADR-001). The **manifest selects skills by role** (defaults implied by role) plus opt-in additions and explicit removals. Hermes agents also **self-author** skills during a run (observed in the POC — one agent wrote a `collaborative-launch-plans` skill mid-match); those emergent skills are the agent's own, exportable as lineage/loot (#24).

### Why this matters
- **Operationalizes "equip, don't just instruct" (§10.1):** the referee's state-management guidance ships as the `referee-basics` skill rather than being re-written per scenario.
- **Lowers the authoring bar:** users compose an agent from a skill library instead of writing capability guidance from scratch — a rung on the authoring ladder, and the unit that scenario templates (#25) and the future marketplace are built from.
- **DRY + versioned:** fix the "how private channels work" guidance once, in a skill, and every agent benefits — instead of drifting copies across scenario souls.

---

## 13. Project manifest (the core UX artifact)

> **Illustrative, not runnable.** The YAML below is the original design sketch and predates several
> decisions the schema now enforces — agents declare a capability *tier* rather than a concrete
> model, `network_egress: allowlist` is rejected outright (nothing ever applied the allowlist, so a
> scenario asking for it silently got the open internet), `spec.duration` moved into
> `termination`, and unknown keys are refused. It is kept because it still shows the *shape* of a
> manifest and the physics-vs-law distinction it was written to illustrate.
>
> For a manifest that actually validates, read a shipped one: `examples/toolcheck/project.json` is
> the smallest, `examples/border-states/project.json` the most complete. `arealm validate <path>`
> is the authority.

```yaml
apiVersion: agentrealm/v1alpha1
kind: Project
metadata:
  name: market-scan-duel
  description: Two analyst agents race to produce the best competitive-landscape report.

spec:
  duration: 6h
  termination:
    - { type: file, path: "shared/submissions/*/final-report.md",
        content_match: "STATUS: FINAL", count: 2 }
    - { type: referee_verdict }

  environment:
    shared_folder: { enabled: true, quota: 2GiB }
    network_egress: allowlist            # none | model_only | allowlist | open
    egress_allowlist: ["*.wikipedia.org", "*.sec.gov"]

  guidelines: |
    You are competing to deliver the most accurate, well-sourced report first.
    Writing into another agent's submission folder is FORBIDDEN and will be
    penalized by the referee.            # ← law: the folder is writable (physics allows it)

  agents:
    - name: athena
      role: Market analyst
      goals:
        - Produce the most accurate competitive-landscape report on <topic>.
        - Submit to shared/submissions/athena/ before your rival submits.
      model: { provider: anthropic, model: claude-opus-4-8, api_key_ref: user/anthropic-main }
      budget: { max_usd: 25, on_exhausted: starve_then_kill, grace_period: 10m }
      tools: [catalog/web-search, catalog/python-sandbox]     # MCP loadout
      skills: [seeds/market-analysis.skill.md]
      resources: [{ src: ./briefs/athena.md, dst: ~/brief.md }]
      memory: ephemeral                  # ephemeral | persistent (lineage)

    - name: loki
      role: Market analyst
      goals: [ ...same race, own submission folder... ]
      model: { provider: openai, model: gpt-5.2, api_key_ref: user/openai-main }
      budget: { max_usd: 25, on_exhausted: starve_then_kill, grace_period: 10m }
      tools: [catalog/web-search]        # asymmetric loadout — part of the game
      memory: ephemeral

  referee:
    enabled: true
    name: themis
    model: { provider: anthropic, model: claude-opus-4-8, api_key_ref: user/anthropic-main }
    budget: { max_usd: 10 }
    rubric: |
      Score submissions 0–100 (accuracy 40, sourcing 30, depth 30). First valid
      submission +10. Sabotage or prompt-injection against a rival: −25/incident.
    powers: { read_dms: true, inspect_private_fs: read_only, verdict_ends_realm: true }
```

A cooperative project is the same schema with shared goals and (typically) no referee — no mode switch anywhere (FR-12).

### 13.5 On-disk form: the portable project package

A project is authored and stored as a **self-contained folder** — exportable, zippable, versionable, restorable, shareable. The folder *is* the manifest, decomposed so it can co-locate binary assets (PDFs, images, scripts) a single file can't hold. It maps to the same internal `Project` model; the flat single-file manifest above is just the trivial case (no external assets). **JSON is canonical** (universal, toolable, JSON-Schema-validated); the loader also accepts YAML for hand-authoring (isomorphic).

```
my-project/                     # the portable package — zip/export/restore THIS
  project.json                  # apiVersion, name/description/author/license/tags, parameters,
                                #   goals, guidelines, restrictions, environment (shared_folder,
                                #   egress), channels/topology, mechanics,
                                #   termination, referee (by id), teams/ordering
  credentials.example.json      # which key HANDLES a runner must supply (never secrets)
  agentrealm.lock.json          # resolved pins: skill commits, model ids, runtime version
  resources/                    # PROJECT-level shared/seed resources (world data, briefs)
  skills/                       # PROJECT-level custom skills (shared / by role)
  agents/
    athena/
      agent.json                # id, name, description, role, model{provider,model,api_key_ref},
                                #   budget, tools (MCP loadout), skills (builtin|local|gh refs),
                                #   memory mode
      persona.md                # SOUL/persona text (or inline in agent.json)
      resources/                # private resources for THIS agent
      skills/                   # local custom skills for THIS agent
    loki/ …
  README.md
# NOT in the shareable package — kept elsewhere:
runs/<timestamp>/               # a realm's OUTPUTS: chronicle, transcript, artifacts, scores, snapshots
```

Design rules that make the package robust:
- **Secrets never live in the package (critical).** BYOK keys are referenced by handle (`api_key_ref`); the runner resolves them from its own keystore/Ledger at run time. `credentials.example.json` lists the required handles (like `.env.example`). A shared package is safe by construction.
- **Definition vs. run outputs are separate.** The folder is pure *inputs* (stateless, shareable). A **run = a realm** produces *outputs* archived under a sibling `runs/` — sharing a project never drags its history. (Realm = a run of a Project; §3.)
- **Roster source of truth = the `agents/` subfolders** (one folder per agent), not a duplicated list in `project.json` — avoids drift. `project.json` holds project-level config + optional teams/ordering and references the referee by id.
- **Two levels of resources & skills:** project-level (shared/seed, by role) *and* per-agent (private). Skills resolve from three sources: the built-in **role/capability library** (§12.5, #37), **local** (in-folder `skills/`), and **remote** (`gh://org/repo@ref`).
- **Remote skills are pinned + sandboxed.** A skill can carry scripts, so gh installs are commit-hash-pinned, checksum-verifiable, cached (offline-restorable), and run under the same container/egress boundary as agents.
- **Reproducible restore via a lockfile.** Sources declare intent (`gh://…@main`, a model alias); `agentrealm.lock.json` pins the resolved commit/version/runtime so a restore reproduces the same setup.
- **Parameters/templating.** `project.json` may declare `parameters` (e.g. `topic`, `rounds`) filled at instantiation, turning a package into a reusable template (scenario templates, #25).
- **Validate the package as a unit.** `arealm validate` checks: JSON-Schema conformance, unique ids, `agents/` folders match roster, skill refs resolve, required credential handles known, and no secret material committed.

---

## 14. Data model (Postgres)

| Table | Purpose / key fields |
|---|---|
| `users` | account, auth |
| `credentials` | `api_key_ref`, provider, encrypted key |
| `projects` | owner, manifest (jsonb), version |
| `realms` | project_id, state, started/ended_at, outcome |
| `agents` | realm_id, spec (jsonb), matrix_user, virtual_key_id, container_id, state, memory_volume |
| `channels` | realm_id, type (commons/team/dm/referee), matrix_room_id, members |
| `messages` | append-only: realm, channel, sender, content, attachments, ts |
| `events` | append-only chronicle: realm, kind (lifecycle/file/spend/score/violation/system), payload (jsonb), ts |
| `scores` | realm, agent, delta, reason, issued_by, ts |
| `spend` | realm, agent, tokens_in/out, usd, model, ts |
| `artifacts` | realm, agent, path, sha256, size, snapshot ref |

`messages` + `events` together *are* the replay.

---

## 15. Tech stack

| Concern | Choice | Why |
|---|---|---|
| Control plane | **Python 3.12 + FastAPI + Pydantic** | Hermes ecosystem is Python; manifest validation via Pydantic; SSE support |
| Database | **Postgres 16** | jsonb manifests, append-only chronicle, LISTEN/NOTIFY for live feeds |
| Containers | **Docker Engine API** (docker-py), single host | v1 scale; `RuntimeAdapter`/Forge isolate this for a later k8s adapter |
| Bus | **Conduit** (Matrix homeserver, Rust, lightweight), driven over the client-server API | Native Hermes gateway support (push wake), rooms/DMs/attachments, free spectating |
| Model gateway | **LiteLLM proxy** | Virtual keys, hard budget caps, BYOK custody, spend telemetry, 100+ providers incl. local |
| Agent runtime | **Hermes Agent** (pinned version) | See §12 |
| System tools | **MCP servers** (Python SDK): Realmtools, Arbiter | Per-agent loadouts are MCP configs — the standard |
| UI | **Vanilla JS**, no build step | A framework would need a CDN or a bundler; the console ships as three static files Gatekeeper serves directly. Replay viewer is *planned*. |
| CLI | **Typer** (`arealm`) | validate/up/status/tail/msg/stop/archive |
| Deploy | **docker compose** | Whole platform on one host |
| Quality | uv, ruff, pytest, mypy | |

---

## 16. Security & threat model

- **Inside a realm (permitted, by design):** agent-vs-agent deception, persuasion, shared-folder poisoning, prompt injection via messages — *if* the user's config leaves those surfaces open (principle 6). The referee + rubric are the mitigation the user opts into.
- **Realm boundary (hard guarantees, non-negotiable):** container escape, control-plane access, cross-realm reach, and reading another agent's private volume are physics-impossible: per-realm Docker networks, no host mounts beyond assigned volumes, resource limits, no Docker socket in any agent container.
- **Key protection:** raw user API keys exist only in Ledger/LiteLLM config. Agents can leak only their own virtual key — scoped to their model and their remaining budget.
- **Egress tiers:** `none` → `model_only` (default; proxy reachable, nothing else) → `allowlist` → `open`. Open egress means the user's agents act on the real internet under the user's responsibility (NFR-6, ToS).
- **Referee trust:** the referee is an LLM agent and can be manipulated by combatants (bribery, injection via messages it reads) — this is *known and acceptable* within a realm; rubric design and `powers` scoping are the user's levers. Chronicle makes any such manipulation auditable after the fact.

---

## 16.5 Telemetry

Structured, machine-readable spans for debugging and observability. Today the highest-value span is
the **LLM call** — it records exactly what an agent's model received (the full rendered system
prompt + tool schemas) and produced (the raw completion + any tool calls). This lets us answer
questions like *"did the referee actually get the `rule` tool, and did it try to call it?"* from
ground truth instead of inferring from the commons transcript.

## Design: OpenTelemetry-aligned, file-backed today

`agentrealm.telemetry` emits one JSON object per span to a file (JSONL). Field names follow the
OpenTelemetry **semantic conventions** — the [GenAI conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
(`gen_ai.*`) for model calls, plus `agentrealm.*` for what OTel doesn't standardize yet (the full
rendered system prompt, tool JSON schemas, and how the call was actually issued).

Because the call sites already speak OTel semantics, the **migration path** is a sink swap, not a
rewrite: replace the JSONL writer in `emit_span` with an OTel SDK tracer (`opentelemetry-sdk`) that
exports spans over OTLP to a collector (Jaeger / Tempo / Grafana). Callers (`emit_span`,
`llm_call_attributes`) don't change.

### Enabling capture

> **Status:** the span contract, the writer, and `arealm trace` all exist and are tested. What does
> not exist yet is a capture call site on the API pipelines: agent traffic goes straight through the
> LiteLLM proxy, so capturing raw I/O there means hooking LiteLLM's logging callbacks. Until that
> lands (#90), the only spans you will see are ones emitted by a provider integration that captures
> its own calls.

Off by default (zero overhead, no content written). Turn it on by pointing a sink env var at a
writable file — any of these work (checked in order):

```
AGENTREALM_TELEMETRY   # preferred
AGENTREALM_LLM_TRACE   # alias
```

Set it on whatever component sits at the LLM chokepoint, then launch a realm as usual. Each model
call — success **or** error — appends one span.


### Inspecting

```sh
# summarise the last calls: model, tokens, tools offered/called, prompt + output previews
uv run arealm trace ~/.agentrealm/llm-trace.jsonl

# isolate one agent's calls (grep the system prompt / completion) and check a specific tool
uv run arealm trace ~/.agentrealm/llm-trace.jsonl --grep "referee-social-deduction" --tool rule --full
```

`--tool rule` reports, per span and in a summary, whether `rule` was **offered** to the model and
whether the model **called** it — the direct check for the among-us "game never ends" investigation.

The raw file is plain JSONL, so `jq` works too:

```sh
jq -r 'select(.attributes["agentrealm.request.tool_names"] | index("rule")) | .attributes["agentrealm.response.tool_calls"]' ~/.agentrealm/llm-trace.jsonl
```

### Span shape

```jsonc
{
  "name": "gen_ai.chat",
  "start_unix_nano": 1752000000000000000,
  "duration_ms": 17342.1,
  "attributes": {
    "gen_ai.system": "anthropic",
    "gen_ai.request.model": "claude-sonnet-5",
    "gen_ai.request.reasoning_effort": "high",
    "gen_ai.usage.input_tokens": 4210,
    "gen_ai.usage.output_tokens": 180,
    "agentrealm.request.model_alias": "realm-7f2a--umpire",
    "agentrealm.request.system_prompt": "…the FULL rendered system prompt, incl. tool protocol…",
    "agentrealm.request.prompt": "…the user turn / transcript…",
    "agentrealm.request.tool_names": ["send_private", "rule", "scoreboard"],
    "agentrealm.request.tool_schemas": [ /* full JSON schemas */ ],
    "agentrealm.response.completion": "…the raw model output…",
    "agentrealm.response.tool_calls": ["rule"]
  }
}
```

### Session attributes (prompt-cache reuse)

A provider integration that reuses a per-agent session can also record how it did so, making the
cache hit-rate measurable rather than assumed:

| Attribute | Meaning |
|---|---|
| `agentrealm.session.id` | the CLI session this agent is using (stable across its calls) |
| `agentrealm.session.resumed` | `false` = created (full transcript sent), `true` = continued (delta only) |
| `agentrealm.session.turns_sent` | how many conversation turns crossed the wire this call |
| `agentrealm.session.chars_sent` | what we actually sent |
| `agentrealm.session.chars_full` | what a stateless call *would* have sent — the two together are the saving |

```sh
# session hit-rate + bytes saved for one realm
jq -r 'select(.attributes["agentrealm.realm.id"]=="<realm>") |
  [.attributes["agentrealm.agent.id"], .attributes["agentrealm.session.resumed"],
   .attributes["agentrealm.session.chars_sent"], .attributes["agentrealm.session.chars_full"]] | @tsv' \
  ~/.agentrealm/llm-trace.jsonl
```

---

## 17. Repository layout

```
agentrealm/
├── src/agentrealm/        # Python package (uv-managed, src layout)
│   ├── core/              # domain model, manifest schema, provider profiles, plugin seam
│   ├── gatekeeper/        # FastAPI app + the web console (static/), realm manager, Scribe API
│   ├── forge/             # provisioner (networks, volumes, containers) + adapters/hermes/
│   ├── herald/            # Matrix bus: rooms, ACLs, mirroring, injection
│   ├── warden/            # watchers, termination rules, turn manager, kill switch
│   ├── ledger/            # model-proxy admin, BYOK keystore, per-agent keys, spend
│   ├── chronicle/         # append-only event + message store, transcripts, final report
│   ├── realmtools/        # the MCP server agents reach — including arbiter.py, the referee's
│   │                      #   privileged tools. One server, two surfaces, separated by token:
│   │                      #   a participant's token cannot call a referee tool.
│   ├── scribe/            # the scenario-authoring assistant (control plane, not a realm agent)
│   ├── telemetry.py       # OTel-aligned span emission
│   └── cli/               # arealm (Typer)
├── tests/
├── examples/              # portable project packages
├── deploy/                # docker-compose.yaml, realmtools.Dockerfile, .env.example
├── scripts/
└── docs/                  # this file (the design), scenario-contract.md (authoring), adr/
```

> **On Arbiter.** §5 lists it as a module because it is one conceptually — the referee's privileged
> tools are a distinct surface with distinct rules. It is *implemented* inside the realmtools
> server rather than as a separate process, because a referee and a participant reach the same
> endpoint and are told apart by their signed token. There was an empty `arbiter/` package holding
> a docstring and nothing else; it has been removed rather than left to imply a module that was
> never there.

---

## 18. Direction

Where this is going, in one paragraph. Issue tracker is the live state; this is the shape.

**Built:** the full realm lifecycle — a manifest becomes provisioned agents on an isolated network,
talking over the bus, metered against per-agent budgets, running deterministic mechanics through
Realmtools, adjudicated by a referee, and concluded by the Warden into a chronicle and a final
report. Plus the web console, the scenario-authoring assistant, and a provider layer that lets one
scenario run on any pipeline.

**Next:** richer refereeing (multi-judge panels, appeals), a replay viewer, agent lineage —
persistent memory and skills carried between realms, and exportable as reusable seeds — a second
`RuntimeAdapter` to prove the core is genuinely runtime-agnostic, A2A envelopes at the bus for
standards-based interop, and a scenario template library.

**Further out:** multi-user and hosted operation, which is a different product with different
security properties and is deliberately not designed here yet.

---

## 19. Open questions

1. Hermes boot-config surface (Spike 1) — largest technical risk; determines fork strategy.
2. Hermes license + upstream churn (188k stars, fast-moving) — pin + thin fork policy.
3. Matrix throughput with very chatty agents — Conduit tuning; custom bus fallback sits behind the `Bus` interface.
4. Idle token burn of always-on agents (Spike 4) — may need an idle-throttle knob in the manifest.
5. Ephemeral-mode guarantees: verifying a "fresh" Hermes memory volume is truly clean.
6. Referee-tool ergonomics: does an LLM referee reliably use `chronicle.search` over large histories, or does it need digests pushed to it periodically?
7. Multi-referee / appeal mechanisms (panel of judges — see judge-panel patterns in the survey) — later.
