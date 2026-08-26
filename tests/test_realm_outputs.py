"""Realm outputs (#84, ADR-005): a scenario declares the files its run produces.

The failure this removes: `teardown_realm` destroys the shared volume and nothing reads it first,
so a scenario whose entire deliverable is a file loses it. Recovering one real brief meant scraping
`run_code` traffic out of the chronicle and reassembling it from code an agent happened to print.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from bearpit.core.schema import Project, ProjectMeta, ProjectSpec


class _Runtime:
    """Just the volume surface the capture path touches."""

    def __init__(self, files: dict[str, str] | None = None, explode: bool = False) -> None:
        self.files = files or {}
        self.explode = explode
        self.removed: list[str] = []

    def read_volume(self, name: str) -> dict[str, str]:
        if self.explode:
            raise RuntimeError("volume unreadable")
        return dict(self.files)

    def remove_volume(self, name: str) -> None:
        self.removed.append(name)


def _capture(tmp_path: Path, globs: list[str], files: dict[str, str],
             explode: bool = False) -> tuple[list[dict[str, Any]], _Runtime]:
    from bearpit.forge.outputs import capture_outputs

    rt = _Runtime(files, explode=explode)
    records = capture_outputs(rt, "r1", "realm-r1-shared", globs, tmp_path)
    return records, rt


def test_a_declared_file_is_written_to_disk_and_recorded(tmp_path):
    records, _ = _capture(tmp_path, ["brief.md"], {"brief.md": "# Brief\n\nbody",
                                                   "README.txt": "seeded by the platform"})
    saved = tmp_path / "r1" / "outputs" / "brief.md"
    assert saved.read_text() == "# Brief\n\nbody"
    assert not (tmp_path / "r1" / "outputs" / "README.txt").exists(), "undeclared files stay out"

    assert len(records) == 1
    rec = records[0]
    assert rec["path"] == "brief.md"
    assert rec["bytes"] == len("# Brief\n\nbody")
    assert rec["sha256"] == hashlib.sha256(b"# Brief\n\nbody").hexdigest()
    assert rec.get("missing") is not True


def test_globs_match_nested_paths_and_preserve_them(tmp_path):
    records, _ = _capture(tmp_path, ["sections/*.md"], {
        "sections/api.md": "A", "sections/store.md": "B", "notes.txt": "C",
    })
    assert (tmp_path / "r1" / "outputs" / "sections" / "api.md").read_text() == "A"
    assert sorted(r["path"] for r in records) == ["sections/api.md", "sections/store.md"]


def test_a_declared_output_that_was_never_written_is_recorded_as_missing(tmp_path):
    """triad-build has ended twice with four good section files and no assembled design.md.
    'The deliverable was never written' is a result the record should state, not a silence."""
    records, _ = _capture(tmp_path, ["design.md"], {"sections/api.md": "A"})
    assert records == [{"path": "design.md", "missing": True}]
    assert not (tmp_path / "r1" / "outputs").exists() or \
        not list((tmp_path / "r1" / "outputs").iterdir())


def test_declaring_nothing_captures_nothing(tmp_path):
    """Most scenarios produce no files and must not grow an outputs directory."""
    records, _ = _capture(tmp_path, [], {"brief.md": "x"})
    assert records == []
    assert not (tmp_path / "r1" / "outputs").exists()


def test_an_unreadable_volume_never_raises(tmp_path):
    """Best-effort, like the flight recorder. A realm that cannot save its output must still
    release its containers, its network and its keys."""
    records, _ = _capture(tmp_path, ["brief.md"], {}, explode=True)
    assert records == []


def test_a_path_cannot_escape_the_outputs_directory(tmp_path):
    """The volume's contents are written by agents. A name is untrusted input."""
    records, _ = _capture(tmp_path, ["*"], {"../../escape.md": "nope", "ok.md": "fine"})
    assert (tmp_path / "r1" / "outputs" / "ok.md").is_file()
    assert not (tmp_path.parent / "escape.md").exists()
    assert all(".." not in r["path"] for r in records)


# --- the schema ---------------------------------------------------------------------------------
def test_spec_outputs_defaults_to_empty_and_accepts_globs():
    assert Project(metadata=ProjectMeta(name="p")).spec.outputs == []
    p = Project(metadata=ProjectMeta(name="p"),
                spec=ProjectSpec(outputs=["brief.md", "sections/*.md"]))
    assert p.spec.outputs == ["brief.md", "sections/*.md"]


@pytest.mark.parametrize("bad", ["/etc/passwd", "../secrets", "a/../../b"])
def test_an_output_pattern_cannot_be_absolute_or_climb(bad):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Project(metadata=ProjectMeta(name="p"), spec=ProjectSpec(outputs=[bad]))


# --- the API surface -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_api_lists_outputs_and_serves_one_for_download(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from bearpit.chronicle import Chronicle, EventKind
    from bearpit.gatekeeper.api import create_app

    monkeypatch.setenv("HOME", str(tmp_path))
    out = tmp_path / ".bearpit" / "realms" / "r1" / "outputs"
    out.mkdir(parents=True)
    (out / "brief.md").write_text("# Brief\n\nthe body")

    chron = await Chronicle.connect("sqlite+aiosqlite:///:memory:")
    await chron.append_event("r1", EventKind.OUTPUT,
                             {"path": "brief.md", "bytes": 17, "sha256": "abc"})
    await chron.append_event("r1", EventKind.OUTPUT, {"path": "design.md", "missing": True})

    class _M:
        max_active = 6
        runs: dict = {}

        def active(self):
            return []

    with TestClient(create_app(chron=chron, manager=_M())) as c:
        listed = c.get("/api/realms/r1/outputs").json()["outputs"]
        got = c.get("/api/realms/r1/outputs/brief.md")
        escape = c.get("/api/realms/r1/outputs/../../../../etc/passwd")
        absent = c.get("/api/realms/r1/outputs/design.md")
    await chron.close()

    by_path = {o["path"]: o for o in listed}
    assert by_path["brief.md"]["available"] is True
    assert by_path["design.md"]["missing"] is True
    assert by_path["design.md"]["available"] is False, "a missing file must not offer a download"

    assert got.status_code == 200 and got.text == "# Brief\n\nthe body"
    assert "attachment" in got.headers["content-disposition"]
    assert escape.status_code in (400, 404), "a traversal reached outside the realm"
    assert absent.status_code == 404
