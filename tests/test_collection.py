import json
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, time
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.cli import main
from app.collectors.http import FetchError, HttpFetcher
from app.config.settings import (
    CollectionSettings,
    RuntimeSettings,
    ScheduleSettings,
    load_project_settings,
)
from app.extractors.article import ArticleExtractor
from app.pipelines.collection import CollectionPipeline
from app.preprocessing import normalize_url
from app.scheduler import upcoming_runs
from app.sources.base import Candidate
from app.sources.guardian import GuardianSource
from app.sources.registry import build_source
from app.storage.collection import CollectionStore
from app.storage.database import create_database_engine
from app.storage.models import Article, ArticleAlias, ArticleRevision, FetchLog, FetchStatus
from app.storage.pipeline_lock import pipeline_lock

ROOT = Path(__file__).resolve().parents[1]
TITLE = "Civic laboratories publish a report on the coast"
SUMMARY = "Local researchers presented the results at a public meeting."
STORY = (
    "Independent researchers presented a new analysis of coastal habitats at a public meeting. "
    "The study describes monitoring work and documents changes recorded over several years."
)
ADDITIONAL = (
    "Residents and local officials discussed the findings after the presentations. "
    "The researchers published their notes and invited the community to review the evidence."
)


def rss(*items: tuple[str, str, str]) -> str:
    entries = "".join(
        f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
        f"<description>{escape(summary)}</description>"
        "<pubDate>Tue, 24 Nov 2026 11:45:00 +0330</pubDate></item>"
        for title, link, summary in items
    )
    return f"<rss version='2.0'><channel><title>World</title>{entries}</channel></rss>"


def webpage(canonical: str, text: str = STORY, *, paywall: bool = False) -> str:
    metadata = json.dumps({"isAccessibleForFree": not paywall})
    return f"""<html lang="en-GB"><head><title>{TITLE}</title>
      <link rel="canonical" href="{canonical}">
      <script type="application/ld+json">{metadata}</script></head>
      <body><nav>REMOVE NAVIGATION</nav>
      <main><article><h1>{TITLE}</h1><p>{text}</p><p>{ADDITIONAL}</p></article></main>
      <footer>REMOVE FOOTER</footer></body></html>"""


def transport(handler: Callable[[httpx.Request], httpx.Response], **kwargs: object) -> HttpFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return HttpFetcher(client, CollectionSettings(min_interval_seconds=0), **kwargs)


@pytest.fixture
def migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    monkeypatch.chdir(tmp_path)
    url = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    monkeypatch.setenv("NEWS_DATABASE_URL", url)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    engine = create_database_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()


def test_normalization_keeps_content_parameters() -> None:
    assert (
        normalize_url(
            "https://BBC.com:443/story?id=7&id=8&at_medium=RSS&UTM_source=a&empty=#section"
        )
        == "https://bbc.com/story?id=7&id=8&empty="
    )


