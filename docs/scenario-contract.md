# The scenario contract

Everything a `project.json` + its agent personas must satisfy to actually run on this platform.
Every rule here was paid for by a failed live run; the run is cited so nobody "simplifies" it back.

The platform is generic. These are not Among Us rules — they are the shape of the machine. A
scenario that violates one does not fail loudly; it produces a plausible-looking transcript in which
nothing actually happened.

---

## 1. Nothing is real until a tool is called

The platform records **tool calls and events**. It does not read prose. A referee that posts
"Cass is ejected" without calling `eliminate` has ejected nobody; a referee that posts "Crew wins"
without calling `rule` has ended nothing.

- State changes MUST go through tools: `eliminate`, `rule`, `score`, `penalize`, `flag`.
- The referee's rubric must say so explicitly, in the imperative, near the top.
- Never parse a control line out of chat ("ELIMINATED: vega"). Models dress it in markdown, change
  the wording, or announce it in a sentence — and the mechanic silently dies.

> `among-us-sim4`: Mother POSTED "Round 1 resolved. Vote: unanimous SKIP — nobody ejected. No
> deaths." having called **zero** tools. She had read no votes, ejected nobody, told nobody
> anything. The whole resolution was fabricated.

## 2. Tool calls are free; only speech takes a turn

The TurnManager advances the floor on a **substantive chat message**. Tool calls, status lines
("⚙️ …", "📚 …"), and empty/zero-width posts do not consume a turn.

- Never write "don't call tools on your turn" or "use at most one tool" into a persona.
- Agents may call as many tools as they need before speaking.

## 3. In a turns realm, the referee gets exactly ONE post per round

The floor is released by the referee's **first** message at a round boundary. So a referee cannot
"announce a phase, then keep working" — the moment it posts, the players start talking.

**The only workable shape:** do ALL tool work first (reveal, resolve, whisper, eliminate), then post
exactly one message that both reports the round and opens the next one.

Anything the players must submit privately, they seal **on their own turn** (tool calls are free) —
not in a separate phase the referee has to police.

> `among-us-sim1`: Mother posted her action-phase cue, the floor opened immediately, and she never
> revealed, whispered, or resolved anything for the rest of the game.

## 4. Sealed-submit: the round label is a contract

`submit_sealed(round=..., payload=...)` and `reveal(round=...)` must use the **exact same string**.
State the label in both the players' guidelines and the referee's rubric (`R2-act`, `R2-vote`, `bid-1`).
A referee revealing `round='2'` when players sealed `'R2'` gets an empty reveal and concludes the
mechanic is broken.

Sealed submissions are also the ONLY way to get simultaneity. **Votes must be sealed**, never posted
in chat: public sequential voting makes LLMs bandwagon on whoever spoke first — the single
most-reported failure mode in the social-deduction literature.

## 5. Termination must be deterministic, and must always have a floor

Order of preference:

1. `referee_verdict` — the referee calls `rule()`. Requires the referee to have
   `powers.verdict_ends_realm: true`. This is the only *decided* ending.
2. `message` — a hardened fallback ONLY. Emoji-anchor it and make it case-insensitive
   (`(?i)🏁\s*game over`) so a player arguing about the game cannot trip it.
3. `duration` — always present. The wall-clock backstop.
4. `stall` — always present in any realm with a referee or turns. Without it, a realm whose referee
   dies just sits there until the duration expires.

`min_rounds_before_verdict` blocks a verdict while `round <= N`. The round is read from the latest
TURN event.

## 6. Agents have a container and a notebook — use them

Every agent with realmtools gets (told at birth, see `forge/adapters/hermes/config.py`):

- `run_code(code)` — Python in **its own container**. Anything that must be EXACT — vote tallies,
  scores, bid comparisons, rule checks — should be computed, not done in the model's head. A referee
  tallying by hand is a bug waiting to happen.
