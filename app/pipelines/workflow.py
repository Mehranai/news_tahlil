"""Compose the collection, analysis, and notification stages."""

import logging
from dataclasses import asdict
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.collectors.http import HttpFetcher
from app.config.settings import ProjectSettings, RuntimeSettings, SourceSettings
from app.extractors.article import ArticleExtractor
from app.intelligence.classifier import build_classifier
from app.notifications.telegram import TelegramSender
from app.pipelines.analysis import AnalysisPipeline
from app.pipelines.collection import CollectionPipeline
from app.pipelines.notifications import NotificationPipeline, NotificationReport
from app.publication_window import PublicationWindow
from app.sources.registry import build_source
from app.storage.analysis import AnalysisStore
from app.storage.collection import CollectionStore
from app.storage.models import FetchStatus
from app.storage.notifications import NotificationStore

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    pass


class Workflow:
    def __init__(
        self,
        project: ProjectSettings,
        runtime: RuntimeSettings,
        client: httpx.Client,
        sessions: sessionmaker[Session],
    ) -> None:
        self.project = project
        self.runtime = runtime
        self.client = client
        self.sessions = sessions

    def analyzer(self) -> AnalysisPipeline:
        if self.project.classification is None:
            raise ConfigurationError("Add a [classification] section to the project TOML")
        key = self.runtime.llm_api_key
        classifier = build_classifier(
            self.project.classification, self.client, key.get_secret_value() if key else None
        )
        return AnalysisPipeline(AnalysisStore(self.sessions), classifier)

    def notifier(self) -> NotificationPipeline:
        token, chat = self.runtime.telegram_bot_token, self.runtime.telegram_chat_id
        if not token or not chat:
            raise ConfigurationError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        try:
            sender = TelegramSender(
                self.client,
                token.get_secret_value().strip(),
                chat.get_secret_value().strip(),
                timeout_seconds=self.project.notifications.timeout_seconds,
            )
        except ValueError:
            raise ConfigurationError("Check the Telegram bot token and chat ID formats") from None
        return NotificationPipeline(
            NotificationStore(self.sessions),
            sender,
            timezone=ZoneInfo(self.project.schedule.timezone),
        )

    def allowed_sources(self) -> list[str]:
        allowed = [s.slug for s in self.project.sources if s.enabled and s.allow_classification]
        if not allowed:
            raise ConfigurationError(
                "Set allow_classification = true for sources licensed for this use"
            )
        return allowed

    def collect(
        self,
        sources: list[SourceSettings],
        *,
        extract_only: bool = False,
        metadata_only: bool = False,
        limit: int | None = None,
    ) -> tuple[list[dict], int]:
        if metadata_only:
            sources = [source.model_copy(update={"fetch_full_text": False}) for source in sources]
        http = HttpFetcher(self.client, self.project.collection)
        adapters = [build_source(source, self.runtime, http) for source in sources]
        pipeline = CollectionPipeline(CollectionStore(self.sessions), ArticleExtractor(http))
        reports = (
            pipeline.extract(adapters, limit=limit) if extract_only else pipeline.collect(adapters)
        )
        failed = any(
            report.status == FetchStatus.FAILED
            or (extract_only and (report.failed_extraction or report.partial))
            for report in reports
        )
        return [asdict(report) for report in reports], int(failed)

    def run(
        self,
        sources: list[SourceSettings],
        *,
        command: str,
        limit: int | None,
        metadata_only: bool = False,
        retry_failed: bool = False,
        retry_unknown: int | None = None,
        check_only: bool = False,
        window: PublicationWindow | None = None,
    ) -> tuple[dict | list, int]:
        if command in {"collect", "extract"}:
            return self.collect(
                sources, extract_only=command == "extract", metadata_only=metadata_only, limit=limit
            )

        allowed = self.allowed_sources()
        analyzer = self.analyzer() if command != "notify" else None
        notifier = self.notifier() if command != "analyze" else None
        if check_only:
            return {}, 0

        reports: dict = {"window": window.as_dict()} if window is not None else {}
        code = 0
        if command in {"run", "retry"}:
            stage = "extract" if command == "retry" else "collect"
            reports[stage], code = self.collect(
                sources, extract_only=stage == "extract", limit=limit
            )
        retry_failed = retry_failed or command == "retry"
        notified = NotificationReport()
        selected_revisions: set[int] = set()

        def send(revision_id: int | None = None) -> None:
            if notifier is None or notified.halted:
                return
            remaining = None if limit is None else limit - notified.selected
            if remaining == 0:
                return
            try:
                result = notifier.notify(
                    allowed,
                    limit=1 if revision_id is not None else remaining,
                    send_uncertain=self.project.notifications.send_uncertain,
                    retry_failed=retry_failed,
                    retry_unknown=retry_unknown,
                    revision_id=revision_id,
                    exclude_revisions=selected_revisions if revision_id is None else (),
                    window=window,
                )
            except SQLAlchemyError as exc:
                notified.halted = "notification_store_error"
                logger.error(
                    "Notification storage operation failed",
                    extra={"fields": {"error_type": type(exc).__name__}},
                )
                return
            notified.include(result)
            if revision_id is not None and result.selected:
                selected_revisions.add(revision_id)

        if analyzer is not None:
            analyzed = analyzer.analyze(
                allowed,
                limit=limit,
                retry_failed=retry_failed,
                after_saved=send if notifier is not None else None,
                window=window,
            )
            reports["analyze"] = asdict(analyzed)
            code = max(code, int(bool(analyzed.failed or analyzed.halted)))
        if notifier is not None:
            send()
            reports["notify"] = asdict(notified)
            code = max(code, int(bool(notified.failed or notified.halted)))
            if retry_unknown is not None and not (
                notified.selected or notified.deferred_until or notified.halted
            ):
                code = 2

        return reports.get(command, reports), code
