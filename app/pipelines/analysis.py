"""Run an interchangeable classifier on eligible saved article revisions."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from app.intelligence.contracts import ClassificationError, Classifier, Relevance
from app.publication_window import PublicationWindow
from app.storage.analysis import AnalysisStore

logger = logging.getLogger(__name__)


@dataclass
class AnalysisReport:
    provider: str
    model: str
    prompt_version: str
    selected: int = 0
    saved: int = 0
    failed: int = 0
    skipped: int = 0
    halted: str | None = None
    relevance: dict[str, int] = field(default_factory=lambda: dict.fromkeys(Relevance, 0))


class AnalysisPipeline:
    def __init__(self, store: AnalysisStore, classifier: Classifier) -> None:
        self.store = store
        self.classifier = classifier

    def analyze(
        self,
        source_slugs: list[str],
        *,
        limit: int | None = None,
        retry_failed: bool = False,
        after_saved: Callable[[int], None] | None = None,
        window: PublicationWindow | None = None,
    ) -> AnalysisReport:
        identity = self.classifier.identity
        candidates = self.store.candidate_ids(
            identity.profile_key,
            source_slugs,
            retry_failed=retry_failed,
            limit=limit,
            window=window,
        )
        report = AnalysisReport(
            identity.provider, identity.model, identity.prompt_version, selected=len(candidates)
        )
        for revision_id in candidates:
            claim = self.store.claim(revision_id, identity.profile_key, retry_failed=retry_failed)
            if claim is None:
                report.skipped += 1
                continue
            try:
                result = self.classifier.classify(claim.article)
            except ClassificationError as exc:
                if self.store.fail(claim, exc.code):
                    report.failed += 1
                else:
                    report.skipped += 1
                logger.warning(
                    "Article classification failed",
                    extra={"fields": {"revision_id": revision_id, "reason": exc.code}},
                )
                if exc.halt_batch:
                    report.halted = exc.code
                    break
                continue
            if self.store.complete(claim, identity, result):
                report.saved += 1
                report.relevance[result.assessment.relevance] += 1
                if after_saved is not None:
                    after_saved(revision_id)
            else:
                report.skipped += 1
        return report
