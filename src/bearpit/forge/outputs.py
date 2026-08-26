"""Capturing a realm's declared output files before the shared volume is destroyed (ADR-005).

The shared folder is the deliverable for several scenarios — `beacon-brief` says a brief pasted
into chat "counts for NOTHING" — and `teardown_realm` used to remove the volume without reading it.

This is the flight recorder's shape, applied to files instead of logs: read once, write beside the
logs, and never let a failure here stop teardown from releasing containers, networks and keys.
"""

from __future__ import annotations

import fnmatch
import hashlib
import logging
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

log = logging.getLogger(__name__)


class _VolumeReader(Protocol):
    def read_volume(self, name: str) -> dict[str, str]: ...


def _safe(rel: str) -> str | None:
    """A path from the volume, made safe to join onto the outputs directory, or None.

    The volume's contents are written by agents, so a filename is untrusted input: `..` segments,
    absolute paths and drive letters are all refused rather than normalised.
    """
    p = PurePosixPath(rel)
    if p.is_absolute() or any(part == ".." for part in p.parts) or ":" in rel:
        log.warning("refusing an unsafe output path from the shared volume: %r", rel)
        return None
    return str(p)


def capture_outputs(
    runtime: _VolumeReader,
    realm_id: str,
    volume: str | None,
    patterns: list[str] | tuple[str, ...],
    base_dir: Path,
) -> list[dict[str, Any]]:
    """Write the files matching `patterns` to `<base_dir>/<realm>/outputs/`, and describe them.

    Returns one record per declared pattern-match: `{path, bytes, sha256}`, or
    `{path, missing: True}` for a pattern that matched nothing. The caller writes those to the
    chronicle — Forge has no chronicle, and event-writing belongs where one already is.

    Never raises. A realm that cannot save its output must still be torn down.
    """
    patterns = [p for p in (patterns or []) if p]
    if not patterns or not volume:
        return []
    try:
        files = runtime.read_volume(volume)
    except Exception as exc:  # noqa: BLE001 - best-effort, exactly like the flight recorder
        log.warning("realm %s: could not read the shared volume to capture outputs: %s",
                    realm_id, exc)
        return []

    out_dir = base_dir / realm_id / "outputs"
    records: list[dict[str, Any]] = []
    for pattern in patterns:
        matched = sorted(p for p in files if fnmatch.fnmatch(p, pattern))
        if not matched:
            # A declared deliverable that was never written is a RESULT, not a silence.
            records.append({"path": pattern, "missing": True})
            continue
        for rel in matched:
            safe = _safe(rel)
            if safe is None:
                continue
            body = files[rel]
            try:
                dest = out_dir / safe
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(body, encoding="utf-8")
            except Exception as exc:  # noqa: BLE001
                log.warning("realm %s: could not write output %r: %s", realm_id, safe, exc)
                continue
            raw = body.encode("utf-8")
            records.append({"path": safe, "bytes": len(raw),
                            "sha256": hashlib.sha256(raw).hexdigest()})
    return records


__all__ = ["capture_outputs"]
