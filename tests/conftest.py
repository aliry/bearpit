import pytest


# Tests must never read the developer's home directory. Without this, ~/.agentrealm/scenarios
# shadows examples/ — a stale saved copy of a scenario silently replaced the repo's own and the
# suite started asserting against whatever happened to be on this machine.
@pytest.fixture(autouse=True)
def _isolate_user_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTREALM_SCENARIOS_DIR", str(tmp_path / "scenarios"))
    monkeypatch.setenv("AGENTREALM_SKILLS_DIR", str(tmp_path / "skills"))
