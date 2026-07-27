"""The deploy stack's env contract.

`deploy/.env.example` is the first thing a newcomer copies, and the README's first command fails
outright if it is missing a variable the compose file requires. That is exactly what happened before
this test existed: the example defined 2 of the 4 required variables, so `docker compose up` aborted
with `required variable REALMTOOLS_SECRET is missing a value` for every new user.

These are static checks over the two files — no Docker, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
COMPOSE = DEPLOY / "docker-compose.yaml"
EXAMPLE = DEPLOY / ".env.example"

# ${VAR}, ${VAR:-default}, ${VAR:?message}
_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(:[-?][^}]*)?\}")


def _referenced() -> dict[str, list[str | None]]:
    """Every env var the compose file interpolates -> EVERY modifier it appears with.

    A list, not a single value: the same variable is referenced more than once (a password is both
    declared on the database service and interpolated into a DSN), with different modifiers each
    time. Keeping only the last one hides a weak `:-default` behind a later bare `${VAR}`.
    """
    out: dict[str, list[str | None]] = {}
    for m in _REF.finditer(COMPOSE.read_text()):
        out.setdefault(m.group(1), []).append(m.group(2))
    return out


def _defined() -> set[str]:
    """Every var the example defines, commented-out optionals included."""
    out = set()
    for line in EXAMPLE.read_text().splitlines():
        line = line.strip().lstrip("#").strip()
        m = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if m:
            out.add(m.group(1))
    return out


def test_every_required_variable_is_in_the_example() -> None:
    """A var with no default (or an explicit `:?`) aborts `docker compose up` when unset."""
    required = {
        name for name, mods in _referenced().items()
        if any(m is None or m.startswith(":?") for m in mods)
    }
    assert required, "expected the compose file to require at least one variable"
    missing = sorted(required - _defined())
    assert not missing, f"deploy/.env.example is missing required vars: {missing}"


def test_no_secret_defaults_to_a_weak_shipped_value() -> None:
    """A `:-` default on a credential is a weak secret every user silently inherits."""
    weak = sorted(
        name for name, mods in _referenced().items()
        if any(m and m.startswith(":-") for m in mods)
        and any(w in name for w in ("PASSWORD", "SECRET", "KEY", "TOKEN"))
    )
    assert not weak, f"credentials must not have shipped defaults: {weak}"


def test_the_example_ships_no_real_values() -> None:
    """Every credential line is blank — a filled-in example gets copied and kept."""
    filled = []
    for line in EXAMPLE.read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if value.strip():
            filled.append(name)
    assert not filled, f"deploy/.env.example must ship empty values, got: {filled}"


def test_no_service_publishes_a_port_on_all_interfaces() -> None:
    """These services hold transcripts, private notes, revealed submissions and the provider key.
    A bare "5432:5432" exposes them to everyone on the operator's network."""
    # Every quoted token in the file, so both `ports: ["5432:5432"]` and the multi-line
    # `- "5432:5432"` form are covered. A published port is host:container or just container.
    exposed = [
        token for token in re.findall(r'"([^"]+)"', COMPOSE.read_text())
        if re.fullmatch(r"\d+:\d+", token)
    ]
    assert not exposed, f"bind these to 127.0.0.1: {exposed}"
