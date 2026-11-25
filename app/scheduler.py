"""Run the pipeline at the configured local times."""

import logging
import signal
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from time import sleep
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from app.config.settings import ScheduleSettings
from app.publication_window import PublicationWindow

logger = logging.getLogger(__name__)


def _slot_trigger(slot: time, zone: ZoneInfo) -> CronTrigger:
    return CronTrigger(hour=slot.hour, minute=slot.minute, second=0, timezone=zone)


def schedule_trigger(settings: ScheduleSettings) -> OrTrigger:
    zone = ZoneInfo(settings.timezone)
    return OrTrigger([_slot_trigger(slot, zone) for slot in settings.times])


def _last_slot(trigger: CronTrigger, through: datetime) -> datetime:
    # A daily trigger can have zero or two occurrences on a daylight-saving transition day.
    through = through.astimezone(UTC)
    horizon = through - timedelta(days=3)
    previous = trigger.get_next_fire_time(None, horizon)
    while True:
        following = trigger.get_next_fire_time(previous, previous)
        if following.astimezone(UTC) > through:
            return previous
        previous = following


def publication_window(settings: ScheduleSettings, scheduled_at: datetime) -> PublicationWindow:
    zone = ZoneInfo(settings.timezone)
    before = scheduled_at.astimezone(UTC) - timedelta(microseconds=1)
    previous = max(
        (_last_slot(_slot_trigger(slot, zone), before) for slot in settings.times),
        key=lambda value: value.astimezone(UTC),
    )
    return PublicationWindow(previous.astimezone(zone), scheduled_at.astimezone(zone))


def upcoming_runs(
    settings: ScheduleSettings, *, now: datetime | None = None, count: int = 5
) -> list[str]:
    zone = ZoneInfo(settings.timezone)
    trigger = schedule_trigger(settings)
    now = now.astimezone(zone) if now is not None else datetime.now(zone)
    runs = []
    previous = None
    for _ in range(count):
        following = trigger.get_next_fire_time(previous, previous or now)
        runs.append(following.isoformat())
        previous = following
    return runs


def serve(settings: ScheduleSettings, job: Callable[[PublicationWindow], int]) -> None:
    zone = ZoneInfo(settings.timezone)
    scheduler = BackgroundScheduler(timezone=zone)
    stopping = False

    def run(trigger: CronTrigger) -> None:
        # This job represents one local time. Its grace period is shorter than the gap
        # between occurrences of that time, so a delayed start still uses the correct slot.
        scheduled_at = _last_slot(trigger, datetime.now(UTC))
        result = job(publication_window(settings, scheduled_at))
        if result:
            logger.warning("Scheduled run did not complete", extra={"fields": {"code": result}})

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    for slot in settings.times:
        trigger = _slot_trigger(slot, zone)
        scheduler.add_job(
            run,
            trigger,
            args=[trigger],
            id=f"news-pipeline-{slot:%H%M}",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=15 * 60,
        )
    previous_term = signal.signal(signal.SIGTERM, stop)
    previous_int = signal.signal(signal.SIGINT, stop)
    try:
        scheduler.start()
        while not stopping:
            sleep(1)
    finally:
        try:
            if scheduler.running:
                scheduler.shutdown(wait=True)
        finally:
            signal.signal(signal.SIGINT, previous_int)
            signal.signal(signal.SIGTERM, previous_term)
