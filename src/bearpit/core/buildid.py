"""A fingerprint of the code a realm actually runs, so two copies can be compared.

The realmtools container is a BUILT image with no source mount, so `docker compose up -d` does not
redeploy it — it has to be rebuilt. Forgetting that is the one failure in this system that
announces itself with nothing: the realm runs, concludes, and writes a verdict that looks exactly
like a good one. It has happened twice, once turning an among-us run into a spurious "crew win"
because a stale component blinded the agents into missing their turns.

Comparing build time to commit time is the obvious check and it lies in both directions — an image
built minutes BEFORE its commit still contains the change, because you build to test and then
commit. So compare the content.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

# The packages the realmtools container actually executes. `gatekeeper`, `cli` and `scribe` are
# host-only: the control plane imports them and the image never runs them, so a change there is not
# a stale image and must not refuse a launch. A check that fires on work it does not cover is a
# check that gets switched off. Derived from realmtools' own imports — keep it in step with them.
RUNTIME_PACKAGES = ("chronicle", "core", "forge", "realmtools")

_FINGERPRINT_CHARS = 12


def source_fingerprint(root: Path | str | None = None) -> str:
    """A short, stable digest of every `.py` file under `RUNTIME_PACKAGES` in `root`.

    `root` is the `bearpit` package directory; the installed package's own location by default, so
    the same call inside the container fingerprints the image's copy. Path-sorted and content-
    hashed, so it is identical for identical trees and differs for any added, removed or edited
    file — including one that exists on only one side, which is what a rebuilt-but-not-copied
    module looks like.

    `__pycache__` is skipped: it appears the moment anything imports the tree and differs between a
    host that has run its tests and a freshly built image, so counting it would mark every launch
    stale. Non-Python files are skipped for the same reason — the image carries no README.
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    if not base.is_dir():
        raise FileNotFoundError(f"no source tree at {base}")
    h = hashlib.sha256()
    seen = 0
    for pkg in RUNTIME_PACKAGES:
        pkg_dir = base / pkg
        if not pkg_dir.is_dir():
            continue
        for f in sorted(pkg_dir.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            # the path matters as much as the bytes: a file moved between modules is a change
            h.update(str(f.relative_to(base)).encode())
            h.update(b"\0")
            h.update(f.read_bytes())
            seen += 1
    if seen == 0:
        raise FileNotFoundError(f"no runtime source under {base} — wrong path, not a match")
    return h.hexdigest()[:_FINGERPRINT_CHARS]


# What to run inside a container to get its own answer. One line, no arguments, no imports beyond
# the package itself, so it works in any image that has bearpit installed.
FINGERPRINT_SNIPPET = (
    "from bearpit.core.buildid import source_fingerprint; print(source_fingerprint())"
)
