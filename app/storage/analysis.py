"""Claim and persist classifier work without holding a transaction during API calls."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from secrets import token_hex

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.intelligence.contracts import ArticleInput, ClassificationResult, ClassifierIdentity
from app.publication_window import PublicationWindow
from app.storage.models import (
    AnalysisJob,
    AnalysisJobStatus,
    AnalysisStatus,
    Article,
    ArticleRevision,
    Classification,
    ExtractionStatus,
    NewsSource,
)


@dataclass(frozen=True)
class Claim:
    job_id: int
    revision_id: int
    token: str
    article: ArticleInput


class AnalysisStore:
    def __init__(self, sessions: sessionmaker[Session], *, lease_seconds: int = 600) -> None:
        self.sessions = sessions
        self.lease_seconds = lease_seconds

    def candidate_ids(
        self,
        profile_key: str,
        source_slugs: list[str],
        *,
        retry_failed: bool,
        limit: int | None,
        window: PublicationWindow | None = None,
    ) -> list[int]:
        statement = (
            select(ArticleRevision.id)
            .join(Article, Article.id == ArticleRevision.article_id)
            .join(NewsSource, NewsSource.id == Article.source_id)
            .outerjoin(
                AnalysisJob,
                and_(
                    AnalysisJob.article_revision_id == ArticleRevision.id,
                    AnalysisJob.profile_key == profile_key,
                ),
            )
            .where(
                NewsSource.slug.in_(source_slugs),
                ArticleRevision.extraction_status != ExtractionStatus.PENDING,
                or_(AnalysisJob.id.is_(None), _claimable(datetime.now(UTC), retry_failed)),
            )
            .order_by(ArticleRevision.id)
            .limit(limit)
        )
        if window is not None:
            statement = statement.where(
                ArticleRevision.published_at > window.start,
                ArticleRevision.published_at <= window.end,
            )
        with self.sessions() as session:
            return list(session.scalars(statement))

    def claim(self, revision_id: int, profile_key: str, *, retry_failed: bool) -> Claim | None:
        now = datetime.now(UTC)
        token = token_hex(16)
        fields = {
            "status": AnalysisJobStatus.RUNNING,
            "claim_token": token,
            "lease_until": now + timedelta(seconds=self.lease_seconds),
        }
        with self.sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            insert = sqlite_insert if dialect == "sqlite" else postgres_insert
            job_id = session.scalar(
                insert(AnalysisJob)
                .values(
                    article_revision_id=revision_id,
                    profile_key=profile_key,
                    attempt_count=1,
                    **fields,
                )
                .on_conflict_do_update(
                    index_elements=[AnalysisJob.article_revision_id, AnalysisJob.profile_key],
                    set_={
                        **fields,
                        "attempt_count": AnalysisJob.attempt_count + 1,
                        "error": None,
                        "finished_at": None,
                    },
                    where=_claimable(now, retry_failed),
                )
                .returning(AnalysisJob.id)
            )
            if job_id is None:
                return None
            revision = session.get_one(ArticleRevision, revision_id)
            article = ArticleInput(
                revision.title, revision.rss_summary, revision.body_text, revision.language
            )
            return Claim(job_id, revision_id, token, article)

    def complete(
        self, claim: Claim, identity: ClassifierIdentity, result: ClassificationResult
    ) -> bool:
        with self.sessions.begin() as session:
            if not _finish_claim(session, claim, AnalysisJobStatus.COMPLETE):
                return False
            classification = Classification(
                article_revision_id=claim.revision_id,
                provider=identity.provider,
                model=identity.model,
                model_version=result.model_version,
                prompt_version=identity.prompt_version,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                **result.assessment.model_dump(),
            )
            session.add(classification)
            session.flush()
            session.execute(
                update(AnalysisJob)
                .where(AnalysisJob.id == claim.job_id)
                .values(classification_id=classification.id)
            )
            session.execute(
                update(ArticleRevision)
                .where(ArticleRevision.id == claim.revision_id)
                .values(analysis_status=AnalysisStatus.COMPLETE)
            )
        return True

    def fail(self, claim: Claim, error: str) -> bool:
        with self.sessions.begin() as session:
            if not _finish_claim(session, claim, AnalysisJobStatus.FAILED, error):
                return False
            session.execute(
                update(ArticleRevision)
                .where(
                    ArticleRevision.id == claim.revision_id,
                    ArticleRevision.analysis_status != AnalysisStatus.COMPLETE,
                )
                .values(analysis_status=AnalysisStatus.FAILED)
            )
        return True


def _claimable(now: datetime, retry_failed: bool) -> ColumnElement[bool]:
    expired = and_(
        AnalysisJob.status == AnalysisJobStatus.RUNNING,
        AnalysisJob.lease_until <= now,
    )
    return or_(expired, AnalysisJob.status == AnalysisJobStatus.FAILED) if retry_failed else expired


def _finish_claim(
    session: Session, claim: Claim, status: AnalysisJobStatus, error: str | None = None
) -> bool:
    finished = session.scalar(
        update(AnalysisJob)
        .where(
            AnalysisJob.id == claim.job_id,
            AnalysisJob.status == AnalysisJobStatus.RUNNING,
            AnalysisJob.claim_token == claim.token,
        )
        .values(
            status=status,
            error=error,
            finished_at=datetime.now(UTC),
            claim_token=None,
            lease_until=None,
        )
        .returning(AnalysisJob.id)
    )
    return finished is not None
