"""The tool-grant seam (#52, ADR-004): the registry, the plugin contract, and where each kind of
validation belongs.

The split under test is the one `SkillRef` already established and this repeats deliberately:

  * the MODEL validates SHAPE and in-manifest consistency — things true of the manifest alone,
  * the REGISTRY validates EXISTENCE — things true only of this machine, right now.

Putting existence in the model would make a scenario that grants `web.search` fail to *load* on a
machine without that plugin, so it could not be viewed, edited or exported either. That is the
#47 failure in a new place: behaviour that depends on which packages happen to be installed.

Every test drives a FAKE plugin through discovery, so nothing here depends on what is installed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest
from pydantic import ValidationError

from bearpit.core import tools as toolmod
from bearpit.core.schema import AgentSpec, Project, ProjectMeta, ProjectSpec
from bearpit.core.tools import ToolProfile, ToolRisk, check_grants, tool_registry


class _FakeEntryPoint:
    def __init__(self, name: str, value: Any, raises: Exception | None = None) -> None:
        self.name = name
        self._value = value
        self._raises = raises

    def load(self) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._value


class _Plugin:
    def __init__(self, *profiles: ToolProfile) -> None:
        self._profiles = profiles

    def tools(self) -> tuple[ToolProfile, ...]:
        return self._profiles


async def _noop(args: dict[str, Any], config: dict[str, Any], ctx: Any) -> Any:
    return "ok"


def _profile(name: str = "web.search", **kw: Any) -> ToolProfile:
    return ToolProfile(
        name=name, label=kw.pop("label", "Search"), description="searches",
        params={"type": "object", "properties": {"query": {"type": "string"}}},
        handler=_noop, **kw,
    )


def _install(monkeypatch: pytest.MonkeyPatch, *eps: _FakeEntryPoint) -> None:
    monkeypatch.setattr(
        toolmod, "_entry_points",
        lambda g: list(eps) if g == toolmod.TOOL_GROUP else [],
    )
    toolmod.reset_tool_cache()


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    toolmod.reset_tool_cache()
    yield
    toolmod.reset_tool_cache()


# --- the registry + plugin contract -----------------------------------------------------------
def test_a_plugin_contributes_a_tool(monkeypatch):
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile())))
    assert tool_registry()["web.search"].label == "Search"


def test_a_plugin_that_fails_to_load_is_skipped(monkeypatch, caplog):
    """The load-bearing property, inherited verbatim from the provider seam: a third-party package
    must never be able to stop the platform from starting, however badly it misbehaves."""
    _install(
        monkeypatch,
        _FakeEntryPoint("broken", None, raises=ImportError("no module named nope")),
        _FakeEntryPoint("good", _Plugin(_profile())),
    )
    with caplog.at_level(logging.WARNING):
        registry = tool_registry()
    assert "web.search" in registry, "one broken plugin took the healthy one down with it"
    assert "broken" in caplog.text


def test_a_plugin_that_raises_while_listing_is_skipped(monkeypatch, caplog):
    class _Angry:
        def tools(self) -> Any:
            raise RuntimeError("kaboom")

    _install(monkeypatch, _FakeEntryPoint("angry", _Angry()),
             _FakeEntryPoint("good", _Plugin(_profile())))
    with caplog.at_level(logging.WARNING):
        assert "web.search" in tool_registry()
    assert "kaboom" in caplog.text


@pytest.mark.parametrize(
    "bad",
    ["websearch", "Web.Search", "web.", ".search", "web.search.deep", "web search", "web-search"],
)
def test_a_badly_named_tool_is_refused_at_the_seam(monkeypatch, caplog, bad):
    """`family.verb`, lowercase. Enforced here so a plugin cannot squat a bare namespace like
    `mcp` that ADR-004 reserves, and so the name is predictable in a manifest."""
    _install(monkeypatch, _FakeEntryPoint("bad", _Plugin(_profile(name=bad))))
    with caplog.at_level(logging.WARNING):
        # the built-ins are always there; what must be absent is the plugin's contribution
        assert bad not in tool_registry()
    assert bad in caplog.text


def test_the_first_registration_of_a_name_wins(monkeypatch, caplog):
    """Two plugins claiming one name is a real conflict. Last-wins would let a package installed
    later silently replace a tool a scenario already depends on, changing what an agent does with
    no manifest edit — so the collision is refused and reported instead."""
    first = _profile(label="First")
    second = _profile(label="Second")
    _install(monkeypatch, _FakeEntryPoint("a", _Plugin(first)),
             _FakeEntryPoint("b", _Plugin(second)))
    with caplog.at_level(logging.WARNING):
        assert tool_registry()["web.search"].label == "First"
    assert "web.search" in caplog.text


def test_the_registry_is_discovered_once(monkeypatch):
    calls = {"n": 0}

    def counting(group: str) -> list[Any]:
        calls["n"] += 1
        return [_FakeEntryPoint("ws", _Plugin(_profile()))] if group == toolmod.TOOL_GROUP else []

    monkeypatch.setattr(toolmod, "_entry_points", counting)
    toolmod.reset_tool_cache()
    for _ in range(3):
        tool_registry()
    assert calls["n"] == 1


# --- the model: shape and in-manifest consistency, no registry ---------------------------------
def _project(tools: list[str] | None = None, spec_tools: dict[str, Any] | None = None) -> Project:
    return Project(
        metadata=ProjectMeta(name="p"),
        spec=ProjectSpec(tools=spec_tools or {}),
        agents=[AgentSpec(id="analyst", tools=tools or [])],
    )


def test_a_well_formed_grant_is_accepted_with_no_plugin_installed(monkeypatch):
    """The point of the split: this must not depend on what is installed."""
    _install(monkeypatch)  # empty registry
    assert _project(["web.search"]).agents[0].tools == ["web.search"]


@pytest.mark.parametrize("bad", ["websearch", "Web.Search", "web.", "web search"])
def test_a_malformed_grant_is_a_schema_error(bad):
    with pytest.raises(ValidationError):
        _project([bad])


def test_a_duplicate_grant_is_a_schema_error():
    with pytest.raises(ValidationError, match="duplicate"):
        _project(["web.search", "web.search"])


def test_a_spec_tools_entry_for_a_tool_nobody_holds_is_an_error():
    """Two ways to say the same thing, one of them silently inert, is exactly how a scenario ends
    up with no backstop at all — the schema already says this about spec-level `duration`."""
    with pytest.raises(ValidationError, match="web.fetch"):
        _project(["web.search"], {"web.fetch": {"allow": ["example.com"]}})


def test_spec_tools_for_a_granted_tool_is_fine():
    p = _project(["web.fetch"], {"web.fetch": {"allow": ["example.com"]}})
    assert p.spec.tools["web.fetch"]["allow"] == ["example.com"]


# --- the registry checks: existence, config, keys ----------------------------------------------
def test_an_unknown_tool_is_reported_with_the_agent_that_wants_it(monkeypatch):
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile())))
    problems = check_grants(_project(["web.crawl"]), key_refs=set())
    assert len(problems) == 1
    assert "web.crawl" in problems[0] and "analyst" in problems[0]


def test_a_config_that_fails_the_tools_own_schema_is_reported(monkeypatch):
    _install(monkeypatch, _FakeEntryPoint("wf", _Plugin(_profile(
        name="web.fetch",
        config_schema={"type": "object",
                       "properties": {"allow": {"type": "array", "items": {"type": "string"}}},
                       "additionalProperties": False},
    ))))
    problems = check_grants(_project(["web.fetch"], {"web.fetch": {"allow": "example.com"}}),
                            key_refs=set())
    assert len(problems) == 1 and "web.fetch" in problems[0]


def test_a_missing_keystore_handle_is_reported_not_fatal(monkeypatch):
    """Actionable, and separable from 'the tool does not exist' — the fix is different."""
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile(api_key_ref="search-main"))))
    problems = check_grants(_project(["web.search"]), key_refs=set())
    assert len(problems) == 1 and "search-main" in problems[0]
    assert check_grants(_project(["web.search"]), key_refs={"search-main"}) == []


def test_a_clean_project_reports_nothing(monkeypatch):
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile())))
    assert check_grants(_project(["web.search"]), key_refs=set()) == []


def test_a_project_granting_nothing_needs_no_registry(monkeypatch):
    """Every scenario shipped today is this case; none of them should notice this feature."""
    _install(monkeypatch)
    assert check_grants(_project(), key_refs=set()) == []


def test_elevated_tools_are_identifiable_for_the_launch_gate(monkeypatch):
    """#57 consumes this; assert the tier survives the round trip rather than discovering later
    that every tool reads as contained."""
    _install(monkeypatch, _FakeEntryPoint("x", _Plugin(
        _profile(name="net.open", risk=ToolRisk.ELEVATED), _profile(name="web.search"))))
    reg = tool_registry()
    assert reg["net.open"].risk is ToolRisk.ELEVATED
    assert reg["web.search"].risk is ToolRisk.CONTAINED  # the default


def test_a_malformed_key_in_spec_tools_is_a_schema_error():
    """A different code path from a malformed grant: the name is a dict KEY here, and a typed key
    is easy to assume is validated when it is not."""
    with pytest.raises(ValidationError):
        Project(
            metadata=ProjectMeta(name="p"),
            spec=ProjectSpec(tools={"webfetch": {"allow": []}}),
            agents=[AgentSpec(id="a")],
        )


def test_the_same_missing_tool_is_reported_once_per_agent(monkeypatch):
    """Two agents both wanting an uninstalled tool is two things to fix, and the operator should
    see whose grant each one is."""
    _install(monkeypatch)
    project = Project(
        metadata=ProjectMeta(name="p"),
        agents=[AgentSpec(id="one", tools=["web.search"]),
                AgentSpec(id="two", tools=["web.search"])],
    )
    problems = check_grants(project, key_refs=set())
    assert len(problems) == 2
    assert {"one", "two"} == {p.split("'")[1] for p in problems}


# --- check_grants must actually be CALLED (#67) ------------------------------------------------
class _LaunchManager:
    """Just enough manager for the launch endpoints."""

    max_active = 6

    def __init__(self) -> None:
        self.runs: dict[str, Any] = {}
        self.started: list[tuple[str, dict[str, Any]]] = []

    def start(self, realm_id: str, project: Any, **kw: Any) -> None:
        self.started.append((realm_id, kw))

    def active(self) -> list[str]:
        return [r for r, _ in self.started]


def _pkg_granting(tmp_path, tool="web.search"):
    import json
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "project.json").write_text(json.dumps({
        "metadata": {"name": "duel"},
        "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "analyst", "tools": [tool],
                    "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}}],
    }))
    return pkg


@pytest.mark.asyncio
async def test_launching_with_an_uninstalled_tool_is_refused_and_says_which(tmp_path, monkeypatch):
    """The grant was checked against the token, not this machine. Realmtools registers from its own
    registry, so an uninstalled tool is never advertised: the agent silently lacks it while the
    scenario's prose still tells it to search. That realm spends money producing nonsense for a
    reason nothing states — the #47 failure, one layer down."""
    from starlette.testclient import TestClient

    from bearpit.chronicle import Chronicle
    from bearpit.gatekeeper.api import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch)  # nothing installed
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    with TestClient(create_app(chron=chron, manager=_LaunchManager())) as c:
        r = c.post("/api/realms", json={"package": str(_pkg_granting(tmp_path))})
    await chron.close()
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    body = json.dumps(detail)
    assert "web.search" in body and "analyst" in body and "not installed" in body


