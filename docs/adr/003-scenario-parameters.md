# ADR-003: Scenario parameters — inline placeholders, bound before the run snapshot

**Status:** Accepted · 2026-08-05
**Input:** Feature request; survey of every text-bearing field in `core/schema.py`; the
replay contract in `POST /api/realms/{id}/rerun`.

## Context

A scenario is a fixed artifact. Running "the same duel, but to 25 points instead of 10", or
"the same negotiation, but the opponent is hostile", means editing `project.json`, running,
and editing it back — which loses the original, produces no record of what varied, and makes
two runs of "the same scenario" quietly incomparable.

`ProjectSpec` has carried an inert stub for this since the schema was written:

```python
parameters: dict[str, Any] = Field(
    default_factory=dict, description="Template params filled at instantiation (deferred; #25)."
)
```

Nothing read it and no example used it, so there is no existing behaviour to preserve.

## Decision

**Scenario text may contain `${...}` placeholders. They are discovered by scanning the prose,
bound to values at load time, and the bound project is what runs.**

### Notation

```
${name}                       name only — no default
${name,default}               with a default
${name,default,description}   with a description
${name,,description}          description, no default
$${name}                      escaped: renders a literal ${name}
```

Parsed by splitting on unescaped commas into at most three parts, so the description — being
last — may contain commas freely. Inside a placeholder, a backslash escapes the next
character, so a default containing a comma or brace is written `${greeting,Hello\, Vela}`.

Names must match `[A-Za-z_][A-Za-z0-9_]*`. Anything else (`${1bad}`, `${a b}`) stays literal
text, so no existing scenario becomes accidentally parameterised.

An **empty middle part means no default**, not "the default is the empty string". The two
differ only in whether the author is warned at launch, and warning is the safer reading.

### Scope: what an agent reads, never what the platform executes

Substituted: `meta.description`, `spec.goals`, `spec.guidelines`, `spec.restrictions`,
`agent.description`, `.persona`, `.goals`, `.responsibilities`, `.rubric`, `.resource_files`,
`.local_skills`.

Not substituted: ids, `agent.name`, model refs, `api_key_ref`, budgets, skill refs, mechanic
config, and `termination.pattern`.

`termination.pattern` is the sharp one and the reason the line is drawn here rather than
"every string". It is a **regular expression**, in which `${x}` is already valid syntax.
Substituting it would silently rewrite a termination condition, and the failure mode is a
realm that never ends — the exact class of bug that #30 was.

### Discovery is by scan; the manifest only overrides

The scan over prose is the source of truth for **which parameters exist**. `spec.parameters`
is optional and layers on top:

| | |
|---|---|
| inline | name, default, description |
| `spec.parameters` | `default`, `description` (both override inline), plus `type`, `choices`, `multiline`, `min`/`max`, which have no inline form |

**The manifest wins on every field it specifies.** This was chosen over erroring on conflict:
an override layer is predictable, and a scenario shared between people often wants a local
default without editing the prose.

The cost is real and is mitigated rather than denied: prose reading `${target_score,10}` can
have an effective default of `25`. So `pit params` and the launch form show the effective
value **and its origin** — `25 (manifest, overrides inline 10)` — turning a silent override
into a visible one.

Two validations prevent rot:

- a `spec.parameters` entry for a name that appears in no text is an **error**. The schema's
  own note about spec-level `duration` says it plainly: *"Two ways to say the same thing, one
  of them silently inert, is exactly how a scenario ends up with no backstop at all."*
- a default not among its own `choices` is an **error**, checked against whichever default wins.

Conflicting inline defaults for one name (`${x,1}` … `${x,2}`) are an **error**; the same name
with a default in one place and none in another is fine.

### Binding happens before the run snapshot

`bind(project, values) -> Project` runs at load, before the resolved project is chronicled.

Every run already records its fully-resolved project, and `rerun?mode=snapshot` replays from
that record. Binding first means a replay reproduces the exact parameter values with no
additional machinery, while `mode=latest` re-reads the package and re-prompts, pre-filled from
the previous run. Binding after the snapshot would have required parameters to be captured,
versioned and re-applied separately — a second replay path, and a second thing to get wrong.

Binding re-runs model validation, because substitution can push a field past its `max_length`.

### Missing values: warn, confirm, then empty

A parameter with no effective default and no supplied value does not silently vanish and does
not hard-fail. The launcher lists what is missing and where each is used, and proceeds only on
an explicit confirmation, substituting the empty string.

Non-interactive callers cannot answer a prompt, so consent is explicit there: `--yes` on the
CLI, a field on the API. Without it, no realm starts. A realm that spends real money on prose
with holes in it should be a decision, not an accident.

## Alternatives considered

**Declared-only parameters** (`spec.parameters` defines, `${name}` references). Richer metadata
and a typo is caught immediately, but every parameter needs ceremony before first use and the
default sits far from the text that reads it. Rejected as too heavy for the common case of one
or two knobs.

**Substituting every string field.** Would allow parameterising models, budgets and termination
phrases, but corrupts `termination.pattern` as described, and a parameterised `agent.id` breaks
the container and room naming a realm is keyed on.

**Hard-failing on a missing value.** Safest, and rejected deliberately: it makes a scenario with
any required parameter impossible to launch with a bare `pit up <path>`, which is how most
scenarios are first tried.

## Consequences

- A typo (`${targt_score}`) creates a new parameter rather than an error. `pit params` and the
  launch form are the mitigation: it surfaces as an unexpected input rather than staying invisible.
- Parameter values are chronicled on the `running` lifecycle event, so what varied is part of
  the permanent record and shows in the report.
- `type`/`min`/`max` shape the input control and validate entry; every value is still
  interpolated as text, because everything in scope is prose.
- Scenarios without placeholders are unaffected: the scan finds nothing and binding is a no-op.
