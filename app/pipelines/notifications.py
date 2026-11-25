"""Dispatch eligible article alerts without automatically replaying uncertain sends."""

import logging
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Protocol
from zoneinfo import ZoneInfo

from app.notifications.message import render_message
from app.notifications.telegram import DeliveryError
from app.publication_window import PublicationWindow
from app.storage.notifications import NotificationStore

logger = logging.getLogger(__name__)


class Sender(Protocol):
    def send(self, text: str) -> str: ...


@dataclass
class NotificationReport:
    selected: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    halted: str | None = None
    deferred_until: str | None = None
    unknown_articles: list[int] = field(default_factory=list)

    def include(self, report: "NotificationReport") -> None:
        self.selected += report.selected
        self.sent += report.sent
        self.failed += report.failed
        self.skipped += report.skipped
        self.halted = report.halted or self.halted
        self.deferred_until = report.deferred_until
        self.unknown_articles = report.unknown_articles


class NotificationPipeline:
    def __init__(
        self,
        store: NotificationStore,
        sender: Sender,
        *,
        timezone: ZoneInfo,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.store = store
        self.sender = sender
        self.timezone = timezone
        self.sleep = sleep
        self._next_send = 0.0

    def notify(
        self,
        source_slugs: list[str],
        *,
        send_uncertain: bool = False,
        retry_failed: bool = False,
        retry_unknown: int | None = None,
        limit: int | None = None,
        revision_id: int | None = None,
        exclude_revisions: Collection[int] = (),
        window: PublicationWindow | None = None,
    ) -> NotificationReport:
        self.store.expire_claims()
        report = NotificationReport()
        if cooldown := self.store.cooldown_until():
            report.deferred_until = cooldown.isoformat()
            report.unknown_articles = self.store.unknown_articles()
            return report

        candidates = self.store.candidates(
            source_slugs,
            send_uncertain=send_uncertain,
            retry_failed=retry_failed,
            retry_unknown=retry_unknown,
            limit=limit,
            revision_id=revision_id,
            exclude_revisions=exclude_revisions,
            window=window,
        )
        report.selected = len(candidates)
        for alert in candidates:
            claim = self.store.claim(
                alert, retry_failed=retry_failed, retry_unknown=retry_unknown is not None
            )
            if claim is None:
                report.skipped += 1
                continue
            try:
                message = render_message(alert, self.timezone)
            except ValueError:
                self.store.failed(
                    claim, "invalid_notification_link", retry_after=None, delivery_unknown=False
                )
                report.failed += 1
                continue
            if (wait := self._next_send - time.monotonic()) > 0:
                self.sleep(wait)
            self._next_send = time.monotonic() + 1
            try:
                message_id = self.sender.send(message)
            except DeliveryError as exc:
                recorded = self.store.failed(
                    claim,
                    exc.code,
                    retry_after=exc.retry_after,
                    delivery_unknown=exc.delivery_unknown,
                )
                if recorded:
                    report.failed += 1
                else:
                    report.skipped += 1
                logger.warning(
                    "Telegram notification failed",
                    extra={"fields": {"article_id": alert.article_id, "reason": exc.code}},
                )
                if exc.halt_batch:
                    report.halted = exc.code
                    break
                continue
            if self.store.sent(claim, message_id):
                report.sent += 1
            else:
                report.skipped += 1
        report.unknown_articles = self.store.unknown_articles()
        if cooldown := self.store.cooldown_until():
            report.deferred_until = cooldown.isoformat()
        return report
