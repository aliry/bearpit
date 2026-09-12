"""Is the realmtools container running the code in this tree?

The container is a BUILT image with no source mount, so `up -d` alone does not redeploy it. When
it drifts, nothing announces it: the realm runs, concludes, and writes a well-formed verdict. It
has happened twice — once an among-us realm reported a "crew win" that no agent had played for.

This asks the container to fingerprint its own copy with the same function the host uses, and
compares. It FAILS CLOSED on every uncertainty: a container too old to answer, unparseable output,
or a Docker error all read as stale, because the one outcome worth preventing is a launch that
proceeds believing something it never checked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from bearpit.core.buildid import FINGERPRINT_SNIPPET, source_fingerprint

_FINGERPRINT = re.compile(r"^[0-9a-f]{12}$")

_HOW = ("rebuild it with `docker compose -f deploy/docker-compose.yaml build realmtools` "
        "and `… up -d realmtools`")


@dataclass(frozen=True)
class ImageCheck:
    matches: bool
    host: str
    container: str | None  # None when the container could not be asked or did not answer usefully
    detail: str


def check_realmtools_image(
    runtime: Any, container: str, *, host_fingerprint: str | None = None
) -> ImageCheck:
    """Compare the container's own fingerprint with this tree's."""
    host = host_fingerprint if host_fingerprint is not None else source_fingerprint()
    try:
        code, out = runtime.exec_python(container, FINGERPRINT_SNIPPET)
    except Exception as exc:  # a Docker error is not evidence that the image is current
        return ImageCheck(False, host, None,
                          f"could not ask container {container!r} what code it is running: {exc}")
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    answer = lines[-1] if lines else ""
    if code != 0 or not _FINGERPRINT.match(answer):
        # An image predating this check cannot import the module that performs it, which is itself
        # conclusive — it is certainly not this tree.
        return ImageCheck(
            False, host, None,
            f"container {container!r} could not report which code it is running "
            f"(exit {code}); it predates this check or is not this build — {_HOW}",
        )
    if answer != host:
        return ImageCheck(
            False, host, answer,
            f"container {container!r} is running {answer}, this tree is {host} — {_HOW}",
        )
    return ImageCheck(True, host, answer, f"container {container!r} matches this tree ({host})")
