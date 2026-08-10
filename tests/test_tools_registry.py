"""The tool-grant seam (#52, ADR-004): the registry, the plugin contract, and where each kind of
validation belongs.

The split under test is the one `SkillRef` already established and this repeats deliberately:

  * the MODEL validates SHAPE and in-manifest consistency — things true of the manifest alone,
  * the REGISTRY validates EXISTENCE — things true only of this machine, right now.

Putting existence in the model would make a scenario that grants `web_search` fail to *load* on a
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


def _profile(name: str = "web_search", **kw: Any) -> ToolProfile:
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
    assert tool_registry()["web_search"].label == "Search"


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
    assert "web_search" in registry, "one broken plugin took the healthy one down with it"
    assert "broken" in caplog.text


def test_a_plugin_that_raises_while_listing_is_skipped(monkeypatch, caplog):
    class _Angry:
        def tools(self) -> Any:
            raise RuntimeError("kaboom")

    _install(monkeypatch, _FakeEntryPoint("angry", _Angry()),
             _FakeEntryPoint("good", _Plugin(_profile())))
    with caplog.at_level(logging.WARNING):
        assert "web_search" in tool_registry()
    assert "kaboom" in caplog.text


@pytest.mark.parametrize(
    "bad",
    ["websearch", "Web.Search", "web.", ".search", "web_search.deep", "web search", "web-search"],
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
        assert tool_registry()["web_search"].label == "First"
    assert "web_search" in caplog.text


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
    assert _project(["web_search"]).agents[0].tools == ["web_search"]


@pytest.mark.parametrize("bad", ["websearch", "Web.Search", "web.", "web search"])
def test_a_malformed_grant_is_a_schema_error(bad):
    with pytest.raises(ValidationError):
        _project([bad])


def test_a_duplicate_grant_is_a_schema_error():
    with pytest.raises(ValidationError, match="duplicate"):
        _project(["web_search", "web_search"])


def test_a_spec_tools_entry_for_a_tool_nobody_holds_is_an_error():
    """Two ways to say the same thing, one of them silently inert, is exactly how a scenario ends
    up with no backstop at all — the schema already says this about spec-level `duration`."""
    with pytest.raises(ValidationError, match="web_fetch"):
        _project(["web_search"], {"web_fetch": {"allow": ["example.com"]}})


def test_spec_tools_for_a_granted_tool_is_fine():
    p = _project(["web_fetch"], {"web_fetch": {"allow": ["example.com"]}})
    assert p.spec.tools["web_fetch"]["allow"] == ["example.com"]


# --- the registry checks: existence, config, keys ----------------------------------------------
def test_an_unknown_tool_is_reported_with_the_agent_that_wants_it(monkeypatch):
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile())))
    problems = check_grants(_project(["web_crawl"]), key_refs=set())
    assert len(problems) == 1
    assert "web_crawl" in problems[0] and "analyst" in problems[0]


def test_a_config_that_fails_the_tools_own_schema_is_reported(monkeypatch):
    _install(monkeypatch, _FakeEntryPoint("wf", _Plugin(_profile(
        name="web_fetch",
        config_schema={"type": "object",
                       "properties": {"allow": {"type": "array", "items": {"type": "string"}}},
                       "additionalProperties": False},
    ))))
    problems = check_grants(_project(["web_fetch"], {"web_fetch": {"allow": "example.com"}}),
                            key_refs=set())
    assert len(problems) == 1 and "web_fetch" in problems[0]


def test_a_missing_keystore_handle_is_reported_not_fatal(monkeypatch):
    """Actionable, and separable from 'the tool does not exist' — the fix is different."""
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile(api_key_ref="search-main"))))
    problems = check_grants(_project(["web_search"]), key_refs=set())
    assert len(problems) == 1 and "search-main" in problems[0]
    assert check_grants(_project(["web_search"]), key_refs={"search-main"}) == []


def test_a_clean_project_reports_nothing(monkeypatch):
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile())))
    assert check_grants(_project(["web_search"]), key_refs=set()) == []


def test_a_project_granting_nothing_needs_no_registry(monkeypatch):
    """Every scenario shipped today is this case; none of them should notice this feature."""
    _install(monkeypatch)
    assert check_grants(_project(), key_refs=set()) == []


def test_elevated_tools_are_identifiable_for_the_launch_gate(monkeypatch):
    """#57 consumes this; assert the tier survives the round trip rather than discovering later
    that every tool reads as contained."""
    _install(monkeypatch, _FakeEntryPoint("x", _Plugin(
        _profile(name="net_open", risk=ToolRisk.ELEVATED), _profile(name="web_search"))))
    reg = tool_registry()
    assert reg["net_open"].risk is ToolRisk.ELEVATED
    assert reg["web_search"].risk is ToolRisk.CONTAINED  # the default


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
        agents=[AgentSpec(id="one", tools=["web_search"]),
                AgentSpec(id="two", tools=["web_search"])],
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


def _pkg_granting(tmp_path, tool="web_search"):
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
    assert "web_search" in body and "analyst" in body and "not installed" in body


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
    assert "web_search" in result.output and "not installed" in result.output


def test_up_refuses_a_grant_that_cannot_work(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from bearpit.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch)
    result = CliRunner().invoke(app, ["up", str(_pkg_granting(tmp_path))])
    assert result.exit_code != 0
    assert "web_search" in result.output


# --- the elevated tier takes consent (#57) -----------------------------------------------------
def _elevated_profile(name="net_open"):
    return _profile(name=name, risk=ToolRisk.ELEVATED)


@pytest.mark.asyncio
async def test_an_elevated_grant_is_refused_until_consented(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from bearpit.chronicle import Chronicle
    from bearpit.gatekeeper.api import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch, _FakeEntryPoint("x", _Plugin(_elevated_profile())))
    pkg = _pkg_granting(tmp_path, tool="net_open")
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    manager = _LaunchManager()
    with TestClient(create_app(chron=chron, manager=manager)) as c:
        blocked = c.post("/api/realms", json={"package": str(pkg)})
        allowed = c.post("/api/realms",
                         json={"package": str(pkg), "allow_elevated_tools": True})
    await chron.close()
    assert blocked.status_code == 400, blocked.text
    detail = blocked.json()["detail"]
    assert detail["elevated"] == [{"agent": "analyst", "tools": ["net_open"]}]
    assert "allow_elevated_tools" in detail["hint"]
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_contained_grants_launch_with_no_prompt(tmp_path, monkeypatch):
    """The tier only means something if the common case is silent — a warning shown on every
    research scenario stops being a warning (#47)."""
    from starlette.testclient import TestClient

    from bearpit.chronicle import Chronicle
    from bearpit.gatekeeper.api import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch, _FakeEntryPoint("ws", _Plugin(_profile())))  # contained by default
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    with TestClient(create_app(chron=chron, manager=_LaunchManager())) as c:
        r = c.post("/api/realms", json={"package": str(_pkg_granting(tmp_path))})
    await chron.close()
    assert r.status_code == 200, r.text


