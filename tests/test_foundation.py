import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.cli import database_status
from app.logging_config import JsonFormatter
from app.storage.database import create_database_engine
from app.storage.models import (
    Article,
    ArticleRevision,
    Classification,
    HumanFeedback,
    NewsSource,
    Notification,
    Relevance,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Config]:
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "nested" / "test.db"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setenv("NEWS_DATABASE_URL", url)
    return url, Config(str(ROOT / "alembic.ini"))


@pytest.fixture
def migrated_session(database: tuple[str, Config]) -> Iterator[Session]:
    url, config = database
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    try:
        with Session(engine) as session, session.begin():
            yield session
    finally:
        engine.dispose()


def test_database_migrates_matches_orm_and_can_be_recreated(
    database: tuple[str, Config],
) -> None:
    url, config = database
    assert database_status(url) == {"backend": "sqlite", "revisions": [], "counts": {}}

    command.upgrade(config, "head")
    command.upgrade(config, "head")
    command.check(config)

    result = database_status(url)
    assert result["revisions"] == ["0005"]
    assert result["counts"] == dict.fromkeys(
        [
            "news_sources",
            "articles",
            "article_aliases",
            "article_revisions",
            "classifications",
            "analysis_jobs",
            "notifications",
            "fetch_logs",
            "pipeline_runs",
            "human_feedback",
        ],
        0,
    )

    command.downgrade(config, "base")
    assert database_status(url)["counts"] == {}
    command.upgrade(config, "head")
    assert database_status(url) == result


def test_sqlite_rejects_unknown_source(migrated_session: Session) -> None:
    with pytest.raises(IntegrityError, match="FOREIGN KEY"), migrated_session.begin_nested():
        migrated_session.add(
            Article(
                source_id=999,
                original_url="https://example.test/news",
                canonical_url="https://example.test/news",
            )
        )


def test_phase_two_migration_preserves_existing_articles(database: tuple[str, Config]) -> None:
    url, config = database
    command.upgrade(config, "0001")
    engine = create_database_engine(url)
    try:
        with Session(engine) as session, session.begin():
            source = NewsSource(slug="existing", name="Existing", rss_url=None)
            session.add(source)
            session.flush()
            session.add(
                Article(
                    source_id=source.id,
                    original_url="https://example.test/story",
                    canonical_url="https://example.test/story",
                )
            )
        command.upgrade(config, "head")
        with Session(engine) as session:
            assert session.scalars(select(Article)).one().canonical_url.endswith("/story")
        assert database_status(url)["counts"]["article_aliases"] == 0
    finally:
        engine.dispose()


def test_article_revisions_model_results_and_notification_are_independent(
    migrated_session: Session,
) -> None:
    session = migrated_session
    source = NewsSource(slug="test", name="Example", rss_url="https://example.test/rss")
    session.add(source)
    session.flush()
    article = Article(
        source_id=source.id,
        original_url="https://example.test/news?utm_source=rss",
        canonical_url="https://example.test/news",
    )
    session.add(article)
    session.flush()
    with pytest.raises(IntegrityError, match="UNIQUE"), session.begin_nested():
        session.add(
            Article(
                source_id=source.id,
                original_url="https://example.test/news?utm_source=email",
                canonical_url=article.canonical_url,
            )
        )

    tehran_time = timezone(timedelta(hours=3, minutes=30))
    original = ArticleRevision(
        article_id=article.id,
        content_hash="a" * 64,
        title="Original Title",
        published_at=datetime(2026, 11, 24, 8, tzinfo=tehran_time),
    )
    updated = ArticleRevision(article_id=article.id, content_hash="b" * 64, title="Updated Title")
    session.add_all([original, updated])
    session.flush()
    session.refresh(original)
    assert original.published_at == datetime(2026, 11, 24, 4, 30, tzinfo=UTC)
    with pytest.raises(IntegrityError, match="UNIQUE"), session.begin_nested():
        session.add(
            ArticleRevision(
                article_id=article.id, content_hash=original.content_hash, title="Duplicate"
            )
        )

    classifications = [
        Classification(
            article_revision_id=original.id,
            provider="test",
            model=model,
            prompt_version="1",
            relevance=relevance,
            confidence=0.8,
            topic="other",
            reason="Synthetic fixture",
            evidence=["Original Title"],
            related_entities=[],
            summary_fa="نمونه آزمایشی",
            needs_review=False,
        )
        for model, relevance in [("A", Relevance.INDIRECT), ("B", Relevance.UNCERTAIN)]
    ]
    session.add_all(classifications)
    session.flush()
    session.add(
        HumanFeedback(
            article_revision_id=original.id,
            classification_id=classifications[0].id,
            relevance=Relevance.INDIRECT,
        )
    )
    session.add(
        Notification(
            article_id=article.id, classification_id=classifications[0].id, channel="telegram"
        )
    )
    session.flush()

    with pytest.raises(IntegrityError, match="UNIQUE"), session.begin_nested():
        session.add(
            Notification(
                article_id=article.id,
                classification_id=classifications[1].id,
                channel="telegram",
            )
        )
    assert session.scalar(select(func.count()).select_from(Classification)) == 2
    assert session.scalar(select(func.count()).select_from(HumanFeedback)) == 1
    assert session.scalar(select(func.count()).select_from(Notification)) == 1


def test_migration_creates_sqlite_check_constraints(database: tuple[str, Config]) -> None:
    url, config = database
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    try:
        constraints = inspect(engine).get_check_constraints("classifications")
    finally:
        engine.dispose()

    assert {item["name"] for item in constraints} == {
        "ck_classifications_confidence_range",
        "ck_classifications_relevance",
    }


def test_log_is_valid_json_with_structured_fields() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 0, "Event %s", ("one",), None)
    record.fields = {"source": "نمونه"}

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "Event one"
    assert payload["fields"] == {"source": "نمونه"}
    assert payload["timestamp"].endswith("+00:00")
