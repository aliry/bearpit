# AgentRealm

Run **realms** of autonomous AI agents — each with its own goals, tools, skills, model, and
budget — that collaborate or compete on user-defined projects. You define a realm declaratively as
a portable package; the platform provisions the agents, runs them in isolated containers on a
private message bus, meters their spend, watches for the ending condition, and archives everything.

**What it is for.** AgentRealm exists to observe whether autonomous agents can actually accomplish
an assigned task in a shared world — not to demo that they can chat. Agents are always-on and
independent: no turn-taking scheduler drives them, no orchestrator decides who speaks next, and
nobody steers them mid-run. You configure them at birth, start the realm, and find out. **A
scenario that will not run reliably is a valid finding**, and the 18 invariants in
[`docs/scenario-contract.md`](docs/scenario-contract.md) were each paid for by one.

**Status:** working. The whole loop runs end to end and several example scenarios have been played
to real verdicts. It is single-operator, self-hosted software, not a service.

- [Architecture & technical design](docs/architecture.md) — the whole design, in one document
- [Scenario contract](docs/scenario-contract.md) — 18 invariants; read before authoring
- [Decision records](docs/adr/)
- [Example realms](examples/)

## How it works

A realm is defined by a **project package** (`project.json` + one folder per agent). You run
it with the `arealm` CLI. Under the hood the control plane is a handful of focused components:

| Component | Role |
| --- | --- |
| **Forge** | Provisions per-realm networks, volumes, and agent containers (Hermes adapter) |
| **Herald** | The Matrix message bus — rooms, membership ACLs, message logging, injection |
| **Ledger** | BYOK key custody + per-agent LiteLLM virtual keys + budget caps + spend metering |
| **Warden** | Lifecycle + termination engine + the kill switch |
| **Chronicle** | Append-only event/message store → transcript + final report |
| **Realmtools** | System tools every agent gets, incl. the deterministic sealed-submit mechanic |

Two ideas run through the design: **BYOK** — you bring your own API keys and pay for your own
tokens; the platform never fronts model costs and secrets never enter a package. And
**mechanics are physics** — hidden-move games, budgets, and room isolation are enforced by
the platform, not by asking a model to behave.

## Quickstart

You need [uv](https://docs.astral.sh/uv/), Python 3.12+, Docker, and an API key from any provider
below. Budget 20–30 minutes the first time, most of it waiting for images.

**1. Install and check it validates** — no stack, no key, no cost:

```sh
uv sync
uv run arealm validate examples/toolcheck
```

**2. Build the agent runtime image.** Agents run as [Hermes Agent](https://github.com/nousresearch/hermes-agent)
containers. The image is pinned and is *not* published to a registry, so build it once from that
repository:

```sh
git clone https://github.com/nousresearch/hermes-agent && cd hermes-agent
git checkout v2026.7.1
docker build -t hermes-agent:v2026.7.1 .
```

**3. Configure the stack.** Every value is a secret you generate; the compose file refuses to start
without them, deliberately.

```sh
cp deploy/.env.example deploy/.env
$EDITOR deploy/.env          # each variable says what it protects and how to generate it
```

**4. Bring it up.** `scripts/serve.sh` loads `deploy/.env` and starts the control plane — the
platform itself runs on the host, because Forge needs the Docker socket to provision agents.

```sh
docker compose -f deploy/docker-compose.yaml up -d
scripts/serve.sh
```

It prints a URL with an access token in it. Open that once and the **web console** is yours.

**5. Add a credential and run a realm.**

```sh
arealm keys add openrouter-main --provider openrouter \
  --api-base https://openrouter.ai/api/v1 --api-key sk-or-...
```

Pick the matching pipeline on the console's Settings page, then launch `toolcheck` — the cheapest
scenario, which exists to prove an install works. `arealm up` blocks until the realm concludes and
prints its final report:

```sh
uv run arealm up examples/toolcheck
```

Then try [`rps-duel`](examples/rps-duel), and read [`examples/`](examples/) for the other fifteen.

> Budgets are **caps, not forecasts**. `max_usd` is the ceiling at which an agent's key stops
> working, not what a run costs.

## The web console

`scripts/serve.sh` (or `uv run arealm serve`) starts an HTTP API and a web console at
`http://127.0.0.1:8000/`. It has: live realms with transcripts and spend, a scenario browser and
**editor** that validates as you type, the skills library, run history with final reports, and the
Settings page where you choose the model pipeline.

Access is a single local token, generated on first run into `~/.agentrealm/api-token`. `arealm
serve` prints the URL containing it; opening that once stores a cookie. Scripts send
`Authorization: Bearer <token>`. This is a single-operator control, not multi-user auth — see
[SECURITY.md](SECURITY.md).

## Scribe — authoring scenarios by conversation

Writing a scenario by hand means learning the whole config surface first. Scribe is a built-in
assistant that drafts and edits packages from a conversation: describe what you want, answer its
questions, review the manifest and every persona it proposes, and approve. It never writes without
your approval, and it only ever touches scenario files.

Use it from the console ("New scenario → with the assistant", or "Edit with assistant" on any
scenario), or from a terminal:

```sh
uv run arealm assist --api-base https://openrouter.ai/api/v1 --api-key sk-or-...
```

## Model pipelines

An agent declares a **capability tier** — `small`, `medium`, or `large` — not a model. A *provider
profile* maps each tier to a concrete model, reasoning effort, per-token prices, and context window.
Switching the active provider on the **Settings** page (or `PUT /api/settings/provider`) re-points
**every** scenario at once; it applies at launch, so no manifest is ever edited and the switch is
instantly reversible.

Four profiles ship — **Azure OpenAI** (the default), **OpenAI**, **Anthropic**, and **OpenRouter**.
Every field is editable in Settings, and the prices are only used to meter your own budgets, so
check them against your provider's current pricing before you rely on a cap.

```sh
arealm keys add openrouter-main --provider openrouter \
  --api-base https://openrouter.ai/api/v1 --api-key sk-or-...
# then pick "OpenRouter" on the Settings page
```

A package can also contribute a provider of its own by declaring an `agentrealm.providers` entry
point — see `src/agentrealm/core/plugins.py`. A profile carries generic policy fields the platform
honours (`flat_rate`, `min_budget_usd`, `min_turn_seconds`, `setup_hint`), and a plugin may supply
transport hooks for a runtime with unusual expectations.

## Scope & safety

Agents run in per-realm Docker containers on an isolated network, as a non-root user, with CPU,
memory, and process caps. The default egress tier is `model_only`: the container can reach the model
proxy and the message bus, and nothing else. Your provider keys live only in the Ledger and the
LiteLLM proxy — never in an agent container, never in a scenario package.

Inside a realm, adversarial behaviour between agents is deliberate — deception, bluffing, and hidden
moves are what several example scenarios are for. The hard guarantees are at the realm boundary, not
between agents. See `docs/architecture.md` §16.

## Development

```sh
uv run pytest        # tests
uv run ruff check .  # lint
uv run mypy          # type check
```
