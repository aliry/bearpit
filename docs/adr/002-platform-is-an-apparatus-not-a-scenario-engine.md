# ADR-002: The platform is an apparatus, not a scenario engine

**Status:** Accepted · 2026-07-15
**Input:** Owner direction during the #34 (game-master pattern) design discussion, after a
proposal drifted toward the platform computing scenario outcomes. Sharpens architecture
Principle 9 and adds Principle 10; clarifies FR-13 and §9.5.

## Context

AgentRealm exists to **observe whether autonomous agents can accomplish assigned tasks** in a
shared world. While generalizing the neutral game-master pattern (#34), two proposed directions
would have had the **platform** (or a platform-run non-agent process) read scenario submissions,
run a scenario's adjudication logic, and compute its result — to route around LLM-referee
unreliability seen in the Border States run (order-label drift, transcription churn).

That is the wrong instinct. If the platform does the scenario's work, it **hides the very thing
the platform is built to measure**: whether the agents themselves — with their tools, skills, and
prompts — can run the task. The existing Principle 9 ("mechanics are physics, not prompts") and
FR-13 ("the platform … computes the *result*", "no LLM free-computation … referee") read as if the
platform should own scenario logic. That framing is misleading.

## Decision

**The platform is scaffolding and a standard agentic surface — never a scenario engine.**

- The platform provides: container lifecycle, the message bus (Matrix), file sharing, budgets/keys,
  the chronicle, and a set of **built-in tools, skills, and plugins** available to agents.
- **Agents drive all scenario-specific logic and state**, through the tools/skills/plugins they
  invoke and control. The platform never runs a scenario's logic autonomously, computes its outcome
  outside an agent's invocation, or inserts a **non-agent actor** to do an agent's job.
- Any capability a referee or agent needs is delivered as a **tool, skill, or plugin** (standard
  agentic contracts) — never platform-side special-case logic. Generic mechanics that ship with the
  platform (sealed-submit escrow, verifiable draw, built-in tally rulesets like `plurality` /
  `dominance` — GENERIC, no single game's rules —, `run_code`) are fine; **embedding one scenario's rules in the control plane is not.**
- **LLM nondeterminism is the phenomenon under study**, not noise to engineer away. A missed tool
  call, the wrong skill, or a bad judgment is signal. Improve it through better models, prompts,
  tools, and skills. A scenario that still cannot run reliably is a **valid finding** (not yet
  achievable with today's LLMs), not a platform defect.

Deterministic mechanics remain **physics** — but "physics" means the tool's *guarantee* is real (a
sealed payload truly cannot be read early; a draw is truly fair). The **agent invokes** the tool and
chooses its payload; the platform does not adjudicate on the agent's behalf. A referee driving
adjudication by *calling* a deterministic scorer or `run_code` is correct tool use; performing that
computation *in prose* — when a tool exists — is the only thing forbidden.

Recorded as architecture Principle 10; Principle 9, FR-13, and §9.5 updated to match.

## Consequences

- **#34 solution space is constrained.** Ruled out: a non-agent process as the referee, and any
  platform-autonomous adjudication. The game-master pattern must be generalized as **agent-invoked
  tools / skills / plugins** — the agent stays in control.
- Border States' shape (an LLM referee invoking a shipped resolver via `run_code`) is **aligned** —
  its failures (transcription) are signal to reduce via better tools/skills/prompts, not by moving
  the work off the agents.
- Reliability work shifts from "make the platform do it" to "give the agents better tools, skills,
  and prompts, and measure what remains."
- A future **plugin** contract (custom, agent-invoked mechanics/scorers a scenario ships) is the
  sanctioned way to deliver bespoke deterministic logic — provided the *agent* invokes it and the
  control plane hosts no scenario-specific rules.
