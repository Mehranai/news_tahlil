"""Construct source adapters from project settings."""

from app.collectors.http import HttpFetcher
from app.config.settings import RuntimeSettings, SourceSettings
from app.sources.aljazeera import AlJazeeraSource
from app.sources.base import Source
from app.sources.bbc import BBCSource
from app.sources.dw import DWSource
from app.sources.guardian import GuardianSource
from app.sources.rss import RssSource

RSS_ADAPTERS: dict[str, type[RssSource]] = {
    "bbc": BBCSource,
    "aljazeera": AlJazeeraSource,
    "dw": DWSource,
}


def build_source(settings: SourceSettings, runtime: RuntimeSettings, http: HttpFetcher) -> Source:
    if settings.slug == "guardian":
        key = runtime.guardian_api_key
        return GuardianSource(settings, http, key.get_secret_value() if key else None)
    return RSS_ADAPTERS.get(settings.slug, RssSource)(settings, http)
