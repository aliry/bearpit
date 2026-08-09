# ADR-004: Agent tool grants — per-agent capabilities from an extensible registry

**Status:** Proposed · 2026-08-09
**Input:** Feature request (web access + web search, per agent, extensible to many tools and to
external MCP servers); survey of the Realmtools tool surface, `core/plugins.py`, the token
contract in `realmtools/tokens.py`, and the four control boundaries (architecture §6).

## Context

Agents today have no web access of any kind. Their entire tool surface is the Realmtools MCP
server — `run_code`, `remember`/`recall`, `send_private`, sealed submissions, and the referee's
Arbiter tools — and every shipped scenario runs at `network_egress: model_only`, so the container
has no route to the internet at all. On the subscription pipeline the shim passes `--tools ""`,
which strips the backend's own built-in search from the prompt as well.

That surface is also **uniform**: every agent in a realm gets the same tools. Nothing in the
platform can say *this* agent may search the web and *that* one may not, which rules out a whole
class of scenario — asymmetric information, where a research advantage is the thing being played
for.

`AgentSpec` has carried an inert stub for this since the schema was written:

```python
tools: list[ShortText] = Field(
    default_factory=list, max_length=50,
    description="MCP tool-server refs = this agent's loadout."
)
```

Nothing reads it, no example sets it, and every shipped scenario leaves it empty — so there is no
existing behaviour to preserve, exactly as with `parameters` before ADR-003.

Two decisions constrain the solution space before we start. ADR-002 says any capability an agent
needs arrives as **a tool, skill, or plugin** — never control-plane special-casing. Architecture §6
says everything the platform enforces maps to one of four boundaries, and a capability that maps to
none of them is *law*, not physics.

## Decision

**Agents are granted tools by name, per agent, from a registry any package can extend. Granted
tools execute on the host and are brokered to the agent; the agent's container gains no new
authority.**

### 1. The grant

`agent.tools` becomes live: a list of tool names, namespaced `family.verb`.

```json
"agents": [
  { "id": "analyst", "tools": ["web.search", "web.fetch"] },
  { "id": "rival",   "tools": ["web.search"] },
  { "id": "sealed",  "tools": [] }
]
```

Names are strings, like `skills`, so the picker stays a multi-select and a manifest stays
diffable. **Per-tool configuration is realm-level**, in a new `spec.tools` block keyed by tool
name, and applies to every agent granted that tool:

```json
"spec": {
  "tools": {
    "web.fetch":  { "allow": ["*.wikipedia.org", "arxiv.org"], "max_calls_per_agent": 20 },
    "web.search": { "max_calls_per_agent": 10 }
  }
}
```

The split is the physics/law split the platform already uses everywhere: **the scenario sets the
policy, the agent holds the grant.** It also keeps the two things that change for different
reasons apart — an author swaps which agent researches far more often than they retune a domain
allowlist — and it gives external MCP servers a home when they arrive (§ Non-goals).

A `spec.tools` entry for a tool no agent is granted is an **error**, for the reason the schema
already gives about spec-level `duration`: *two ways to say the same thing, one of them silently
inert, is exactly how a scenario ends up with no backstop at all.*

### 2. The registry: one seam, `bearpit.tools`

A tool is contributed through an entry point in the `bearpit.tools` group, mirroring
`bearpit.providers` exactly — including its failure rule: **a plugin that fails to import, or
raises, is logged and skipped; a third-party package must never stop the platform from starting.**

A `ToolProfile` declares:

| field | purpose |
|---|---|
| `name` | namespaced id, e.g. `web.search` |
| `label`, `description` | the picker's label; the description the *agent* sees in its tool list |
| `params` | JSON Schema for the call arguments |
| `config_schema` | JSON Schema for this tool's `spec.tools[name]` block |
| `api_key_ref` | keystore handle it needs, or `None` |
| `risk` | `contained` \| `elevated` — drives the launch gate (§7) |
| `cost_per_call_usd` | for quotas and the run record |
| `handler(args, config, ctx)` | async; **runs on the host** |

`web.fetch` ships in-tree: it needs no key and no third party, so the platform should not require
an install to be useful. **`web.search` ships as a separate public plugin** (`bearpit-websearch`,
Brave-backed), because the search backend is a vendor choice with a vendor key, and baking one into
core makes that choice look like the platform's rather than the operator's.

### 3. Execution: brokered by the host, never in the container

