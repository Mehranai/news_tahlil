"""Record the start and outcome of full pipeline runs."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.storage.models import PipelineRun


class RunHistory:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self.sessions = sessions

    def start(self, trigger: str) -> int:
        with self.sessions.begin() as session:
            row = PipelineRun(trigger=trigger)
            session.add(row)
            session.flush()
            return row.id

    def finish(self, run_id: int, code: int, report: dict | None, error: str | None = None) -> None:
        with self.sessions.begin() as session:
            session.execute(
                update(PipelineRun)
                .where(PipelineRun.id == run_id)
                .values(
                    finished_at=datetime.now(UTC),
                    exit_code=code,
                    report=report,
                    error=error,
                )
            )

    def recent(self, *, limit: int, timezone: ZoneInfo) -> list[dict]:
        with self.sessions() as session:
            rows = session.scalars(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(limit))
            outcomes = {None: "unfinished", 0: "complete", 3: "skipped"}
            return [
                {
                    "id": row.id,
                    "trigger": row.trigger,
                    "started_at": row.started_at.astimezone(timezone).isoformat(),
                    "finished_at": row.finished_at.astimezone(timezone).isoformat()
                    if row.finished_at
                    else None,
                    "status": outcomes.get(row.exit_code, "failed"),
                    "exit_code": row.exit_code,
                    "error": row.error,
                    "report": row.report,
                }
                for row in rows
            ]
