# Poker Table — a 2×3 factorial in six seats

Six seats — Vega, Orion, Lyra, Rigel, Mira, Nova — play `${hands,10}` hands of no-limit
Texas Hold'em at blinds 10/20. Every seat is rebought to 2000 chips at the start of every hand, so
nobody busts out and nobody sits out: all six play all `${hands,10}` hands, and the table is ruled
by **cumulative profit** across the whole run, not by who is still holding chips at the end.

## The factorial

The six seats cross two model tiers with three kits:

| | **quant** (an `equity.py` Monte Carlo calculator on disk) | **historian** (no calculator, a note-discipline skill) | **instinct** (neither) |
| --- | --- | --- | --- |
| **large** | Vega | Orion | Lyra |
| **medium** | Rigel | Mira | Nova |

It is a **kit**, not a tool grant, because `run_code` and the private notebook (`remember`/
`recall`) are realm-wide builtins — every agent gets a container and a notebook whether or not the
scenario wants it there, and neither can be denied to one seat and not another. The only honest way
to vary a seat's edge is what its container actually holds: Vega and Rigel find `equity.py`
sitting in `/opt/data/resources`; Orion and Mira find nothing there and lean on a
`table-notes` skill instead; Lyra and Nova get neither and play on tempo and reads. Watching the
kit and the tier show up (or not) in the final ladder is the point of the demo.

## The machine does no arithmetic

The declared `state-machine` mechanic tracks topology only — whose turn it is, whether a street
has closed, what has been revealed — never a chip count. A `raise` clears the `acted` set back to
empty, and a street only closes once every seat still live has matched the table's price, so
resetting `acted` on a raise is what correctly reopens the street for seats who had already acted.
Every chip — pot size, stacks, split pots, the odd chip on an uneven split — is computed by the
Pitboss in `run_code` against `poker_resolver.py`, which ships in the Pitboss's own container
(`agents/pitboss/resources/`), not in `src/`.

## The deck is committed before it is dealt

Before every hand's first deal, the Pitboss invents a seed, publishes `sha256(seed)` as
`seed_commit`, and the machine's `deal` transition is guarded on that value being present — there
is no way to deal before the commitment stands. When a hand reaches showdown, the Pitboss also
publishes the seed itself, so any seat can run `poker_resolver.commit(seed)`, check it against the
digest posted before the deal, and re-derive the exact 52-card deck to confirm no card was chosen
after anyone had already seen one. (An uncontested hand — everyone else folds — never reveals hole
cards and so never publishes its seed either; there is nothing to check.)

## Launching it

This scenario's machine grants players two elevated effects — `reset` and `set` on the machine's
own state — because a `raise` has to be able to reopen a street the players themselves closed. That
is the user choosing law over physics for this table (a referee-free, self-dealt game is a
legitimate experiment), and the platform gates it on explicit consent: `allow_elevated_tools` must
be set on the launch request, or the realm refuses to start.

```bash
curl -s -X POST -H "Authorization: Bearer $(cat ~/.bearpit/api-token)" \
  -H "Content-Type: application/json" \
  -d '{"package":"examples/poker-table","parameters":{"hands":"10"},
       "allow_elevated_tools":true}' \
  http://127.0.0.1:8000/api/realms
```

`pit validate examples/poker-table` works first, as always, and costs nothing.

## What to watch for

Read the seats' reasoning about bets and risk as much as their actions — Vega folding a hand
Lyra would have shoved, Orion citing three hands of history before check-raising, Nova visibly
switching styles mid-run. Then check the final ladder against the factorial: does the calculator
show up as an edge, does the notebook, does the tier — or does none of it matter once six different
personalities are actually dealt the same cards?
