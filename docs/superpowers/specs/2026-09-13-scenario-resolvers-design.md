# Scenario resolvers — deterministic law the scenario ships and the platform never reads

**Status:** reviewed and decided (owner, 2026-09-13). Not built.
**Decides:** the question architecture §9.5 leaves open — how a scenario-specific deterministic
check is packaged as something an agent invokes. Requires **ADR-006**.
**Evidence:** a read of the whole chronicle (297 realms) and of the code, then five independent
adversarial reviews (security, architecture, product, feasibility, future flexibility). Appendix A
records what each changed and where they disagreed.

## 1. The problem, in numbers

Rules are *physics* (the platform makes them impossible) or *law* (a referee agent must catch and
penalise). The chronicle says how law goes:

| finding | number |
|---|---|
| violations issued by any referee, in any live realm, ever | **0** — 297 realms, 11 referee identities |
| contested poker showdowns settled with **no** call to the deterministic resolver | **4 of 16 (25%)** |
| of those, settled wrongly | **0 of 3** hand-checked |
| players naming actual cards, which the scenario forbids | **33+ messages, every real poker realm** |
| of those, caught | **0** — the referee is not in the commons by design |
| illegal raises attempted | **0 of 125** (short calls are *unmeasurable*: `call` carries no amount) |
| chip totals at hand boundaries | **96 of 96 exact** |
| a referee folding the last live seat, or settling a hand twice | happened; the second was refused by physics, the first was not |
| referee share of poker spend | **43%** |

What this says, honestly:

- **Where physics exists it works.** The money balances; a second `settle` was refused.
- **Where the rule is law, it has never been enforced.** Not once, anywhere.
- **The referee's answers were right; its procedure was not.** A quarter of showdowns skipped the
  resolver and still paid the right seat. That is a *guarantee* problem, not a *correctness* one —
  and it is also, per Principle 10, a finding about LLM referees worth being able to measure.
- **The gap is not the players.** Nobody has attempted an illegal bet.

Today's resolver (`agents/pitboss/resources/poker_resolver.py`) is, to the platform, inert text:
never read, hashed or verified; seeded into a volume the referee can overwrite (same uid, `rw`);
passed through `${param}` substitution like a persona paragraph; executed only when the LLM composes
a `run_code` snippet that imports it.

## 2. What already decides most of this

**ADR-002 pre-approves the idea, with two conditions:**

> A future **plugin** contract (custom, agent-invoked mechanics/scorers a scenario ships) is the
> sanctioned way to deliver bespoke deterministic logic — **provided the *agent* invokes it and the
> control plane hosts no scenario-specific rules.**

**Principle 9 draws the line inside a mechanic:** the mechanic is a generic tool the agent invokes;
"physics" means the tool's guarantee is real, *not* that the platform runs the scenario on the agent's
behalf.

**The machine spec fixed the engine's shape:** no arithmetic and no clock; compute happens in a tool
call whose result a role writes in with an ordinary effect.

**Two things already in the tree that this design must reckon with, not build beside:**

- `realmtools/tally.py` has `register_ruleset()` and `core/schema.py` accepts any
  `ruleset: "custom:<name>"` at parse time — a scenario-scoped *code* seam for scoring, in the
  mechanic **11 examples** use. It is unsandboxed, unhashed, unchronicled, and nothing in the tree
  or any plugin calls it: a manifest can declare `custom:x` today and fail at tally time. This
  design **absorbs** it.
- `forge/skills.py` ships an entire hidden-role game's vocabulary in `src/`, deliberately, under a
  naming contract (*"a family skill MUST be named after its family"*). So the repo's actual rule is
  not "no scenario logic in core" but "scenario-family logic in core only where the name declares
  it." Resolvers sit under the **strict** rule — nothing about any scenario in `src/` — because a
  resolver is executable and a skill is prose, and the guard in §4 is built for the strict rule.

The word: **resolver.** Architecture §9.5, the machine spec §1 and §6 already say "a shipped
resolver the referee invokes." This ADR formalises a thing the repo already does. ("Oracle" was the
draft's word; it reads as an actor beside Arbiter and Warden, and in the industry it names the
pattern for bringing *external* data in — the opposite of a function defined by having none.)

## 3. Design

### 3.1 One seam, three consumers

