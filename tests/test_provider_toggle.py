"""The global model-provider toggle: persisted state, the API, and the Platform.run apply seam."""

import pytest
from starlette.testclient import TestClient

from bearpit.chronicle import Chronicle, EventKind
from bearpit.core.schema import AgentSpec, ModelCategory, Project, ProjectMeta
from bearpit.gatekeeper.api import create_app


def _project():
    return Project(metadata=ProjectMeta(name="p"), agents=[
        AgentSpec(id="lead", model_category=ModelCategory.LARGE),
        AgentSpec(id="helper", model_category=ModelCategory.SMALL),
    ])


# --- persisted state --------------------------------------------------------
def test_active_provider_defaults_to_azure(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.gatekeeper import appstate
    assert appstate.active_provider() == "azure"


def test_set_and_read_active_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.gatekeeper import appstate
    appstate.set_active_provider("anthropic")
    assert appstate.active_provider() == "anthropic"


def test_set_unknown_provider_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.gatekeeper import appstate
    with pytest.raises(ValueError, match="unknown model provider"):
        appstate.set_active_provider("gemini")


def test_corrupt_state_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".bearpit").mkdir()
    (tmp_path / ".bearpit" / "platform.json").write_text("{not json")
    from bearpit.gatekeeper import appstate
    assert appstate.active_provider() == "azure"


# --- the API ----------------------------------------------------------------
class FakeManager:
    max_active = 6

    def active(self):
        return []


@pytest.fixture
async def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path / "scen"))
    monkeypatch.setenv("BEARPIT_EXAMPLES_DIR", str(tmp_path / "examples"))
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await chron.append_event("r", EventKind.LIFECYCLE, {"event": "running"})
    app = create_app(chron=chron, manager=FakeManager())
    with TestClient(app) as c:
        yield c
    await chron.close()


def test_settings_reports_provider_and_options(app_client):
    s = app_client.get("/api/settings").json()
    assert s["model_provider"] == "azure"
    assert s["model_categories"] == ["small", "medium", "large"]
    names = {p["name"] for p in s["model_providers"]}
    # a superset: an installed provider plugin legitimately adds its own pipelines
    assert {"azure", "openai", "anthropic", "openrouter"} <= names
    anthropic = next(p for p in s["model_providers"] if p["name"] == "anthropic")
    assert anthropic["ready"] is False  # no anthropic keystore handle yet
    assert anthropic["needs_key_ref"] == "anthropic-main"
    # each provider exposes its editable category table
    assert set(anthropic["categories"]) == {"small", "medium", "large"}
    assert anthropic["categories"]["large"]["model"] == "claude-opus-5"
    assert anthropic["categories"]["large"]["effort"] == "high"


def test_put_model_config_edits_a_category(app_client):
    s = app_client.get("/api/settings").json()
    cfg = {p["name"]: {"api_key_ref": p["api_key_ref"], "categories": p["categories"]}
           for p in s["model_providers"]}
    cfg["anthropic"]["categories"]["large"] = {
        "model": "opus-4.8", "effort": "max",
        "input_cost_per_token": 1.5e-5, "output_cost_per_token": 7.5e-5, "context_length": 200000}
    r = app_client.put("/api/settings/model-config", json={"providers": cfg})
    assert r.status_code == 200
    after = app_client.get("/api/settings").json()
    large = next(p for p in after["model_providers"]
                 if p["name"] == "anthropic")["categories"]["large"]
    assert large["model"] == "opus-4.8" and large["effort"] == "max"


def test_put_model_config_rejects_missing_model(app_client):
    bad = {"azure": {"categories": {"small": {"model": ""}, "medium": {"model": "m"},
                                    "large": {"model": "m"}}}}
    assert app_client.put("/api/settings/model-config", json={"providers": bad}).status_code == 400


def test_put_provider_switches_pipeline(app_client):
    r = app_client.put("/api/settings/provider", json={"model_provider": "anthropic"})
    assert r.status_code == 200 and r.json()["model_provider"] == "anthropic"
    assert app_client.get("/api/settings").json()["model_provider"] == "anthropic"


