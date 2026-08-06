"""Scenario parameters — `${name,default,description}` placeholders bound per run (ADR-003).

A scenario is otherwise a fixed artifact: running "the same duel, but to 25 points" means
editing `project.json`, running, and editing it back. That loses the original, records nothing
about what varied, and makes two runs of "the same scenario" quietly incomparable.

Three rules from the ADR, because each one is load-bearing and none is obvious from the code:

1. **The scan is the source of truth.** A parameter exists because it appears in the prose.
   `spec.parameters` cannot introduce one; it only layers metadata over what the scan found, and
   declaring one that appears nowhere is an error — the schema's own note about spec-level
   `duration` explains why inert fields are worse than missing ones.

2. **The manifest overrides inline.** Both `default` and `description` may be given in either
   place, and the manifest wins. That is deliberately NOT the repo's usual one-home-per-fact
   rule, so the resolved value carries where it came from (`origin_*`) and every surface shows
   it — an override the author cannot see is the whole risk of this design.

3. **Prose only, never executable fields.** `termination.pattern` is a regex in which `${x}` is
   already valid syntax; substituting there would silently rewrite a termination condition and
   produce a realm that never ends. That was #30.

This module is pure: no IO, no schema mutation. `bind()` returns a NEW project and re-validates
it, because substitution can push a field past its `max_length`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bearpit.core.schema import Project

# `$${` is the escape for a literal `${`. Matched FIRST so it never becomes a placeholder.
# A placeholder body is any run of (escaped-char | not-backslash-and-not-brace), which lets a
# default or description contain `,` `}` `\` when backslash-escaped.
_TOKEN = re.compile(r"\$\$\{|\$\{((?:\\.|[^\\}])*)\}")
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

VALUE_TYPES = ("string", "int", "number", "bool")


def _unescape(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text)


def _split_unescaped(body: str, limit: int) -> list[str]:
    """Split on unescaped commas into at most `limit` parts.

    The final part keeps its commas, so a description reads naturally:
    `${t,10,Points needed, before the bell}` -> ["t", "10", "Points needed, before the bell"]
    """
    parts: list[str] = []
    buf: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            buf.append(ch)
            buf.append(body[i + 1])
            i += 2
            continue
        if ch == "," and len(parts) < limit - 1:
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return parts


@dataclass(frozen=True)
class Placeholder:
    """One `${...}` occurrence, as written."""

    name: str
    default: str | None  # None = no default given (an EMPTY middle part is still None)
    description: str | None


def parse_placeholders(text: str) -> list[Placeholder]:
    """Every valid placeholder in a string, in order. Invalid ones are ignored.

    A body whose name does not match `[A-Za-z_][A-Za-z0-9_]*` is left alone rather than
    reported: `${1bad}` and `${a b}` are plain text, so no existing scenario becomes
    accidentally parameterised by this feature shipping."""
    found: list[Placeholder] = []
    for m in _TOKEN.finditer(text):
        body = m.group(1)
        if body is None:  # a `$${` escape
            continue
        parts = _split_unescaped(body, 3)
        name = parts[0].strip()
        if not _NAME.match(name):
            continue
        raw_default = parts[1] if len(parts) > 1 else ""
        # An empty middle means NO default, not "the default is empty". The two differ only in
        # whether the author is warned at launch, and warning is the safer reading.
        default = _unescape(raw_default) if raw_default != "" else None
        description = _unescape(parts[2]).strip() if len(parts) > 2 and parts[2].strip() else None
        found.append(Placeholder(name=name, default=default, description=description))
    return found


def substitute(text: str, values: dict[str, str]) -> str:
    """Replace every valid placeholder with its value; unescape `$${` to `${`.

    A name missing from `values` becomes the empty string — callers decide whether that is
    acceptable (see `missing_values`), so this stays total and never raises mid-render."""

    def repl(m: re.Match[str]) -> str:
        body = m.group(1)
        if body is None:
            return "${"
        parts = _split_unescaped(body, 3)
        name = parts[0].strip()
        if not _NAME.match(name):
            return m.group(0)  # not a placeholder; leave exactly as written
        return values.get(name, "")

    return _TOKEN.sub(repl, text)


@dataclass(frozen=True)
class Parameter:
    """A parameter as resolved for one scenario: what it is, and where each fact came from."""

    name: str
    default: str | None = None
    description: str | None = None
    type: str = "string"
    choices: list[str] | None = None
    multiline: bool = False
    minimum: float | None = None
    maximum: float | None = None
    # "inline" | "manifest" | None — surfaced everywhere, because a manifest default silently
    # overriding the one written in the prose is the known cost of the override design.
    default_origin: str | None = None
    description_origin: str | None = None
    inline_default: str | None = None
    occurrences: list[str] = field(default_factory=list)  # dotted field paths, in scan order

    @property
    def required(self) -> bool:
        """No effective default: the author must supply a value or explicitly accept empty."""
        return self.default is None

    @property
    def overridden(self) -> bool:
        """The manifest changed a default the prose already stated."""
        return (
            self.default_origin == "manifest"
            and self.inline_default is not None
            and self.inline_default != self.default
        )


class ParameterError(ValueError):
    """A scenario's parameters are inconsistent. Raised at load, never mid-run."""


