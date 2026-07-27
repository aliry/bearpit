"""SQLAlchemy models for the Chronicle (architecture §14).

Two append-only tables carry the realm's history: `messages` (the bus firehose) and
`events` (everything else — lifecycle, spend, score, file, violation, verdict, system).
Together they *are* the replay. `JSON` maps to Postgres `jsonb` in prod and to JSON on
SQLite in tests, so the same models run in both.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, BigInteger, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Event(Base):
    """One chronicled event. Append-only: rows are never updated or deleted."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    realm_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)  # lifecycle|spend|score|file|…
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)  # event time, ms epoch


class Message(Base):
    """One chronicled message (the bus firehose). Append-only."""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    realm_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(128), index=True)
    sender: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text)
    attachments: Mapped[list[str]] = mapped_column(JSON, default=list)
    ts_ms: Mapped[int] = mapped_column(BigInteger, index=True)
