"""The provider-plugin seam (#77): entry-point discovery, transport hooks, CLI contribution.

Every test drives a FAKE plugin through the discovery function, so nothing here depends on what is
actually installed. The load-bearing property is the last group: a third-party package must never be
able to stop the platform from starting, however badly it misbehaves.
"""

from __future__ import annotations

from typing import Any

import pytest
import typer

from bearpit.core import plugins
from bearpit.core.plugins import (
    DEFAULT_HOOKS,
    ProviderHooks,
    hooks_for,
    load_command_plugins,
    plugin_profiles,
)
from bearpit.core.providers import AZURE, default_providers, is_provider
from bearpit.core.schema import ModelRef


class _FakeEntryPoint:
    def __init__(self, name: str, value: Any, raises: Exception | None = None) -> None:
        self.name = name
        self._value = value
        self._raises = raises

    def load(self) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._value


def _install(monkeypatch: pytest.MonkeyPatch, group: str, eps: list[_FakeEntryPoint]) -> None:
    """Point discovery at `eps` for `group` (and nothing for any other group)."""
    monkeypatch.setattr(
        plugins, "_entry_points", lambda g: list(eps) if g == group else []  # type: ignore[arg-type]
    )
    plugins.reset_plugin_cache()


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    plugins.reset_plugin_cache()
    yield
    plugins.reset_plugin_cache()


_PROFILE = {
    "label": "Fake",
    "api_key_ref": "fake-main",
    "categories": {
        "small": {"model": "fake-s"}, "medium": {"model": "fake-m"}, "large": {"model": "fake-l"},
    },
}


class _FakePlugin:
    def __init__(self, hooks: ProviderHooks | None = None) -> None:
        self._hooks = hooks

    def profiles(self) -> dict[str, dict[str, Any]]:
        return {"fake": dict(_PROFILE)}

    def hooks(self, provider: str) -> ProviderHooks | None:
        return self._hooks if provider == "fake" else None


# --- discovery + profile merge ----------------------------------------------------------------
def test_a_plugins_profiles_join_the_provider_table(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, plugins.PROVIDER_GROUP, [_FakeEntryPoint("fake", _FakePlugin())])

    assert plugin_profiles()["fake"]["label"] == "Fake"
    cfg = default_providers()
    assert "fake" in cfg and AZURE in cfg  # additive: the built-ins survive
    assert is_provider("fake")


def test_an_entry_point_may_be_a_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, plugins.PROVIDER_GROUP, [_FakeEntryPoint("fake", _FakePlugin)])
    assert "fake" in plugin_profiles()


def test_no_plugins_installed_leaves_the_built_in_table_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, plugins.PROVIDER_GROUP, [])
    assert plugin_profiles() == {}
    assert AZURE in default_providers()


# --- hooks ------------------------------------------------------------------------------------
def _ref(effort: str | None = "high") -> ModelRef:
    return ModelRef(provider="fake", model="fake-m", api_key_ref="fake-main", effort=effort)


def test_default_hooks_are_the_plain_behaviour() -> None:
    assert DEFAULT_HOOKS.encode_model(_ref()) == ("fake-m", "high")
    assert DEFAULT_HOOKS.agent_request_key(_ref(), "tok") is None


def test_unknown_provider_gets_the_default_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, plugins.PROVIDER_GROUP, [_FakeEntryPoint("fake", _FakePlugin())])
    assert hooks_for("azure") is DEFAULT_HOOKS
    assert hooks_for("nonesuch") is DEFAULT_HOOKS


def test_a_plugins_hooks_are_returned_for_its_own_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    custom = ProviderHooks(
        encode_model=lambda m: (f"{m.model}::{m.effort}", None),
        agent_request_key=lambda m, token: token,
    )
    _install(monkeypatch, plugins.PROVIDER_GROUP, [_FakeEntryPoint("fake", _FakePlugin(custom))])

    assert hooks_for("fake").encode_model(_ref()) == ("fake-m::high", None)
    assert hooks_for("fake").agent_request_key(_ref(), "tok-1") == "tok-1"
    assert hooks_for("azure") is DEFAULT_HOOKS  # scoped to the plugin's own providers


# --- a broken plugin must never break the platform ---------------------------------------------
def test_a_plugin_that_fails_to_import_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        plugins.PROVIDER_GROUP,
        [
            _FakeEntryPoint("broken", None, raises=ImportError("no such module")),
            _FakeEntryPoint("fake", _FakePlugin()),
        ],
    )
    assert list(plugin_profiles()) == ["fake"]  # the good one still loads
    assert AZURE in default_providers()


def test_a_plugin_of_the_wrong_shape_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, plugins.PROVIDER_GROUP, [_FakeEntryPoint("junk", object())])
    assert plugin_profiles() == {}


def test_a_plugin_that_raises_in_profiles_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Exploding:
        def profiles(self) -> dict[str, dict[str, Any]]:
            raise RuntimeError("boom")

    _install(monkeypatch, plugins.PROVIDER_GROUP, [_FakeEntryPoint("boom", _Exploding())])
    assert plugin_profiles() == {}
    assert hooks_for("anything") is DEFAULT_HOOKS


def test_a_plugin_that_raises_in_hooks_falls_back_to_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadHooks(_FakePlugin):
        def hooks(self, provider: str) -> ProviderHooks | None:
            raise RuntimeError("boom")

    _install(monkeypatch, plugins.PROVIDER_GROUP, [_FakeEntryPoint("fake", _BadHooks())])
    assert hooks_for("fake") is DEFAULT_HOOKS


def test_a_plugin_returning_a_non_hooks_object_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _WrongHooks(_FakePlugin):
        def hooks(self, provider: str) -> Any:
            return {"encode_model": "nope"}

    _install(monkeypatch, plugins.PROVIDER_GROUP, [_FakeEntryPoint("fake", _WrongHooks())])
    assert hooks_for("fake") is DEFAULT_HOOKS


# --- CLI subcommand contribution ---------------------------------------------------------------
def test_a_plugin_can_contribute_a_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    def register(app: typer.Typer) -> None:
        @app.command()
        def contributed() -> None:
            """A command from a plugin."""

    _install(monkeypatch, plugins.COMMAND_GROUP, [_FakeEntryPoint("extra", register)])
    app = typer.Typer()
    load_command_plugins(app)

    assert [c.name or c.callback.__name__ for c in app.registered_commands] == ["contributed"]


def test_a_broken_command_plugin_does_not_kill_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    def good(app: typer.Typer) -> None:
        @app.command()
        def ok() -> None:
            """Fine."""

    def bad(app: typer.Typer) -> None:
        raise RuntimeError("boom")

    _install(
        monkeypatch,
        plugins.COMMAND_GROUP,
        [
            _FakeEntryPoint("bad", bad),
            _FakeEntryPoint("explodes-on-load", None, raises=ImportError("gone")),
            _FakeEntryPoint("good", good),
        ],
    )
    app = typer.Typer()
    load_command_plugins(app)  # must not raise

    assert len(app.registered_commands) == 1
