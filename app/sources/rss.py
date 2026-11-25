"""Shared parsing for publisher RSS and RDF feeds."""

import logging
from datetime import UTC, datetime
from urllib.parse import urljoin

import feedparser

from app.collectors.http import FetchError, HttpFetcher
from app.config.settings import SourceSettings
from app.preprocessing import normalize_language, plain_html, within_hosts
from app.sources.base import Candidate

logger = logging.getLogger(__name__)


class RssSource:
    publisher_hosts: tuple[str, ...] = ()
    default_language: str | None = None

    def __init__(self, settings: SourceSettings, http: HttpFetcher) -> None:
        self.settings = settings
        self.http = http
        feed_host = settings.rss_url.host if settings.rss_url else None
        self.hosts = self.publisher_hosts + ((feed_host,) if feed_host else ())

    def allows_article_url(self, url: str) -> bool:
        return within_hosts(url, self.hosts)

    def retrieve(self, candidate: Candidate) -> Candidate:
        return candidate

    def fetch(self) -> list[Candidate]:
        if self.settings.rss_url is None:
            raise FetchError("missing_rss_endpoint")
        download = self.http.get(str(self.settings.rss_url), allow=self.allows_article_url)
        parsed = feedparser.parse(
            download.body,
            response_headers={
                "content-location": download.url,
                "content-type": download.headers.get("content-type", "application/xml"),
            },
        )
        if not parsed.version or (parsed.bozo and not parsed.entries):
            raise FetchError("invalid_feed")
        if parsed.bozo:
            logger.warning(
                "Recovered valid entries from an imperfect XML feed",
                extra={"fields": {"source": self.settings.slug}},
            )

        feed_language = normalize_language(parsed.feed.get("language")) or self.default_language
        candidates = []
        for entry in parsed.entries:
            link = entry.get("link")
            title = plain_html(entry.get("title"))
            if not isinstance(link, str) or not link or not title:
                continue
            try:
                url = urljoin(download.url, link)
            except ValueError:
                continue
            if not self.allows_article_url(url):
                continue
            parsed_date = entry.get("published_parsed") or entry.get("updated_parsed")
            candidates.append(
                Candidate(
                    url=url,
                    title=title,
                    summary=plain_html(entry.get("summary")),
                    published_at=_utc_date(parsed_date),
                    language=normalize_language(entry.get("language")) or feed_language,
                )
            )
        if parsed.entries and not candidates:
            raise FetchError("no_valid_feed_items")
        return candidates


def _utc_date(parsed_date: tuple[int, ...] | None) -> datetime | None:
    if parsed_date is not None:
        try:
            return datetime(*parsed_date[:6], tzinfo=UTC)
        except ValueError:
            pass
    return None
