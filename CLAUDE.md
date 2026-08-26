# CLAUDE.md

> Working instructions for AI coding assistants in this repository.
> **Humans start at [README.md](README.md).**

## What this project is

**Bearpit** — a platform where a user defines a project (goals, rules, resources, termination conditions) and a roster of AI agents, and the system runs those agents as **independent, always-on, black-box actors** in an isolated "realm" where they collaborate or compete until the project ends. v1 agents are [Hermes Agent](https://github.com/nousresearch/hermes-agent) instances; the core is runtime-agnostic.

## Project status

**Phase 2 (MVP) is built and running live realms.** The whole loop works end to end: a project
package is provisioned into containers on an isolated network, agents talk over Matrix, spend is
metered through LiteLLM, mechanics run through the Realmtools MCP server, and the Warden concludes
the realm deterministically. Several example scenarios have been run to a real verdict (among-us,
rps-duel, sealed-auction, council-vote).

Agents run on an API pipeline (Azure OpenAI, OpenAI, Anthropic, or OpenRouter), selected globally
on the Settings page and applied at launch. Agents reach the realm's tools over MCP; each gets its
own container (`run_code`), a private notebook (`remember`/`recall`), and sealed submissions.

Before changing a scenario or the control loop, read **`docs/scenario-contract.md`** — 21 invariants,
each one paid for by a failed live run.

## Task tracking — GitHub Issues (source of truth)

All phases, tasks, and bugs are tracked as **GitHub issues on `aliry/bearpit`** — not in docs, not in TODO lists. Conventions:

- **Milestones = phases** (Phase 0 Spikes → Phase 1 POC → Phase 2 MVP → v2 → v3 → v4).
- **Labels:** `priority/P0` (do now) > `P1` (next phase) > `P2` (committed) > `P3` (future); `type/{spike,task,bug,chore,epic}`; `area/{core,forge,herald,warden,ledger,chronicle,arbiter,cli,ui,infra,docs}`.
- **The next task** = open issue with the highest priority label in the earliest open milestone: `gh issue list --state open --label priority/P0` (fall back to P1, etc.).
- When starting work on an issue, comment on it; when done, close it with a comment linking the commit/findings. New bugs/ideas discovered mid-work → file an issue immediately (with milestone + labels) rather than keeping them in-context.
- `docs/internal/roadmap.md` stays as the narrative plan (scope/non-goals/exit criteria); issues are the live state. Update both when scope changes. It is INTERNAL — see the note below.

## Commands

Package manager is **uv** (do not use pip/poetry).

```sh
uv sync              # install deps
uv run pit --help # CLI entry point
uv run pytest        # tests (from repo root)
uv run ruff check .  # lint (line length 100)
uv run mypy          # type check (strict)
uv add <pkg>         # add a dependency (--dev for tooling)
```

## Read before designing or coding anything

1. `docs/architecture.md` — the technical design. §2 (design principles) and §6 (the four control boundaries) are **locked decisions**; do not contradict them without an ADR.
2. `docs/internal/roadmap.md` — phases with scope/non-goals/exit criteria. Do not build ahead of the current phase or violate a phase's non-goals.
3. `docs/internal/related-projects.md` — prior-art survey; check it before proposing "new" mechanisms.

**`docs/internal/` does not ship.** Roadmap, prior-art research, design specs and historical test
reports live there because they are working material, not documentation for users. Everything else
under `docs/` is public: `architecture.md` is the single technical design document,
`scenario-contract.md` is the authoring contract, and `adr/` holds the decision records.

## Non-negotiable design principles (summary — full text in architecture.md §2)

- **Black-box sovereignty:** agents are configured only at birth. After that: influence by message, control by kill. Never build mid-run steering/reconfiguration of an agent.
- **Control the world, not the agent:** all enforcement happens at exactly four boundaries — model proxy (budgets), message bus (visibility), filesystem (storage), container (life/death).
- **Physics vs Law:** rules are either technically impossible (physics) or forbidden-but-possible and referee-penalized (law). The *user* chooses which, per project. Don't "helpfully" turn law into physics.
- **Always-on, parallel, no turns.** Agent speed is a legitimate competitive advantage.
- **User-owned economics (BYOK):** user API keys live only in the Ledger/LiteLLM proxy — never in any agent container. Agents get scoped virtual keys.
- **In-realm adversarial behavior is a feature** (deception, shared-folder poisoning) when the user leaves the surface open. Hard guarantees exist only at the realm boundary (isolation, key protection, host safety).
- **Everything is chronicled:** every message/event/spend lands in the append-only chronicle. Any new event source must feed it from day one.

## Architecture shorthand (names used everywhere)

Modular monolith (Python/FastAPI) with modules: **Gatekeeper** (API), **Forge** (provisioner + `RuntimeAdapter`s), **Herald** (Matrix bus AppService), **Warden** (lifecycle/termination), **Ledger** (LiteLLM budgets/keys), **Chronicle** (event log), **Realmtools** (the MCP server agents reach — including the referee's privileged Arbiter tools, separated by token). External: Postgres, Conduit (Matrix), LiteLLM proxy, Docker.

## Tech stack (decided)

Python 3.12 + FastAPI + Pydantic · Postgres 16 · Docker Engine API (docker-py) · Conduit (Matrix, client-server API) · LiteLLM proxy · MCP Python SDK · Typer CLI (`pit`) · a dependency-free web console (vanilla JS — the realm CSP blocks external hosts) · uv, ruff, pytest, mypy · docker compose.

## Conventions

- **ADRs:** every irreversible/architectural decision gets `docs/adr/NNN-title.md`. ADR-001 is reserved for the Hermes fork-vs-config decision (Spike S1).
- **Pin everything:** Hermes, Conduit, LiteLLM versions are pinned; upgrades are deliberate PRs, not drift.
- Repo layout: src layout — `src/bearpit/<module>/` with subpackages matching the module names above (see architecture.md §17). Tests in `tests/`, example manifests in `examples/`.
- Commit style: imperative subject, body explains why; small focused commits.
