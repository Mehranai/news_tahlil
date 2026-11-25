"""Source adapter contract consumed by the collection pipeline."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.config.settings import SourceSettings


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str
    published_at: datetime | None = None
    summary: str | None = None
    language: str | None = None
    body: str | None = None
    allow_article_page: bool = True


class Source(Protocol):
    settings: SourceSettings

    def fetch(self) -> list[Candidate]: ...

    def retrieve(self, candidate: Candidate) -> Candidate: ...

    def allows_article_url(self, url: str) -> bool: ...