def test_elevated_grants_lists_who_holds_what(monkeypatch):
    from bearpit.core.tools import elevated_grants

    _install(monkeypatch, _FakeEntryPoint("x", _Plugin(_elevated_profile(), _profile())))
    project = Project(
        metadata=ProjectMeta(name="p"),
        agents=[AgentSpec(id="scraper", tools=["net_open", "web_search"]),
                AgentSpec(id="analyst", tools=["web_search"])],
    )
    assert elevated_grants(project) == {"scraper": ["net_open"]}


def test_up_asks_before_running_an_elevated_grant(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from bearpit.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch, _FakeEntryPoint("x", _Plugin(_elevated_profile())))
    result = CliRunner().invoke(app, ["up", str(_pkg_granting(tmp_path, tool="net_open"))],
                                input="n\n")
    assert result.exit_code == 1
    assert "reach past the realm" in result.output and "net_open" in result.output


@pytest.mark.asyncio
async def test_rerun_checks_tool_grants_too(tmp_path, monkeypatch):
    """Rerun is a launch, and the easier one to forget — it takes no request body, and both gates
    were once written into `create_realm` twice while rerun had neither. A tool installed when a
    realm first ran can be gone by the time someone replays it."""
    from starlette.testclient import TestClient

    from bearpit.chronicle import Chronicle, EventKind
    from bearpit.gatekeeper.api import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch)  # the tool the recorded run used is no longer installed
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    project = Project(metadata=ProjectMeta(name="p"),
                      agents=[AgentSpec(id="analyst", tools=["web_search"])])
    await chron.append_event("old", EventKind.LIFECYCLE, {
        "event": "running", "require_mention": True,
        "project": project.model_dump(mode="json"),
    })
    with TestClient(create_app(chron=chron, manager=_LaunchManager())) as c:
        r = c.post("/api/realms/old/rerun?mode=snapshot")
    await chron.close()
    assert r.status_code == 400, r.text
    assert "web_search" in json.dumps(r.json()["detail"])