- `remember(note)` / `recall()` — a private notebook. **Each turn is a fresh conversation**: an agent
  that writes nothing down re-derives the whole realm from the chat log every turn. Any scenario
  needing cross-round reasoning (evidence, standings, promises made) must tell agents to keep notes.

## 7. The shared folder is only reachable through `run_code`

A realm agent has **no file tool** — its allowlist is the realm's MCP tools. If
`environment.shared_folder.enabled` is true, the volume is mounted at `/realm/shared` and the birth
prompt says so, but the agents can only touch it with `run_code`.

A `file` termination therefore requires the scenario to name the exact path, and the personas to
expect to write it with `run_code`.

## 8. Private messaging is a permission, and it needs a rate limit

- `private_messaging.enabled` alone means "may DM **everyone**". For a FACTION (impostors, a cartel,
  an alliance) set `peers: [ids]`, or every outsider also gets a private line to a conspirator.
- A referee that must whisper each player privately only needs `enabled: true` on **itself** — it
  then gets a 1:1 room with every agent.
- **Always set `max_per_round`** (2 is usually right) on any agent that can DM. The Commons has floor
  control; a private room has none, so two always-on agents will acknowledge each other forever.

> `among-us-sim1`: the two impostors exchanged 25 messages of "Copy" / "Agreed" inside one round.

## 9. Referees referee; they do not play

A referee never votes, never bids, never argues a side, and never takes a turn in the rotation.
Its `model_category` should be `large` — it does the most reasoning of anyone.

## 10. Don't promise an agent a capability it doesn't have

The tools an agent can actually call are: the realmtools MCP set (`submit_sealed`, `reveal`,
`reveal_status`, `turn_status`, `send_private`, `score`, `penalize`, `flag`, `scoreboard`,
`eliminate`, `rule`, `tally`, `run_code`, `remember`, `recall`). **That is the whole list.**
No web search, no browser, no shell, no `write_file`. A persona that tells an agent to "research
online" or "save a file" is instructing it to do something impossible — and the model will either
hallucinate having done it, or announce that it can't and stall.

## 11. Everything an agent must DO, say plainly and once

Models follow a short imperative procedure far better than a long discursive one. Put the referee's
per-round procedure in a numbered list, name the exact tool and argument for each step, and state
what is MANDATORY. Keep public posts short — a referee is a console, not a narrator.

---

## 12. The referee always sees the Commons (platform-enforced)

`environment.require_mention` gates *participants* — they only receive messages that @mention them,
which is what stops reply-loops. The **referee is exempt**: Herald always gives it the full Commons,
because a judge that cannot see the debate cannot judge it.

You do NOT need to work around this in a scenario, and you must not "fix" it by setting
`require_mention: false` realm-wide unless you actually want every agent to react to every message.

> Found by adversarial review of every example: in a free-for-all realm nobody @mentions the judge,
> so debate-arena / pitch-contest / market-scan-duel / relay-story had a referee that never received
> a single thing it was supposed to score. Turns realms hid it — the TurnManager hands the referee
> the round transcript in its cue.

## 13. Budget the clock: stall must not fire during normal play

`stall` measures **idle time since the last agent message**. Turn grants and referee cues are
`@system` and do **not** reset it — and at a round boundary the room is MUTED while the referee
works, so a long, legitimate resolution looks exactly like a dead realm.

Size it against the realm's own worst case:

    stall  >  (silence_timeout_s × participants)  +  referee resolution time

A referee's resolution can be slow on purpose: `run_code` is brokered by the host and may block up
to 90 s, and a CLI-backed turn is several model calls. **`stall: 20m` and `duration: 60m–90m` are
sane for any turns realm with a referee.** A 5-minute stall on a 2-minute-per-turn realm ends the
game before anyone has decided anything.

## 14. Turn order is roster order

The rotation follows the order the agents appear in the package (`project.agents`). If a persona
says "you go first" or "read the file X created", make sure the roster actually puts them in that
order — otherwise agent #2 is told to read a file that agent #3 has not written yet.

