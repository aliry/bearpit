"""Provider plugins — how an installed package contributes a model pipeline to the platform.

A *provider* is mostly data: a profile mapping each capability tier to a concrete model, plus the
policy fields the resolver honours (`core.providers`). Any package can contribute one by declaring
an entry point in the `bearpit.providers` group; its profiles are merged into the built-in table
and become selectable on the Settings page like any other pipeline.

Two things about a pipeline are NOT data, because they are properties of how a particular runtime
wants to be *talked to* rather than of the platform:

  * `encode_model`      — some proxies expect reasoning effort encoded into the model string rather
                          than sent as a request parameter.
  * `agent_request_key` — some runtimes need the agent's own realm identity token forwarded as the
                          request credential, so the thing on the far side can act AS that agent.

Those live behind `ProviderHooks`, whose defaults are exactly the plain behaviour: send the model
name, send the effort, use the keystore credential. A provider that needs neither — every API
provider the platform ships — never touches this module.

A plugin that fails to import, or that raises, is logged and skipped. A third-party package must
never be able to stop the platform from starting.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import EntryPoint, entry_points
from typing import Any, Protocol, runtime_checkable

from bearpit.core.schema import ModelRef

PROVIDER_GROUP = "bearpit.providers"
COMMAND_GROUP = "bearpit.commands"

log = logging.getLogger(__name__)


def _default_encode_model(model: ModelRef) -> tuple[str, str | None]:
    """The plain encoding: the model name, and the effort as a separate request parameter."""
    return model.model, model.effort


def _no_key_override(model: ModelRef, realm_token: str | None) -> str | None:
    """The plain credential: whatever the keystore resolves for this provider."""
    return None


@dataclass(frozen=True)
class ProviderHooks:
    """Transport quirks of one provider. Both default to the plain behaviour."""

    encode_model: Callable[[ModelRef], tuple[str, str | None]] = _default_encode_model
    agent_request_key: Callable[[ModelRef, str | None], str | None] = _no_key_override


DEFAULT_HOOKS = ProviderHooks()


@runtime_checkable
class ProviderPlugin(Protocol):
    """What an `bearpit.providers` entry point must resolve to."""

    def profiles(self) -> dict[str, dict[str, Any]]:
        """Provider profiles this plugin contributes, keyed by provider name."""

    def hooks(self, provider: str) -> ProviderHooks | None:
        """Transport hooks for one of this plugin's providers, or None for the defaults."""


def _entry_points(group: str) -> list[EntryPoint]:
    """Discovery, isolated so tests can substitute it."""
    return list(entry_points(group=group))


def _load(ep: EntryPoint) -> ProviderPlugin | None:
    try:
        obj = ep.load()
    except Exception as exc:  # noqa: BLE001 - a broken plugin must not break the platform
        log.warning("provider plugin %r failed to load: %s", ep.name, exc)
        return None
    # An entry point may resolve to a ready instance, a class, or a factory function. A class has a
    # `profiles` attribute (the unbound function), so test for it explicitly rather than by shape.
    if isinstance(obj, type) or (callable(obj) and not hasattr(obj, "profiles")):
        try:
            obj = obj()
        except Exception as exc:  # noqa: BLE001
            log.warning("provider plugin %r failed to construct: %s", ep.name, exc)
            return None
    if not callable(getattr(obj, "profiles", None)):
        log.warning("provider plugin %r has no profiles() — ignored", ep.name)
        return None
    return obj  # type: ignore[no-any-return]


_plugins: tuple[ProviderPlugin, ...] | None = None
_owners: dict[str, ProviderPlugin] | None = None


def load_plugins() -> tuple[ProviderPlugin, ...]:
    """Every installed provider plugin. Discovered once per process."""
    global _plugins
    if _plugins is None:
        _plugins = tuple(p for p in (_load(ep) for ep in _entry_points(PROVIDER_GROUP)) if p)
    return _plugins


def plugin_profiles() -> dict[str, dict[str, Any]]:
    """Profiles contributed by installed plugins, merged in installation order."""
    out: dict[str, dict[str, Any]] = {}
    for plugin in load_plugins():
        try:
            contributed = plugin.profiles()
        except Exception as exc:  # noqa: BLE001
            log.warning("provider plugin %r raised in profiles(): %s", plugin, exc)
            continue
        if isinstance(contributed, dict):
            out.update({k: v for k, v in contributed.items() if isinstance(v, dict)})
    return out


def _owner_of(provider: str) -> ProviderPlugin | None:
    global _owners
    if _owners is None:
        owners: dict[str, ProviderPlugin] = {}
        for plugin in load_plugins():
            try:
                names = list(plugin.profiles())
            except Exception:  # noqa: BLE001 - already warned in plugin_profiles()
                continue
            for name in names:
                owners.setdefault(name, plugin)
        _owners = owners
    return _owners.get(provider)


def hooks_for(provider: str) -> ProviderHooks:
    """The transport hooks for `provider` — the plain defaults unless a plugin overrides them."""
    plugin = _owner_of(provider)
    if plugin is None or not callable(getattr(plugin, "hooks", None)):
        return DEFAULT_HOOKS
    try:
        hooks = plugin.hooks(provider)
    except Exception as exc:  # noqa: BLE001
        log.warning("provider plugin %r raised in hooks(%r): %s", plugin, provider, exc)
        return DEFAULT_HOOKS
    return hooks if isinstance(hooks, ProviderHooks) else DEFAULT_HOOKS


def reset_plugin_cache() -> None:
    """Forget discovered plugins (tests, and any process that installs one at runtime)."""
    global _plugins, _owners
    _plugins = None
    _owners = None


def load_command_plugins(app: Any) -> None:
    """Let installed packages contribute `pit` subcommands (`bearpit.commands`).

    Each entry point is a callable taking the Typer app. A failing one is reported and skipped —
    a broken plugin costs you its command, not the CLI."""
    for ep in _entry_points(COMMAND_GROUP):
        try:
            register = ep.load()
            register(app)
        except Exception as exc:  # noqa: BLE001 - never let a plugin break `pit`
            log.warning("command plugin %r failed to register: %s", ep.name, exc)
