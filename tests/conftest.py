import pytest


# Tests must never read the developer's home directory. Without this, ~/.bearpit/scenarios
# shadows examples/ — a stale saved copy of a scenario silently replaced the repo's own and the
# suite started asserting against whatever happened to be on this machine.
#
# HOME itself is redirected for the same reason, and it is not hypothetical: the active model
# provider lives in ~/.bearpit/platform.json, so with a real HOME the provider-fallback gate (#47)
# would pass or fail depending on which plugins happen to be installed on the machine running the
# suite.
@pytest.fixture(autouse=True)
def _isolate_user_dirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("BEARPIT_SCENARIOS_DIR", str(tmp_path / "scenarios"))
    monkeypatch.setenv("BEARPIT_SKILLS_DIR", str(tmp_path / "skills"))
