"""Refusing to launch against a realmtools image that is not the code in the tree (#106)."""
from __future__ import annotations

from bearpit.forge.staleness import check_realmtools_image


class FakeRuntime:
    """Protocol-for-IO: the real one shells into Docker, this one answers."""

    def __init__(self, exit_code=0, output="abc123def456\n"):
        self.calls: list[tuple[str, str]] = []
        self._exit, self._out = exit_code, output

    def exec_python(self, container_id, code, *, timeout_s=30, user="10000"):
        self.calls.append((container_id, code))
        return self._exit, self._out


def test_matching_fingerprints_are_a_pass():
    r = FakeRuntime(0, "abc123def456\n")
    c = check_realmtools_image(r, "pit-realmtools", host_fingerprint="abc123def456")
    assert c.matches and c.container == "abc123def456"


def test_a_different_fingerprint_names_both_sides():
    """The operator has to know WHICH is stale, and a message that says only 'mismatch' sends them
    to rebuild the wrong thing."""
    r = FakeRuntime(0, "999999999999\n")
    c = check_realmtools_image(r, "pit-realmtools", host_fingerprint="abc123def456")
    assert not c.matches
    assert "abc123def456" in c.detail and "999999999999" in c.detail
    assert "build realmtools" in c.detail, "it must say how to fix it"


def test_an_image_too_old_to_answer_is_stale_not_an_error():
    """The check lives in the code it checks, so an image old enough to predate it cannot report a
    fingerprint at all — that failure is itself conclusive, and must read as stale rather than
    crash a launch or, worse, pass it."""
    r = FakeRuntime(1, "ModuleNotFoundError: No module named 'bearpit.core.buildid'")
    c = check_realmtools_image(r, "pit-realmtools", host_fingerprint="abc123def456")
    assert not c.matches and c.container is None
    assert "could not report" in c.detail


def test_noise_around_the_answer_is_tolerated():
    """A container can print a warning before the value; taking the last line keeps a stray log
    line from reading as a mismatch."""
    r = FakeRuntime(0, "warning: something\nabc123def456\n")
    assert check_realmtools_image(
        r, "pit-realmtools", host_fingerprint="abc123def456").matches


def test_an_unusable_answer_is_stale_rather_than_trusted():
    """Anything that is not a fingerprint must fail closed. Reading garbage as a match is the one
    outcome this check exists to prevent."""
    for junk in ("", "\n", "not-a-hash!!", "abc"):
        c = check_realmtools_image(FakeRuntime(0, junk), "c", host_fingerprint="abc123def456")
        assert not c.matches, f"{junk!r} must not pass"


def test_a_docker_failure_fails_closed():
    """If the runtime itself raises, a launch must not proceed on the assumption it was fine."""
    class Boom:
        def exec_python(self, *a, **k):
            raise RuntimeError("docker daemon unreachable")
    c = check_realmtools_image(Boom(), "c", host_fingerprint="abc123def456")
    assert not c.matches and "docker daemon unreachable" in c.detail


def test_it_asks_the_named_container_and_runs_the_shared_snippet():
    from bearpit.core.buildid import FINGERPRINT_SNIPPET
    r = FakeRuntime()
    check_realmtools_image(r, "pit-realmtools", host_fingerprint="abc123def456")
    assert r.calls and r.calls[0][0] == "pit-realmtools"
    assert r.calls[0][1] == FINGERPRINT_SNIPPET, "both sides must run the same function"
