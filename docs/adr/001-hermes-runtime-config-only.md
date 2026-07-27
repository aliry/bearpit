# ADR-001: Hermes Agent as v1 runtime — config-only integration (no fork)

**Status:** Accepted · 2026-07-05
**Input:** Spike S1 (findings summarised below), source exploration of hermes-agent
`v2026.7.1` (commit `7c1a029`).

## Context

AgentRealm's Forge must birth Hermes Agent instances fully configured (identity/goals,
MCP tool loadout, model endpoint → LiteLLM virtual key, Matrix bus credentials, seed
skills, state volume) with **no mid-run control** — per the black-box principle. The open
question (roadmap Spike S1) was whether this requires config alone, a thin patch, or a
maintained fork.

## Decision

**Integrate the stock Hermes Agent Docker image, pinned to `v2026.7.1`, via configuration
only.** The Forge Hermes adapter generates, per agent: a `HERMES_HOME` volume containing
`SOUL.md` (identity), `config.yaml` (system-prompt extension, model/custom+base_url, MCP
loadout, skills dirs, aux-provider pinning, memory/idle knobs), and `.env` (virtual key,
Matrix credentials). No fork, no patches, no upstream surgery.

Every AgentRealm-required injection point verified as config/env/volume-achievable —
matrix with citations in the spike findings. The only PATCH-level capability found
(stripping Hermes's built-in guidance scaffolding from the system prompt) is **not
required**: we define the persona and append operator law; Hermes's own scaffolding is
part of the runtime we chose.

Version upgrades are deliberate events: re-run the S1 config-surface checklist against the
new tag before repinning (the config surface — SOUL.md, `agent.system_prompt`,
`skills.external_dirs`, Matrix env vars, `HERMES_HOME` — is the contract we depend on).

## Alternatives considered

- **Thin fork with a control API** — rejected: violates black-box sovereignty (we don't
  want a control API), adds permanent upstream-merge burden against a fast-moving repo
  (~22 releases/yr), and S1 found no gap that requires it.
- **Different v1 runtime** (Claude Agent SDK harness, AgentScope agents) — not needed;
  remains the documented pivot if a future Hermes release breaks the config contract.
  AgentScope arrives anyway as adapter #2 in v3 ("Open Realm").

## Consequences

- Adapter work is config-generation + container orchestration only — Phase 1 POC can start
  immediately (prerequisite: Docker Desktop on the dev machine).
- We accept Hermes's built-in prompt scaffolding and its behavioral evolution across pins
  as part of the runtime's character.
- Design constraints adopted from S1 caveats (C1–C9): no `_`-prefixed Matrix localparts;
  aux-provider pinning + no extra provider keys (leak containment, with `model_only`
  egress as physics backstop); Herald pre-creates all rooms including agent-pair DMs;
  `MATRIX_E2EE_MODE=off` (AppService observability); mention-gating vs free-response is a
  per-realm choice with known infinite-loop risk in free mode (budget caps backstop);
  `agent.api_max_retries: 1`; no Honcho; idle knobs (`session_reset`, memory nudges) set
  per realm cost policy.
