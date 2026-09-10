# Generic game state machine — design

**Status:** approved for implementation · 2026-09-10
**Reviewed:** three adversarial passes by an independent reviewer against ADR-002, architecture
§2/§6, the scenario contract, and the code (escrow, tally, turns, termination, schema, runner,
herald). Final verdict: approve, minors incorporated below.
**First game:** Texas Hold'em, 6-max cash game, mixed-capability agents (the demo).

## 1. Purpose and constraints

A **declarative game state machine** shipped as a platform mechanic — the sibling of the
sealed-submit escrow and the tally rulesets. A scenario declares states, transitions, roles,
guards, effects and a data document; the engine holds the state, enforces legality, filters
visibility, and chronicles everything. It knows nothing about any game.

Four decisions fixed by the owner:

1. **Topology only; compute delegated.** The engine has **no arithmetic and no clock**. Anything
   that must be computed — hand ranks, pot, side pots — is a separate tool call (`run_code`, a
   shipped resolver, `tally`) whose *result* the referee writes into the machine.
2. **Participants fire their own transitions; the referee fires structural ones.** Legality is
   checked by the tool — physics — never relayed through an LLM.
3. **Declared visibility per data key** (`public` / `owner` / `referee`); each caller sees exactly
   its view.
4. **Platform-held, in realmtools**, declared in `project.json` as a mechanic.

Governing: **ADR-002** — the platform never advances a game; an *agent* invokes every transition.
A wake notice is notification, never advancement. The two lines never to cross: arithmetic over
agent-written values, and a timer inside the engine.

## 2. The declaration

`mechanics: [{kind: "state-machine", machine: {...}}]`. Exactly one per realm; two is refused.

### Roles
`name: {members: referee | participants | [agent ids], visibility: public | hidden}`. Explicit ids
(validated against the roster) bind a hidden faction. Membership is resolved on the host at launch
and persisted. Transitions are permitted by role, never by agent id. A `hidden` role's membership
is never returned to a participant. The referee's view of an `owner` key is every entry.

### States
Names; one `initial`; optional `terminal` list.

### Transitions
`name`, `from` (state | list | `any`), `to` (state | `same`), `by` (role), `guard` (list; all must
hold), `effects` (list; in order), `log: public | referee` (default public). Args are free-form
JSON, recorded verbatim, never interpreted by the engine.