def test_live_path_with_fixtures_survives_a_source_failure(migrated_database: Engine) -> None:
    project = load_project_settings()
    runtime = RuntimeSettings(GUARDIAN_API_KEY="fixture-key")
    project.sources[1].enabled = True
    project.sources[1].fetch_full_text = True
    api_calls = []
    rdf = """<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel rdf:about="https://rss.dw.com/rdf/rss-en-all"><title>DW</title></channel>
      <item rdf:about="https://www.dw.com/en/culture/a-123">
        <title>New theater opens</title><link>https://www.dw.com/en/culture/a-123</link>
        <description>Venues expand their program.</description>
        <dc:language>en-US</dc:language><dc:date>2026-11-24T07:30:00Z</dc:date>
      </item></rdf:RDF>"""

    def handle(request: httpx.Request) -> httpx.Response:
        match request.url.host:
            case "feeds.bbci.co.uk":
                return httpx.Response(
                    200, text=rss(("Local commuters", "https://bbc.com/news/story", ""))
                )
            case "www.aljazeera.com":
                return httpx.Response(503)
            case "rss.dw.com":
                return httpx.Response(200, text=rdf)
            case "content.guardianapis.com":
                assert request.url.params["api-key"] == "fixture-key"
                assert request.url.params["show-fields"] == "trailText,body"
                assert "q" not in request.url.params
                page = request.url.params["page"]
                api_calls.append(page)
                return httpx.Response(
                    200,
                    json={
                        "response": {
                            "status": "ok",
                            "pages": 2,
                            "results": [
                                {
                                    "type": "article",
                                    "webTitle": f"Regional research {page}",
                                    "webUrl": f"https://www.theguardian.com/world/story-{page}",
                                    "webPublicationDate": "2026-11-24T08:00:00Z",
                                    "fields": {"trailText": SUMMARY, "body": f"<p>{STORY}</p>"},
                                }
                            ],
                        }
                    },
                )
        raise AssertionError(f"Unexpected request host: {request.url.host}")

    http = transport(handle, sleep=lambda _: None)
    reports = CollectionPipeline(
        CollectionStore(sessionmaker(migrated_database)), ArticleExtractor(http)
    ).collect([build_source(source, runtime, http) for source in project.sources])

    assert [report.status for report in reports] == [
        FetchStatus.COMPLETE,
        FetchStatus.COMPLETE,
        FetchStatus.FAILED,
        FetchStatus.COMPLETE,
    ]
    assert api_calls == ["1", "2"]
    assert [report.new_articles for report in reports] == [1, 2, 0, 1]
    with Session(migrated_database) as session:
        dw = session.scalar(
            select(ArticleRevision).where(ArticleRevision.title == "New theater opens")
        )
        assert dw is not None
        assert dw.published_at == datetime(2026, 11, 24, 7, 30, tzinfo=UTC)
        assert dw.language == "en-us"
        assert dw.extraction_status == "partial"
        assert session.scalar(select(FetchLog.error).where(FetchLog.status == "failed")) == (
            "http_status (503)"
        )


def test_guardian_uses_rss_without_key_and_reports_api_rejection() -> None:
    settings = load_project_settings(ROOT / "app" / "config" / "defaults.toml")
    guardian = next(source for source in settings.sources if source.slug == "guardian")
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        if request.url.host == "content.guardianapis.com":
            return httpx.Response(403)
        return httpx.Response(
            200, text=rss((TITLE, "https://www.theguardian.com/world/news", SUMMARY))
        )

    http = transport(handle)
    assert GuardianSource(guardian, http, None).fetch()[0].title == TITLE
    with pytest.raises(FetchError, match=r"http_status \(403\)"):
        GuardianSource(guardian, http, "fixture-key").fetch()
    assert calls == ["www.theguardian.com", "content.guardianapis.com"]


def test_guardian_retrieves_saved_story_through_the_single_item_api() -> None:
    settings = load_project_settings(ROOT / "app" / "config" / "defaults.toml")
    guardian = next(source for source in settings.sources if source.slug == "guardian")
    guardian = guardian.model_copy(update={"fetch_full_text": True})
    path = "/world/2026/nov/24/coast"
    url = f"https://www.theguardian.com{path}"

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "content.guardianapis.com"
        assert request.url.path == path
        assert request.url.params["show-fields"] == "trailText,body"
        return httpx.Response(
            200,
            json={
                "response": {
                    "status": "ok",
                    "content": {
                        "webUrl": url,
                        "webTitle": TITLE,
                        "fields": {"trailText": SUMMARY, "body": f"<p>{STORY}</p>"},
                    },
                }
            },
        )

    source = GuardianSource(guardian, transport(respond), "fixture-key")
    restored = source.retrieve(Candidate(url, TITLE, summary=SUMMARY))
    assert restored.url == url
    assert restored.body == STORY
    assert not restored.allow_article_page