@pytest.mark.asyncio
async def test_rerun_gates_an_elevated_grant_and_accepts_consent(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from bearpit.chronicle import Chronicle, EventKind
    from bearpit.gatekeeper.api import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    _install(monkeypatch, _FakeEntryPoint("x", _Plugin(_elevated_profile())))
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    project = Project(metadata=ProjectMeta(name="p"),
                      agents=[AgentSpec(id="scraper", tools=["net_open"])])
    await chron.append_event("old", EventKind.LIFECYCLE, {
        "event": "running", "require_mention": True,
        "project": project.model_dump(mode="json"),
    })
    with TestClient(create_app(chron=chron, manager=_LaunchManager())) as c:
        blocked = c.post("/api/realms/old/rerun?mode=snapshot")
        allowed = c.post("/api/realms/old/rerun?mode=snapshot&allow_elevated_tools=true")
    await chron.close()
    assert blocked.status_code == 400 and "net_open" in json.dumps(blocked.json()["detail"])
    assert allowed.status_code == 200, allowed.text


def test_a_grant_survives_the_editor_save_and_reload(tmp_path, monkeypatch):
    """The round trip the browser caught and the suite did not.

    `_agent_files` builds an explicit allowlist, so a field missing from it is dropped in silence:
    the editor showed the grant, the save reported success, and the package never carried it. The
    assertion has to go through the real writer AND the real loader, or it proves nothing.
    """
    from bearpit.core.package import load_package
    from bearpit.gatekeeper.scenarios import write_scenario

    base = tmp_path / "scen"
    base.mkdir(parents=True, exist_ok=True)
    payload = {
        "metadata": {"name": "granting"},
        "spec": {"termination": [{"type": "manual"}],
                 "tools": {"web_fetch": {"max_calls_per_agent": 5}}},
        "agents": [
            {"id": "analyst", "model_category": "medium", "persona": "x",
             "tools": ["web_fetch"]},
            {"id": "sealed", "model_category": "medium", "persona": "y"},
        ],
    }
    written = write_scenario(base, "granting", payload)
    project = load_package(str(base / (written.get("name") or "granting")))
    by_id = {a.id: a for a in project.agents}
    assert by_id["analyst"].tools == ["web_fetch"], "the editor's grant did not reach the package"
    assert by_id["sealed"].tools == []
    assert project.spec.tools == {"web_fetch": {"max_calls_per_agent": 5}}


@pytest.mark.asyncio
async def test_the_editor_reads_back_the_grants_it_saved(tmp_path, monkeypatch):
    """Writer and reader again, from the other side.

    The save path was fixed first, and the editor still showed "no tools" for an agent that had
    one — because the READ endpoint omitted the field. Half a round trip is worse than none: the
    grant is on disk, invisible in the editor, and dropped by the next save.
    """
    from starlette.testclient import TestClient

    from bearpit.chronicle import Chronicle
    from bearpit.gatekeeper.api import create_app
    from bearpit.gatekeeper.scenarios import write_scenario

    monkeypatch.setenv("HOME", str(tmp_path))
    base = tmp_path / "scen"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(base))
    write_scenario(base, "granting", {
        "metadata": {"name": "granting"},
        "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "analyst", "model_category": "medium", "persona": "x",
                    "tools": ["web_fetch"]}],
    })
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    with TestClient(create_app(chron=chron, manager=_LaunchManager())) as c:
        payload = c.get("/api/packages/granting").json()
    await chron.close()
    analyst = next(a for a in payload["agents"] if a["id"] == "analyst")
    assert analyst["tools"] == ["web_fetch"]


