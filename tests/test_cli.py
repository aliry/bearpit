"""CLI: version, validate (ok + failure), schema export, and read commands (status/tail/archive)."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bearpit.chronicle import Chronicle, EventKind
from bearpit.cli.main import app
from bearpit.core.jsonschema import agent_schema, project_schema

runner = CliRunner()


def _flat_manifest(p: Path) -> Path:
    f = p / "project.json"
    f.write_text(json.dumps({
        "metadata": {"name": "flat-demo"},
        "spec": {"termination": [{"type": "manual"}]},
        "agents": [
            {"id": "vela",
             "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}},
            {"id": "themis", "role": "referee",
             "model": {"provider": "azure", "model": "m", "api_key_ref": "azure-main"}},
        ],
    }))
    return f


def test_version():
    r = runner.invoke(app, ["version"])
    assert r.exit_code == 0 and "bearpit" in r.output


def test_validate_ok(tmp_path: Path):
    f = _flat_manifest(tmp_path)
    r = runner.invoke(app, ["validate", str(f)])
    assert r.exit_code == 0
    assert "flat-demo" in r.output and "referee:  themis" in r.output


def test_validate_rejects_secret(tmp_path: Path):
    f = tmp_path / "project.json"
    f.write_text(json.dumps({
        "metadata": {"name": "bad"},
        "agents": [{"id": "vela",
                    "model": {"provider": "openai", "model": "m",
                              "api_key_ref": "sk-secret000111222333"}}],
    }))
    r = runner.invoke(app, ["validate", str(f)])
    assert r.exit_code == 1
    assert "invalid" in r.output.lower()


def test_schema_export(tmp_path: Path):
    r = runner.invoke(app, ["schema", "--out", str(tmp_path)])
    assert r.exit_code == 0
    proj = json.loads((tmp_path / "project.schema.json").read_text())
    agent = json.loads((tmp_path / "agent.schema.json").read_text())
    assert proj["properties"]["apiVersion"]  # alias honored
    # ModelRef is nested via $defs; descriptions flow from the models into the schema
    model_props = agent["$defs"]["ModelRef"]["properties"]
    assert "api_key_ref" in model_props
    assert model_props["api_key_ref"]["description"]


def test_schema_functions_shape():
    assert project_schema()["title"] == "Project"
    assert agent_schema()["title"] == "AgentSpec"


# --- read commands against a temp SQLite chronicle --------------------------
@pytest.fixture
def db(tmp_path: Path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'chron.db'}"
    monkeypatch.setenv("BEARPIT_DATABASE_URL", url)
    return url


async def _seed(url: str) -> None:
    c = await Chronicle.connect(url)
    await c.append_event("duel1", EventKind.LIFECYCLE, {"event": "running"})
    await c.append_event("duel1", EventKind.SPEND, {"agent": "vela", "usd": 0.03})
    await c.append_event("duel1", EventKind.SPEND, {"agent": "vela", "usd": 0.01})
    await c.record_message("duel1", "!c", "@vela", "rock")
    await c.record_message("duel1", "!c", "@orin", "paper")
    await c.close()


def test_status(db: str):
    import asyncio

    asyncio.run(_seed(db))
    r = runner.invoke(app, ["status", "duel1"])
    assert r.exit_code == 0
    assert "state=running" in r.output
    assert "spend vela: $0.0400" in r.output  # 0.03 + 0.01


def test_tail(db: str):
    import asyncio

    asyncio.run(_seed(db))
    r = runner.invoke(app, ["tail", "duel1", "-n", "1"])
    assert r.exit_code == 0
    assert "@orin: paper" in r.output and "@vela: rock" not in r.output  # only last 1


def test_archive(db: str, tmp_path: Path):
    import asyncio

    asyncio.run(_seed(db))
    out = tmp_path / "archives"
    r = runner.invoke(app, ["archive", "duel1", "--out", str(out)])
    assert r.exit_code == 0
    assert (out / "duel1" / "transcript.txt").exists()
    report = (out / "duel1" / "report.md").read_text()
    assert "vela: $0.0400" in report


def test_stop_sets_flag(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))
    r = runner.invoke(app, ["stop", "myrealm"])
    assert r.exit_code == 0
    assert (tmp_path / ".bearpit" / "realms" / "myrealm.stop").exists()


# ------------------------------------------------------------------ parameters (ADR-003, #41)

def _param_manifest(p: Path, spec_extra: dict | None = None) -> Path:
    f = p / "project.json"
    f.write_text(json.dumps({
        "metadata": {"name": "param-demo", "description": "a ${category,fruit} relay"},
        "spec": {
            "goals": ["reach ${target,10,Points to win}", "name it ${team_name,,Who is playing}"],
            "termination": [{"type": "manual"}],
            **(spec_extra or {}),
        },
        "agents": [{"id": "orin", "model_category": "medium",
                    "persona": "You play for ${team_name}"}],
    }))
    return f


def test_params_lists_what_a_scenario_takes(tmp_path: Path) -> None:
    result = runner.invoke(app, ["params", str(_param_manifest(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "3 parameter(s), 1 required" in result.output
    assert "team_name" in result.output and "(required)" in result.output
    assert "Points to win" in result.output          # inline description
    assert "spec.goals[0]" in result.output          # where it is used


def test_params_shows_that_the_manifest_overrode_the_inline_default(tmp_path: Path) -> None:
    """The one real cost of letting the manifest win is an override the author cannot see."""
    f = _param_manifest(tmp_path, {"parameters": {"target": {"default": "99"}}})
    result = runner.invoke(app, ["params", str(f)])
    assert result.exit_code == 0, result.output
    assert "'99'" in result.output
    assert "overrides the inline default '10'" in result.output


def test_validate_lists_parameters_and_flags_required(tmp_path: Path) -> None:
    """`validate` is what an author runs after editing, so a typo that invents a parameter has to
    be visible there rather than at launch."""
    result = runner.invoke(app, ["validate", str(_param_manifest(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "parameters: 3" in result.output
    assert "team_name*" in result.output
    assert "1 required" in result.output


def test_validate_rejects_a_declaration_for_an_unused_parameter(tmp_path: Path) -> None:
    f = _param_manifest(tmp_path, {"parameters": {"ghost": {"default": "x"}}})
    result = runner.invoke(app, ["validate", str(f)])
    assert result.exit_code != 0
    assert "no scenario text uses" in result.output


def test_up_refuses_a_value_outside_its_choices(tmp_path: Path) -> None:
    """Rejected before anything is provisioned — a typo costs a message, not a container."""
    f = _param_manifest(tmp_path, {"parameters": {"category": {"choices": ["fruit", "colour"]}}})
    result = runner.invoke(app, ["up", str(f), "--param", "category=furniture",
                                 "--param", "team_name=X", "--yes"])
    assert result.exit_code == 2
    assert "must be one of" in result.output


def test_up_refuses_an_unknown_parameter(tmp_path: Path) -> None:
    result = runner.invoke(app, ["up", str(_param_manifest(tmp_path)),
                                 "--param", "nope=1", "--yes"])
    assert result.exit_code == 2
    assert "not a parameter of this scenario" in result.output


def test_up_refuses_a_malformed_param(tmp_path: Path) -> None:
    result = runner.invoke(app, ["up", str(_param_manifest(tmp_path)), "--param", "teamname"])
    assert result.exit_code == 2
    assert "must be name=value" in result.output


def test_up_warns_about_a_missing_value_and_stops_without_consent(tmp_path: Path) -> None:
    """A realm that spends real money on prose with holes in it should be a decision."""
    result = runner.invoke(app, ["up", str(_param_manifest(tmp_path))], input="n\n")
    assert result.exit_code == 1
    assert "have no value and no default" in result.output
    assert "team_name" in result.output
    assert "Continuing will leave them empty" in result.output


# --- the provider gate (#47) ------------------------------------------------------------------
def _unresolvable_provider(tmp_path: Path) -> None:
    import json
    d = Path(os.environ["HOME"]) / ".bearpit"
    d.mkdir(parents=True, exist_ok=True)
    (d / "platform.json").write_text(json.dumps({"model_provider": "vanished-cli"}))


def test_up_stops_when_the_configured_provider_is_unavailable(tmp_path: Path) -> None:
    """Declining costs a message. Not declining used to cost a metered run nobody chose."""
    _unresolvable_provider(tmp_path)
    result = runner.invoke(app, ["up", str(_param_manifest(tmp_path)), "-p", "team_name=X"],
                           input="n\n")
    assert result.exit_code == 1
    assert "vanished-cli" in result.output and "azure" in result.output


def test_up_says_nothing_about_providers_when_the_stored_one_resolves(tmp_path: Path) -> None:
    """The warning must be rare enough to mean something."""
    result = runner.invoke(app, ["up", str(_param_manifest(tmp_path)), "-p", "team_name=X"],
                           input="n\n")
    assert "vanished-cli" not in result.output


def test_up_does_not_say_it_twice(tmp_path: Path) -> None:
    """The module logs a WARNING and the CLI prints its own prompt; both reached the terminal, so
    the same problem appeared to happen twice."""
    _unresolvable_provider(tmp_path)
    result = runner.invoke(app, ["up", str(_param_manifest(tmp_path)), "-p", "team_name=X"],
                           input="n\n")
    assert result.output.count("its provider plugin is not installed") == 1


def test_up_honours_the_scenario_s_own_require_mention(tmp_path: Path, monkeypatch) -> None:
    """`pit up` computed `require_mention=not free_response`, ignoring the manifest entirely.

    The API has always read `spec.environment.require_mention`; the CLI never did. So a scenario
    that opts into free response — which is how a referee gets to SEE what it judges, since
    `referee_sees_all` is false whenever turns are on and mentions are required — ran mention-gated
    from the CLI and the referee saw nothing.

    Live, turns-debate's three advocates each posted a substantive argument and the chair ruled
    "no advocate's argument reached the judge". Eight shipped scenarios set this flag, six of them
    with referees.
    """
    import json

    seen: dict[str, object] = {}

    async def spy(realm_id, project, *, require_mention, parameters=None, **kw):
        seen["require_mention"] = require_mention
        return "done"

    monkeypatch.setattr("bearpit.cli.main._run_realm", spy)
    monkeypatch.setenv("HOME", str(tmp_path))

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "project.json").write_text(json.dumps({
        "metadata": {"name": "freeform"},
        "spec": {"termination": [{"type": "manual"}],
                 "environment": {"require_mention": False}},
        "agents": [{"id": "a", "model": {"provider": "azure", "model": "m",
                                         "api_key_ref": "azure-main"}},
                   {"id": "b", "model": {"provider": "azure", "model": "m",
                                         "api_key_ref": "azure-main"}}],
    }))

    result = runner.invoke(app, ["up", str(pkg), "-y"])
    assert result.exit_code == 0, result.output
    assert seen["require_mention"] is False, (
        "the CLI overrode the scenario's own environment policy"
    )
