"""Collect articles independently from each publisher and record run outcomes."""

import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from app.collectors.http import FetchError
from app.extractors.article import ArticleExtractor, Material
from app.preprocessing import normalize_url
from app.sources.base import Candidate, Source
from app.storage.collection import CollectionStore
from app.storage.models import ExtractionStatus, FetchStatus

logger = logging.getLogger(__name__)


@dataclass
class SourceReport:
    source: str
    status: FetchStatus = FetchStatus.COMPLETE
    discovered: int = 0
    new_articles: int = 0
    new_revisions: int = 0
    unchanged: int = 0
    complete: int = 0
    partial: int = 0
    failed_extraction: int = 0
    error: str | None = None


class CollectionPipeline:
    def __init__(self, store: CollectionStore, extractor: ArticleExtractor) -> None:
        self.store = store
        self.extractor = extractor

    def collect(self, sources: Iterable[Source]) -> list[SourceReport]:
        return [self._collect_source(source) for source in sources if source.settings.enabled]

    def extract(self, sources: Iterable[Source], *, limit: int | None = None) -> list[SourceReport]:
        eligible = {
            source.settings.slug: source
            for source in sources
            if source.settings.enabled and source.settings.fetch_full_text
        }
        if not eligible:
            return []
        grouped: dict[str, list[Candidate]] = defaultdict(list)
        for slug, candidate in self.store.incomplete(list(eligible), limit):
            grouped[slug].append(candidate)
        return [
            self._collect_source(eligible[slug], candidates) for slug, candidates in grouped.items()
        ]

    def _collect_source(
        self, source: Source, saved_candidates: list[Candidate] | None = None
    ) -> SourceReport:
        report = SourceReport(source.settings.slug)
        run = self.store.start(source.settings)
        try:
            candidates = source.fetch() if saved_candidates is None else saved_candidates
            report.discovered = len(candidates)
            resolved: list[tuple[Material, datetime]] = []
            visited: set[str] = set()
            for candidate in candidates:
                normalized = normalize_url(candidate.url)
                if normalized is None or normalized in visited:
                    continue
                visited.add(normalized)
                if saved_candidates is not None:
                    try:
                        candidate = source.retrieve(candidate)
                    except FetchError as exc:
                        report.failed_extraction += 1
                        logger.warning(
                            "Article retrieval failed",
                            extra={"fields": {"source": report.source, "reason": str(exc)}},
                        )
                        continue
                material = self.extractor.resolve(source, candidate)
                if saved_candidates is None or material.status == ExtractionStatus.COMPLETE:
                    resolved.append((material, datetime.now(UTC)))
                match material.status:
                    case ExtractionStatus.COMPLETE:
                        report.complete += 1
                    case ExtractionStatus.PARTIAL:
                        report.partial += 1
                    case ExtractionStatus.FAILED:
                        report.failed_extraction += 1

            saved = self.store.save(run, report.discovered, resolved)
            report.new_articles = saved.new_articles
            report.new_revisions = saved.new_revisions
            report.unchanged = saved.unchanged
        except (FetchError, SQLAlchemyError) as exc:
            report.status = FetchStatus.FAILED
            report.error = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
            self.store.fail(run, report.error, discovered=report.discovered)
            logger.warning(
                "Collection failed for one source",
                extra={"fields": {"source": report.source, "reason": report.error}},
            )
        return report
