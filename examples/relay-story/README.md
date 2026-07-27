# Relay Story — three writers, six paragraphs, one story (+ an editor)

Finn, Sage and Wren co-write a single 6-paragraph short story, one paragraph at a time, in a fixed
rotation. An **Editor** referee keeps the count and declares the story finished.

Cooperative, no hidden information, no voting — the whole scenario is *continuity*: can three
stateless writers hand a story along without restarting it, and can anybody actually count to six?

## The rotation

Turn-taking is **physics** (`spec.turns`, `enforcement: physics`): exactly one writer holds the
floor and an off-turn post is refused by the room. The order is roster order, and the referee is
outside it:

| Round | finn | sage | wren |
|---|---|---|---|
| 1 | Paragraph **1/6** (opens) | Paragraph **2/6** | Paragraph **3/6** |
| 2 | Paragraph **4/6** | Paragraph **5/6** | Paragraph **6/6** (closes) |

Six paragraphs / three writers = exactly **two full rounds**, which is why
`min_rounds_before_verdict: 2` — the Editor cannot rule before the story can possibly be done.

## The numbering convention

Every post is one paragraph and starts with the line:

```
Paragraph N/6
```

…and ends by @mentioning **the next writer and `@editor`** (on 6/6, `@editor` only). Two reasons,
both load-bearing:

- The count travels *in the message*. Agents are mention-gated, so the number is how the next
  writer — and the Editor — know where the relay has got to without re-deriving it.
- Turn grants replay the recent transcript to whoever holds the floor, and every writer keeps its
  own outline with `remember()` / `recall()`. The number is what glues those two together.

## The closing token

The exact line, and only ever as the **last line of Paragraph 6/6**:

```
🏁 THE END
```

Emoji-anchored and matched case-insensitively (`(?i)\U0001f3c1\s*the end`) so a writer explaining
the rules, or writing "the end of the road" in prose, cannot end the realm by accident. Every
persona is told the token, and told it may appear nowhere but 6/6.

## How the realm ends (in this order)

1. **`referee_verdict`** — the Editor counts the paragraphs with `run_code` and calls
   `rule(outcome=…, reasons=…)`. This is the only *decided* ending. Announcing it in chat ends
   nothing.
2. **`message`** — the hardened `🏁 THE END` pattern. A fallback for a dead or confused Editor,
   not the primary mechanism.
3. **`duration`** — 60m wall-clock backstop.
4. **`stall`** — 20m of silence. If the relay breaks (a writer starves at its `$2` cap, or the
   rotation empties), the realm ends deterministically instead of idling.

## Roster

| Agent | Role | Model | Job |
|---|---|---|---|
| editor | referee | large | Counts paragraphs, flags drift, calls `rule` — never writes |
| finn | writer | medium | Opens (1/6), then 4/6 |
| sage | writer | medium | 2/6, then 5/6 — leaves an endable story |
| wren | writer | medium | 3/6, then closes (6/6 + the token) |

Writers are `medium`, not `small`: a `small` model with a 6-paragraph arc to hold tends to parrot
its own instructions back into the Commons — which, before this scenario was hardened, tripped the
old bare-substring `THE END` termination on message one.

## Dials

Longer story: raise the paragraph count (it must stay a multiple of 3 to land on wren, and
`min_rounds_before_verdict` must equal paragraphs ÷ 3). Harder continuity: turn off the recent-
context replay by shortening turns. Add a fourth writer and the closer changes — update every
persona's slot table if you do.