def test_canonical_links_republication_and_updated_body(migrated_database: Engine) -> None:
    bbc = load_project_settings().sources[0]
    bbc.fetch_full_text = True
    stage = 1

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == "feeds.bbci.co.uk":
            items = [(TITLE, "https://bbc.com/news/repost", SUMMARY)]
            if stage == 1:
                items = [
                    (TITLE, "https://bbc.com/news/original?utm_source=rss", SUMMARY),
                    (TITLE, "https://bbc.com/news/alternate", SUMMARY),
                    *items,
                ]
            return httpx.Response(200, text=rss(*items))
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if stage == 3:
            return httpx.Response(403)
        canonical = "/news/repost" if path == "/news/repost" else "/news/original"
        body = STORY if stage == 1 else STORY + " Updated measurements were published today."
        return httpx.Response(
            200, text=webpage(canonical, body), headers={"content-type": "text/html"}
        )

    http = transport(handle)
    pipeline = CollectionPipeline(
        CollectionStore(sessionmaker(migrated_database)), ArticleExtractor(http)
    )
    source = build_source(bbc, RuntimeSettings(), http)

    first = pipeline.collect([source])[0]
    stage = 2
    revised = pipeline.collect([source])[0]
    duplicate = pipeline.collect([source])[0]
    stage = 3
    blocked = pipeline.collect([source])[0]

    assert (first.new_articles, first.new_revisions, first.unchanged) == (1, 1, 2)
    assert (revised.new_articles, revised.new_revisions) == (0, 1)
    assert (duplicate.new_revisions, duplicate.unchanged) == (0, 1)
    assert (blocked.new_revisions, blocked.partial) == (0, 1)
    with Session(migrated_database) as session:
        article = session.scalars(select(Article)).one()
        assert article.canonical_url == "https://bbc.com/news/original"
        assert article.original_url.endswith("?utm_source=rss")
        assert set(session.scalars(select(ArticleAlias.url))) == {
            "https://bbc.com/news/alternate",
            "https://bbc.com/news/repost",
        }
        revisions = session.scalars(select(ArticleRevision).order_by(ArticleRevision.id)).all()
        assert len(revisions) == 2
        assert revisions[0].language == "en-gb"
        assert STORY in revisions[0].body_text
        assert "REMOVE NAVIGATION" not in revisions[0].body_text
        assert "REMOVE FOOTER" not in revisions[0].body_text
        assert "Updated measurements" in revisions[1].body_text
        assert all(item.extraction_status == "complete" for item in revisions)


def test_rate_limit_stops_later_requests_to_same_host() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(429, headers={"retry-after": "90"})

    http = transport(handle)
    for path in ("/first", "/second"):
        with pytest.raises(FetchError, match="429"):
            http.get(f"https://example.test{path}", allow=lambda _: True)
    assert requests == ["/first"]


def test_retries_transient_http_and_timeout_then_recovers() -> None:
    sleeps = []
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("test timeout", request=request)
        if attempts == 2:
            return httpx.Response(503, headers={"retry-after": "3"})
        return httpx.Response(200, content=b"ready")

    download = transport(handle, sleep=sleeps.append).get(
        "https://example.test/", allow=lambda _: True
    )

    assert download.body == b"ready"
    assert sleeps == [1, 3]


def test_robots_paywall_and_off_publisher_redirect_keep_feed_summary(
    migrated_database: Engine,
) -> None:
    bbc = load_project_settings().sources[0]
    bbc.fetch_full_text = True
    seen = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path))
        if request.url.host == "feeds.bbci.co.uk":
            items = [
                (f"{TITLE} {path}", f"https://www.bbc.com/{path}", SUMMARY)
                for path in ("blocked", "subscriber", "redirect")
            ]
            return httpx.Response(200, text=rss(*items))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /blocked\n")
        if request.url.path == "/subscriber":
            return httpx.Response(200, text=webpage("/subscriber", paywall=True))
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "https://other.test/article"})
        raise AssertionError("An unavailable article should never be fetched")

    http = transport(handle)
    report = CollectionPipeline(
        CollectionStore(sessionmaker(migrated_database)), ArticleExtractor(http)
    ).collect([build_source(bbc, RuntimeSettings(), http)])[0]

    assert report.partial == 3
    assert report.new_articles == 3
    assert seen == [
        ("feeds.bbci.co.uk", "/news/world/rss.xml"),
        ("www.bbc.com", "/robots.txt"),
        ("www.bbc.com", "/subscriber"),
        ("www.bbc.com", "/redirect"),
    ]
    with Session(migrated_database) as session:
        assert all(
            article.body_text is None and article.rss_summary == SUMMARY
            for article in session.scalars(select(ArticleRevision))
        )


