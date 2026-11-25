"""Run the pipeline at the configured local times."""

import logging
import signal
from collections.abc import Callable
from datetime import datetime
from time import sleep
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from app.config.settings import ScheduleSettings

logger = logging.getLogger(__name__)


def schedule_trigger(settings: ScheduleSettings) -> OrTrigger:
    zone = ZoneInfo(settings.timezone)
    return OrTrigger(
        [
            CronTrigger(hour=slot.hour, minute=slot.minute, second=0, timezone=zone)
            for slot in settings.times
        ]
    )


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


def serve(settings: ScheduleSettings, job: Callable[[], int]) -> None:
    scheduler = BackgroundScheduler(timezone=ZoneInfo(settings.timezone))
    stopping = False

    def run() -> None:
        result = job()
        if result:
            logger.warning("Scheduled run did not complete", extra={"fields": {"code": result}})

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    scheduler.add_job(
        run,
        schedule_trigger(settings),
        id="news-pipeline",
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
