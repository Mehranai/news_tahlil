"""Guardian RSS and paginated, licensed Content API access."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from app.collectors.http import FetchError, HttpFetcher
from app.config.settings import SourceSettings
from app.preprocessing import normalize_language, plain_html, within_hosts
from app.sources.base import Candidate
from app.sources.rss import RssSource

MAX_PAGES = 20


class GuardianSource(RssSource):
    publisher_hosts = ("theguardian.com",)
    default_language = "en"

    def __init__(self, settings: SourceSettings, http: HttpFetcher, api_key: str | None) -> None:
        super().__init__(settings, http)
        self._api_key = api_key

    def retrieve(self, candidate: Candidate) -> Candidate:
        endpoint = self.settings.api_search_url
        if not (endpoint and self._api_key):
            return candidate
        if endpoint.scheme != "https" or not endpoint.host:
            raise FetchError("guardian_api_requires_https")
        if not self.allows_article_url(candidate.url):
            raise FetchError("invalid_article_url")

        host = endpoint.host
        origin = urlsplit(str(endpoint)).netloc
        path = urlsplit(candidate.url).path
        download = self.http.get(
            f"https://{origin}{path}",
            allow=lambda url: within_hosts(url, (host,)),
            params={"api-key": self._api_key, "show-fields": "trailText,body"},
            follow_redirects=False,
        )
        try:
            response = json.loads(download.body)["response"]
            item = response["content"]
            if response["status"] == "ok" and isinstance(item, dict):
                if article := self._article(item):
                    return replace(article, url=candidate.url)
        except (ValueError, KeyError, TypeError):
            pass
        raise FetchError("invalid_guardian_response")

    def fetch(self) -> list[Candidate]:
        endpoint = self.settings.api_search_url
        if not (endpoint and self._api_key):
            if not self.settings.rss_url:
                raise FetchError("missing_guardian_api_credentials")
            return super().fetch()
        if endpoint.scheme != "https" or not endpoint.host:
            raise FetchError("guardian_api_requires_https")

        api_host = endpoint.host
        show_fields = "trailText,body" if self.settings.fetch_full_text else "trailText"
        since = datetime.now(UTC).date() - timedelta(days=1)
        candidates = []
        for page in range(1, MAX_PAGES + 1):
            download = self.http.get(
                str(endpoint),
                allow=lambda url: within_hosts(url, (api_host,)),
                params={
                    "api-key": self._api_key,
                    "section": "world",
                    "from-date": since.isoformat(),
                    "order-by": "newest",
                    "show-fields": show_fields,
                    "page-size": 50,
                    "page": page,
                },
                follow_redirects=False,
            )
            results, pages = _parse_response(download.body)
            for item in results:
                candidate = self._article(item)
                if candidate:
                    candidates.append(candidate)
            if page >= pages:
                return candidates
        raise FetchError("guardian_pagination_limit")

    def _article(self, item: dict) -> Candidate | None:
        if item.get("type", "article") not in {"article", "liveblog"}:
            return None
        url = item.get("webUrl")
        title = plain_html(item.get("webTitle"))
        if not isinstance(url, str) or not self.allows_article_url(url) or not title:
            return None
        fields = item.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        return Candidate(
            url=url,
            title=title,
            published_at=_parse_date(item.get("webPublicationDate")),
            summary=plain_html(fields.get("trailText")),
            language=normalize_language(fields.get("lang")) or self.default_language,
            body=(
                plain_html(fields.get("body"), preserve_paragraphs=True)
                if self.settings.fetch_full_text
                else None
            ),
            allow_article_page=False,
        )


def _parse_response(body: bytes) -> tuple[list[dict], int]:
    try:
        payload = json.loads(body)["response"]
        if payload["status"] == "ok":
            results = payload["results"]
            pages = payload["pages"]
            if (
                isinstance(results, list)
                and all(isinstance(item, dict) for item in results)
                and isinstance(pages, int)
                and pages >= 0
            ):
                return results, pages
    except (ValueError, UnicodeError, KeyError, TypeError):
        pass
    raise FetchError("invalid_guardian_response")


def _parse_date(value: object) -> datetime | None:
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value)
            if date.tzinfo is not None:
                return date.astimezone(UTC)
        except ValueError:
            pass
    return None
