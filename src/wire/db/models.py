"""SQLAlchemy 2.0 models matching the schema in SPEC.MD §6.

All timestamp columns store naive UTC. Display in Europe/Stockholm happens at
the boundary (Telegram messages, digest); never in the DB.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    """Naive UTC. SQLite stores DateTime without tz info; we treat all
    datetimes in the DB as UTC by convention."""
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    github_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    actor: Mapped[str | None] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    triage_score: Mapped[float | None] = mapped_column(Float)
    triage_reason: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"))

    session: Mapped[Session | None] = relationship(back_populates="events")

    __table_args__ = (
        Index("idx_events_repo_time", "repo", "occurred_at"),
        Index("idx_events_session", "session_id"),
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    repo: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_reason: Mapped[str | None] = mapped_column(String)  # idle | max_hours | immediate
    drafted_at: Mapped[datetime | None] = mapped_column(DateTime)
    skip_reason: Mapped[str | None] = mapped_column(Text)

    events: Mapped[list[Event]] = relationship(
        back_populates="session", order_by="Event.occurred_at"
    )
    drafts: Mapped[list[Draft]] = relationship(back_populates="session")


class Draft(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    # status: pending | approved | rejected | edited | expired

    session: Mapped[Session | None] = relationship(back_populates="drafts")
    decisions: Mapped[list[Decision]] = relationship(back_populates="draft")
    post: Mapped[Post | None] = relationship(back_populates="draft", uselist=False)

    __table_args__ = (Index("idx_drafts_status", "status"),)


class Decision(Base):
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String, nullable=False)  # approved | rejected | edited
    reject_reason: Mapped[str | None] = mapped_column(String)
    edited_text: Mapped[str | None] = mapped_column(Text)
    edit_diff: Mapped[str | None] = mapped_column(Text)  # JSON-encoded diff
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    draft: Mapped[Draft] = relationship(back_populates="decisions")


class Post(Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id"))
    twitter_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    posted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    draft: Mapped[Draft | None] = relationship(back_populates="post")
    metrics: Mapped[list[Metric]] = relationship(
        back_populates="post", order_by="Metric.fetched_at"
    )


class Metric(Base):
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    impressions: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    retweets: Mapped[int | None] = mapped_column(Integer)
    replies: Mapped[int | None] = mapped_column(Integer)
    bookmarks: Mapped[int | None] = mapped_column(Integer)

    post: Mapped[Post] = relationship(back_populates="metrics")

    __table_args__ = (Index("idx_metrics_post", "post_id"),)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)  # ollama | claude
    model: Mapped[str | None] = mapped_column(String)
    fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    called_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)

    __table_args__ = (Index("idx_llm_calls_month", "called_at"),)


class VoiceProfile(Base):
    __tablename__ = "voice_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    profile_text: Mapped[str] = mapped_column(Text, nullable=False)


class BudgetOverride(Base):
    """`/extend` records — bumps the monthly cap by `amount_usd` for the
    calendar month indicated by `effective_month` (YYYY-MM in UTC)."""

    __tablename__ = "budget_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    effective_month: Mapped[str] = mapped_column(String, nullable=False)  # "2026-04"
    amount_usd: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)


class BotState(Base):
    """Single-row k/v table for runtime flags (paused_until, last_alert_pct, ...)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)


# Sentinel to verify exports
__all__ = [
    "Base",
    "BotState",
    "BudgetOverride",
    "Decision",
    "Draft",
    "Event",
    "LLMCall",
    "Metric",
    "Post",
    "Session",
    "VoiceProfile",
    "utc_now",
]
