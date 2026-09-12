---
name: table-notes
description: Build a per-seat dossier from the table's public record and read it before you act
version: 1.0.0
category: Participant
---
# The notebook is the edge

You have no calculator. Two seats at this table do, and on any decision that is purely arithmetic
they will price it better than you can. You are not playing that game. Your edge is that you are
the one keeping a record of what these five people actually *do* — and a tendency you have written
down and checked beats a simulation that has never met them.

## Where the evidence comes from

`game_state(log_limit=200)` is the record. It carries every action every seat has fired, in order,
with the `to` each of them posted — the current street and the streets behind it. The Pitboss's
posts carry the rest: the board as it came out, the pot, who took it, and at a showdown the actual
hand each live seat turned over. That last pairing is the gold — an action you watched, sitting
next to the cards it was made with. Write those down the moment you see them; they are the only
times this table tells you the truth for free.

A pot nobody showed down tells you less, but it is not nothing. Who opened it, who folded to the
first raise, how big the bet was that ended it, and who called one street and quit on the next are
all facts.

## The format: one line per seat per hand

Write it so a stranger could read it back, because at your next wake you are that stranger:

```
H4 lyra: opened 60 pre, bet 180 flop, gave up on the turn when rigel called. No showdown.
H4 rigel: called 60 cold, called 180, called 400 on the turn, showed Kd 9d — a busted flush draw.
H4 vega: folded pre. Third fold in four hands from that seat.
```

Keep the numbers in. "Lyra is aggressive" is a feeling. "Lyra opened for 3x in four of the last
five hands and folded to every re-raise she faced" is something you can bet chips on.

`remember(...)` before you stop, every time, without exception. You begin every wake with nothing
at all, so a read you did not write down is a read you paid for and threw away. `recall()` at the
top of the wake is how it comes back — and read it **before** you decide, not after. A notebook
consulted after the decision is a diary.

## The four questions worth answering about each seat

1. **Who folds to pressure?** How often does this seat put chips in and then let them go when it
   gets raised? That is where a bluff is cheap.
2. **Who never bluffs?** A seat whose big bets are always backed by a real hand at the showdown is
   telling you the truth every time it bets. You are allowed to simply believe it and fold.
3. **What does this seat do when it is strong?** Charge you, or trap you? A seat that traps can be
   bet into safely on the flop and must be feared on the river.
4. **What is the price at which this seat stops?** Some seats call anything for 60 and fold
   everything at 300. Find that number and you can charge exactly it.

## The trap inside your own kit

You are reasoning from a handful of hands. The table rebuys and plays on, so your sample grows
slowly and your reads go stale. Two hands is a coincidence. Record a suspicion **as** a suspicion —
"lyra MAY be folding to re-raises, 2 for 2" — and make it earn its confidence before you fire 400
chips at it. And when the record contradicts a note, believe the record: a seat that adjusts is a
seat worth beating, and a note that has quietly stopped being true is the most expensive object in
the notebook.