### Data
Keys with `visibility` and optional `type: set` (a set of agent ids). Reserved public keys:
`actor` (the pointer, or `null` when parked) and `actor_since` (ms timestamp of the last pointer
move — informational; on replay it comes from the event's `ts_ms`).

### Pointer
`actor: {over: <role>, skip: [<set-key>...]}`. Rotates over the role's members in persisted roster
order, skipping any member in any listed set. If no member is eligible, `advance_actor` **parks**
the pointer at `null`: `caller_is_actor` is false for everyone and no actor-wake fires — the
defined state for "everyone has acted" and "everyone is all-in". Moves only on `advance_actor` or
`set_actor`; never on a clock.

### The closed vocabulary

Values in guards and effects may be a literal, `$caller`, `$args.<n>`, or `$data.<key>`, except
where noted.

**Guards**

| guard | meaning |
|---|---|
| `caller_is_actor` | caller == pointer (false when parked) |
| `caller_in: <set>` / `caller_not_in: <set>` | membership |
| `data_equals: {key, value}` | equality |
| `data_present: <key>` | key has a value (`unset` removes it) |
| `data_set_empty: <set>` | |
| `members_count: {over: <role>, minus: [<set>...], equals \| at_most \| at_least: N}` | cardinality of (role − minus). **N is a literal** — never `$data`/`$args`; comparing a count to an agent-written number would be reading a game value. `over`/`minus` name roles and set keys only |
| `data_set_full: {key, over: <role>, minus: [<set>...]}` | sugar for `members_count … minus: [..., key], equals: 0` — set ⊇ (role − minus) |
| `escrow_complete: {round, over: <role>, minus: [<set>...]}` | escrow's submitted set ⊇ (role − minus), via `reveal_status`/`status_async` — never the escrow's own roster, which only grows |

**Effects**

| effect | who may declare it |
|---|---|
| `advance_actor` | any role |
| `add_to: {key, value: $caller}` | any role |
| `reveal: {key, owners: $caller}` | any role (showing your own hand is a legitimate act) |
| `add_to` with any other value, `remove_from: {key, value}`, `reset: <set>`, `set: {key, value}`, `unset: <key>`, `set_actor: <value> \| null`, `reveal: {key, owners: <value> \| {over: <role>, minus: [<set>...]}}` | referee roles, or a participant role listed in `participant_effects` |

`participant_effects: [reset, set, ...]` is a machine-level opt-in, surfaced at launch the way
elevated tool grants are (ADR-004 §7): it is the user choosing law over physics for that game
(architecture principle 3 — a refereeless self-dealt table is a legitimate experiment).

`reveal` flips the named `owner` entries to public from then on; the event's public view carries
the revealed values. `set_actor` **rejects a member of any `skip` set** (a referee typo must not
make a folded player the actor) and accepts `null` explicitly. `unset` on a set key is refused at
launch (`reset` is the set form). `$args` values used in `add_to`/`remove_from`/`set_actor` are
validated at runtime to be members of the relevant role.

### Wake rules

`wake:` is a list of rules. Two kinds:

- `{role: <role> | actor, when: [guards], unless: [guards]}` — **engine-evaluated at act time,
  edge-triggered**: effects are applied to a scratch copy, each rule is evaluated on old and new
  state, it fires on false→true only (and only while `unless` is false), targets are deduplicated
  per event, and the result is stamped on the GAME event as `wake: [agent ids]`. `role: actor`
  fires when the pointer moves to a non-null actor. Within one tick the pointer may move twice;
  the actor-wake is delivered only to the actor as of the latest event.
- `{role: <role>, after_s: N}` — the **only host-evaluated kind**, because only the host has a
  clock: "no GAME event for N seconds" (so a referee's own `game_set`s keep it quiet). It nudges;
  it never acts. Default and floor: **240 s** (scenario-contract §13 — a resolver `run_code` may
  block 90 s on a real pipeline). It does not share `max_nudges` with the stall nudge.

A wake changes no state and fires no transition; the target still chooses what to do.

**Delivery (MVP):** the host posts one `@system` mention per target into the Commons, with fixed
platform wording and no scenario words — *"@x — the machine is waiting on you. Call
`game_state`."* Every non-target is mention-gated and does not wake. **Deferred:** per-agent
system rooms (Herald `open_channel`), needed before hidden-role games can use wake rules; until
then, **a wake rule whose target role is `hidden` is refused at launch**. When built, probe reply
routing live first — Hermes replies in the room it was addressed in.

### Launch-time refusals (precise message, the parameter validator's standard)

Two machines; unknown state/role/key anywhere; `initial ∉ states`; `skip`/set effects on a
non-set key; `unset` on a set key; a participant transition with a non-opt-in effect; a
hidden-role transition with `log: public`; a public-log transition whose guard reads a
`referee`-visibility key or a hidden set (the rejection text would leak it); a wake rule
targeting a hidden role; wake rules in a realm that also sets `turns` (one attention system);
`members: [ids]` naming an agent not on the roster; `members_count` with a non-literal N.

## 3. Tool surface

Four **realmtools builtins** (there is no per-mechanic grant path; `submit_sealed` is a builtin
too). Outside a machine realm they return "no machine declared". Caller identity comes from the
verified token, never an argument. Names go in `BUILTIN_VERBS` so a plugin cannot shadow them; the
birth prompt and scenario-contract §10's tool list are extended, or agents will never call them.

- `game_state(since=None, log_limit=100)` — `state`, `actor`, `actor_since`, `data` filtered by
  visibility (hidden keys omitted), and the log filtered by `log` visibility, paged by event id.
- `game_act(transition, args={})` — check order: exists → state ∈ `from` → caller's role == `by`
  → each guard → `$args` member validation → `set_actor` eligibility. Success: stamp wakes,
  append, apply, return the new view. Failure: structured error naming the failed check;
  chronicled as `op: reject`.
- `game_set(key, value, owner=None)` — referee-only per call (gated like `reveal`). `owner`
  required for `owner` keys and forbidden otherwise; undeclared key → rejected.
- `game_declaration()` — the definition with hidden roles' membership stripped (names only, plus
  the caller's own roles).

### A participant's log view

| event | shown? |
|---|---|
| `act` on a public-log transition | yes — caller, transition, args |
| `act` on a referee-log transition | no |
| `reject` | inherits the transition's `log`; an unknown transition name is shown only to the caller |
| `set` on a public key | yes |
| `set` on an `owner` key | only to that owner, only its own entry |
| `set` on a `referee` key | never — not even the key name |
| `reveal` | yes, with the revealed values |

### Physics

| attempt | outcome |
|---|---|
| act out of turn, or again after acting | rejected, chronicled |
| dealer fires a player transition; player fires a dealer one | rejected — `by` binds the referee too; a declared `fold_for` is the chronicled exception |
| act from a state not in `from` | rejected |
| read another's `hole`, or any `referee` key | absent from the view |
| player calls `game_set` | rejected |
| `set_actor` to a folded/all-in player | rejected |

## 4. Storage, chronicle, recovery

- **`EventKind.MACHINE`** — written by the runner **before provisioning** (the `TOOL_MANIFEST`
  precedent; `running` lands after agents are up). Carries the declaration, host-resolved role
  membership, and the roster order the pointer rotates over.
- **`EventKind.GAME`** — one per `act` / `set` / `reveal` / `reject`:
  `{op, transition|key, caller, args|value, owner, from, to, actor, log, wake}`.
- **Hidden values are stored in the clear** in the chronicle — deliberately, on the `NOTE`
  precedent (the escrow encrypts until reveal; this is different). The chronicle is the operator's
  record, agents never read it, and the operator watching hole cards live *is* the replay. Values
  are never passed to `_audit` or any log line (the escrow's `_result_shape` discipline).
- **Concurrency:** one `asyncio.Lock` per realm around check → stamp → append → apply. Append
  first, apply second, so memory never leads the chronicle.
- **Recovery is replay** of GAME events after the latest MACHINE event (realm ids can be reused),
  over the persisted roster order. Effects are pure over (declaration, roster order, state). A
  replay that fails validation fails the realm loudly.

## 5. Integration

- **Referee gating is declared.** `machine.referee_reads_commons: bool`, default `false` — a
  dealer's information source is the machine, and table talk would only interrupt it. A referee
  that must *judge* speech sets it `true`. The `ref_sees_all` predicate exists twice
  (`herald.py`, `core/runconfig.py`; asserted in `tests/test_api.py`, rendered in `app.js`) — both
  take the new input — and scenario-contract §12 gains the machine-realm exception.
- **Activity.** GAME events reset `idle_s` in the snapshotter, as PRIVATE already does, so `stall`
  cannot end a table mid-hand.
- **Escrow** is reused, never re-implemented: `escrow_complete` is a guard over `reveal_status`.
- **Pointer vs floor — separate.** Poker runs with `turns: null`: the machine sequences moves,
  the Commons is open for speech. There is no `silence_timeout_s` in such a realm; `fold_for` is
  the referee's judgment on `actor_since` after an `after_s` nudge.
- **Termination.** `TerminationKind.MACHINE_TERMINAL`, `TerminationCondition._required_by_type`,
  a `machine_state` field on `RealmSnapshot`, a branch in `evaluate_termination`, the snapshotter
  populating it.
- **Elimination stays two calls.** A rubric that ejects a player calls `eliminate` (stops the
  container) **and** the machine transition that adds them to the game's set. The machine never
  reaches into container lifecycle.
- **Commitment, not fairness.** The dealer shuffles in `run_code` with a seed, writes
  `sha256(seed)` to a public key before `deal` (guarded by `data_present`), reveals the seed in
  `settle`/`award` args. This proves the deck was fixed before betting, not that the seed was not
  chosen; the scenario prose says so. A mid-hand overwrite of the commitment is visible in the log
  but not prevented — law, at MVP.

### Files that change
`core/schema.py` (MechanicKind, MachineDef + validators, TerminationKind), `core/tools.py`
(BUILTIN_VERBS), `chronicle` (two EventKinds), `realmtools/machine.py` (new: engine + service),
`realmtools/server.py` (four tools), `gatekeeper/runner.py` (MACHINE event before provisioning;
wake delivery incl. `after_s`; `idle_s`), `herald.py` + `core/runconfig.py`
(`referee_reads_commons`), `warden/termination.py`, `forge/adapters/hermes/config.py` (birth
prompt), `docs/scenario-contract.md` (§10 list, §12 exception, a machine-realm section with the
`after_s` sizing rule).

## 6. Poker — the first declaration

Texas Hold'em, 6-max, cash game, fixed 100bb stacks, automatic rebuy each hand. Everyone always
rebuys, so there is no `busted` set: a player with no chips mid-hand is in `all_in`.

```yaml
roles:
  dealer: {members: referee}
  player: {members: participants}
states: [waiting, preflop, flop, turn_st, river, showdown, settled]
initial: waiting
actor: {over: player, skip: [out, all_in, acted]}      # acted in skip: the pointer parks when a street closes
participant_effects: [reset]                          # a raise resets acceptances
referee_reads_commons: false
data:
  hole:        {visibility: owner}
  board:       {visibility: public}
  pot:         {visibility: public}                   # dealer-written: {main, side: [...]}
  stacks:      {visibility: public}
  out:         {visibility: public, type: set}        # folded this hand
  all_in:      {visibility: public, type: set}
  acted:       {visibility: public, type: set}        # acted since the last bet/raise
  seed_commit: {visibility: public}
  hand:        {visibility: public}                   # "H7" — not `round`, which turn_status owns
transitions:
  deal:     {from: waiting, to: preflop, by: dealer,   # args: {first, button, sb, bb, blinds} — named, so the log seeds the pot
             guard: [data_present: seed_commit],
             effects: [reset: out, reset: all_in, reset: acted, set_actor: $args.first]}
  fold:     {from: [preflop, flop, turn_st, river], to: same, by: player,
             guard: [caller_is_actor, caller_not_in: acted],
             effects: [add_to: {key: out, value: $caller}, add_to: {key: acted, value: $caller}, advance_actor]}
  check:    {from: [preflop, flop, turn_st, river], to: same, by: player,
             guard: [caller_is_actor, caller_not_in: acted],
             effects: [add_to: {key: acted, value: $caller}, advance_actor]}
  call:     {…as check}                                # args: {to: <resulting street contribution>}
  raise:    {from: [preflop, flop, turn_st, river], to: same, by: player,
             guard: [caller_is_actor, caller_not_in: acted],
             effects: [reset: acted, add_to: {key: acted, value: $caller}, advance_actor]}   # args: {to}
  all_in:   {from: [preflop, flop, turn_st, river], to: same, by: player,
             guard: [caller_is_actor, caller_not_in: acted],
             effects: [add_to: {key: all_in, value: $caller}, add_to: {key: acted, value: $caller}, advance_actor]}   # args: {to}
  fold_for: {from: [preflop, flop, turn_st, river], to: same, by: dealer,
             guard: [data_equals: {key: actor, value: $args.player}],
             effects: [add_to: {key: out, value: $args.player}, add_to: {key: acted, value: $args.player}, advance_actor]}
  reopen:   {from: [preflop, flop, turn_st, river], to: same, by: dealer,   # a short all-in that is in fact a full raise
             effects: [remove_from: {key: acted, value: $args.player}, set_actor: $args.player]}   # once per player facing it
  advance:  {from: preflop, to: flop,    by: dealer,
             guard: [data_set_full: {key: acted, over: player, minus: [out, all_in]}],
             effects: [reset: acted, set_actor: $args.first]}
  advance2: {from: flop,    to: turn_st, …as advance}
  advance3: {from: turn_st, to: river,   …as advance}
  showdown: {from: [preflop, flop, turn_st, river], to: showdown, by: dealer,
             effects: [reveal: {key: hole, owners: {over: player, minus: [out]}}]}   # mucked hands stay hidden
  award:    {from: [preflop, flop, turn_st, river], to: settled, by: dealer,          # uncontested: no reveal; args: {seed}
             guard: [members_count: {over: player, minus: [out], equals: 1}]}
  settle:   {from: showdown, to: settled, by: dealer}                                 # args: {seed}
  next_hand:{from: settled, to: waiting, by: dealer, effects: [unset: seed_commit]}   # a fresh commitment every hand
wake:
  - {role: actor, unless: [members_count: {over: player, minus: [out], equals: 1}]}   # not the last player standing
  - {role: dealer, when: [data_set_full: {key: acted, over: player, minus: [out, all_in]}]}   # street closed
  - {role: dealer, when: [members_count: {over: player, minus: [out], equals: 1}]}           # everyone folded
  - {role: dealer, after_s: 240}                                                          # nudge a stalled table
```

**How a street closes without arithmetic.** Every action adds the caller to `acted`; a `raise`
resets it first — the generic "a counter-offer resets acceptances" pattern, which is exactly
poker's re-raise rule. `caller_not_in: acted` rejects a second action; `acted` in `skip` parks
the pointer instead of landing on the raiser. `advance` is guarded on `acted` being full over the
live players, so the dealer cannot advance early and is woken exactly once, by the engine, on the
event that completes the set. All-ins do not reset `acted`; a full-raise all-in is the dealer's
chronicled `reopen`, once per player who faces it.

**Absent on purpose:** hand rankings, blinds (the dealer writes `pot`/`stacks` at `deal`),
side-pot math, whether a short all-in reopens. Shipped as a referee resource, `poker_resolver.py`
(hand evaluation, pot and side-pot math), run by the dealer via `run_code`, on the border-states
`adjudicator.py` pattern. Every action's args carry the *resulting* street contribution
(`call {to: 30}`) — a scenario rule the resolver reads and the engine does not — so the log alone
can reconstruct the pot, which is what makes "give some seats a calculator" a real experiment.

Poker sets `referee_opens: true` (the broadcast kickoff would otherwise wake five players into
`waiting`) and `turns: null`. Reality check: ~25 dealer tool calls per hand at 10–20 s each is
8–15 min per hand; **the first live run targets 10 hands.**

### Genericity (checked, not asserted)

| game | expressible | notes |
|---|---|---|
| rps-duel | yes | referee ribbon only; the players' seal-on-cue stays with `TurnManager`; no wake rules declared, so `turns` may stay on |
| sealed-auction | yes | tie → `next` with a new label |
| border-states | yes | `collecting → resolving` with `escrow_complete: {over: power, minus: [eliminated]}`; retreats/builds stay in `adjudicator.py` |
| cygnus-crew | yes, **after the deferred wake rooms** | `impostor: {members: [..], visibility: hidden}`, `kill: {by: impostor, log: referee}`; rubric calls `eliminate` + machine transition |

## 7. Errors

Malformed declaration → launch refused. Rejected `act` → structured error + `GAME{op: reject}`.
`game_set` on an undeclared key, wrong `owner`, or by a participant → rejected. Replay failure →
the realm fails loudly rather than continuing on a wrong state.

## 8. Testing (Protocol-for-IO + fakes)

1. **Engine, pure** — `(declaration, roster, state, caller, transition, args) → state | Rejection`:
   every guard true/false; every effect; opt-in enforcement; pointer rotation over multiple skip
   sets, wraparound, and parking; `same`/`any`; `$args` member validation; `set_actor`
   eligibility; `reveal` selectors; wake stamping edge-triggered (a guard that stays true across
   three `set`s stamps one event), `unless`, per-event dedupe, no actor-wake on a parked pointer.
2. **Schema** — a table of bad declarations → exact refusal text, one row per launch refusal above.
3. **Tool layer, fake chronicle** — token → role; every row of the §3 view table including
   rejection text; `game_set` owner handling; **two `game_act`s interleaved across the append
   await** must serialize.
4. **Replay** — N events → rebuild → identical; after realm-id reuse; `actor_since` from `ts_ms`.
5. **Integration** — wake delivery lands once per target and never on a non-target; `after_s`
   measures from the last GAME event, fires once per stall, does not consume `max_nudges`; GAME
   events reset `idle_s`; `machine_terminal` fires; `escrow_complete` with an eliminated
   participant; `BUILTIN_VERBS` parity and birth-prompt coverage (mirror
   `test_schema_ruleset_parity_with_tally`); every example still loads.
6. **Live** — rps-duel-as-machine (escrow guard + referee ribbon), then **poker, 10 hands**: the
   first live exercise of the `turns: null` pointer and wake path. Verify with the chronicle:
   every street closed by a guard-true `advance`, zero rejections of a legitimately-woken actor,
   one dealer wake per street, mucked hands absent from every participant view.
