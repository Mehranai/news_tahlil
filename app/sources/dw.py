"""Deutsche Welle English RSS 1.0/RDF adapter."""

from app.sources.rss import RssSource


class DWSource(RssSource):
    publisher_hosts = ("dw.com", "dw.de")
    default_language = "en"