`run_code` and `send_private` already solved this shape and it is reused verbatim: **Realmtools
records the intent, the host performs it, and answers.** A `TOOL_CALL` event carries the verified
caller, tool name and arguments; the host — which holds the keystore and the only internet route —
executes the profile's handler and writes `TOOL_RESULT`.

Realmtools itself gets no network egress and no API keys, for the reason it holds no Docker socket
today: *a socket there would turn any bug in this small server into host root.* The same logic
applies to a search key.

The agent container's `network_egress` is **unchanged** — `model_only`, no route out. A brokered
tool is the platform acting on the agent's behalf, under the platform's credentials, in the
chronicle. That is a Model-proxy/Filesystem-class control, not a Container-class one, so §6 holds
without amendment.

### 4. Grants travel in the signed token

`mint_token()` gains a `grants` field, extending the precedent `is_referee` already set: **authority
lives in the signed token, never in a tool argument.** An agent that guesses a tool name it was not
granted is refused, exactly as it cannot submit as a peer.

The payload becomes `realm:agent:role:roster:grants`. A missing fifth field parses as *no grants*,
so an old token stays valid and the cutover is not a hard one — but note that this code runs in
**both** the host Forge and the Realmtools container, and the two must be deployed together.

### 5. An agent sees only the tools it holds

Tool *listing* is filtered by the caller's token, not just tool *invocation*. This is not polish.
Issue #41 established that idle tools tempt agents into misusing them and waste turns; a tool that
appears in the list and then refuses is worse than one that was never offered, because the agent
spends a turn discovering it and may retry.

FastMCP's support for per-request tool filtering is the one **implementation risk** in this ADR. If
it cannot filter per token, the fallback is a per-agent mount path (`/mcp/<token>`) that Realmtools
routes itself — more surface, same guarantee. This must be settled by a spike before the rest is
built, because it decides the server's shape.

### 6. Cost: quotas now, dollar budgets later

Tool calls cost real money, and the per-agent `budget` is a LiteLLM virtual key that meters *model*
spend at the proxy — it cannot see a search API bill.

v1 enforces **call quotas**, not dollars: `max_calls_per_agent`, defaulting from the tool profile,
enforced at the broker where it is genuinely physics. Every call is chronicled with its declared
cost, and the run record reports tool spend per agent alongside model spend.

Folding tool spend into the agent's budget cap is deliberately deferred: it needs the Ledger to
meter something the proxy never sees, which is its own piece of work. Until then a quota is honest
about being a quota, which is better than a dollar figure that only counts half the spending.

### 7. Launching: consent scaled to blast radius

Two tiers, because a warning shown on every research scenario stops being a warning — the lesson
from #47.

- **`contained`** (`web.search`, `web.fetch`): metered, chronicled, no isolation change. Launches
  silently. The grants are visible on the scenario card, in the agent editor, and in the run record.