A **resolver** is a pure function the scenario ships: JSON in, JSON out, no side effects, no network,
no clock, no memory between calls. The platform knows its name, who may invoke it, what it may see,
its schemas, its timeout, and the hash of its code. It never knows what the function is about.

Three things consume the same seam, listed by how many shipped scenarios can use each **today**:

| consumer | shape | reach today |
|---|---|---|
| **tally ruleset** `ruleset: "custom:<name>"` | replaces `register_ruleset`; `{submissions, config}` → `TallyResult` | the sealed-submit family, **11 of 23** |
| **direct-call tool** `resolve_<name>` | listed to declared roles with a typed input schema; the caller rules on the answer | any referee or role, **23 of 23** |
| **transition guard** `{"resolver": "<name>"}` | a predicate in the machine's guard list; refuses the act | machine scenarios with agent-fired transitions — **1 of 23** (`poker-table`; the machine is three days old) |

Ordering the phases by reach is what keeps this from being a poker feature (§9).

### 3.2 Packaging: code in the package, policy in the manifest

```
<package>/
  resolvers/
    _shared/            # optional; on the import path of every resolver in this package
      cards.py
    showdown/
      resolver.py       # entry: one JSON object on stdin → one JSON object on stdout, exit 0
      ranking.py        # siblings importable; a resolver is a directory, not a file
    bet_legal/
      resolver.py
```

Declared in `project.json`, beside `spec.tools` and `spec.parameters` — *the scenario sets the
policy* (ADR-004 §1), and a reviewer reads one file to see everything a package asks for:

```jsonc
"spec": {
  "resolvers": {
    "showdown": {
      "description": "Verifies the dealer's settlement: recomputes the award and refuses a mismatch.",
      "invoke": [],                        // roles that may call resolve_showdown directly; [] = guard/tally only
      "sees": "public",                    // what the envelope carries: public | caller | referee  (§3.3)
      "input":  { /* JSON Schema for a direct call's arguments */ },
      "output": { /* JSON Schema; must admit {"ok": boolean, "reason": string} for guard use */ },
      "timeout_s": 2,                      // guards ≤ 2; direct calls and tally ≤ 30
      "config": { "buy_in": 2000 }         // DATA, parameter-substitutable — the tally precedent
    }
  }
}
```

Loader rules, all generic:
- name `^[a-z][a-z0-9_]{1,30}$`; the tool it becomes is `resolve_<name>`, validated by the same
  `family_verb` regex as every other tool and refused if it collides with a builtin or a grant.
- a resolver directory: `.py` files only, stdlib only, ≤ 256 KB total; `_shared/` likewise.
- **code is never parameter-substituted**; `config` is. (`resources/*.py` being substituted today is
  a separate defect — filed, out of scope.)
- the directory tree is hashed (`sha256` over relative paths + contents, `_shared/` included). The
  hash is what the chronicle records, what the console shows, and what replay verifies against.
- a machine transition or a tally may only name a declared resolver — the launch validator refuses
  otherwise, exactly as it refuses an undeclared data key.

### 3.3 The envelope, and `sees`

Every invocation receives one JSON object:

```jsonc
{
  "resolver": "showdown", "via": "guard:settle" | "call" | "tally",
  "caller": "pitboss", "roles": {"dealer": ["pitboss"], "player": ["vega", ...]},   // public role map
  "now_ms": 1757000000000,                 // the platform's timestamp; the engine still has no clock
  "args": { ... },                         // guard: the act's args · call: the validated tool input · tally: submissions
  "state": { ... },                        // the machine view at the declared sight (absent for tally)
  "config": { ... }
}
```

`sees` is the resolver's **declared sight**, and it is what makes multi-party laws expressible
without blinding the input:

| `sees` | the envelope's `state` is | allowed when |
|---|---|---|
| `public` | `view()` as an anonymous outsider — public keys, revealed entries | always |
| `caller` | `view()` as the invoker — plus their own owner entries | always |
| `referee` | `view()` as the referee — everything | **guard:** only on a transition whose `log` is `referee`, the same rule that already refuses a hidden-reading guard on a public-log transition. **Direct call:** the envelope is clamped to the caller's own lens, so a participant can never obtain referee sight by calling a resolver |

