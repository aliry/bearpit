"""The global model-provider toggle: persisted state, the API, and the Platform.run apply seam."""

import pytest
from starlette.testclient import TestClient

from agentrealm.chronicle import Chronicle, EventKind
from agentrealm.core.schema import AgentSpec, ModelCategory, Project, ProjectMeta
from agentrealm.gatekeeper.api import create_app


def _project():
    return Project(metadata=ProjectMeta(name="p"), agents=[
        AgentSpec(id="lead", model_category=ModelCategory.LARGE),
        AgentSpec(id="helper", model_category=ModelCategory.SMALL),
    ])


# --- persisted state --------------------------------------------------------
def test_active_provider_defaults_to_azure(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agentrealm.gatekeeper import appstate
    assert appstate.active_provider() == "azure"


def test_set_and_read_active_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agentrealm.gatekeeper import appstate
    appstate.set_active_provider("anthropic")
    assert appstate.active_provider() == "anthropic"


def test_set_unknown_provider_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agentrealm.gatekeeper import appstate
    with pytest.raises(ValueError, match="unknown model provider"):
        appstate.set_active_provider("gemini")


def test_corrupt_state_falls_back_to_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".agentrealm").mkdir()
    (tmp_path / ".agentrealm" / "platform.json").write_text("{not json")
    from agentrealm.gatekeeper import appstate
    assert appstate.active_provider() == "azure"


# --- the API ----------------------------------------------------------------
class FakeManager:
    max_active = 6

    def active(self):
        return []


@pytest.fixture
async def app_client(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AGENTREALM_SCENARIOS_DIR", str(tmp_path / "scen"))
    monkeypatch.setenv("AGENTREALM_EXAMPLES_DIR", str(tmp_path / "examples"))
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

    async def run(self, realm_id, project, factory, **kw):
        self.captured = project
        return "done"


async def test_platform_run_applies_active_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from agentrealm.gatekeeper import appstate
    from agentrealm.gatekeeper.service import Platform

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
    from agentrealm.forge import RealmHandles
    from agentrealm.gatekeeper.service import Platform
    from agentrealm.herald import BusProvision

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
