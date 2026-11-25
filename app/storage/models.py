"""Relational schema for the news and its processing history."""

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from app.intelligence.contracts import Relevance as Relevance


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    PARTIAL = "partial"
    FAILED = "failed"


class AnalysisStatus(StrEnum):
    PENDING = "pending"
    COMPLETE = "complete"
    FAILED = "failed"


class AnalysisJobStatus(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"
    UNKNOWN = "unknown"


class FetchStatus(StrEnum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


def _enum_type(values: type[StrEnum], name: str) -> Enum:
    return Enum(
        values,
        values_callable=lambda members: [member.value for member in members],
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        name=name,
    )


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC consistently and restore an aware datetime when reading it."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("datetime must include a timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_N_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class NewsSource(Base):
    __tablename__ = "news_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    rss_url: Mapped[str | None] = mapped_column(Text)


class Article(Base):
    __tablename__ = "articles"
    __table_args__ = (UniqueConstraint("source_id", "canonical_url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("news_sources.id"))
    original_url: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)


class ArticleAlias(Base):
    __tablename__ = "article_aliases"
    __table_args__ = (UniqueConstraint("source_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("news_sources.id"))
    article_id: Mapped[int] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(Text)


class ArticleRevision(Base):
    __tablename__ = "article_revisions"
    __table_args__ = (UniqueConstraint("article_id", "content_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)
    rss_summary: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(35))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    extraction_status: Mapped[ExtractionStatus] = mapped_column(
        _enum_type(ExtractionStatus, "extraction_status"), default=ExtractionStatus.PENDING
    )
    analysis_status: Mapped[AnalysisStatus] = mapped_column(
        _enum_type(AnalysisStatus, "analysis_status"), default=AnalysisStatus.PENDING
    )


class Classification(Base):
    __tablename__ = "classifications"
    __table_args__ = (CheckConstraint("confidence BETWEEN 0 AND 1", name="confidence_range"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_revision_id: Mapped[int] = mapped_column(
        ForeignKey("article_revisions.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(160))
    model_version: Mapped[str | None] = mapped_column(String(160))
    prompt_version: Mapped[str] = mapped_column(String(80))
    relevance: Mapped[Relevance] = mapped_column(_enum_type(Relevance, "relevance"))
    confidence: Mapped[float]
    topic: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    related_entities: Mapped[list[str]] = mapped_column(JSON, default=list)
    summary_fa: Mapped[str] = mapped_column(Text)
    needs_review: Mapped[bool]
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    analyzed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    __table_args__ = (UniqueConstraint("article_revision_id", "profile_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_revision_id: Mapped[int] = mapped_column(
        ForeignKey("article_revisions.id", ondelete="CASCADE")
    )
    profile_key: Mapped[str] = mapped_column(String(64))
    status: Mapped[AnalysisJobStatus] = mapped_column(_enum_type(AnalysisJobStatus, "status"))
    claim_token: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempt_count: Mapped[int]
    error: Mapped[str | None] = mapped_column(String(100))
    classification_id: Mapped[int | None] = mapped_column(
        ForeignKey("classifications.id", ondelete="SET NULL")
    )
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("article_id", "channel"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"))
    classification_id: Mapped[int | None] = mapped_column(
        ForeignKey("classifications.id", ondelete="SET NULL")
    )
    channel: Mapped[str] = mapped_column(String(40))
    status: Mapped[NotificationStatus] = mapped_column(
        _enum_type(NotificationStatus, "status"), default=NotificationStatus.PENDING
    )
    attempt_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    claim_token: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    external_message_id: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now, onupdate=_utc_now)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class FetchLog(Base):
    __tablename__ = "fetch_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("news_sources.id"), index=True)
    status: Mapped[FetchStatus] = mapped_column(
        _enum_type(FetchStatus, "status"), default=FetchStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    discovered_count: Mapped[int] = mapped_column(default=0)
    saved_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    trigger: Mapped[str] = mapped_column(String(15))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    exit_code: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(String(100))
    report: Mapped[dict | None] = mapped_column(JSON)


class HumanFeedback(Base):
    __tablename__ = "human_feedback"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_revision_id: Mapped[int] = mapped_column(
        ForeignKey("article_revisions.id", ondelete="CASCADE"), index=True
    )
    classification_id: Mapped[int | None] = mapped_column(
        ForeignKey("classifications.id", ondelete="SET NULL")
    )
    relevance: Mapped[Relevance] = mapped_column(_enum_type(Relevance, "relevance"))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=_utc_now)