The output leak is closed at the *output*: a rejection's `reason` is capped (512 chars) and is
chronicled at the transition's `log` visibility; a `RESOLVER` event's own visibility is **derived
from `sees`** — `referee` sight is chronicled referee-only, always. The author never chooses a row's
visibility. (The draft let them, and a public row on a dealer-invoked resolver would have published
every folded hole card.)

A trade between A and B that must check B's balance is now writable: `sees: referee` on a
`log: referee` transition, invoked by A. The refusal tells A the trade failed — which is what
enforcing that law inherently reveals, and which the author accepted by declaring it.

### 3.4 Sealed execution

One container per realm that declares any resolver, `realm-<id>--resolver` — a component name the
existing teardown and `reap_orphans` prefix lists already honour, so it is torn down with the realm
and never reaped while the realm lives. (The draft's `realm-<id>-oracle` matched neither list: the
reaper would have deleted it mid-run and teardown would have leaked it after.)

| property | value | closes |
|---|---|---|
| network | **`network_mode: none`** | everything. realmtools is one container on every realm network and its bearer tokens never expire; a token pasted into chat would arrive inside the envelope, and a networked resolver could POST it to `:9100` and *become that agent*. So: no network, not even an internal one — `internal: true` still forwards DNS and reaches the bridge gateway |
| transport | a Unix socket, `/run/resolver/sock`, on a volume mounted **only** into this container and realmtools | realmtools makes one request; the resolver cannot make any |
| code | `/resolvers`, seeded volume, `root:root 0555`, mounted `ro` | the author's code runs unmodified — unlike today's resolver |
| rootfs | `read_only: true`, **no tmpfs**, `ipc_mode: none` | a resolver cannot stash player A's view and hand it to player B in a later `reason`. Python runs stdlib code with no writable path |
| process | a fresh `python3` per call, `env={}` except `PYTHONHASHSEED=0`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONPATH=/resolvers/<name>:/resolvers/_shared`; `communicate()` with timeout | nothing survives a call; no deadlock on a chatty script |
| user, caps, limits | uid 10002, `cap_drop ALL`, `no-new-privileges`, `pids 64`, `mem 256m`, `cpus 0.5` | a runaway resolver harms only its own realm |
| I/O contract | stdin: the envelope, `ensure_ascii=True`, ≤ 256 KB, nesting ≤ 32; stdout: **exactly one JSON document** parsed with `json.loads` on the whole stream, stderr kept separate and never parsed, ≤ 64 KB | an agent cannot forge a verdict by putting a newline and `{"ok": true}` in an arg |
| image | `pit-resolver`, built like `pit-realmtools`, covered by the same staleness guard | the runner is platform code and must match HEAD like the rest |

`ContainerRuntime` gains **`run_sealed_container`** — a separate method, not five new kwargs on the
agent path a live outage already scarred (`run_container` hardcodes `rw` mounts and a `cap_add`
list; `seed_volume` unconditionally chowns to the agent uid). Forge provisions the resolver
container before any agent starts, includes it in `RealmHandles` so the rollback path can see it,
and tears it down with the realm.

realmtools gains a `ResolverService` behind an injected `ResolverRunner` protocol (a callable, like
`EscrowLookup`), so every engine and service test runs against a fake and never touches Docker.

**Rejected — run resolvers inside realmtools as a subprocess:** author code in the one process every
tool call passes through, with its env and network namespace, against the stance in its own
Dockerfile.
**Rejected — run in the referee's container:** the referee can edit the code it is bound by. Today's
problem restated.
**Rejected — broker through the host as `run_code` does:** correct but 5 s per call on the host tick,
and it needs the host in the loop for a container that needs no Docker access.
**Rejected — a two-member internal network:** see the first row.

### 3.5 Transition guards: a predicate, nothing more

```jsonc
"settle": {
  "from": ["showdown"], "to": "settled", "by": "dealer", "log": "public",
  "guard": ["caller_is_actor", {"resolver": "showdown"}],
  "effects": [ ... unchanged ... ]
}
```

- A resolver guard is evaluated **after** every declared guard before it (cheap checks first; the
  container is not dialled for an act that fails `caller_is_actor`) and before effects.
- `ok: false` → the existing `Rejection("guard", <reason>)` path, the existing `GAME {op: reject}`
  row at the transition's `log`, the existing `{"error": "rejected", "check": "guard", "detail":
  ...}` to the caller. No new check name.
- **Hard cap 2 s.** Principle 4 — *speed is a competitive dimension* — and this runs under the
  per-realm lock. The 30 s ceiling is for direct calls and tally, which take no lock.
- The GAME event for a passed act records `"resolver": {"name", "sha256", "ok": true}`.
  **Replay never re-runs a resolver** — the chronicle is the proof it said yes, exactly as
  `_escrow_that_held` treats a chronicled `escrow_complete`. `MACHINE_VERSION` stays 1: the field is
  additive, and both version checks blank a realm on mismatch.
- `eng.act` stays pure. `MachineService.act` becomes `precheck` (pure) → await the runner if the
  transition names one → `eng.act(...)`. A ~40-line refactor mirroring how `EscrowLookup` is
  injected and awaited under the same lock, with the same "must not hang" rule.

**Not in this design: a resolver whose output becomes game state** (`$resolver.<field>` in
effects). That is a *computation* the engine would evaluate over agent-written values and write into
state — a real amendment to the machine spec's "two lines," a write channel that needs consent, and
the half that carried the draft's leak past the static copy check. If it is ever wanted it gets its
own explicitly named declaration (`"resolver": {"name", "provides": [...]}`), its own consent line,
and its own sentence in the ADR. The verifying guard below makes it unnecessary for the case in
hand.

### 3.6 Direct calls

`resolve_<name>` is listed to the roles in `invoke`, from the realm's chronicled manifest, through
the per-caller tool listing that already serves granted tools. Its description and input schema are
the manifest's, verbatim; the platform validates the arguments against the schema and the answer
against the output schema and interprets neither. The caller decides what to do with the answer —
usually a referee, usually with `penalize` or `rule`.

### 3.7 Tally

`ruleset: "custom:<name>"` resolves to `resolvers/<name>/`, invoked with `via: "tally"`, `args` =
the revealed submissions, and the mechanic's `config`. The output must validate as a `TallyResult`.
`register_ruleset` and the runtime dispatch-dict mutation are **removed** — one seam, and the
audited one is the only one.

### 3.8 Chronicle, console, audit, replay

`RESOLVER` event: `{resolver, sha256, via, caller, sees, input, output, ok, exit, ms}`, visibility
derived from `sees` (§3.3). No DB migration: `Event.kind` is a string column.

The timeline renders `RESOLVER` rows and resolver-refused acts through the same lens discipline as
GAME rows, labelled `resolver:<name>@<sha8>`. The launch dialog lists every declared resolver with
its sight and hash — information, not consent (§3.11).

Audit findings, both **mechanical** (the module's docstring forbids norms, and the draft's
`ruling_without_oracle` was one): `resolver_declared_never_invoked` and `resolver_failed`.

`pit audit --replay-resolvers <realm>` re-runs every recorded envelope **against the package on
disk** — the code at the chronicled hash, refusing if the package no longer matches — inside the
sealed image, never in the host process. A nondeterministic resolver, or a chronicle edited after the
fact, is a finding. (The draft verified chronicle code against a chronicle hash, which is circular.)

### 3.9 Failure is closed — and terminal, not silent

"The resolver said no" and "the resolver could not answer" are different events. The first is a
rejection. The second — container unreachable, timeout, crash, malformed output, schema failure — is
a `RESOLVER` event with `exit != 0`, the act is refused, and **after N consecutive failures
(default 3) the Warden concludes the realm with `cause: mechanic_failure`**.

The draft said a dead resolver container would "stall" the realm. It would not: `stall` measures
idle time since the last agent *message*, and a referee retrying a refused `settle`, re-woken by the
machine's own timer, never goes idle. It would burn the whole five-hour budget and produce no
verdict. A mechanic the realm depends on being gone is a reason to stop, and now it is one.

### 3.10 Concurrency

A guard resolver costs ~100 ms under the realm lock (process spawn + script), bounded at 2 s. Reads
(`game_state`) take no lock. The tripwire for revisiting this is **act rate, not resolver latency**:
a realm firing more than a few resolver-guarded acts per second — a continuous market, not a card
game — would queue behind its own guards, and Principle 4 says that queue is not acceptable. The
optimistic path (validate outside the lock against a state version; apply under it if unchanged) is
the known answer, deferred until a scenario has that act rate.

### 3.11 Consent

Guards and direct calls need no new consent line: a guard only *restricts*, a direct call only
*answers* with data the caller's lens already allows, and neither runs with any authority. A
warning shown on every scenario stops being a warning (ADR-004 §7). A resolver with `sees: referee`
is visible in the launch dialog's resolver list, which is where a reviewer looks.

## 4. Keeping the core ignorant — the guard set

The draft proposed grepping `src/` for every resolver name and schema property. That fails on day
one (`reason` appears 70 times in `src/`; `board`, `raise`, `fold` are ordinary words) and, reduced
to names, is defeated by a rename. `test_public_surface.py` already teaches the lesson: a guard that
cries wolf gets disabled. Instead:

1. **A synthetic generality test (build).** The test *generates* a scenario whose resolver name,
   schema properties and description are random UUIDs; loads it, validates it, chronicles its
   manifest, invokes it as a direct call, as a guard, and as a tally ruleset, against a fake runner.
   If the platform can run a resolver whose name it has never seen, the vocabulary is closed. This
   proves the property positively and catches the concept-without-the-word case a grep cannot.
2. **No shipped package name in executable `src/` (build).** Ban `poker-table`, `rps-machine`, …
   outside comments and prompt markdown. Verified to pass today (every hit is a comment or scribe
   text), so it is a ratchet from a clean baseline — the only kind of guard that survives.
3. **Extend `tests/test_examples.py` (cheap).** Every resolver in every example loads, hashes and
   round-trips its schemas through the one loader, with no scenario-conditional branch.
4. **A line in `docs/scenario-contract.md` (write down).** *A platform constant justified by one
   scenario's timing must name a second scenario or carry an issue.* It would have caught
   `AFTER_S_FLOOR = 240` (justified in its own comment by one scenario's resolver latency) and it
   will catch `timeout_s`.

Not built: a grep over `metadata.tags` — the tags are `turns`, `tools`, `game`; two are module names.

## 5. Future flexibility — what the contract serves, and what it deliberately does not

Eight invented archetypes were stressed against the contract. With the `sees` declaration, the
envelope's `now_ms` and role map, and multi-file resolvers, the contract serves — with **no `src/`
change** — a hidden-role accusation check, commit–reveal dice verification, bilateral contract
enforcement, and a continuous order book *up to* the lock's act rate (§3.10). Every change the
remaining archetypes would need is **generic** (a chronicle slice, an out-of-lock execution model,
a larger sandbox), never scenario-specific; that is the test, and the design passes it.

Deliberately outside this contract, named so nobody builds them by accident:

| need | why not a resolver | today's answer |
|---|---|---|
| judging a produced artifact (a file tree, a program that must pass tests) — half of `examples/` is artifact-shaped | not a pure function of an envelope; needs the shared folder, minutes, a writable sandbox | ADR-005 capture + a referee reading the artifact: law |
| a check that needs an LLM | not deterministic | the referee |
| a check on a timer, or that mutates state | the engine has no clock and a resolver has no effects | `after_s` wake rules; effects |
| reading chat or files on the referee's behalf when the referee was denied them | launders a Herald-enforced boundary through author code | `referee_reads_commons: true` — one boolean |
| cross-realm state (a tournament bracket) | outside the realm | not yet designed |

Decisions the draft fixed that this version changes: resolvers are directories (one resolver split
into two must not duplicate its ranking code); `sees` replaces "the invoker's view only"; the lock
cost is stated with its real tripwire; `reads` is dropped; `$resolver.*` is deferred with a named
shape.

## 6. The tensions, resolved

1. **Deterministic law vs the user's choice of physics or law.** Nothing changes a rule's category on
   the platform's initiative. The author chooses per rule, and the contract gains the rule of thumb:
   *prefer the closed vocabulary — if a JSON guard expresses the check, ship no code; law first — a
   rule whose breaking is interesting stays law; make a rule physics only where its breaking would
   corrupt the game's bookkeeping or where the referee cannot observe the evidence.* Applied to poker:
   fold-the-last-seat is a JSON guard (`members_count … at_least: 2`); minimum raise stays law
   (0 incidents); settlement becomes a verifying guard (25% skipped).
2. **Agent-invoked vs trustworthy.** The agent invokes; the platform guarantees the code ran
   unmodified, sealed, on the declared sight, and recorded it. An agent can pass args — never a fake
   world — *except* where the state key a resolver reads was written by that same agent's `game_set`
   (poker's `stacks` is). Say so per scenario; the verifying guard is the answer for that case.
3. **ADR-004 vs scenario-scoped tools.** Resolvers are the scenario-scoped tool ADR-004 lacked, and
   the reason a folder may ship one with no install and no review is that it can do nothing but
   compute. Anything effectful stays an installed, reviewed ADR-004 tool. Two tiers, one line.
4. **Author code called "physics."** The platform guarantees the plumbing; the scenario guarantees
   the logic. The console labels every resolver decision by name and hash; the replay makes the
   author's half checkable after the fact.
5. **Never-invoked vs mis-invoked.** Where the evidence says it matters, do not rely on invocation:
   the guard runs on the transition. Everywhere else, record and audit — mechanically.

## 7. The first consumers

**Poker.**
- `settle` gains `{"resolver": "showdown"}` — a **verifying** guard. The dealer still runs the
  procedure and publishes `stacks` as today; the guard recomputes the award from the public view
  (revealed hands, board, pot, `out`, `all_in`) and refuses a settle whose numbers disagree, with
  `reason: "expected vega +240"`. The referee keeps doing the work (the measurement survives), a
  wrong settlement becomes impossible, and the resolver is *rewritten*, not moved — `award()` returns
  pots and rankings, not a stacks map, and contributions are not machine state (a `stacks_at_deal`
  key, set at `deal`, makes them derivable).
- `resolve_bet_legal`, `invoke: ["dealer"]` — deterministic evidence for a suspected short call.
  Law, on purpose.
- `fold_for` gains `{"members_count": {"over": "player", "minus": ["out"], "at_least": 2}}`. **No
  code.**
- Card naming: `referee_reads_commons: true`, and the persona's "table talk is never evidence"
  line goes — that boolean was the design, and it was set the other way. Law.

**Pre-registered prediction, written before the next run:** the count of `settle` rejections across
~17 showdowns. If 0, the referee's settlements were right all along and phase 1 bought a guarantee;
§1 says as much. If > 0, one row justifies the feature. Secondary: referee spend share, 43%; the
prediction is that it moves by under five points, because the cost is wakes × prompt, not showdown
tool calls.

**The sealed-submit family.** One example (`sealed-auction`, or `jury-unanimous`) moves its scoring
to `ruleset: "custom:<name>"` — the seam's first real consumer, in the mechanic most scenarios use.

**Not `rps-machine`.** Its transitions are all `by: ref`; players never fire one. The draft named
it as a generality proof and it cannot be one.

## 8. Non-goals

Effects, network, packages, or state in a resolver (ADR-004); the platform validating a resolver's
*logic*; replacing `run_code` or `resources/`; `$resolver.*` in effects; chronicle reads; remote
resolvers; languages other than Python; any change to who may fire a transition.

## 9. Phasing — by reach and by evidence

| phase | delivers | proves |
|---|---|---|
| **0 — no resolver code** | #121 (args sanitisation — live today); `fold_for` as a JSON guard; `referee_reads_commons: true` for poker | two of poker's five laws fixed with data, not code |
| **1 — the seam** | packaging + loader + hash; `pit-resolver` image, `run_sealed_container`, Forge lifecycle incl. rollback/teardown/reaper tests; `ResolverService` + `ResolverRunner` protocol; `resolve_<name>` per role; **tally `custom:`** (and `register_ruleset` removed); `RESOLVER` events + timeline rows; the §4 guard set; ADR-006 + contract rule; one sealed-submit scenario on a custom ruleset | a resolver runs for a scenario with no machine at all — the seam is not a poker feature |
| **2 — guards** | `{"resolver"}` in guards, 2 s cap, `precheck`/`act` split, GAME `resolver` record, replay proof, `mechanic_failure`; poker `showdown` verifying guard + `resolve_bet_legal`; the pre-registered run | the prediction in §7, and a full game where no wrong settle can land |
| **3 — replay** | `pit audit --replay-resolvers`; the two mechanical findings; measured lock cost | a decision re-derived from the chronicle alone; the optimistic-lock decision made on data |

Size, from the feasibility review: **L — ~25 files, 15–20 focused engineer-days**, the multiplier
being this tree's documentation bar rather than the logic. Riskiest: Forge lifecycle, the one part
the fake-based test convention cannot prove — so it gets an opt-in Docker-marked integration test
(`pyproject.toml` declares only `live` today; a `docker` marker is part of phase 1).

## 10. Done when

- The synthetic-UUID resolver runs as a call, a guard and a tally ruleset in the test suite.
- A sealed-submit scenario tallies on a custom ruleset with **no diff under `src/`** beyond phase 1.
- The Docker-marked test proves the sealed container has no network interface but loopback, an
  empty environment, and no writable path; and that teardown removes it and the reaper never does.
- A poker realm runs to a verdict in which every `settle` carries a resolver record, `sum(stacks)+pot`
  balances at every boundary, and the pre-registered count is written next to the prediction.
- A seat's lens shows a resolver refusal's `reason` and nothing another seat could not see.
- Replay of an archived machine realm (before this change) still renders — `MACHINE_VERSION` unchanged.

## 11. Decisions — made

1. **Guards wait for a second machine scenario.** Phase 1 ships first. Then one more machine
   scenario with agent-fired transitions is authored — a trade or contract game, which §5 found the
   contract can express — and the guard is designed against both. The poker settlement guarantee
   waits that long; in the meantime the dealer has `resolve_showdown` and every call is chronicled.
   *Why:* a generic API shaped by a single consumer is the overfitting pattern this design exists to
   avoid, and phase 1 already delivers the evidence benefit to 23 scenarios.
2. **Verify, never compute.** A resolver guard refuses a wrong result; it never writes one. The
   agent still does the work, the procedural-compliance measurement survives, there is no write
   channel, no consent line, and no amendment to the machine spec. `$resolver.*` remains a named,
   deferred shape (§3.5) and is not on any roadmap.
3. **Provision only when declared; check the image every launch.** The sealed container follows the
   shared-folder convention — present only for scenarios that declare a resolver — so half of all
   realms carry no inert component. The `pit-resolver` staleness check runs on every launch
   regardless, so the image cannot drift unnoticed the way `pit-realmtools` did this morning. Both
   lifecycle branches are covered by the Docker-marked test.

Phase 2 in §9 is therefore gated on the second scenario existing, and its first task is authoring it.

---

## Appendix A — review provenance

Five reviews, five verdicts, each "approve with changes"; the feasibility review "feasible with
changes." What each changed, and where they disagreed:

| review | insisted on | adopted? |
|---|---|---|
| security | no network at all; Unix socket; derived visibility; no tmpfs; strict single-JSON stdout; teardown-safe naming; replay against the package not the chronicle | all |
| architecture | rename to *resolver*; declaration in `project.json`; **split predicate from computation**; guard-list shape; 2 s cap; absorb `register_ruleset`; the named generality proofs did not exist | all — and it disagreed with product on whether `settle` belongs in the first build (see below) |
| product | §3.8's stall story was wrong → `mechanic_failure`; `fold_for` needs no code; the settle migration is a rewrite; 4 of 16 not 5 of 17; short calls unmeasurable; drop `reads`; pre-register the prediction | all |
| feasibility | replay must never re-run a resolver — the GAME event carries the record; `run_sealed_container` as its own method; three Forge lifecycle holes; do not bump `MACHINE_VERSION`; no migration needed | all |
| future flexibility | invoker-only sight was the largest foreclosure → `sees`; the lock's real tripwire is act rate; a positive synthetic test instead of a name grep; `ruling_without_oracle` was normative; single-file broke the migration; `AFTER_S_FLOOR` and `inspect_private_fs` are existing debt | all; `inspect_private_fs` stays a dead field to retire separately |

**The one disagreement, and how it was resolved.** Architecture said cut the computing half from
the first build because it hides the 25% measurement Principle 10 calls a finding; product said make
`settle` deterministic first because it is the only evidence-backed gap. Both are satisfied by a
guard that *verifies* rather than computes: the referee still performs the procedure (measurement
kept), a wrong settlement cannot land (guarantee gained), and the rejection count is the number
product asked to pre-register. It also needs no `$resolver.*`, which is what carried the leak.

**Found along the way and filed separately:** #121 — `game_act` args are uncapped and unsanitised;
a NUL or lone surrogate in an argument is refused by Postgres `jsonb` inside the realm lock.
