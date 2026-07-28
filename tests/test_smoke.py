"""Smoke test: the package and all module boundaries import."""

import importlib

import pytest

MODULES = [
    "bearpit",
    "bearpit.core",
    "bearpit.gatekeeper",
    "bearpit.forge",
    "bearpit.forge.adapters.hermes",
    "bearpit.herald",
    "bearpit.warden",
    "bearpit.ledger",
    "bearpit.realmtools",
    "bearpit.cli",
]


@pytest.mark.parametrize("module", MODULES)
def test_imports(module: str) -> None:
    importlib.import_module(module)
