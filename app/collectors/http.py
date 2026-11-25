"""Bounded HTTP fetching with explicit redirect and retry handling."""

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin

import httpx

from app.config.settings import CollectionSettings

USER_AGENT = "IranNewsIntelligence/0.2"
RETRYABLE = {408, 429, 500, 502, 503, 504}
REDIRECTS = {301, 302, 303, 307, 308}
MAX_BODY_BYTES = 8_000_000
MAX_RETRY_WAIT = 30


class FetchError(Exception):
    """Describe a fetch failure without including URLs, credentials, or article content."""

    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(f"{code} ({status})" if status is not None else code)


@dataclass(frozen=True)
class Download:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes = b""


class HttpFetcher:
    def __init__(
        self,
        client: httpx.Client,
        settings: CollectionSettings,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._settings = settings
        self._sleep = sleep
        self._last_request: dict[str, float] = {}
        self._host_intervals: dict[str, float] = {}
        self._blocked_until: dict[str, float] = {}

    def set_host_interval(self, url: str, seconds: float) -> None:
        host = httpx.URL(url).host
        self._host_intervals[host] = max(seconds, self._host_intervals.get(host, 0))

    def get(
        self,
        url: str,
        *,
        allow: Callable[[str], bool],
        params: dict[str, str | int] | None = None,
        follow_redirects: bool = True,
        accept_missing: bool = False,
    ) -> Download:
        for _ in range(6):
            if not allow(url):
                raise FetchError("url_not_allowed")
            try:
                result = self._get_with_retry(url, params=params)
            except httpx.InvalidURL:
                raise FetchError("invalid_request_url") from None
            if result.status in REDIRECTS and follow_redirects:
                location = result.headers.get("location")
                if not location:
                    raise FetchError("redirect_missing_location")
                try:
                    url = urljoin(url, location)
                except ValueError:
                    raise FetchError("invalid_redirect_url") from None
                params = None
                continue
            if 200 <= result.status < 300 or (accept_missing and result.status in {404, 410}):
                return result
            raise FetchError("http_status", result.status)
        raise FetchError("too_many_redirects")

    def _get_with_retry(self, url: str, *, params: dict[str, str | int] | None) -> Download:
        host = httpx.URL(url).host
        for attempt in range(self._settings.attempts):
            now = time.monotonic()
            if self._blocked_until.get(host, 0) > now:
                raise FetchError("host_rate_limited", 429)
            interval = max(self._settings.min_interval_seconds, self._host_intervals.get(host, 0))
            if host in self._last_request:
                remaining = self._last_request[host] + interval - now
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request[host] = time.monotonic()

            last_attempt = attempt + 1 == self._settings.attempts
            try:
                result = self._request(url, params)
            except httpx.RequestError as exc:
                if last_attempt:
                    kind = "timeout" if isinstance(exc, httpx.TimeoutException) else "network"
                    raise FetchError(kind) from None
                self._sleep(2**attempt)
                continue

            if result.status not in RETRYABLE:
                return result
            delay = _retry_after(result.headers.get("retry-after"))
            if delay is not None and delay > MAX_RETRY_WAIT:
                self._blocked_until[host] = time.monotonic() + delay
                return result
            if last_attempt:
                if result.status == 429:
                    self._blocked_until[host] = time.monotonic() + (delay or MAX_RETRY_WAIT)
                return result
            self._sleep(max(2**attempt, delay or 0))
        raise AssertionError("HTTP attempt count must be positive")

    def _request(self, url: str, params: dict[str, str | int] | None) -> Download:
        with self._client.stream(
            "GET",
            url,
            params=params,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            timeout=self._settings.timeout_seconds,
        ) as response:
            content = bytearray()
            if 200 <= response.status_code < 300:
                # Enforce the limit after decoding HTTP compression.
                for chunk in response.iter_bytes(chunk_size=65_536):
                    content.extend(chunk)
                    if len(content) > MAX_BODY_BYTES:
                        raise FetchError("response_too_large")
            return Download(
                str(response.url), response.status_code, response.headers, bytes(content)
            )


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        if value.isascii() and value.isdecimal():
            return float(int(value))
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return max(0, (when - datetime.now(UTC)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None
