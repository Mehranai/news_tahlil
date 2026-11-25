"""Normalize article links and text before storage."""

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from lxml import etree, html

TRACKING_KEYS = {
    "at_campaign",
    "at_medium",
    "cmpid",
    "dclid",
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def normalize_url(value: str) -> str | None:
    if not value or "\\" in value or any(char.isspace() or ord(char) < 32 for char in value):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
        ):
            return None
        host = parts.hostname.encode("idna").decode("ascii")
        port = parts.port
    except ValueError:
        return None

    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        netloc = f"{netloc}:{port}"
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS and not key.lower().startswith("utm_")
    ]
    return urlunsplit((parts.scheme, netloc, parts.path or "/", urlencode(query), ""))


def within_hosts(url: str, hosts: tuple[str, ...]) -> bool:
    normalized = normalize_url(url)
    if normalized is None:
        return False
    host = urlsplit(normalized).hostname
    return any(host == domain or host.endswith(f".{domain}") for domain in hosts)


def clean_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def plain_html(value: object, *, preserve_paragraphs: bool = False) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        root = html.fragment_fromstring(value, create_parent=True)
    except (ValueError, etree.ParserError, etree.XMLSyntaxError):
        return clean_text(value) or None
    for element in root.xpath(".//script | .//style | .//noscript"):
        element.drop_tree()
    if preserve_paragraphs:
        blocks = (clean_text(node.text_content()) for node in root.xpath(".//p | .//li | .//h2"))
        paragraphs = "\n\n".join(block for block in blocks if block)
        if paragraphs:
            return paragraphs
    return clean_text(root.text_content()) or None


def normalize_language(value: object) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*", value):
        return value.replace("_", "-").lower()[:35]
    return None