def test_concurrent_saves_cannot_create_duplicate_articles(migrated_database: Engine) -> None:
    bbc = load_project_settings().sources[0]
    feed = rss((TITLE, "https://bbc.com/news/same", SUMMARY))
    http = transport(lambda _: httpx.Response(200, text=feed))
    source = build_source(bbc, RuntimeSettings(), http)
    material = ArticleExtractor(http).resolve(source, source.fetch()[0])
    store = CollectionStore(sessionmaker(migrated_database))
    runs = [store.start(bbc), store.start(bbc)]
    items = [(material, datetime.now(UTC))]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda run: store.save(run, 1, items), runs))

    assert sorted(result.new_articles for result in results) == [0, 1]
    assert sorted(result.new_revisions for result in results) == [0, 1]


def test_collection_cli_selects_one_source(
    migrated_database: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.aljazeera.com"
        return httpx.Response(
            200, text=rss((TITLE, "https://www.aljazeera.com/news/coast", SUMMARY))
        )

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr("app.cli.httpx.Client", lambda: client)

    assert main(["collect", "--source", "aljazeera"]) == 0
    reports = json.loads(capsys.readouterr().out)
    assert [report["source"] for report in reports] == ["aljazeera"]
    assert reports[0]["new_articles"] == 1


def test_schedule_uses_each_configured_slot_and_wraps_to_the_next_day() -> None:
    settings = ScheduleSettings(timezone="Asia/Tehran", times=[time(17), time(8, 15), time(11)])
    assert upcoming_runs(settings, now=datetime(2026, 11, 24, 5, tzinfo=UTC)) == [
        "2026-11-24T11:00:00+03:30",
        "2026-11-24T17:00:00+03:30",
        "2026-11-25T08:15:00+03:30",
        "2026-11-25T11:00:00+03:30",
        "2026-11-25T17:00:00+03:30",
    ]


def test_cli_extracts_saved_article_and_refuses_overlap(
    migrated_database: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    article = "https://world.example.test/story"
    (tmp_path / "config.toml").write_text(
        "[collection]\nmin_interval_seconds = 0\n"
        '[[sources]]\nslug="world"\nname="World"\n'
        'rss_url="https://world.example.test/rss"\nfetch_full_text=true\n',
        encoding="utf-8",
    )
    accessed = []

    def handle(request: httpx.Request) -> httpx.Response:
        accessed.append(request.url.path)
        if request.url.path == "/rss":
            return httpx.Response(200, text=rss((TITLE, article, SUMMARY)))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:")
        assert request.url.path == "/story"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=webpage(article))

    client_factory = httpx.Client
    monkeypatch.setattr(
        "app.cli.httpx.Client", lambda: client_factory(transport=httpx.MockTransport(handle))
    )
    with pipeline_lock(migrated_database) as acquired:
        assert acquired
        assert main(["collect", "--metadata-only"]) == 3
        assert accessed == []
    assert main(["collect", "--metadata-only"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["partial"] == 1
    assert main(["extract", "--limit", "1"]) == 0
    report = json.loads(capsys.readouterr().out)[0]
    assert report["new_articles"] == 0
    assert report["new_revisions"] == report["complete"] == 1
    assert accessed == ["/rss", "/robots.txt", "/story"]
    assert main(["extract"]) == 0
    assert json.loads(capsys.readouterr().out) == []
