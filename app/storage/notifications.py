"""Persist notification claims, confirmations, and retry times."""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, aliased, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.intelligence.contracts import Relevance
from app.notifications.message import Alert
from app.publication_window import PublicationWindow
from app.storage.evaluation import latest_classification_id, latest_feedback_id
from app.storage.models import (
    Article,
    ArticleRevision,
    Classification,
    HumanFeedback,
    NewsSource,
    Notification,
)
from app.storage.models import NotificationStatus as Status

CHANNEL = "telegram"


@dataclass(frozen=True)
class DeliveryClaim:
    notification_id: int
    token: str


class NotificationStore:
    def __init__(self, sessions: sessionmaker[Session], *, lease_seconds: float = 360) -> None:
        self.sessions = sessions
        self.lease_seconds = lease_seconds

    def expire_claims(self) -> None:
        now = datetime.now(UTC)
        with self.sessions.begin() as session:
            session.execute(
                update(Notification)
                .where(
                    Notification.channel == CHANNEL,
                    Notification.status == Status.SENDING,
                    Notification.lease_until <= now,
                )
                .values(status=Status.UNKNOWN, lease_until=None, last_error="delivery_interrupted")
            )

    def cooldown_until(self) -> datetime | None:
        with self.sessions() as session:
            return session.scalar(
                select(func.max(Notification.next_attempt_at)).where(
                    Notification.channel == CHANNEL,
                    Notification.status == Status.FAILED,
                    Notification.last_error == "telegram_rate_limited",
                    Notification.next_attempt_at > datetime.now(UTC),
                )
            )

    def unknown_articles(self) -> list[int]:
        with self.sessions() as session:
            return list(
                session.scalars(
                    select(Notification.article_id)
                    .where(Notification.channel == CHANNEL, Notification.status == Status.UNKNOWN)
                    .order_by(Notification.article_id)
                )
            )

    def candidates(
        self,
        source_slugs: list[str],
        *,
        send_uncertain: bool,
        retry_failed: bool,
        retry_unknown: int | None,
        limit: int | None,
        revision_id: int | None = None,
        exclude_revisions: Collection[int] = (),
        window: PublicationWindow | None = None,
    ) -> list[Alert]:
        revision = aliased(ArticleRevision)
        latest_revision = (
            select(revision.id)
            .where(revision.article_id == Article.id)
            .order_by(revision.last_seen_at.desc(), revision.id.desc())
            .limit(1)
            .correlate(Article)
            .scalar_subquery()
        )
        relevance = func.coalesce(HumanFeedback.relevance, Classification.relevance)
        reason = case(
            (
                HumanFeedback.relevance != Classification.relevance,
                func.coalesce(HumanFeedback.note, "نوع ارتباط در بازبینی انسانی تعیین شد."),
            ),
            else_=Classification.reason,
        )
        labels = [Relevance.DIRECT, Relevance.INDIRECT]
        if send_uncertain:
            labels.append(Relevance.UNCERTAIN)
        statement = (
            select(
                Article.id,
                Classification.id,
                NewsSource.name,
                ArticleRevision.title,
                Article.original_url,
                Article.canonical_url,
                ArticleRevision.published_at,
                relevance,
                Classification.topic,
                Classification.summary_fa,
                reason,
            )
            .select_from(Article)
            .join(NewsSource, NewsSource.id == Article.source_id)
            .join(ArticleRevision, ArticleRevision.id == latest_revision)
            .join(Classification, Classification.id == latest_classification_id())
            .outerjoin(HumanFeedback, HumanFeedback.id == latest_feedback_id())
            .outerjoin(
                Notification,
                and_(Notification.article_id == Article.id, Notification.channel == CHANNEL),
            )
            .where(NewsSource.slug.in_(source_slugs), relevance.in_(labels))
            .order_by(Classification.analyzed_at, Classification.id)
            .limit(limit)
        )
        if revision_id is not None:
            statement = statement.where(ArticleRevision.id == revision_id)
        if window is not None:
            statement = statement.where(
                ArticleRevision.published_at > window.start,
                ArticleRevision.published_at <= window.end,
            )
        if exclude_revisions:
            statement = statement.where(ArticleRevision.id.not_in(exclude_revisions))
        if retry_unknown is not None:
            statement = statement.where(
                Article.id == retry_unknown, Notification.status == Status.UNKNOWN
            )
        else:
            statement = statement.where(
                or_(
                    Notification.id.is_(None),
                    _ready_status(datetime.now(UTC), retry_failed=retry_failed),
                )
            )
        with self.sessions() as session:
            return [Alert(*row) for row in session.execute(statement)]

    def claim(
        self, alert: Alert, *, retry_failed: bool, retry_unknown: bool
    ) -> DeliveryClaim | None:
        now = datetime.now(UTC)
        token = token_hex(16)
        with self.sessions.begin() as session:
            if not retry_unknown:
                insert = (
                    sqlite_insert
                    if session.get_bind().dialect.name == "sqlite"
                    else postgres_insert
                )
                session.execute(
                    insert(Notification)
                    .values(article_id=alert.article_id, channel=CHANNEL)
                    .on_conflict_do_nothing(
                        index_elements=[Notification.article_id, Notification.channel]
                    )
                )
            eligible = (
                Notification.status == Status.UNKNOWN
                if retry_unknown
                else _ready_status(now, retry_failed=retry_failed)
            )
            notification_id = session.scalar(
                update(Notification)
                .where(
                    Notification.article_id == alert.article_id,
                    Notification.channel == CHANNEL,
                    eligible,
                )
                .values(
                    classification_id=alert.classification_id,
                    status=Status.SENDING,
                    claim_token=token,
                    lease_until=now + timedelta(seconds=self.lease_seconds),
                    attempt_count=Notification.attempt_count + 1,
                    next_attempt_at=None,
                    last_error=None,
                )
                .returning(Notification.id)
            )
        return DeliveryClaim(notification_id, token) if notification_id is not None else None

    def sent(self, claim: DeliveryClaim, message_id: str) -> bool:
        return self._finish(
            claim, Status.SENT, external_message_id=message_id, sent_at=datetime.now(UTC)
        )

    def failed(
        self, claim: DeliveryClaim, code: str, *, retry_after: int | None, delivery_unknown: bool
    ) -> bool:
        next_attempt = (
            datetime.now(UTC) + timedelta(seconds=retry_after) if retry_after is not None else None
        )
        return self._finish(
            claim,
            Status.UNKNOWN if delivery_unknown else Status.FAILED,
            last_error=code,
            next_attempt_at=next_attempt,
        )

    def _finish(self, claim: DeliveryClaim, status: Status, **fields: object) -> bool:
        with self.sessions.begin() as session:
            return (
                session.scalar(
                    update(Notification)
                    .where(
                        Notification.id == claim.notification_id,
                        Notification.claim_token == claim.token,
                        Notification.status.in_([Status.SENDING, Status.UNKNOWN]),
                    )
                    .values(status=status, claim_token=None, lease_until=None, **fields)
                    .returning(Notification.id)
                )
                is not None
            )


def _ready_status(now: datetime, *, retry_failed: bool) -> ColumnElement[bool]:
    due = Notification.next_attempt_at <= now
    if retry_failed:
        due = or_(due, Notification.next_attempt_at.is_(None))
    return or_(
        Notification.status == Status.PENDING, and_(Notification.status == Status.FAILED, due)
    )