def test_put_unknown_provider_is_400(app_client):
    assert app_client.put("/api/settings/provider", json={"model_provider": "nope"}).status_code \
        == 400


# --- the apply seam: Platform.run resolves the project for the active provider ---------------
class _FakeRunner:
    def __init__(self):
        self.captured = None
        self.kwargs = None

    async def run(self, realm_id, project, factory, **kw):
        self.captured = project
        self.kwargs = kw
        return "done"


async def test_platform_run_applies_active_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.gatekeeper import appstate
    from bearpit.gatekeeper.service import Platform

    appstate.set_active_provider("anthropic")
    runner = _FakeRunner()
    platform = Platform(
        settings=None, chronicle=None, runtime=None, herald=None, ledger=None,
        forge=None, warden=None, runner=runner, system_password="pw",
    )
    await platform.run("realm1", _project())
    agents = {a.id: a.require_model() for a in runner.captured.agents}
    assert agents["lead"].model == "claude-opus-5" and agents["lead"].effort == "high"
    assert agents["helper"].model == "claude-haiku-4-5"  # small
    assert agents["helper"].effort == "low"
    assert all(m.api_key_ref == "anthropic-main" for m in agents.values())


async def test_the_snapshot_factory_hands_realmtools_tokens_to_the_runner(
    tmp_path, monkeypatch
):
    """The one-line wiring between Forge and the redactor, which nothing else covers.

    Forge mints each agent's realmtools bearer; `Platform.run`'s factory is what passes them into
    the LiveSnapshot so `run_code` output can be masked. Removing that line broke nothing visible —
    the Forge test still passed, the Runner test still passed, and agents' live credentials would
    quietly have gone back into the append-only chronicle. Hence this test."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.forge import RealmHandles
    from bearpit.gatekeeper.service import Platform
    from bearpit.herald import BusProvision

    captured = {}

    class _Runner:
        async def run(self, realm_id, project, factory, **kw):
            handles = RealmHandles(
                realm_id="r1", network="realm-r1", shared_volume=None, agents={},
                agent_tokens={"lead": "rt-lead-token", "helper": "rt-helper-token"},
            )
            bus = BusProvision(commons_room="!c:realm.local", creds={})
            captured["snapshot"] = factory("r1", handles, bus, None)
            return "done"

    platform = Platform(
        settings=None, chronicle=None, runtime=None, herald=None, ledger=None,
        forge=None, warden=None, runner=_Runner(), system_password="pw",
    )
    await platform.run("r1", _project())

    snapshot = captured["snapshot"]
    assert snapshot._agent_tokens == {"lead": "rt-lead-token", "helper": "rt-helper-token"}


# --- the fallback must be loud, not silent (#47) -----------------------------

def _store_unknown_provider(tmp_path, name="vanished-cli"):
    """Write a provider the platform cannot resolve — exactly what happens when the plugin that
    contributes it is not installed.

    The name is synthetic on purpose. Naming a real plugin-contributed provider would make the
    test pass or fail on whether that plugin happens to be installed in the environment running
    the suite — which is testing the venv, not the code."""
    import json
    d = tmp_path / ".bearpit"
    d.mkdir(parents=True, exist_ok=True)
    (d / "platform.json").write_text(json.dumps({"model_provider": name}))


def test_a_stored_provider_whose_plugin_is_gone_is_reported_as_a_fallback(tmp_path, monkeypatch):
    """`uv sync` prunes a separately-installed plugin, so the provider it contributes stops being
    known and the setting silently evaporates. A flat-rate pipeline is replaced by a metered one,
    so the operator gets billed for a run they believed was free — with the correct setting still
    on disk, being ignored."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.gatekeeper import appstate
    _store_unknown_provider(tmp_path)

    choice = appstate.resolve_provider()
    assert choice.stored == "vanished-cli"
    assert choice.name == "azure", "it still resolves to something runnable"
    assert choice.available is False
    assert choice.fell_back is True
    # a clause about the stored provider, so each surface composes its own sentence
    assert choice.reason == "its provider plugin is not installed"


