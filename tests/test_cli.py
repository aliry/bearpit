"""CLI: version, validate (ok + failure), schema export, and read commands (status/tail/archive)."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agentrealm.chronicle import Chronicle, EventKind
from agentrealm.cli.main import app
from agentrealm.core.jsonschema import agent_schema, project_schema

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
    assert r.exit_code == 0 and "agentrealm" in r.output


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
    monkeypatch.setenv("AGENTREALM_DATABASE_URL", url)
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
    assert (tmp_path / ".agentrealm" / "realms" / "myrealm.stop").exists()
