"""BBC News World adapter."""

from app.sources.rss import RssSource


class BBCSource(RssSource):
    publisher_hosts = ("bbc.com", "bbc.co.uk")
    default_language = "en"
