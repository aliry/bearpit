# Triad Build — four engineers co-author one design doc (+ an editor who ships it)

**Lead**, **Store**, **Scale** and **Review** co-author a URL-shortener design in the shared
folder: one section file each, assembled by the lead into `/realm/shared/design.md`, reviewed,
revised and signed. **The Editor** — the referee — reads the finished file, checks it against an
acceptance checklist, and calls `rule()`. That verdict is what ends the realm.

## Why it is built this way

Four rules of the platform shape this scenario, and each one was learned from a failed run:

- **The shared folder is only reachable through `run_code`.** An agent has no `write_file` tool, no
  shell and no editor. So the guidelines and every persona name the tool, the exact path, and the
  `os.makedirs` call. "Collaborate on design.md" produces four agents describing sections in chat
  and an empty volume.
- **A realm only gets realmtools if it needs them.** Forge wires the Realmtools MCP when a project
  declares a mechanic, turns, DMs — or a **referee**. Without the Editor, this project would be
  provisioned with no `run_code` at all, and the birth prompt would not even tell the agents that
  `/realm/shared` exists. The Editor is what makes the file deliverable reachable.
- **One file, four always-on writers, is a clobber.** The natural idiom is
  `open(path,'w').write(...)`: two concurrent writers and a section silently vanishes. So each
  agent owns `/realm/shared/sections/<id>.md`, and **only lead** writes `design.md`.
- **Nothing is real until a tool is called.** "STATUS: FINAL" said in chat finalizes nothing —
  Review *prepends* it to the file with `run_code`. An editor that posts "accepted" has shipped
  nothing — only `rule()` ends the realm.

## The roster

| Agent | Role | Model | Writes |
|---|---|---|---|
| Lead | participant | medium | `sections/lead.md` (API) — **and assembles `design.md`** |
| Store | participant | medium | `sections/store.md` (storage & data model) |
| Scale | participant | medium | `sections/scale.md` (scaling & caching) |
| Review | participant | medium | `sections/review.md` (risks & open questions); prepends `STATUS: FINAL` |
| The Editor | **referee** | large | nothing — reads, checks, `rule()`s |

Always-on and parallel — no turns. `require_mention: false`, so a broadcast like "store.md written"
actually reaches the other four; with the default (`true`) each agent would only ever receive
messages that @mention it, and a four-way co-authoring realm would never converge.

## The protocol

1. **Draft** — each agent writes its own section file with `run_code`.
2. **Review** — read the other three files with `run_code`; post short, concrete notes.
3. **Revise** — authors rewrite their own file. A revision pasted into chat changes nothing.
4. **Assemble** — lead, and only lead, builds `/realm/shared/design.md` from the four section files
   (API → Storage → Scaling → Risks).
5. **Sign** — each of the four posts exactly `✅ SIGNED` in the commons, once, and only after
   `design.md` exists and they have read it back.
6. **Finalize** — Review prepends `STATUS: FINAL` to `design.md` with `run_code`.
7. **Ship** — the Editor's checklist passes → `rule(outcome, reasons)` → one closing line
   `🏁 SHIPPED — …`.

The phases are stated in `spec.guidelines` in exactly this order. Without a stated order, four
parallel agents have no shared notion of when signing is legitimate — and they sign an empty folder.

## The Editor's acceptance checklist

`design.md` exists, first line exactly `STATUS: FINAL`; all four sections present; no section is a
stub; the sections don't contradict each other; all four `✅ SIGNED` posts are in. If it fails, the
Editor posts one line naming exactly what is missing and who owes it, and polls again. After twelve
polls it rules anyway — an unfinished doc ships as "Not shipped" rather than hanging the realm.

## How it ends

In order (first match wins):

1. **`referee_verdict`** — the Editor calls `rule()`. The intended ending, and the only *decided* one.
2. **`message`** — hardened fallback: `(?i)🏁\s*SHIPPED` in the commons, in case the Editor decided
   but its `rule()` call failed. It is anchored on the **Editor's own** closing line, not on the
   engineers' sign-offs: a `SIGNED`-counting rule would fire on the fourth signature — *before* the
   Editor had read the doc — and the realm would end with an unjudged artifact. (The old rule was
   worse still: it matched the bare substring `SIGNED` anywhere in any message, so "I'll post SIGNED
   once Store finishes" counted, and four such lines from one agent ended the realm on an empty
   volume.)
3. **`duration`** — 60m wall-clock backstop.
4. **`stall`** — 20m of total silence. Four agents on `$3` caps with `starve_then_kill` can all go
   quiet; without this the realm would sit there until the duration expired. The Editor posts a
   one-line status whenever it polls, so a live Editor keeps a working realm alive.

There is deliberately **no `file` termination on `STATUS: FINAL`**. It would fire the instant Review
prepended the marker — before the Editor had read a word — which is precisely the decided ending
this scenario exists to have.

## Run it

```bash
# 1. bring up the platform stack
docker compose -f deploy/docker-compose.yaml up -d

# 2. register the credential handle (BYOK — your key, your spend; see the top-level README)

# 3. validate, then run
arealm validate examples/triad-build
arealm up examples/triad-build
```

Watch it with `arealm tail <realm>`; read the doc and the verdict afterwards with
`arealm archive <realm>`.

## Difficulty dials

Drop the four engineers to `small` to watch how much of the failure is simply "the model never
emitted a valid `run_code` call". Swap the subject in `spec.goals` and the four personas for any
system the models know cold. To make convergence harder, give Store and Scale deliberately
incompatible starting assumptions and let the Editor's contradiction check (checklist item **d**)
do its work.