def test_a_normal_setting_is_not_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.gatekeeper import appstate
    appstate.set_active_provider("anthropic")
    choice = appstate.resolve_provider()
    assert (choice.name, choice.stored, choice.fell_back) == ("anthropic", "anthropic", False)
    assert choice.reason == ""


def test_no_setting_at_all_is_not_a_fallback(tmp_path, monkeypatch):
    """Never configured is a different fact from configured-but-unavailable, and only the second
    one is worth interrupting anybody about."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.gatekeeper import appstate
    choice = appstate.resolve_provider()
    assert (choice.name, choice.stored, choice.fell_back) == ("azure", None, False)


def test_active_provider_keeps_its_contract(tmp_path, monkeypatch):
    """Existing callers keep working — the fallback is added information, not a behaviour change."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.gatekeeper import appstate
    _store_unknown_provider(tmp_path)
    assert appstate.active_provider() == "azure"


@pytest.mark.asyncio
async def test_the_settings_api_surfaces_the_fallback(tmp_path, monkeypatch):
    """The Settings page showed `azure` as though it were the choice, not a substitution."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_unknown_provider(tmp_path)
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    with TestClient(create_app(chron=chron, manager=FakeManager())) as c:
        s = c.get("/api/settings").json()
    await chron.close()
    assert s["model_provider"] == "azure"
    assert s["provider_fallback"]["stored"] == "vanished-cli"
    assert s["provider_fallback"]["effective"] == "azure"
    assert "not installed" in s["provider_fallback"]["reason"]


@pytest.mark.asyncio
async def test_no_fallback_key_when_nothing_is_wrong(tmp_path, monkeypatch):
    """The key is absent-as-None, not merely falsy: the page must not render a warning for the
    ordinary case where the stored provider resolves."""
    monkeypatch.setenv("HOME", str(tmp_path))
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    with TestClient(create_app(chron=chron, manager=FakeManager())) as c:
        s = c.get("/api/settings").json()
    await chron.close()
    assert s["provider_fallback"] is None


# --- the launch gate: a fallback is expensive, so it takes a decision (#47) --------------------
async def test_run_config_records_the_fallback(tmp_path, monkeypatch):
    """The run record is the only place a finished realm can be asked what it actually ran on."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_unknown_provider(tmp_path)
    from bearpit.gatekeeper.service import Platform

    runner = _FakeRunner()
    platform = Platform(
        settings=None, chronicle=None, runtime=None, herald=None, ledger=None,
        forge=None, warden=None, runner=runner, system_password="pw",
    )
    await platform.run("r1", _project(), allow_provider_fallback=True)
    cfg = runner.kwargs["run_config"]
    assert cfg["provider"] == "azure"
    assert cfg["provider_fallback"] == {
        "stored": "vanished-cli",
        "effective": "azure",
        "reason": cfg["provider_fallback"]["reason"],
    }
    assert "not installed" in cfg["provider_fallback"]["reason"]


async def test_run_config_has_no_fallback_when_the_provider_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from bearpit.gatekeeper.service import Platform

    runner = _FakeRunner()
    platform = Platform(
        settings=None, chronicle=None, runtime=None, herald=None, ledger=None,
        forge=None, warden=None, runner=runner, system_password="pw",
    )
    await platform.run("r1", _project())
    assert runner.kwargs["run_config"]["provider_fallback"] is None


