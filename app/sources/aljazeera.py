"""Al Jazeera English adapter."""

from app.sources.rss import RssSource


class AlJazeeraSource(RssSource):
    publisher_hosts = ("aljazeera.com",)
    default_language = "en"
