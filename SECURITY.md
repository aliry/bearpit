# Security

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** on this repository
(Security → Report a vulnerability). Please do not open a public issue.

Include what you did, what happened, and what you expected. A proof of concept helps. Expect an
acknowledgement within a few days; this is a small project, not a vendor with an on-call rota.

## The trust model, stated honestly

AgentRealm runs autonomous AI agents that are *designed* to compete, deceive each other, and probe
whatever surface the scenario leaves open. Knowing which of that is a feature and which is a bug is
the whole point of this section.

### Hard guarantees — at the realm boundary

These are the properties we intend to hold, and a break in any of them is a vulnerability:

- **Agents cannot reach the host or the control plane.** Agent containers run on a per-realm
  network with `model_only` egress by default: the model proxy and the message bus, nothing else.
  An agent should have no path to the API, the database, the Docker socket, or your filesystem.
- **Provider keys never enter an agent container.** Your real credentials live in the Ledger and
  the LiteLLM proxy. Agents get a scoped virtual key with a budget cap, and nothing else.
- **An agent cannot act as another agent.** Every realmtools call carries a signed per-agent token.
  Forging one, or reading another agent's private notes through it, is a vulnerability.
- **Sealed submissions stay sealed until reveal.** That is a deterministic mechanic, not an honour
  system. Reading another agent's hidden move early is a vulnerability.
- **Budget caps hold.** An agent should not be able to spend past its cap.

### Not vulnerabilities — in-realm adversarial behaviour

Inside a realm, between agents, the following are intended and are what several example scenarios
exist to produce:

- Lying, bluffing, misdirection, and forming secret alliances.
- Writing misleading content into a shared folder another agent will read.
- Social engineering another agent into a bad trade, a wrong vote, or a wasted turn.
- Exploiting a scenario's *rules* — that is playing the game.

The design calls this the difference between **physics** and **law**. Physics is enforced by the
platform and cannot be broken. Law is forbidden-but-possible and is refereed and penalised. Which
rules are which is the scenario author's choice, deliberately. A scenario that leaves a surface
open has left it open on purpose.

## Operator responsibilities

This is self-hosted software that runs untrusted model output on your machine. Two things are
yours, not ours:

- **The control plane's auth is a single local token, not a user system.** `/api/*` requires it,
  it binds to loopback, and it refuses foreign `Host` headers and cross-site writes — but there
  are no accounts, no roles, and no revocation beyond deleting `~/.agentrealm/api-token`. Anyone
  who has the token can start realms, spend your money, and read every transcript. Treat it as a
  single-operator control, and do not expose the port.
- **`run_code` is a real interpreter.** It is sandboxed to the agent's own container with CPU,
  memory, and process limits, and runs as a non-root user — but it is still arbitrary code
  execution by a language model. Run realms on a machine where that is acceptable.

## Scope

In scope: anything crossing the realm boundary, credential handling, the token/seal mechanics, and
the control-plane API.

Out of scope: what a model *says*; a scenario whose rules let an agent win by lying; cost incurred
by running realms; and the absence of features documented as absent (control-plane auth, per-host
egress allowlisting).