def test_the_worked_example_grants_asymmetrically():
    """`fact-race` is the demonstration that this epic exists for: the same question put to one
    agent that can look things up and one that cannot. If the grants ever equalise, the scenario
    still runs and quietly stops demonstrating anything."""
    from bearpit.core.package import load_package

    project = load_package("examples/fact-race")
    by_id = {a.id: a for a in project.agents}
    assert by_id["scout"].tools == ["web_fetch"]
    assert by_id["pundit"].tools == [], "the whole point is that Pundit cannot look things up"
    assert by_id["judge"].tools == ["web_fetch"], "the referee must be able to check the answer"
    # and the realm-level policy that bounds it
    assert project.spec.tools["web_fetch"]["max_calls_per_agent"] == 4
    assert "*.wikipedia.org" in project.spec.tools["web_fetch"]["allow"]


# --- what the first live run taught (naming + shadowing) ---------------------------------------
@pytest.mark.parametrize("dotted", ["web.fetch", "web.search", "mcp.research"])
def test_a_dotted_tool_name_is_refused_at_the_seam(monkeypatch, caplog, dotted):
    """ADR-004 originally specified `family.verb`. A dot survives MCP perfectly — the SDK lists it
    and calls it, and a spike proved exactly that — and then dies one layer further on: model
    function-calling APIs allow only [A-Za-z0-9_-] in a tool name.

    Live, the agent held the grant, realmtools advertised the tool with the right schema, and the
    agent said "no web.fetch tool exists". Refusing the name here is how a plugin author finds out
    at install time instead of mid-realm.
    """
    _install(monkeypatch, _FakeEntryPoint("bad", _Plugin(_profile(name=dotted))))
    with caplog.at_level(logging.WARNING):
        assert dotted not in tool_registry()
    assert "never reaches the model" in caplog.text


def test_a_dotted_grant_is_a_schema_error():
    with pytest.raises(ValidationError):
        _project(["web.fetch"])


@pytest.mark.parametrize("verb", ["run_code", "submit_sealed", "reveal_status", "send_private",
                                  "turn_status"])
def test_a_plugin_cannot_shadow_a_realmtools_verb(monkeypatch, caplog, verb):
    """A new hazard created by the rename. With a dot separator a collision with `run_code` was
    impossible; with an underscore it is one plausible name away, and shadowing it would be a
    privilege question rather than an inconvenience.

    Only the MULTI-WORD verbs are reachable: the name rule requires an underscore, so `rule`,
    `remember` and `eliminate` are already unspendable as plugin names. This list is exactly the
    set a plugin could otherwise take."""
    _install(monkeypatch, _FakeEntryPoint("greedy", _Plugin(_profile(name=verb))))
    with caplog.at_level(logging.WARNING):
        assert verb not in tool_registry()
    assert "realmtools verb" in caplog.text