def _prose_fields(project: Project) -> list[tuple[str, str]]:
    """(dotted path, text) for every field a parameter may fill.

    Prose an agent READS. Never ids, model refs, budgets, mechanic config, or
    `termination.pattern` — see the module docstring."""
    out: list[tuple[str, str]] = []

    def add(path: str, value: Any) -> None:
        if isinstance(value, str) and value:
            out.append((path, value))

    add("metadata.description", project.metadata.description)
    for i, g in enumerate(project.spec.goals):
        add(f"spec.goals[{i}]", g)
    add("spec.guidelines", project.spec.guidelines)
    add("spec.restrictions", project.spec.restrictions)
    for a in project.agents:
        add(f"agents.{a.id}.description", a.description)
        for i, g in enumerate(a.goals):
            add(f"agents.{a.id}.goals[{i}]", g)
        for i, r in enumerate(a.responsibilities):
            add(f"agents.{a.id}.responsibilities[{i}]", r)
        add(f"agents.{a.id}.persona", a.persona)
        add(f"agents.{a.id}.rubric", a.rubric)
        for name, body in a.resource_files.items():
            add(f"agents.{a.id}.resource_files[{name}]", body)
        for name, body in a.local_skills.items():
            add(f"agents.{a.id}.local_skills[{name}]", body)
    return out


def scan(project: Project) -> list[Parameter]:
    """Every parameter this scenario takes, resolved and ordered by first appearance.

    Raises ParameterError when the scenario is internally inconsistent — conflicting inline
    defaults, a manifest entry for a parameter that appears nowhere, or a default outside its
    own `choices`. All three are author mistakes that must surface at load rather than as
    strange prose halfway through a run."""
    inline_default: dict[str, str] = {}
    inline_desc: dict[str, str] = {}
    occurrences: dict[str, list[str]] = {}
    order: list[str] = []

    for path, text in _prose_fields(project):
        for ph in parse_placeholders(text):
            if ph.name not in occurrences:
                occurrences[ph.name] = []
                order.append(ph.name)
            if path not in occurrences[ph.name]:
                occurrences[ph.name].append(path)
            if ph.default is not None:
                prior = inline_default.get(ph.name)
                if prior is not None and prior != ph.default:
                    raise ParameterError(
                        f"parameter {ph.name!r} has two different inline defaults: "
                        f"{prior!r} and {ph.default!r}. Give it one, or move the default to "
                        f"spec.parameters."
                    )
                inline_default[ph.name] = ph.default
            if ph.description is not None:
                inline_desc.setdefault(ph.name, ph.description)

    declared = project.spec.parameters or {}
    unknown = [n for n in declared if n not in occurrences]
    if unknown:
        raise ParameterError(
            "spec.parameters declares "
            + ", ".join(repr(n) for n in sorted(unknown))
            + " but no scenario text uses "
            + ("them" if len(unknown) > 1 else "it")
            + ". Use ${name} somewhere, or remove the declaration — a parameter nothing reads "
            "is a setting that silently does nothing."
        )

    params: list[Parameter] = []
    for name in order:
        raw = declared.get(name)
        # Normally a validated `ParameterSpec`; a plain dict when scan() is handed a raw doc
        # (the scenario editor previews before the model is built).
        if raw is None:
            spec: dict[str, Any] = {}
        elif isinstance(raw, dict):
            spec = raw
        else:
            spec = raw.model_dump()

        # The manifest wins on both, and the origin rides along so every surface can show it.
        m_default = spec.get("default")
        default = str(m_default) if m_default is not None else inline_default.get(name)
        default_origin = (
            "manifest" if m_default is not None
            else ("inline" if name in inline_default else None)
        )
        m_desc = spec.get("description")
        description = str(m_desc) if m_desc else inline_desc.get(name)
        description_origin = (
            "manifest" if m_desc else ("inline" if name in inline_desc else None)
        )

        vtype = str(spec.get("type", "string"))
        if vtype not in VALUE_TYPES:
            raise ParameterError(
                f"parameter {name!r} has type {vtype!r}; expected one of "
                + ", ".join(VALUE_TYPES)
            )
        raw_choices = spec.get("choices")
        choices = [str(c) for c in raw_choices] if isinstance(raw_choices, list) else None
        if choices is not None and default is not None and default not in choices:
            raise ParameterError(
                f"parameter {name!r} has default {default!r}, which is not one of its choices "
                f"({', '.join(map(repr, choices))})."
            )
        params.append(
            Parameter(
                name=name,
                default=default,
                description=description,
                type=vtype,
                choices=choices,
                multiline=bool(spec.get("multiline", False)),
                minimum=_as_float(spec.get("min")),
                maximum=_as_float(spec.get("max")),
                default_origin=default_origin,
                description_origin=description_origin,
                inline_default=inline_default.get(name),
                occurrences=occurrences[name],
            )
        )
    return params


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def resolve_values(params: list[Parameter], supplied: dict[str, str]) -> dict[str, str]:
    """Effective value per parameter: what was supplied, else its default, else empty."""
    out: dict[str, str] = {}
    for p in params:
        if p.name in supplied and supplied[p.name] != "":
            out[p.name] = supplied[p.name]
        elif p.default is not None:
            out[p.name] = p.default
        else:
            out[p.name] = ""
    return out


