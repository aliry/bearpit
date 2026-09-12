"""Resolve a project's machine declaration into the MACHINE chronicle record: the declaration
plus who is in each role and the roster order the pointer rotates over. Host-resolved once at
launch and persisted, so a replay sees exactly what the run saw."""
from __future__ import annotations

from typing import Any

from bearpit.core.schema import Project
from bearpit.realmtools.machine_service import MACHINE_VERSION


def machine_record(project: Project) -> dict[str, Any] | None:
    m = project.spec.machine
    if m is None:
        return None
    referee = project.referee.id if project.referee else None
    roster = [a.id for a in project.agents if referee is None or a.id != referee]
    members: dict[str, list[str]] = {}
    for name, role in m.roles.items():
        if role.members == "referee":
            members[name] = [referee] if referee else []
        elif role.members == "participants":
            members[name] = list(roster)
        else:
            members[name] = list(role.members)
    return {"version": MACHINE_VERSION, "declaration": m.model_dump(by_alias=True, mode="json"),
            "members": members, "roster": roster, "referee": referee}