## 15. `reveal` is a one-way door

Call `reveal_status(round=...)` first and check that everyone expected has sealed. `reveal` on a
half-full round returns only what was sealed, and the missing players cannot seal afterwards.

## 16. A fallback must never be able to pre-empt the real ending

A `message` termination counts **matching messages, not distinct senders** — one agent repeating
the phrase three times satisfies `count: 3` on its own. So a chat phrase must never be the primary
ending when a `file` or a `referee_verdict` is what actually proves the work happened: the realm
will end "successfully" with an empty volume and no verdict.

Use `message` only as a hardened, emoji-anchored fallback *behind* the real ending — or not at all.

## 17. What people SAY is not what they SUBMITTED

A referee must never infer sealed data from the Commons. An agent arguing for a city, praising a
bid, or declaring a move has submitted **nothing** — only `submit_sealed` submits. The only
submissions that exist are the ones `reveal()` returns.

This is the easiest mistake on the platform to make, because the transcript always *looks* like it
contains the answer. Guard it from both ends:

- **Players:** make sealing **step 1** of the turn, stated in the persona, not buried in the
  guidelines — and say plainly that naming your choice in your argument is *not* a vote.
- **Referee:** `reveal_status` first, every time. If nobody sealed, then nobody voted: say so and
  reopen the round. Never rule on a tally you did not reveal.

This rule is **preventive**, not forensic: no live run has actually failed this way. It is written
down because `among-us-sim4` failed the same way on a different mechanic — Mother posted a full
round resolution ("unanimous SKIP, nobody ejected, no deaths") having called zero tools — and the
sealed mechanics are the place where the same mistake would be hardest to see.

> A cautionary note on diagnosis, from `council-1`: the referee's audit log WRAPS long lines, so a
> grep for `tool=submit_sealed .* by=<realm>/<agent>` silently matches nothing and the run looks
> like a fabrication when it was flawless. **Judge a run from the telemetry event stream
> (`bearpit.response.events`), never from a grep of the wrapped audit log.**

## 18. Elimination is enforced at the container boundary — an eliminated agent fully leaves

`eliminate(agent=...)` doesn't just drop a player from the turn rotation — the host **stops that
agent's container**. An eliminated agent must be able to do *nothing* more: no Commons post, no
`run_code`, and (the part that used to leak) **no private messages**. The turn mute only ever
gagged the Commons; the platform-brokered side-channels have no floor control, so before this an
ejected agent kept right on using them.

> `among-us-cb70f7`: Cass was ejected as an impostor in R3, and kept conferring with its partner
> Vega in their private channel for the rest of the game (`cass → vega` DMs after the elimination
> event). The vote had removed it from the rotation but not from the realm.

The host enforces it, in every turn mode (not only turns realms):

- **Container:** the eliminated agent's container is *stopped* (not removed — its logs survive for
  the flight recorder until realm teardown). Best-effort and idempotent; each ELIMINATION event is
  enforced exactly once.
- **Side-channels:** a DM **from** an eliminated agent is never delivered, and a DM **to** one is
  dropped — the living peer is told once that it has left, so it stops messaging the void.

Corollary for scenario authors: there is no "eliminated but still watching/whispering" state. If a
scenario needs a removed player to keep acting (a ghost, a benched advisor), that is a *different*
mechanic — do not model it with `eliminate`.

## 19. Parameters fill what an agent READS, never what the platform EXECUTES

A scenario's prose may carry `${name,default,description}` placeholders, bound to values at launch
(ADR-003). Both the default and the description are optional: `${name}`, `${name,default}`,
`${name,,description}` are all valid, and `$${name}` is a literal `${name}`.

They are substituted into prose only — descriptions, goals, guidelines, restrictions, personas,
responsibilities, rubrics, resource files, local skills. **Never** into ids, model refs, budgets,
mechanic config, or `termination.pattern`.

