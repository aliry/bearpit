"""Container ownership — which agent containers a sweep may destroy.

`arealm reap` and `Forge.reap_orphans` had drifted: the CLI destroyed every container matching
`realm-` while its own docstring promised it only ever destroyed containers belonging to no running
realm. Running it during a live realm killed the run mid-flight, and because the flight log only
writes on CONCLUDE, the chronicle never got its ending. Both now share one rule.
"""

from __future__ import annotations

from agentrealm.forge import orphan_containers

FOUND = {
    "realm-duel-1-vela": "c1",
    "realm-duel-1-orin": "c2",
    "realm-jury-1-juror-a": "c3",
    "realm-old-run-ghost": "c4",
}


def test_nothing_active_means_everything_is_an_orphan() -> None:
    """The platform is the only thing that can run a realm, so anything alive while it is down is
    an orphan by definition."""
    assert orphan_containers(FOUND, []) == FOUND


def test_a_live_realms_containers_are_spared() -> None:
    orphans = orphan_containers(FOUND, ["duel-1"])
    assert set(orphans) == {"realm-jury-1-juror-a", "realm-old-run-ghost"}


def test_hyphenated_realm_and_agent_ids_resolve_correctly() -> None:
    """The reason ownership is a prefix match and never a name parse: 'jury-1' + 'juror-a' means
    `'realm-jury-1-juror-a'.rpartition('-')` yields the realm 'jury-1-juror'. Parsing would spare an
    orphan or, far worse, destroy a live agent."""
    assert "realm-jury-1-juror-a" not in orphan_containers(FOUND, ["jury-1"])
    # and a realm whose id is a PREFIX of another's must not sweep the other's containers
    assert orphan_containers({"realm-duel-10-vela": "x"}, ["duel-1"]) == {"realm-duel-10-vela": "x"}


def test_every_active_realm_is_spared_at_once() -> None:
    assert orphan_containers(FOUND, ["duel-1", "jury-1"]) == {"realm-old-run-ghost": "c4"}


def test_an_unknown_active_realm_spares_nothing_extra() -> None:
    assert orphan_containers(FOUND, ["never-ran"]) == FOUND