@pytest.mark.asyncio
async def test_launching_with_every_granted_tool_present_is_untouched(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from bearpit.chronicle import Chronicle
    from bearpit.gatekeeper.api import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile())))
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    with TestClient(create_app(chron=chron, manager=_LaunchManager())) as c:
        r = c.post("/api/realms", json={"package": str(_pkg_granting(tmp_path))})
    await chron.close()
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_a_granted_tool_whose_key_is_missing_is_refused_too(tmp_path, monkeypatch):
    """It would fail on every call at run time. Saying so up front costs a message; not saying so
    costs the run."""
    from starlette.testclient import TestClient

    from bearpit.chronicle import Chronicle
    from bearpit.gatekeeper.api import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile(api_key_ref="search-main"))))
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    with TestClient(create_app(chron=chron, manager=_LaunchManager())) as c:
        r = c.post("/api/realms", json={"package": str(_pkg_granting(tmp_path))})
    await chron.close()
    assert r.status_code == 400
    assert "search-main" in json.dumps(r.json()["detail"])


def test_validate_reports_tool_problems(tmp_path, monkeypatch):
    """`validate` is what an author runs after editing; a grant that cannot work should surface
    there rather than at launch."""
    from typer.testing import CliRunner

    from bearpit.cli.main import app

    _install(monkeypatch)
    result = CliRunner().invoke(app, ["validate", str(_pkg_granting(tmp_path))])
    assert "web.search" in result.output and "not installed" in result.output


def test_up_refuses_a_grant_that_cannot_work(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from bearpit.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch)
    result = CliRunner().invoke(app, ["up", str(_pkg_granting(tmp_path))])
    assert result.exit_code != 0
    assert "web.search" in result.output
