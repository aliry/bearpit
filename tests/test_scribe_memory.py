"""Curatable markdown memory (Task 4).

The store is deliberately not a database: one human-readable .md file per note, plus a regenerated
INDEX.md. Curation is honored — a note deleted by hand disappears from recall/search.
"""

from __future__ import annotations

from pathlib import Path

from agentrealm.scribe.memory import Memory


async def test_remember_writes_a_file_and_an_index_line(tmp_path: Path) -> None:
    mem = Memory(tmp_path)
    mem_id = await mem.remember("Referees should use model_category=large.", kind="best-practice")
    files = [p for p in tmp_path.glob("*.md") if p.name != "INDEX.md"]
    assert len(files) == 1
    assert mem_id in files[0].read_text()
    index = (tmp_path / "INDEX.md").read_text()
    assert mem_id in index
    assert "best-practice" in index


async def test_recall_returns_most_recent_first(tmp_path: Path) -> None:
    mem = Memory(tmp_path)
    await mem.remember("first note", kind="note")
    await mem.remember("second note", kind="note", tags=["turns"])
    recalled = await mem.recall()
    assert recalled[0] == "second note"
    assert "first note" in recalled


async def test_search_matches_on_keyword_and_tags(tmp_path: Path) -> None:
    mem = Memory(tmp_path)
    await mem.remember("Keep referee rubrics short.", kind="best-practice", tags=["referee"])
    await mem.remember("Bids are sealed, never posted.", kind="best-practice", tags=["auction"])
    assert await mem.search("sealed") == ["Bids are sealed, never posted."]
    assert await mem.search("referee") == ["Keep referee rubrics short."]  # matches a tag
    assert await mem.search("nothing-here") == []


async def test_hand_deleting_a_file_removes_it_from_recall(tmp_path: Path) -> None:
    mem = Memory(tmp_path)
    await mem.remember("keep me", kind="note")
    await mem.remember("delete me", kind="note")
    to_delete = next(
        p for p in tmp_path.glob("*.md") if p.name != "INDEX.md" and "delete me" in p.read_text()
    )
    to_delete.unlink()
    recalled = await mem.recall()
    assert recalled == ["keep me"]