async def test_platform_run_refuses_a_fallback_without_consent(tmp_path, monkeypatch):
    """The backstop. The API pre-checks, but a caller that never asked must not spend money on a
    provider it did not choose."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_unknown_provider(tmp_path)
    from bearpit.gatekeeper.service import Platform, ProviderFallbackError

    runner = _FakeRunner()
    platform = Platform(
        settings=None, chronicle=None, runtime=None, herald=None, ledger=None,
        forge=None, warden=None, runner=runner, system_password="pw",
    )
    with pytest.raises(ProviderFallbackError) as exc:
        await platform.run("r1", _project())
    assert "vanished-cli" in str(exc.value) and "azure" in str(exc.value)
    assert runner.kwargs is None, "nothing was provisioned"


# --- the launch endpoints refuse a silent substitution (#47) ----------------------------------
class _LaunchManager(FakeManager):
    """FakeManager with the bits the launch endpoints touch."""

    def __init__(self):
        self.runs: dict[str, object] = {}
        self.started: list[tuple[str, dict]] = []

    def start(self, realm_id, project, **kw):
        self.started.append((realm_id, kw))

    def active(self):
        return [r for r, _ in self.started]


def _tiny_package(tmp_path):
    import json
    pkg = tmp_path / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "project.json").write_text(json.dumps({
        "metadata": {"name": "duel"},
        "spec": {"termination": [{"type": "manual"}]},
        "agents": [{"id": "v", "model": {"provider": "azure", "model": "m",
                                         "api_key_ref": "azure-main"}}],
    }))
    return pkg


@pytest.mark.asyncio
async def test_launching_on_a_fallback_is_a_400_that_names_both_providers(tmp_path, monkeypatch):
    """Structured like the missing-parameter 400 (ADR-003): the UI renders it into a decision."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_unknown_provider(tmp_path)
    pkg = _tiny_package(tmp_path)
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    manager = _LaunchManager()
    with TestClient(create_app(chron=chron, manager=manager)) as c:
        r = c.post("/api/realms", json={"package": str(pkg)})
    await chron.close()
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert detail["provider_fallback"]["stored"] == "vanished-cli"
    assert detail["provider_fallback"]["effective"] == "azure"
    assert "allow_provider_fallback" in detail["hint"]
    assert manager.started == [], "no realm was provisioned"


@pytest.mark.asyncio
async def test_consent_lets_the_launch_through_and_reaches_the_platform(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_unknown_provider(tmp_path)
    pkg = _tiny_package(tmp_path)
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    manager = _LaunchManager()
    with TestClient(create_app(chron=chron, manager=manager)) as c:
        r = c.post("/api/realms",
                   json={"package": str(pkg), "allow_provider_fallback": True})
    await chron.close()
    assert r.status_code == 200, r.text
    # consent must travel all the way to Platform.run, or the backstop rejects the run later
    assert manager.started[0][1]["allow_provider_fallback"] is True


@pytest.mark.asyncio
async def test_an_ordinary_launch_is_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    pkg = _tiny_package(tmp_path)
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    manager = _LaunchManager()
    with TestClient(create_app(chron=chron, manager=manager)) as c:
        r = c.post("/api/realms", json={"package": str(pkg)})
    await chron.close()
    assert r.status_code == 200, r.text
    assert manager.started[0][1]["allow_provider_fallback"] is False


@pytest.mark.asyncio
async def test_rerun_is_gated_too(tmp_path, monkeypatch):
    """Rerun is a launch. It was the easier one to forget: it takes no request body."""
    monkeypatch.setenv("HOME", str(tmp_path))
    _store_unknown_provider(tmp_path)
    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await chron.append_event("old", EventKind.LIFECYCLE, {
        "event": "running", "require_mention": True,
        "project": _project().model_dump(mode="json"),
    })
    manager = _LaunchManager()
    with TestClient(create_app(chron=chron, manager=manager)) as c:
        blocked = c.post("/api/realms/old/rerun?mode=snapshot")
        allowed = c.post("/api/realms/old/rerun?mode=snapshot&allow_provider_fallback=true")
    await chron.close()
    assert blocked.status_code == 400, blocked.text
    assert blocked.json()["detail"]["provider_fallback"]["stored"] == "vanished-cli"
    assert allowed.status_code == 200, allowed.text
    assert manager.started[0][1]["allow_provider_fallback"] is True