def missing_values(params: list[Parameter], supplied: dict[str, str]) -> list[Parameter]:
    """Parameters with no default AND no supplied value — the ones a launcher must warn about."""
    return [p for p in params if p.required and not supplied.get(p.name)]


def validate_values(params: list[Parameter], supplied: dict[str, str]) -> list[str]:
    """Human-readable problems with supplied values. Empty list = fine.

    Checked before a realm is provisioned, so a typo costs a message rather than a container."""
    problems: list[str] = []
    by_name = {p.name: p for p in params}
    for name, value in supplied.items():
        p = by_name.get(name)
        if p is None:
            problems.append(f"{name!r} is not a parameter of this scenario")
            continue
        if value == "":
            continue
        if p.choices is not None and value not in p.choices:
            problems.append(
                f"{name!r} must be one of {', '.join(map(repr, p.choices))} (got {value!r})"
            )
        if p.type in ("int", "number"):
            try:
                num = float(value)
            except ValueError:
                problems.append(f"{name!r} must be a {p.type} (got {value!r})")
                continue
            if p.type == "int" and num != int(num):
                problems.append(f"{name!r} must be a whole number (got {value!r})")
            if p.minimum is not None and num < p.minimum:
                problems.append(f"{name!r} must be at least {p.minimum:g} (got {value!r})")
            if p.maximum is not None and num > p.maximum:
                problems.append(f"{name!r} must be at most {p.maximum:g} (got {value!r})")
        if p.type == "bool" and value.lower() not in ("true", "false", "yes", "no", "1", "0"):
            problems.append(f"{name!r} must be true or false (got {value!r})")
    return problems


def bind(project: Project, values: dict[str, str]) -> Project:
    """A NEW project with every prose field substituted. The bound project is what runs.

    Re-validated through the model rather than mutated in place: substitution can push a field
    past its `max_length`, and a 60k-character persona must fail here — at load, naming the
    field — rather than deep inside provisioning."""
    doc = project.model_dump(mode="json", exclude_none=True)

    def sub(text: Any) -> Any:
        return substitute(text, values) if isinstance(text, str) else text

    meta = doc.get("metadata", {})
    if "description" in meta:
        meta["description"] = sub(meta["description"])
    spec = doc.get("spec", {})
    if "goals" in spec:
        spec["goals"] = [sub(g) for g in spec["goals"]]
    for key in ("guidelines", "restrictions"):
        if key in spec:
            spec[key] = sub(spec[key])
    for agent in doc.get("agents", []):
        for key in ("description", "persona", "rubric"):
            if key in agent:
                agent[key] = sub(agent[key])
        for key in ("goals", "responsibilities"):
            if key in agent:
                agent[key] = [sub(g) for g in agent[key]]

    from bearpit.core.schema import Project as _Project

    bound = _Project.model_validate(doc)

    # `resource_files` and `local_skills` are loader state marked `exclude=True`, so they are NOT
    # in the dump and a round-trip drops them entirely. `_project_snapshot` carries them
    # alongside for exactly this reason; here they are re-attached, substituted, after validation.
    # Missing this silently deletes every reference file and hand-written skill an agent was
    # given — the agent still boots, just without the material it was supposed to read.
    by_id = {a.id: a for a in project.agents}
    for agent in bound.agents:
        original = by_id.get(agent.id)
        if original is None:
            continue
        agent.resource_files = {
            k: substitute(v, values) for k, v in original.resource_files.items()
        }
        agent.local_skills = {k: substitute(v, values) for k, v in original.local_skills.items()}
    return bound


__all__ = [
    "Parameter",
    "ParameterError",
    "Placeholder",
    "VALUE_TYPES",
    "bind",
    "missing_values",
    "parse_placeholders",
    "resolve_values",
    "scan",
    "substitute",
    "validate_values",
]
