"""Export JSON Schema for the project package (§13.5, #38).

`project.schema.json` validates a project.json (or a flat manifest); `agent.schema.json`
validates one agents/<id>/agent.json. Editors and CI can point at these for autocomplete +
validation. The field descriptions come straight from the Pydantic models (schema.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bearpit.core.schema import AgentSpec, Project


def project_schema() -> dict[str, Any]:
    return Project.model_json_schema(by_alias=True)


def agent_schema() -> dict[str, Any]:
    return AgentSpec.model_json_schema()


def write_schemas(out_dir: str | Path) -> list[Path]:
    """Write project + agent JSON Schemas into `out_dir`; return the written paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, schema in (
        ("project.schema.json", project_schema()),
        ("agent.schema.json", agent_schema()),
    ):
        path = out / name
        path.write_text(json.dumps(schema, indent=2) + "\n")
        written.append(path)
    return written
