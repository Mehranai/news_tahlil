"""Build a usable article from feed data and, when permitted, its article page."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from lxml import etree, html
from protego import Protego

from app.collectors.http import USER_AGENT, FetchError, HttpFetcher
from app.preprocessing import normalize_language, normalize_url
from app.sources.base import Candidate, Source
from app.storage.models import ExtractionStatus

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Material:
    candidate: Candidate
    input_url: str
    canonical_url: str
    language: str | None
    status: ExtractionStatus
    body: str | None = None


class ArticleExtractor:
    def __init__(self, http: HttpFetcher) -> None:
        self.http = http
        self._robots: dict[str, Protego | None] = {}

    def resolve(self, source: Source, candidate: Candidate) -> Material:
        requested = normalize_url(candidate.url)
        if requested is None:
            raise FetchError("invalid_article_url")
        if candidate.body:
            return Material(
                candidate,
                requested,
                requested,
                candidate.language,
                ExtractionStatus.COMPLETE,
                candidate.body,
            )

        if source.settings.fetch_full_text and candidate.allow_article_page:
            try:
                return self._fetch_page(source, candidate, requested)
            except FetchError as exc:
                logger.info(
                    "Article page unavailable",
                    extra={"fields": {"source": source.settings.slug, "reason": str(exc)}},
                )
        return _material(candidate, requested, requested, candidate.language)

    def _fetch_page(self, source: Source, candidate: Candidate, requested: str) -> Material:
        allow = source.allows_article_url
        download = self.http.get(
            requested, allow=lambda url: allow(url) and self._permitted_by_robots(url, allow)
        )
        response_type = download.headers.get("content-type", "").split(";")[0].strip().lower()
        if response_type and response_type not in {"text/html", "application/xhtml+xml"}:
            raise FetchError("article_is_not_html")
        try:
            page = html.fromstring(download.body)
        except (ValueError, etree.ParserError, etree.XMLSyntaxError):
            raise FetchError("invalid_article_html") from None
        if _uses_paywall(page):
            raise FetchError("article_access_restricted")

        canonical = normalize_url(download.url) or requested
        for href in page.xpath("//link[@rel='canonical']/@href"):
            try:
                link = normalize_url(urljoin(download.url, href.strip()))
            except ValueError:
                continue
            if link and allow(link):
                canonical = link
                break
        language = normalize_language(page.get("lang")) or candidate.language
        from trafilatura import extract

        try:
            text = extract(
                page,
                url=download.url,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
        except (ValueError, etree.LxmlError):
            raise FetchError("article_extraction_failed") from None
        return _material(candidate, requested, canonical, language, text)

    def _permitted_by_robots(self, url: str, allow: Callable[[str], bool]) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            try:
                download = self.http.get(f"{origin}/robots.txt", allow=allow, accept_missing=True)
                parser = Protego.parse(download.body.decode("utf-8-sig", errors="replace"))
                crawl_delay = parser.crawl_delay(USER_AGENT)
                rate = parser.request_rate(USER_AGENT)
                interval = max(crawl_delay or 0, rate.seconds / rate.requests if rate else 0)
                # A publisher can ask for pauses longer than this collection run can wait.
                if interval > 60:
                    self._robots[origin] = None
                else:
                    self.http.set_host_interval(url, interval)
                    self._robots[origin] = parser
            except FetchError:
                self._robots[origin] = None
        rules = self._robots[origin]
        return rules is not None and rules.can_fetch(url, USER_AGENT)


def _material(
    candidate: Candidate,
    requested: str,
    canonical: str,
    language: str | None,
    body: str | None = None,
) -> Material:
    body = body.strip() if body else None
    if body:
        status = ExtractionStatus.COMPLETE
    elif candidate.summary:
        status = ExtractionStatus.PARTIAL
    else:
        status = ExtractionStatus.FAILED
    return Material(candidate, requested, canonical, language, status, body)


def _uses_paywall(page: html.HtmlElement) -> bool:
    indicators = page.xpath(
        "//meta[@name='isAccessibleForFree']/@content"
        " | //*[@itemprop='isAccessibleForFree']/@content"
        " | //*[@data-testid='paywall' or @data-paywall='true']"
    )
    if any(not isinstance(item, str) or _is_false(item) for item in indicators):
        return True
    for contents in page.xpath("//script[@type='application/ld+json']/text()"):
        try:
            data = json.loads(contents)
        except (ValueError, RecursionError):
            continue
        if _contains_restricted_content(data):
            return True
    return False


def _contains_restricted_content(data: object) -> bool:
    pending = [data]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if _is_false(item.get("isAccessibleForFree")):
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _is_false(value: object) -> bool:
    return value is False or (isinstance(value, str) and value.strip().lower() == "false")