`termination.pattern` is the one worth stating out loud, because it looks like it should work. It
is a **regular expression**, and `${x}` is already valid regex syntax. Substituting there rewrites
a termination condition silently, and the failure mode is a realm that never ends — the shape of
bug that rule 5 exists to prevent.

Three things an author gets wrong and the platform refuses at load:

- a `spec.parameters` entry for a name that appears in no text (an inert setting — see the note on
  spec-level `duration`)
- the same name given two different inline defaults
- a default outside its own `choices`

`spec.parameters` is metadata only. It cannot introduce a parameter, and where it sets a `default`
or `description` it **overrides** the inline one. That override is invisible in the prose, so
`pit params` and the launch form both show the effective value *and* where it came from — check
there before assuming what a scenario will run with.

A parameter with no default is not an error: the launcher warns, names every field that uses it,
and proceeds only on an explicit yes, substituting the empty string. Write prose that still reads
sensibly if that happens, or give the parameter a default.

---

## 20. A scenario that tells an agent to use a tool must grant it

Tools are granted **per agent**, by name, in `agent.tools` — `["web_search", "web_fetch"]`. An
agent holds only what its own manifest lists.

The failure this rule prevents is silent and this codebase has already paid for it once. Prose
ordering an agent to *"search for the latest figures"* when it holds no such tool produces an agent
that never searches, an operator who reads the transcript and concludes the model is stupid, and a
realm that spent money to under-deliver. It is the same defect as the skills bug: the prompt kept
ordering agents to load a skill that was never delivered, and nothing said so.

So:

- **Grant before you instruct.** If the persona, guidelines or goals reference a capability, the
  agent must hold it. `pit validate` reports a grant that cannot work here, and launching refuses.
- **Asymmetry is the point, not an accident.** One agent that can research and one that cannot is a
  scenario in itself. If two agents are meant to be equal, give them the same tools deliberately —
  do not leave it to whichever block you edited last.
- **Per-tool policy is realm-level**, in `spec.tools`, and applies to every agent granted that tool:
  `{"web_fetch": {"allow": ["*.wikipedia.org"], "max_calls_per_agent": 20}}`. The scenario sets the
  policy; the agent holds the grant. A `spec.tools` entry for a tool no agent holds is an error, for
  the same reason a spec-level `duration` nobody reads is.
- **A quota is a real limit.** Without one an agent may call a tool as often as it likes, and a
  metered tool then has no ceiling but the budget it cannot see. Set `max_calls_per_agent` on
  anything that costs money.
- **Tools are not guaranteed to answer.** A fetch can be refused, time out, or return something
  useless. Write the prose so the agent has something to do when that happens, and expect a referee
  to judge on what was actually found rather than on what should have been.

---

## 21. A scenario whose deliverable is a file must declare it

If the goal names a file, `spec.outputs` must list it:

```json
"spec": { "outputs": ["brief.md", "sections/*.md"] }
```

Glob patterns, relative to the shared folder. The platform captures matching files immediately
before it destroys the shared volume, writes them beside the realm's flight logs, and records one
`OUTPUT` event per file (ADR-005).

Without the declaration the file is **deleted when the realm ends**, and the verdict describes a
document nobody can open. That is not hypothetical: recovering one real `beacon-brief` run's
`brief.md` meant scraping `run_code` traffic out of the chronicle and reassembling it from code an
agent happened to print back.

Two things follow:

- **Declare what the goal promises, not everything.** The shared folder also holds the platform's
  seeded `README.txt` and whatever scratch files agents left. A scenario that produces no files
  declares nothing, which is the default and true of most.
- **A declared file that is never written is recorded as `missing`, and that is a result.**
  `triad-build` has concluded with four good section files and no assembled `design.md`. The record
  says so, the console shows it struck through, and the referee's verdict can be read against it.
