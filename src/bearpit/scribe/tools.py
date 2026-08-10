"""Authoring tools — the verbs Scribe's loop can call, as thin wrappers over the package store.

Every tool returns a plain string the loop appends to the conversation (the model relays it to the
user). The safety-critical rule (spec §3, §12): `create_scenario` / `edit_scenario` run
`validate_scenario` FIRST and refuse to write when it fails, returning the problems; on success they
`snapshot()` the prior state, then `write()`. `validate_scenario` and `preview_changes` never write.

Edit patch shape (resolves spec §15's open question — concrete and testable):
  {"project": {...json-merge-patch...}}      merge into project.json (metadata/spec); null deletes.
  {"agent": {"id": "<id>", "replace": {...}}}  replace one agent wholesale (append if new).
Both keys may appear in one patch.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import ValidationError

from bearpit.core.package import PackageError
from bearpit.core.schema import Project
from bearpit.core.tools import check_grants, keystore_handles
from bearpit.scribe.store import PackageStore
from bearpit.scribe.types import ToolCall, ToolSpec
from bearpit.scribe.validate import validate_scenario
from bearpit.scribe.versions import diff_projects


class SnapshotSink(Protocol):
    """What the tools need from the version store: snapshot the prior state before a write."""

    async def snapshot(self, name: str, project: Project | None) -> str: ...


class MemoryStore(Protocol):
    """What the tools/loop need from memory (curatable notes)."""

    async def remember(self, text: str, kind: str, tags: list[str] | None = None) -> str: ...

    async def recall(self, limit: int = 20) -> list[str]: ...

    async def search(self, query: str) -> list[str]: ...


_NOT_FOUND = (FileNotFoundError, KeyError, PackageError)


def _merge(target: Any, patch: Any) -> Any:
    """JSON merge patch (RFC 7386): recursive dict merge; a null value deletes the key."""
    if not isinstance(patch, dict):
        return patch
    base = dict(target) if isinstance(target, dict) else {}
    for k, v in patch.items():
        if v is None:
            base.pop(k, None)
        elif isinstance(v, dict):
            base[k] = _merge(base.get(k), v)
        else:
            base[k] = v
    return base


def _apply_patch(manifest: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(manifest)
    project_patch = patch.get("project")
    if isinstance(project_patch, dict):
        out = _merge(out, project_patch)
    agent_patch = patch.get("agent")
    if isinstance(agent_patch, dict):
        aid = agent_patch.get("id")
        replacement = agent_patch.get("replace")
        if isinstance(replacement, dict):
            new_agent = {**replacement}
            new_agent.setdefault("id", aid)
            agents = [dict(a) for a in (out.get("agents") or [])]
            for i, a in enumerate(agents):
                if a.get("id") == aid:
                    agents[i] = new_agent
                    break
            else:
                agents.append(new_agent)
            out["agents"] = agents
    return out


def _format_schema_errors(exc: ValidationError) -> str:
    lines = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(root)"
        lines.append(f"- {loc}: {e['msg']}")
    return "Cannot build a valid scenario:\n" + "\n".join(lines)


def _problems(errors: list[str]) -> str:
    return "Not written — fix these problems first:\n" + "\n".join(f"- {e}" for e in errors)


def draft_problems(spec: dict[str, Any]) -> str | None:
    """Validate a proposed FULL manifest (schema + contract) WITHOUT writing — the check behind
    `propose_scenario`. Returns None when the draft is clean, else the problems for the model to
    fix and re-propose."""
    try:
        project = Project.model_validate(spec)
    except ValidationError as exc:
        return _format_schema_errors(exc)
    result = validate_scenario(project)
    problems = list(result.errors) if not result.ok else []
    # A granted tool that does not exist here passes the schema — the name is well formed — and
    # then does nothing at run time: the agent never sees it while the prose still tells it to
    # look things up. Catching it at proposal time is the difference between a fixable draft and
    # a realm that quietly under-delivers.
    problems.extend(check_grants(project, key_refs=keystore_handles()))
    if problems:
        return "Invalid — fix these problems and re-propose:\n" + "\n".join(
            f"- {e}" for e in problems
        )
    return None


class AuthoringTools:
    """The tool loadout the loop dispatches against a `PackageStore`."""

    def __init__(self, store: PackageStore, versions: SnapshotSink, memory: MemoryStore) -> None:
        self._store = store
        self._versions = versions
        self._memory = memory

    async def dispatch(self, call: ToolCall) -> str:
        """Route a tool call to its handler and return the result string."""
        args = call.arguments or {}
        name = call.name
        if name == "list_scenarios":
            return await self._list_scenarios()
        if name == "read_scenario":
            return await self._read_scenario(str(args.get("name", "")))
        if name == "create_scenario":
            return await self._create_scenario(str(args.get("name", "")), args.get("spec") or {})
        if name == "edit_scenario":
            return await self._edit_scenario(str(args.get("name", "")), args.get("patch") or {})
        if name == "validate_scenario":
            return self._validate_spec(args.get("spec") or {})
        if name == "list_skills":
            return self._list_skills()
        if name == "list_tools":
            return self._list_tools()
        if name == "read_skill":
            return self._read_skill(str(args.get("ref", "")))
        if name == "preview_changes":
            return await self._preview_changes(str(args.get("name", "")), args.get("patch") or {})
        return f"Unknown tool {name!r}."

    async def _list_scenarios(self) -> str:
        return json.dumps(await self._store.list(), indent=2)

    async def _read_scenario(self, name: str) -> str:
        try:
            project = await self._store.read(name)
        except _NOT_FOUND:
            return f"No scenario named {name!r}."
        doc = project.model_dump(mode="json", by_alias=True, exclude_none=True)
        return json.dumps(doc, indent=2)

    async def _create_scenario(self, name: str, spec: dict[str, Any]) -> str:
        name = name or str(spec.get("metadata", {}).get("name", ""))
        if not name:
            return "A scenario needs a name (metadata.name or the `name` argument)."
        try:
            project = Project.model_validate(spec)
        except ValidationError as exc:
            return _format_schema_errors(exc)
        result = validate_scenario(project)
        if not result.ok:
            return _problems(result.errors)
        prior = await self._read_prior(name)
        await self._versions.snapshot(name, prior)
        await self._store.write(name, project)
        return f"Created scenario {name!r} ({len(project.agents)} agents)."

    async def _edit_scenario(self, name: str, patch: dict[str, Any]) -> str:
        try:
            prior = await self._store.read(name)
        except _NOT_FOUND:
            return f"No scenario named {name!r} to edit."
        manifest = _apply_patch(prior.model_dump(by_alias=True, exclude_none=True), patch)
        try:
            project = Project.model_validate(manifest)
        except ValidationError as exc:
            return _format_schema_errors(exc)
        result = validate_scenario(project)
        if not result.ok:
            return _problems(result.errors)
        await self._versions.snapshot(name, prior)
        await self._store.write(name, project)
        return f"Edited scenario {name!r}."

    def _validate_spec(self, spec: dict[str, Any]) -> str:
        try:
            project = Project.model_validate(spec)
        except ValidationError as exc:
            return _format_schema_errors(exc)
        result = validate_scenario(project)
        if result.ok and not result.warnings:
            return "Valid — no problems found."
        parts: list[str] = []
        if result.ok:
            parts.append("Valid, with warnings:")
        else:
            parts.append("Invalid — problems:")
        parts.extend(f"- {e}" for e in result.errors)
        parts.extend(f"- (warning) {w}" for w in result.warnings)
        return "\n".join(parts)

    async def _preview_changes(self, name: str, patch: dict[str, Any]) -> str:
        try:
            prior = await self._store.read(name)
        except _NOT_FOUND:
            return f"No scenario named {name!r} to preview."
        manifest = _apply_patch(prior.model_dump(by_alias=True, exclude_none=True), patch)
        try:
            after = Project.model_validate(manifest)
        except ValidationError as exc:
            return _format_schema_errors(exc)
        return diff_projects(prior, after)

    def _list_tools(self) -> str:
        """What this platform can actually grant, so the assistant cannot invent a tool.

        Readiness travels with each entry: a tool whose key is missing can still be granted — the
        scenario is fine and the operator adds the key — but the user should be told, at the
        moment they ask for it, rather than at launch.
        """
        from bearpit.core.tools import keystore_handles, tool_registry

        have = keystore_handles()
        return json.dumps([
            {"name": p.name, "label": p.label, "description": p.description,
             "risk": str(p.risk), "cost_per_call_usd": p.cost_per_call_usd,
             "ready": not p.api_key_ref or p.api_key_ref in have,
             "needs_key_ref": p.api_key_ref if p.api_key_ref and p.api_key_ref not in have
             else None}
            for p in sorted(tool_registry().values(), key=lambda x: x.name)
        ], indent=2)

    def _list_skills(self) -> str:
        from bearpit.gatekeeper.scenarios import list_skills

        return json.dumps(list_skills(), indent=2)

    def _read_skill(self, ref: str) -> str:
        from bearpit.gatekeeper.scenarios import skill_content

        source, sep, rest = ref.partition(":")
        if not sep:
            source, rest = "builtin", ref
        content = skill_content(source, rest)
        return content if content is not None else f"No skill {ref!r}."

    async def _read_prior(self, name: str) -> Project | None:
        try:
            return await self._store.read(name)
        except _NOT_FOUND:
            return None


def _obj_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


_SPEC_PROP = {
    "type": "object",
    "description": "A full scenario manifest: metadata, spec (goals/guidelines/environment/"
    "termination/mechanics/turns), and an inline agents[] roster (each with id, role, "
    "model_category, persona, goals, and — for a referee — rubric/powers).",
}
_PATCH_PROP = {
    "type": "object",
    "description": "A patch: {'project': {...json-merge-patch on metadata/spec, null deletes...}} "
    "and/or {'agent': {'id': '<id>', 'replace': {...whole agent...}}}.",
}

TOOL_SPECS: list[ToolSpec] = [
    ToolSpec(
        name="list_scenarios",
        description="List existing scenarios with their titles, agent counts, and summaries.",
        parameters=_obj_schema({}),
    ),
    ToolSpec(
        name="read_scenario",
        description="Read one scenario's full manifest (project + agents) as JSON.",
        parameters=_obj_schema({"name": {"type": "string"}}, ["name"]),
    ),
    ToolSpec(
        name="create_scenario",
        description="Create a new scenario. Validated first; refuses to write if invalid.",
        parameters=_obj_schema({"name": {"type": "string"}, "spec": _SPEC_PROP}, ["name", "spec"]),
    ),
    ToolSpec(
        name="edit_scenario",
        description="Edit an existing scenario with a patch. Validated first; refuses if invalid.",
        parameters=_obj_schema(
            {"name": {"type": "string"}, "patch": _PATCH_PROP}, ["name", "patch"]
        ),
    ),
    ToolSpec(
        name="validate_scenario",
        description="Check a scenario manifest against the schema + contract. Never writes.",
        parameters=_obj_schema({"spec": _SPEC_PROP}, ["spec"]),
    ),
    ToolSpec(
        name="list_skills",
        description="List the skills available to wire into agents (builtin + user library).",
        parameters=_obj_schema({}),
    ),
    ToolSpec(
        name="list_tools",
        description="List the tools that can be granted to an agent on THIS platform, with what "
                    "each does, whether its key is configured, and what a call costs. Grant only "
                    "from this list — a tool that is not here does not exist.",
        parameters=_obj_schema({}),
    ),
    ToolSpec(
        name="read_skill",
        description="Read a skill's SKILL.md by ref ('builtin:agent-basics' or a bare name).",
        parameters=_obj_schema({"ref": {"type": "string"}}, ["ref"]),
    ),
    ToolSpec(
        name="preview_changes",
        description="Show the human-readable diff a patch would make to a scenario. Never writes.",
        parameters=_obj_schema(
            {"name": {"type": "string"}, "patch": _PATCH_PROP}, ["name", "patch"]
        ),
    ),
    # The two guided-mode verbs (spec §18). Both END your turn — the loop gives them special
    # handling: ask_user hands the floor to the user; propose_scenario validates without writing
    # and shows a valid draft to the user for approval (errors come straight back to you).
    ToolSpec(
        name="ask_user",
        description="Ask the user ONE question and end your turn — their next message is the "
        "answer. Optionally offer 2-4 suggested choices shown as clickable chips.",
        parameters=_obj_schema(
            {
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Suggested answers shown as clickable chips (optional).",
                },
            },
            ["question"],
        ),
    ),
    ToolSpec(
        name="propose_scenario",
        description="Propose a FULL scenario manifest for the user to approve. Validates (schema "
        "+ contract) but NEVER writes: a valid draft is shown to the user and ends your turn; "
        "validation errors come back as the tool result for you to fix and re-propose.",
        parameters=_obj_schema({"spec": _SPEC_PROP}, ["spec"]),
    ),
]
