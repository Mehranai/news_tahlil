import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.cli import main
from app.notifications.message import Alert, render_message
from app.notifications.telegram import TelegramSender
from app.pipelines.notifications import NotificationPipeline
from app.storage.database import create_database_engine
from app.storage.models import (
    Article,
    ArticleRevision,
    Classification,
    HumanFeedback,
    NewsSource,
    Notification,
)
from app.storage.notifications import NotificationStore

ROOT = Path(__file__).resolve().parents[1]
TEHRAN = ZoneInfo("Asia/Tehran")
TOKEN = "123:fixture-token"
PUBLISHED = datetime(2026, 11, 24, 8, 30, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{(tmp_path / 'notifications.db').as_posix()}"
    monkeypatch.setenv("NEWS_DATABASE_URL", url)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = create_database_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def seed(engine: Engine, name: str, label: str, *, slug: str = "bbc") -> int:
    with Session(engine) as session, session.begin():
        source = session.scalar(select(NewsSource).where(NewsSource.slug == slug))
        if source is None:
            source = NewsSource(slug=slug, name="Test news")
            session.add(source)
            session.flush()
        url = f"https://example.test/{name}"
        article = Article(
            source_id=source.id, original_url=f"{url}?utm_source=rss", canonical_url=url
        )
        session.add(article)
        session.flush()
        revision = ArticleRevision(
            article_id=article.id,
            title=f"{name} <Headline>",
            content_hash="a" * 64,
            published_at=PUBLISHED,
        )
        session.add(revision)
        session.flush()
        add_label(session, revision.id, label)
        return article.id


def add_label(session: Session, revision_id: int, label: str) -> None:
    session.add(
        Classification(
            article_revision_id=revision_id,
            provider="fixture",
            model="fixture-model",
            prompt_version="fixture",
            relevance=label,
            confidence=0.83,
            topic="nuclear_program",
            reason="دلیل ارتباط",
            summary_fa="این خبر یک نمونهٔ آزمایشی است.",
            needs_review=label == "uncertain",
        )
    )


def successful_send(message_id: int = 51) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": message_id}})


def pipeline(database: Engine, client: httpx.Client) -> NotificationPipeline:
    sender = TelegramSender(client, TOKEN, "-1001234", timeout_seconds=5)
    return NotificationPipeline(
        NotificationStore(sessionmaker(database)), sender, timezone=TEHRAN, sleep=lambda _: None
    )


def test_latest_human_label_controls_unsent_alerts(database: Engine) -> None:
    dismissed = seed(database, "dismissed", "direct")
    promoted = seed(database, "promoted", "unrelated")
    with Session(database) as session, session.begin():
        revisions = dict(
            session.execute(select(ArticleRevision.article_id, ArticleRevision.id)).all()
        )
        session.add_all(
            [
                HumanFeedback(article_revision_id=revisions[dismissed], relevance="unrelated"),
                HumanFeedback(article_revision_id=revisions[promoted], relevance="uncertain"),
                HumanFeedback(
                    article_revision_id=revisions[promoted], relevance="indirect", note="بازبینی شد"
                ),
            ]
        )

    messages = []

    def handle(request: httpx.Request) -> httpx.Response:
        messages.append(json.loads(request.content)["text"])
        return successful_send()

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert pipeline(database, client).notify(["bbc"]).sent == 1
        assert pipeline(database, client).notify(["bbc"]).sent == 0
    assert len(messages) == 1
    assert "نوع ارتباط: غیرمستقیم" in messages[0]
    assert "بازبینی شد" in messages[0]
    assert "/promoted" in messages[0]


def test_notifies_direct_indirect_once_and_supports_uncertain_opt_in(database: Engine) -> None:
    direct = seed(database, "direct", "direct")
    indirect = seed(database, "indirect", "indirect")
    uncertain = seed(database, "uncertain", "uncertain")
    seed(database, "unrelated", "unrelated")
    seed(database, "excluded", "direct", slug="guardian")
    messages = []

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        data = json.loads(request.content)
        assert data["chat_id"] == -1001234
        assert "parse_mode" not in data
        assert data["link_preview_options"] == {"is_disabled": True}
        messages.append(data["text"])
        return successful_send(50 + len(messages))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        sender = pipeline(database, client)
        assert sender.notify(["bbc"]).sent == 2
        assert sender.notify(["bbc"]).selected == 0
        assert sender.notify(["bbc"], send_uncertain=True).sent == 1
        assert sender.notify(["bbc"], send_uncertain=True).selected == 0

    assert len(messages) == 3
    assert "direct <Headline>" in messages[0]
    assert "هسته‌ای" in messages[0]
    assert "2026-11-24 12:00" in messages[0]
    assert messages[0].endswith("https://example.test/direct?utm_source=rss")
    assert "غیرمستقیم" in messages[1]
    assert "نیازمند بررسی" in messages[2]
    with Session(database) as session:
        rows = session.scalars(select(Notification).order_by(Notification.id)).all()
        assert [row.article_id for row in rows] == [direct, indirect, uncertain]
        assert [row.external_message_id for row in rows] == ["51", "52", "53"]
        assert all(row.status == "sent" and row.sent_at and row.attempt_count == 1 for row in rows)


def test_only_the_latest_revision_and_result_can_trigger_an_alert(database: Engine) -> None:
    edited = seed(database, "edited", "direct")
    reevaluated = seed(database, "reevaluated", "indirect")
    with Session(database) as session, session.begin():
        session.add(ArticleRevision(article_id=edited, title="New version", content_hash="b" * 64))
        result = session.scalar(
            select(ArticleRevision.id).where(ArticleRevision.article_id == reevaluated)
        )
        add_label(session, result, "unrelated")
    store = NotificationStore(sessionmaker(database))
    assert (
        store.candidates(
            ["bbc"], send_uncertain=False, retry_failed=False, retry_unknown=None, limit=None
        )
        == []
    )


def test_rate_limit_persists_and_resumes_only_after_wait(database: Engine) -> None:
    first = seed(database, "first", "direct")
    seed(database, "second", "indirect")
    requests = 0

    def handle(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return httpx.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 300}}
            )
        return successful_send(requests + 100)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        initial = pipeline(database, client).notify(["bbc"])
        assert initial.halted == "telegram_rate_limited"
        assert initial.deferred_until is not None
        with Session(database) as session, session.begin():
            row = session.scalars(select(Notification)).one()
            assert row.article_id == first
            assert row.next_attempt_at > datetime.now(UTC) + timedelta(seconds=270)
        deferred = pipeline(database, client).notify(["bbc"], retry_failed=True)
        assert deferred.selected == 0 and deferred.deferred_until
        assert requests == 1
        with Session(database) as session, session.begin():
            session.execute(update(Notification).values(next_attempt_at=datetime.now(UTC)))
        assert pipeline(database, client).notify(["bbc"]).sent == 2
    assert requests == 3


