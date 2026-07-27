"""Scribe's curatable, host-side markdown memory (§10).

Not a database — a directory of human-readable `.md` files (one note per file with light
frontmatter) plus a regenerated `INDEX.md`. It holds design best-practices, user preferences, and
per-scenario notes, and persists across sessions. Because it is plain files, the user (or a future
Scribe self-review) can read, correct, or delete a note by hand — and that curation is honored:
recall/search read the files themselves, so a deleted note simply disappears.
"""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"\s*---\n(?P<fm>.*?)\n---\s*\n?(?P<body>.*)", re.S)


def _render(mem_id: str, kind: str, tags: list[str], text: str) -> str:
    return f"---\nid: {mem_id}\nkind: {kind}\ntags: {', '.join(tags)}\n---\n\n{text.strip()}\n"


def _split(content: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter dict, body) for a memory file (tolerant of a hand-edited file)."""
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return {}, content.strip()
    fm: dict[str, str] = {}
    for line in m.group("fm").splitlines():
        key, sep, value = line.partition(":")
        if sep:
            fm[key.strip()] = value.strip()
    return fm, m.group("body").strip()


class Memory:
    """A directory of markdown notes with `remember` / `recall` / `search`."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    async def remember(self, text: str, kind: str, tags: list[str] | None = None) -> str:
        """Write one note and return its id. `kind` groups notes (best-practice / preference /
        scenario-note); `tags` are free-form keywords (also matched by `search`)."""
        tags = tags or []
        # A fixed-width, time-ordered id so filenames sort chronologically; a random suffix avoids
        # a collision if two notes land in the same nanosecond.
        mem_id = f"{time.time_ns():020d}-{secrets.token_hex(3)}"
        (self._root / f"{mem_id}.md").write_text(_render(mem_id, kind, tags, text))
        self._rebuild_index()
        return mem_id

    async def recall(self, limit: int = 20) -> list[str]:
        """The bodies of the most recent notes, newest first."""
        return [_split(f.read_text())[1] for f in self._files()[:limit]]

    async def search(self, query: str) -> list[str]:
        """The bodies of notes whose file text (body + kind + tags) contains `query`."""
        q = query.lower()
        hits: list[str] = []
        for f in self._files():
            content = f.read_text()
            if q in content.lower():
                hits.append(_split(content)[1])
        return hits

    def _files(self) -> list[Path]:
        files = [p for p in self._root.glob("*.md") if p.name != "INDEX.md"]
        return sorted(files, key=lambda p: p.name, reverse=True)  # newest first

    def _rebuild_index(self) -> None:
        """Regenerate INDEX.md from the current files, so it reflects hand-deletions too."""
        lines = ["# Scribe memory index", ""]
        for f in self._files():
            fm, body = _split(f.read_text())
            first = body.splitlines()[0] if body else ""
            lines.append(f"- `{fm.get('id', f.stem)}` [{fm.get('kind', '')}] {first[:80]}")
        (self._root / "INDEX.md").write_text("\n".join(lines) + "\n")
