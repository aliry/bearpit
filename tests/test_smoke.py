"""Smoke test: the package and all module boundaries import."""

import importlib

import pytest

MODULES = [
    "agentrealm",
    "agentrealm.core",
    "agentrealm.gatekeeper",
    "agentrealm.forge",
    "agentrealm.forge.adapters.hermes",
    "agentrealm.herald",
    "agentrealm.warden",
    "agentrealm.ledger",
    "agentrealm.realmtools",
    "agentrealm.cli",
]


@pytest.mark.parametrize("module", MODULES)
def test_imports(module: str) -> None:
    importlib.import_module(module)