- **`elevated`** (any plugin that declares it; container egress and external MCP servers when they
  land): launching returns **400** listing exactly which agent gets which tool, and proceeds only on
  `allow_elevated_tools=true` — the same structured-refusal shape as an unfilled required parameter
  (ADR-003) and a substituted provider (#47), reusing that machinery rather than inventing a third.

The tier is declared by the tool, so a contributed plugin can put itself behind the gate without
the platform knowing anything about it.

### 8. `web.fetch` and the server-side request forgery problem

Host-brokering is what makes this safe from the *container's* point of view, and it is exactly what
makes it dangerous from the *host's*. The host can reach the operator's LAN, the control plane on
loopback, and cloud metadata endpoints that need no credentials at all. An agent choosing the URL
is an untrusted party choosing where the host connects.

Every one of these is required, and each needs a test that fails without it:

- **Scheme allowlist** — `http`, `https`. Nothing else, and no credentials in the URL.
- **Address validation after DNS resolution** — reject if *any* resolved address is loopback,
  private (RFC1918), link-local (incl. `169.254.0.0/16` and `fe80::/10`), unique-local (`fc00::/7`),
  multicast, or otherwise reserved.
- **Connect to the validated address**, with the `Host` header set — resolving and then handing the
  hostname to the HTTP client re-resolves it and reopens DNS rebinding.
- **Re-validate every redirect**, capped at 3. A permitted host redirecting to `127.0.0.1` is the
  standard bypass.
- **No inherited authority** — no cookie jar, no auth headers, no proxy credentials.
- **Bounded response** — 10s timeout, 256 KB cap, `text/*` + JSON/XML content types only, returned
  as text. Never binary.
- **Optional domain allowlist** — `spec.tools["web.fetch"].allow`. When set, only those hosts.
- **Chronicled** — the requested URL, every redirect hop, the final URL, status and byte count.

The realm-boundary guarantee this preserves is the one SECURITY.md already makes: in-realm
adversarial behaviour is a feature; *host* safety is not negotiable.

### 9. UI

The agent editor gains a **Tools** field that is the existing Skills control — chips with a
remove `✕`, plus an "add tool…" select — because that pattern is already learned and inventing a
second multi-select for the same job is how an editor becomes inconsistent. Each chip's ⓘ gives
the tool's description, cost per call and tier; `elevated` tools carry the amber treatment already
used for a substituted provider.

The picker lists what is actually **installed and ready**: a tool whose keystore handle is missing
is shown disabled with its setup hint, exactly as an unconfigured provider is on the Settings page.
A Settings **Tools** panel lists installed tool plugins, their handles and readiness, mirroring
Model pipeline.

The run record shows, per agent, the tools granted, calls made, and cost.

### 10. The Scribe

The scenario builder must be able to grant tools, and must not be able to invent them.

- A new authoring tool `list_tools()` returns the live registry — name, description, tier, and
  whether its key is configured. The Scribe can only grant what exists, and can tell the user when
  a tool would need a key they have not added.
- `persona.md`'s agent template gains `"tools": [...]`, and the guided create/edit flows raise
  tools when the user describes research, current events, or outside data.
- `validate.py` treats an unknown tool name as a draft problem, like an unknown skill.

### 11. Scenario contract

A new invariant: **a scenario that tells an agent to use a tool must grant that tool.** Prose
ordering an agent to "search for the latest figures" when it holds no `web.search` is the same
defect as the skills bug this codebase already paid for — the prompt kept ordering agents to load a
skill that was never delivered.

## Alternatives considered

**Per-agent container egress (`net.open`) in v1.** Rejected for now, deliberately, and not because
it is unattractive: it is a *real* capability where brokering is a narrow one — any protocol, any
client, `pip install`, the agent's own tooling. But it removes the realm-isolation guarantee for
that agent, and the honest version of that feature needs its own design: what a compromised or
merely adversarial agent can then reach, whether an egress proxy with an allowlist is the answer
(today `network_egress: allowlist` is explicitly rejected as unimplemented for exactly this reason),
and how exfiltration is detected rather than merely permitted. The `elevated` tier and its consent
gate are built now so that work has somewhere to land.

**Provider-native web search.** Cheapest — no key, no billing surface — and rejected: availability
varies by pipeline and there is none at all on the subscription shim, so the same scenario would
behave differently on different pipelines. That directly contradicts architecture §2.8, *"switching
providers is a config change and not a behaviour change."*

**One hardcoded search backend.** Fastest to ship and it forfeits the request: "extend in future to
a lot more tools, even external MCP servers" *is* the seam, so building the seam is the feature.

**Tools as skills.** Skills are text the agent reads. A tool is an executable capability with a
credential and a bill. Conflating them puts an API key's lifecycle inside a markdown file.

**Realm-level grants only.** Simpler, and it forfeits asymmetry — one agent who can research and one
who cannot is a scenario primitive, not a configuration corner.

## Non-goals (this ADR)

- **Container egress / `net.open`.** Deferred as above; needs its own ADR.
- **External MCP servers.** The grant namespace (`mcp.<name>`) and the `spec.tools` config block are
  designed to hold them, but the trust model — a third party that sees realm content, holds its own
  credentials, and can change behaviour under you — is a separate decision.
- **Dollar budgets for tool spend** through the Ledger (§6).
- Tools for the Scribe's own authoring session; that is a different surface with a different threat
  model.

## Consequences

- `agent.tools` stops being inert. Every shipped scenario has `tools: []`, so nothing changes for
  them and the scan-vs-declare problem ADR-003 had does not arise: grants are always explicit.
- The token gains a field, so **host and Realmtools container must be deployed together** — the
  hazard that has bitten this repo before.
- A tool plugin that is uninstalled leaves a scenario granting a tool that no longer resolves. That
  is the #47 failure in a new place, and it gets the #47 treatment: name it at launch rather than
  dropping the grant silently.
- The chronicle gains `TOOL_CALL` / `TOOL_RESULT`; per the "everything is chronicled" principle
  these feed the report from day one.
- A realm can now spend money outside the model proxy for the first time. Quotas bound it; the run
  record makes it visible.