@pytest.mark.parametrize("failure", ["connect", "rejected"])
def test_confirmed_failure_can_be_retried(database: Engine, failure: str) -> None:
    seed(database, "retry", "direct")
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "connect":
                raise httpx.ConnectTimeout(str(request.url), request=request)
            return httpx.Response(403, json={"ok": False, "error_code": 403})
        return successful_send()

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        sender = pipeline(database, client)
        assert sender.notify(["bbc"]).failed == 1
        if failure == "rejected":
            assert sender.notify(["bbc"]).selected == 0
        assert sender.notify(["bbc"], retry_failed=failure == "rejected").sent == 1
    with Session(database) as session:
        row = session.scalars(select(Notification)).one()
        assert row.attempt_count == 2 and row.last_error is None


@pytest.mark.parametrize("failure", ["timeout", "gateway", "invalid_success"])
def test_ambiguous_delivery_is_only_retried_for_the_selected_article(
    database: Engine, failure: str
) -> None:
    article_id = seed(database, "unknown", "direct")
    requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("response was lost", request=request)
            if failure == "gateway":
                return httpx.Response(502, text="Bad gateway")
            return httpx.Response(200, json={"ok": True})
        return successful_send()

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        sender = pipeline(database, client)
        assert sender.notify(["bbc"]).unknown_articles == [article_id]
        assert sender.notify(["bbc"], retry_failed=True).selected == 0
        assert requests == 1
        assert sender.notify(["bbc"], retry_unknown=article_id).sent == 1
    assert requests == 2


