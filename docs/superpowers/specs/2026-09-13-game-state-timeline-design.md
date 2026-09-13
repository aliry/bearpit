# Game state in the transcript — a generic design

**Status:** approved design, not yet built.
**Depends on:** the game state machine (PR #102) and its chronicle events.

## 1. The problem

A machine realm chronicles everything it does — every transition, every write, every rejection —
and none of it is visible in the console. The realm page shows messages; the game is invisible. To
answer "what did vega actually hold when it folded?" you query Postgres by hand.

Worse, the transcript can mislead on its own. An agent's post is what it *said*; the GAME event is
what it *did*, and this session has repeatedly found the two disagreeing — a seat narrating a fold
it had not fired, a dealer publishing a street before it closed. The two streams belong side by
side.

## 2. What this is NOT

Not a poker viewer. Nothing in this design knows what a hand, a pot or a street is. Every label,
grouping and rendering decision is derived from the machine's own declaration, which already states
the roles, the states, the pointer, and every data key's `visibility` and `type`. A design that
needs a per-scenario renderer has failed; `rps-machine` must light up with no extra work.

This is the apparatus principle applied to the UI: the console reports what the chronicle holds.

## 3. Shape

### 3.1 One new endpoint

```
GET /api/realms/{realm_id}/machine?as=<agent_id|referee>
```

```jsonc
{
  "declaration": { "roles": …, "states": …, "data": …, "actor": …, "transitions": … },
  "as": "vega",                       // the lens this payload was rendered through
  "seats": ["vega", "orion", …],      // role members + "referee", for the lens selector
  "state":  { "state": "river", "actor": "vega", "data": { … } },
  "timeline": [
    { "id": 41368, "ts_ms": 1757…, "op": "act", "transition": "call",
      "caller": "vega", "args": {"to": "20"},
      "changes": [ {"key": "acted",   "kind": "set_add", "value": "vega"},
                   {"key": "$actor",  "kind": "value", "from": "vega", "to": "orion"} ] }
  ]
}
```

`changes` is the whole point: a row says what moved, not just what was called.

### 3.2 Diff the lens, not the state

The obvious implementation — diff raw `MachineState`, then filter — leaks: a hidden key that
changed still shows as "something changed", and the absence of a row is itself information.

So: replay the GAME events once, and at each step compute `view(defn, bindings, state, caller)`
**before and after**, and diff those two dicts. Privacy is then a property of the construction
rather than a filter someone can forget. A key the caller cannot see never enters the comparison,
so it can neither appear nor be inferred from a gap.

Rows themselves pass through `log_row(defn, bindings, payload, caller)`, which already implements
the §3 visibility table. A row the caller may not see is omitted entirely.

One pass, O(events). `replay()` already walks the events applying each; this captures a view
snapshot per step rather than only the final state.

### 3.3 Rendering, derived from the declaration

| declared | change emitted |
|---|---|
| `{"visibility": "public"}` | `{"kind": "value", "key": "pot", "from": 30, "to": 370}` |
| `{"visibility": "public", "type": "set"}` | `{"kind": "set", "key": "acted", "added": [...], "removed": [...]}` |
| `{"visibility": "owner"}` | `{"kind": "owner", "key": "hole", "owner": "vega", "from": null, "to": "Ah Kd"}` |
| `{"visibility": "referee"}` | nothing, unless the lens is the referee |
| the pointer | `{"kind": "actor", "from": "vega", "to": "orion"}` |
| the FSM state | `{"kind": "state", "from": "preflop", "to": "flop"}` |

The state and the pointer are discriminated by `kind` rather than by a reserved key name, so the
platform never has to forbid a scenario from naming a data key `state` or `actor`.

`actor_since` is deliberately not diffed: it moves whenever `actor` does and says nothing the
`actor` change does not already say.

Rendering follows from `kind` alone — the console needs no per-scenario knowledge, and a key it has
never seen renders correctly the first time.

## 4. Boundaries

- **Read-only.** No endpoint here mutates a realm.
- **Works on an archived realm**, because it replays the chronicle rather than reading live memory.
- **A realm with no machine** returns `{"declaration": null, "timeline": []}` and the UI shows
  nothing — no empty panel on the scenarios that do not use one.
- **Cost**: a full replay per request. Poker's ten hands produce ~450 GAME events, which is
  nothing; if a realm ever makes this slow, page by event id — do not cache, because a live realm's
  timeline grows under the cache.

## 5. Done when

`rps-machine` and `poker-table` both render their state and timeline with no scenario-specific code
in either the API or the console, the lens shows a participant strictly less than the referee sees,
and a UI smoke check drives the merged feed.
