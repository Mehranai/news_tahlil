"""Persist collection runs, article identities, and content revisions."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import SourceSettings
from app.extractors.article import Material
from app.preprocessing import clean_text
from app.sources.base import Candidate
from app.storage.models import (
    Article,
    ArticleAlias,
    ArticleRevision,
    ExtractionStatus,
    FetchLog,
    FetchStatus,
    NewsSource,
)

MIN_REPOST_SUMMARY_LENGTH = 120


@dataclass(frozen=True)
class Run:
    id: int
    source_id: int


@dataclass
class SaveCounts:
    new_articles: int = 0
    new_revisions: int = 0
    unchanged: int = 0


class CollectionStore:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def incomplete(self, source_slugs: list[str], limit: int | None) -> list[tuple[str, Candidate]]:
        latest = (
            select(ArticleRevision.id)
            .where(ArticleRevision.article_id == Article.id)
            .order_by(ArticleRevision.last_seen_at.desc(), ArticleRevision.id.desc())
            .limit(1)
            .correlate(Article)
            .scalar_subquery()
        )
        query = (
            select(NewsSource.slug, Article.canonical_url, ArticleRevision)
            .join(Article, Article.source_id == NewsSource.id)
            .join(ArticleRevision, ArticleRevision.id == latest)
            .where(
                NewsSource.slug.in_(source_slugs),
                ArticleRevision.extraction_status != ExtractionStatus.COMPLETE,
            )
            .order_by(ArticleRevision.last_seen_at.desc(), ArticleRevision.id.desc())
            .limit(limit)
        )
        with self.sessions() as session:
            return [
                (slug, Candidate(url, row.title, row.published_at, row.rss_summary, row.language))
                for slug, url, row in session.execute(query)
            ]

    def start(self, source: SourceSettings) -> Run:
        fields = {"name": source.name, "rss_url": str(source.rss_url) if source.rss_url else None}
        with self.sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            insert = sqlite_insert if dialect == "sqlite" else postgres_insert
            source_id = session.execute(
                insert(NewsSource)
                .values(slug=source.slug, **fields)
                .on_conflict_do_update(index_elements=[NewsSource.slug], set_=fields)
                .returning(NewsSource.id)
            ).scalar_one()
            log = FetchLog(source_id=source_id)
            session.add(log)
            session.flush()
            return Run(id=log.id, source_id=source_id)

    def save(
        self, run: Run, discovered: int, items: Sequence[tuple[Material, datetime]]
    ) -> SaveCounts:
        counts = SaveCounts()
        with self.sessions.begin() as session:
            # The first write serializes deduplication for this source, with no network I/O
            # in the transaction. SQLite takes a write lock, PostgreSQL locks the source row.
            session.execute(
                update(NewsSource)
                .where(NewsSource.id == run.source_id)
                .values(name=NewsSource.name)
            )
            for material, seen_at in items:
                _save_article(session, run.source_id, material, seen_at, counts)

            session.execute(
                update(FetchLog)
                .where(FetchLog.id == run.id)
                .values(
                    status=FetchStatus.COMPLETE,
                    finished_at=datetime.now(UTC),
                    discovered_count=discovered,
                    saved_count=counts.new_revisions,
                )
            )
        return counts

    def fail(self, run: Run, reason: str, *, discovered: int = 0) -> None:
        with self.sessions.begin() as session:
            session.execute(
                update(FetchLog)
                .where(FetchLog.id == run.id)
                .values(
                    status=FetchStatus.FAILED,
                    finished_at=datetime.now(UTC),
                    discovered_count=discovered,
                    error=reason,
                )
            )


def _lookup_url(session: Session, source_id: int, url: str) -> Article | None:
    direct = session.scalar(
        select(Article).where(Article.source_id == source_id, Article.canonical_url == url)
    )
    if direct is not None:
        return direct
    return session.scalar(
        select(Article)
        .join(ArticleAlias, ArticleAlias.article_id == Article.id)
        .where(ArticleAlias.source_id == source_id, ArticleAlias.url == url)
    )


def _find_republication(
    session: Session, source_id: int, material: Material, fingerprint: str
) -> Article | None:
    # Title alone and short generic teasers cannot establish that two links are the same story.
    if not material.body and len(material.candidate.summary or "") < MIN_REPOST_SUMMARY_LENGTH:
        return None
    return session.scalar(
        select(Article)
        .join(ArticleRevision, ArticleRevision.article_id == Article.id)
        .where(Article.source_id == source_id, ArticleRevision.content_hash == fingerprint)
        .order_by(Article.id)
        .limit(1)
    )


def _save_article(
    session: Session, source_id: int, material: Material, seen_at: datetime, counts: SaveCounts
) -> None:
    candidate = material.candidate
    fingerprint = _content_hash(material)
    original = _lookup_url(session, source_id, material.input_url)
    canonical = (
        original
        if material.canonical_url == material.input_url
        else _lookup_url(session, source_id, material.canonical_url)
    )
    article = (
        original or canonical or _find_republication(session, source_id, material, fingerprint)
    )
    if article is None:
        article = Article(
            source_id=source_id,
            original_url=candidate.url,
            canonical_url=material.canonical_url,
            first_seen_at=seen_at,
        )
        session.add(article)
        session.flush()
        counts.new_articles += 1
    elif (
        original is not None and canonical is None and material.canonical_url != material.input_url
    ):
        # An article initially seen only in RSS now has a canonical link from its page.
        previous_url = article.canonical_url
        if previous_url == material.input_url:
            article.canonical_url = material.canonical_url
            _add_alias(session, source_id, article.id, previous_url)

    if original is None and material.input_url != article.canonical_url:
        _add_alias(session, source_id, article.id, material.input_url)
    if canonical is None and material.canonical_url not in {
        article.canonical_url,
        material.input_url,
    }:
        _add_alias(session, source_id, article.id, material.canonical_url)

    revision = _find_revision(session, article.id, material, fingerprint)
    if revision is not None:
        revision.last_seen_at = max(revision.last_seen_at, seen_at)
        if revision.published_at is None:
            revision.published_at = candidate.published_at
        if revision.language is None:
            revision.language = material.language
        if material.body and candidate.summary:
            revision.rss_summary = candidate.summary
        counts.unchanged += 1
        return

    session.add(
        ArticleRevision(
            article_id=article.id,
            title=candidate.title,
            published_at=candidate.published_at,
            fetched_at=seen_at,
            last_seen_at=seen_at,
            rss_summary=candidate.summary,
            body_text=material.body,
            language=material.language,
            content_hash=fingerprint,
            extraction_status=material.status,
        )
    )
    counts.new_revisions += 1


def _add_alias(session: Session, source_id: int, article_id: int, url: str) -> None:
    session.add(ArticleAlias(source_id=source_id, article_id=article_id, url=url))


def _find_revision(
    session: Session, article_id: int, material: Material, fingerprint: str
) -> ArticleRevision | None:
    revisions = select(ArticleRevision).where(ArticleRevision.article_id == article_id)
    if not material.body:
        # Keep existing complete text through transient extraction failures when
        # the feed has not supplied any new text.
        latest = session.scalar(
            revisions.order_by(
                ArticleRevision.last_seen_at.desc(), ArticleRevision.id.desc()
            ).limit(1)
        )
        if (
            latest is not None
            and latest.body_text
            and latest.title == material.candidate.title
            and latest.rss_summary == material.candidate.summary
        ):
            return latest
    return session.scalar(revisions.where(ArticleRevision.content_hash == fingerprint))


def _content_hash(material: Material) -> str:
    candidate = material.candidate
    body = clean_text(material.body) if material.body else None
    summary = clean_text(candidate.summary) if candidate.summary and not body else None
    data = json.dumps([clean_text(candidate.title), body, summary], ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()
