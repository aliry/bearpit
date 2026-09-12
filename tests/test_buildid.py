"""The fingerprint that says whether two copies of Bearpit are running the same code (#106).

A stale realmtools image is the one failure in this system that announces itself with nothing: the
realm runs, concludes, and writes a verdict that looks exactly like a good one. It has happened
twice — once producing a spurious "crew win" from an among-us realm.
"""
from __future__ import annotations

import pytest

from bearpit.core.buildid import RUNTIME_PACKAGES, source_fingerprint


def test_the_same_tree_fingerprints_the_same(tmp_path):
    """Two copies of one tree must agree, or the check cries wolf on every launch."""
    for pkg in RUNTIME_PACKAGES:
        (tmp_path / "a" / pkg).mkdir(parents=True)
        (tmp_path / "b" / pkg).mkdir(parents=True)
        (tmp_path / "a" / pkg / "m.py").write_text("x = 1\n")
        (tmp_path / "b" / pkg / "m.py").write_text("x = 1\n")
    assert source_fingerprint(tmp_path / "a") == source_fingerprint(tmp_path / "b")


def test_one_changed_byte_changes_the_fingerprint(tmp_path):
    for pkg in RUNTIME_PACKAGES:
        (tmp_path / "a" / pkg).mkdir(parents=True)
        (tmp_path / "b" / pkg).mkdir(parents=True)
        (tmp_path / "a" / pkg / "m.py").write_text("x = 1\n")
        (tmp_path / "b" / pkg / "m.py").write_text("x = 1\n")
    (tmp_path / "b" / RUNTIME_PACKAGES[0] / "m.py").write_text("x = 2\n")
    assert source_fingerprint(tmp_path / "a") != source_fingerprint(tmp_path / "b")


def test_a_file_that_only_one_side_has_changes_it(tmp_path):
    """The live miss: `audit.py` existed on the host and not in the image. A fingerprint that only
    hashed the files present on BOTH sides would have called that pair identical."""
    for pkg in RUNTIME_PACKAGES:
        (tmp_path / "a" / pkg).mkdir(parents=True)
        (tmp_path / "b" / pkg).mkdir(parents=True)
        (tmp_path / "a" / pkg / "m.py").write_text("x = 1\n")
        (tmp_path / "b" / pkg / "m.py").write_text("x = 1\n")
    (tmp_path / "b" / RUNTIME_PACKAGES[0] / "extra.py").write_text("y = 2\n")
    assert source_fingerprint(tmp_path / "a") != source_fingerprint(tmp_path / "b")


def test_compiled_caches_and_non_python_are_ignored(tmp_path):
    """A `__pycache__` appears the moment anything imports the tree, and differs between a host
    that has run the tests and a freshly built image. Counting it would make every launch stale."""
    for pkg in RUNTIME_PACKAGES:
        (tmp_path / "a" / pkg).mkdir(parents=True)
        (tmp_path / "b" / pkg / "__pycache__").mkdir(parents=True)
        (tmp_path / "a" / pkg / "m.py").write_text("x = 1\n")
        (tmp_path / "b" / pkg / "m.py").write_text("x = 1\n")
        (tmp_path / "b" / pkg / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\x00\x01")
        (tmp_path / "b" / pkg / "notes.md").write_text("not code")
    assert source_fingerprint(tmp_path / "a") == source_fingerprint(tmp_path / "b")


def test_only_the_code_realmtools_actually_runs_counts(tmp_path):
    """`gatekeeper` is host-only — the control plane imports it, the realmtools container does not.
    Refusing a launch because a docstring changed in a package the image never executes is how a
    safety check gets switched off."""
    for pkg in RUNTIME_PACKAGES:
        (tmp_path / "a" / pkg).mkdir(parents=True)
        (tmp_path / "b" / pkg).mkdir(parents=True)
        (tmp_path / "a" / pkg / "m.py").write_text("x = 1\n")
        (tmp_path / "b" / pkg / "m.py").write_text("x = 1\n")
    (tmp_path / "a" / "gatekeeper").mkdir()
    (tmp_path / "a" / "gatekeeper" / "api.py").write_text("host only\n")
    assert source_fingerprint(tmp_path / "a") == source_fingerprint(tmp_path / "b")
    assert "gatekeeper" not in RUNTIME_PACKAGES


def test_the_real_tree_fingerprints_without_error():
    """It must work on the actual package, not just on fixtures."""
    fp = source_fingerprint()
    assert isinstance(fp, str) and len(fp) == 12
    assert fp == source_fingerprint(), "it must be stable across calls"


def test_a_missing_tree_is_an_error_not_a_silent_match(tmp_path):
    """Fingerprinting nothing must not quietly equal fingerprinting nothing else — that would make
    a misconfigured path read as 'the code matches'."""
    with pytest.raises(FileNotFoundError):
        source_fingerprint(tmp_path / "nope")
