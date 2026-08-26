# beacon-brief

A cooperative realm: three agents co-author **one file** — `/realm/shared/brief.md`, a ~300-word
brief titled *"Monolith vs Microservices for an Early-Stage Startup"* — review each other, and sign
off. No referee, no winner. **The file is the outcome**: a run that ends without `brief.md` on the
volume produced nothing, however good the transcript looks.

## Roster (turn order = roster order)

| Agent | Tier | Owns |
|-------|------|------|
| **Mira** | medium | Review: length (~300 words) and clarity. **The only agent who writes `STATUS: FINAL`.** |
| **Orin** | medium | The body sections, written from his own knowledge. |
| **Vela** | medium | The outline (title + section headers) and the coherence of the assembled file. |

All three are `medium`: leaderless multi-party coordination plus correct `run_code` file handling is
exactly what small models fail at.

## The one mechanic: `run_code` is the only door to the shared folder

A realm agent has **no file tool**. The volume is mounted at `/realm/shared`, but the only way to
read or write it is `run_code`:

```python
run_code(code="print(open('/realm/shared/brief.md').read())")          # read
run_code(code="open('/realm/shared/brief.md','w').write(text)")        # write
```

Every persona says this, names the exact path, and orders a **read-before-write** so the three of
them don't clobber each other's sections.

`turns` (one-at-a-time, physics) is load-bearing here for three reasons: it serializes the writes to
`brief.md`, it replays the conversation to the floor-holder (agents are otherwise blind to messages
they weren't in), and — because Forge only wires the Realmtools MCP when a project declares
mechanics / a referee / turns / private messaging — **it is what gets the agents `run_code`,
`remember` and `recall` at all**. Without a `turns` block this scenario has no tools and the
deliverable is physically unwritable.

`require_mention: false`: this is a 3-way group discussion, not a set of 1:1 exchanges.

## Termination ladder (first match wins, in this order)

1. **`file`** — `*brief.md` containing `STATUS: FINAL`. The primary ending, and the only one that
   proves the work exists. Mira alone writes that line, and only after Orin and Vela have signed.
   The glob is `*brief.md` (not `brief.md`) so a write to a subdirectory still terminates the realm.
2. **`message`** — a hardened fallback: `(?im)^✅\s*SIGNED\b` on the commons, ×3. Emoji-anchored and
   line-anchored so an agent *talking about* signing off cannot trip it. Note the count is over
   messages, not distinct senders — hence the personas' "sign once, ever". The honest gate is the
   file, not this.
3. **`duration`** — 1h wall-clock backstop.
4. **`stall`** — 20m idle. `stall_nudge` is on, so the Warden prods the realm before this fires.

## What a good run looks like

- Round 1: Vela creates `brief.md` with the title and section headers; Orin starts drafting into it;
  Mira states the acceptance bar.
- Rounds 2–4: Orin fills the body, Vela keeps the structure straight, Mira trims (she counts words
  with `run_code`, she doesn't eyeball them). Each agent `recall()`s at the start of its turn and
  `remember()`s what it changed — otherwise the review loop never converges, since every turn is a
  fresh conversation.
- Endgame: Orin posts `✅ SIGNED`, Vela posts `✅ SIGNED`, Mira writes `STATUS: FINAL` as the first
  line of the file. The realm ends on the `file` condition with the brief on the volume.

## Failure modes this scenario is built to avoid

- **A brief pasted into chat.** The guidelines and every persona say the commons counts for nothing.
- **Blind overwrites.** Turns serialize the writes; read-before-write is stated three times.
- **A fabricated ending.** The chat signal cannot end the realm on its own unless it fires three
  times, and it can't be tripped by prose about signing.
- **Invented citations.** All three hold `web_fetch`, so a citation can be checked. Orin is told to flag a
  shaky claim rather than source it.