def test_concurrent_claims_and_expired_delivery_never_replay_automatically(
    database: Engine,
) -> None:
    article_id = seed(database, "concurrent", "indirect")
    store = NotificationStore(sessionmaker(database), lease_seconds=0)
    select_args = dict(send_uncertain=False, retry_failed=False, retry_unknown=None, limit=None)
    (alert,) = store.candidates(["bbc"], **select_args)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda _: store.claim(alert, retry_failed=False, retry_unknown=False), range(2)
            )
        )
    winner = next(claim for claim in claims if claim is not None)
    assert claims.count(None) == 1
    store.expire_claims()
    assert store.unknown_articles() == [article_id]
    assert store.candidates(["bbc"], **select_args) == []
    assert store.sent(winner, "123") is True
    assert store.unknown_articles() == []


def test_message_preserves_whole_link_and_respects_telegram_size() -> None:
    original = "https://example.test/" + "x" * 4000
    canonical = "https://example.test/story"
    alert = Alert(
        1,
        1,
        "Publisher",
        "📌" * 2000,
        original,
        canonical,
        None,
        "indirect",
        "not_mapped",
        "خلاصه " * 1000,
        "دلیل " * 1000,
    )
    message = render_message(alert, TEHRAN)
    assert len(message.encode("utf-16-le")) // 2 <= 4096
    assert message.endswith(canonical)
    assert "تاریخ انتشار: نامشخص" in message
    assert "دسته‌بندی: سایر" in message
    with pytest.raises(ValueError, match="invalid_notification_link"):
        render_message(replace(alert, canonical_url="javascript:void(0)"), TEHRAN)


def test_cli_uses_env_credentials_and_never_logs_them(
    database: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed(database, "cli", "direct")
    (tmp_path / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=@news_fixture\n", encoding="utf-8"
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    (tmp_path / "config.toml").write_text(
        '[[sources]]\nslug="bbc"\nname="BBC"\nrss_url="https://example.test/rss"\n'
        "allow_classification=true\n",
        encoding="utf-8",
    )

    def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["chat_id"] == "@news_fixture"
        raise httpx.ReadError(str(request.url), request=request)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr("app.cli.httpx.Client", lambda: client)
    assert main(["notify", "--limit", "1"]) == 1
    output = capsys.readouterr()
    assert TOKEN not in output.err and TOKEN not in output.out
    assert json.loads(output.out)["halted"] == "delivery_unknown"


def test_migration_preserves_existing_confirmed_delivery(database: Engine) -> None:
    article = seed(database, "old-delivery", "direct")
    config = Config(str(ROOT / "alembic.ini"))
    command.downgrade(config, "0003")
    with database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO notifications (article_id, channel, status, attempt_count,"
                " external_message_id, updated_at, sent_at)"
                " VALUES (:article, 'telegram', 'sent', 1, '170', :sent_at, :sent_at)"
            ),
            {"article": article, "sent_at": "2026-11-24 10:00:00"},
        )
    command.upgrade(config, "head")
    with Session(database) as session:
        row = session.scalars(select(Notification)).one()
        assert row.article_id == article
        assert row.status == "sent" and row.external_message_id == "170"
    store = NotificationStore(sessionmaker(database))
    assert (
        store.candidates(
            ["bbc"], send_uncertain=True, retry_failed=True, retry_unknown=None, limit=None
        )
        == []
    )
