"""Command-line entry points for collecting, classifying, and announcing news."""

import argparse
import json
import logging
import sqlite3
from pathlib import Path
from typing import TypedDict
from zoneinfo import ZoneInfo

import httpx
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy import func, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import (
    ProjectSettings,
    RuntimeSettings,
    load_project_settings,
)
from app.evaluation import evaluate_fixtures
from app.intelligence.classifier import build_classifier
from app.intelligence.contracts import ClassificationError, Relevance
from app.local_model import DEFAULT_MODEL, LocalModelError, serve_model
from app.logging_config import configure_logging
from app.pipelines.workflow import ConfigurationError, Workflow
from app.scheduler import serve, upcoming_runs
from app.storage.database import create_database_engine
from app.storage.evaluation import EvaluationStore
from app.storage.models import Base
from app.storage.pipeline_lock import pipeline_lock
from app.storage.runs import RunHistory

logger = logging.getLogger(__name__)


class DatabaseStatus(TypedDict):
    backend: str
    revisions: list[str]
    counts: dict[str, int]


def database_status(database_url: str) -> DatabaseStatus:
    """Inspect the schema and counts without creating an absent SQLite database."""
    url = make_url(database_url)
    backend = url.get_backend_name()
    if (
        backend == "sqlite"
        and url.database
        and url.database != ":memory:"
        and url.query.get("uri") != "true"
        and not Path(url.database).is_file()
    ):
        return {"backend": backend, "revisions": [], "counts": {}}

    engine = create_database_engine(database_url)
    try:
        with engine.connect() as connection:
            revisions = MigrationContext.configure(connection).get_current_heads()
            present = set(inspect(connection).get_table_names())
            counts = {
                table.name: connection.execute(select(func.count()).select_from(table)).scalar_one()
                for table in Base.metadata.sorted_tables
                if table.name in present
            }
    finally:
        engine.dispose()
    return {"backend": backend, "revisions": list(revisions), "counts": counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="news-intel")
    parser.add_argument("--config", type=Path, help="path to the project TOML configuration")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("status", help="show the schedule, sources, and database status")
    runs = subcommands.add_parser("runs", help="check scheduler state and see recent pipeline runs")
    runs.add_argument("--limit", type=_positive, default=5)
    model_server = subcommands.add_parser("serve-model", help="start the local llama.cpp server")
    model_server.add_argument(
        "--model-file", type=Path, default=DEFAULT_MODEL, help="path to GGUF weights"
    )
    model_server.add_argument("--server", type=Path, help="path to the llama-server executable")
    model_server.add_argument(
        "--gpu-layers", type=_nonnegative, help="layers to offload, or 0 for CPU"
    )
    model_server.add_argument("--device", help="llama.cpp device name, such as CUDA0")
    collect = subcommands.add_parser("collect", help="fetch and store articles for enabled sources")
    collect.add_argument("--metadata-only", action="store_true", help="skip article page and body")
    extract = subcommands.add_parser("extract", help="extract saved articles missing full text")
    for stage in (collect, extract):
        stage.add_argument(
            "--source", action="append", help="source slug, repeat to select several"
        )
    analyze = subcommands.add_parser("analyze", help="classify eligible saved article revisions")
    analyze.add_argument("--retry-failed", action="store_true", help="include failed jobs")
    notify = subcommands.add_parser("notify", help="send eligible article alerts to Telegram")
    recovery = notify.add_mutually_exclusive_group()
    recovery.add_argument(
        "--retry-failed", action="store_true", help="retry failed sends after fixing their cause"
    )
    recovery.add_argument(
        "--retry-unknown",
        type=_positive,
        metavar="ARTICLE_ID",
        help="retry one unconfirmed delivery",
    )
    run = subcommands.add_parser("run", help="run collection, analysis, and notifications once")
    retry = subcommands.add_parser("retry", help="resume incomplete extraction and failed jobs")
    schedule = subcommands.add_parser("schedule", help="run the pipeline at the configured times")
    schedule.add_argument("--preview", action="store_true", help="show the next five run times")
    for stage in (extract, analyze, notify, run, retry, schedule):
        stage.add_argument(
            "--limit", type=_positive, help="maximum number of items per queued stage"
        )
    review = subcommands.add_parser("review", help="inspect saved revisions and their labels")
    review_filter = review.add_mutually_exclusive_group()
    review_filter.add_argument(
        "--revision", type=_positive, help="include the text of one revision"
    )
    review_filter.add_argument("--needs-review", action="store_true", help="show the review queue")
    review_filter.add_argument("--related", action="store_true", help="show related model results")
    review.add_argument("--limit", type=_positive, default=20)
    feedback = subcommands.add_parser("feedback", help="record or revise a human relevance label")
    feedback.add_argument("revision_id", type=_positive)
    feedback.add_argument("label", type=Relevance, choices=list(Relevance))
    feedback.add_argument("--note", help="optional explanation of the human label")
    evaluate = subcommands.add_parser("evaluate", help="measure a classifier against labeled data")
    dataset = evaluate.add_mutually_exclusive_group()
    dataset.add_argument("--fixtures", type=Path, help="run the configured model on synthetic JSON")
    dataset.add_argument(
        "--common-only", action="store_true", help="compare models on shared revisions"
    )
    args = parser.parse_args(argv)

    configure_logging("INFO")
    try:
        runtime = RuntimeSettings()
        configure_logging(runtime.log_level)
        project = load_project_settings(args.config)
    except (ValidationError, OSError, ValueError) as exc:
        logger.error("Invalid project configuration", extra={"fields": {"detail": str(exc)}})
        return 2

    if args.command == "status":
        return _status(runtime, project)
    if args.command == "serve-model":
        try:
            return serve_model(
                project.classification,
                model_file=args.model_file,
                server_path=args.server,
                gpu_layers=args.gpu_layers,
                device=args.device,
            )
        except LocalModelError as exc:
            logger.error(str(exc))
            return 2
        except OSError as exc:
            logger.error(
                "Could not launch llama-server",
                extra={"fields": {"error_type": type(exc).__name__}},
            )
            return 1
        except KeyboardInterrupt:
            return 130
    if args.command == "runs":
        return _runs(runtime, project, limit=args.limit)
    if args.command == "evaluate" and args.fixtures is not None:
        return _evaluate_synthetic(runtime, project, args.fixtures)
    if args.command in {"review", "feedback", "evaluate"}:
        if args.command == "feedback" and args.note and len(args.note.strip()) > 1500:
            feedback.error("--note must be at most 1500 characters")
        return _human_labels(runtime, args)
    if args.command == "schedule" and args.preview:
        _print_json(
            {"timezone": project.schedule.timezone, "next_runs": upcoming_runs(project.schedule)}
        )
        return 0
    if args.command in {"collect", "extract"}:
        selected = set(args.source or ())
        enabled = {source.slug for source in project.sources if source.enabled}
        if missing := selected - enabled:
            stage = collect if args.command == "collect" else extract
            stage.error(f"unknown or disabled source(s): {', '.join(sorted(missing))}")
    if args.command == "schedule":
        return _serve_schedule(runtime, project, args)
    return _run_stages(runtime, project, args)


def _positive(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def _nonnegative(raw: str) -> int:
    value = int(raw)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return value


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def _runs(runtime: RuntimeSettings, project: ProjectSettings, *, limit: int) -> int:
    engine = None
    try:
        engine = create_database_engine(runtime.database_url.get_secret_value())
        if not inspect(engine).has_table("pipeline_runs"):
            logger.error("Apply the database migrations with: uv run alembic upgrade head")
            return 1
        with pipeline_lock(engine, scope="scheduler") as available:
            scheduler_running = not available
        with pipeline_lock(engine) as available:
            pipeline_running = not available
        history = RunHistory(sessionmaker(engine, class_=Session))
        _print_json(
            {
                "scheduler_running": scheduler_running,
                "pipeline_running": pipeline_running,
                "timezone": project.schedule.timezone,
                "next_runs": upcoming_runs(project.schedule) if scheduler_running else [],
                "runs": history.recent(limit=limit, timezone=ZoneInfo(project.schedule.timezone)),
            }
        )
    except (SQLAlchemyError, sqlite3.Error, OSError, ImportError) as exc:
        logger.error(
            "Could not read run history", extra={"fields": {"error_type": type(exc).__name__}}
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


def _serve_schedule(
    runtime: RuntimeSettings, project: ProjectSettings, args: argparse.Namespace
) -> int:
    if code := _run_stages(runtime, project, args, check_only=True):
        return code
    engine = None
    try:
        engine = create_database_engine(runtime.database_url.get_secret_value())
        with pipeline_lock(engine, scope="scheduler") as acquired:
            if not acquired:
                logger.warning("Scheduler is already running for this database")
                return 3
            logger.info(
                "Scheduler started",
                extra={"fields": {"next_runs": upcoming_runs(project.schedule)}},
            )
            serve(project.schedule, lambda: _run_stages(runtime, project, args))
    except (SQLAlchemyError, sqlite3.Error, OSError, ImportError) as exc:
        logger.error(
            "Scheduler could not start", extra={"fields": {"error_type": type(exc).__name__}}
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    return 0


def _human_labels(runtime: RuntimeSettings, args: argparse.Namespace) -> int:
    engine = None
    try:
        engine = create_database_engine(runtime.database_url.get_secret_value())
        if not inspect(engine).has_table("analysis_jobs"):
            logger.error("Apply the database migrations with: uv run alembic upgrade head")
            return 1
        store = EvaluationStore(sessionmaker(engine, class_=Session))
        if args.command == "feedback":
            with pipeline_lock(engine) as acquired:
                if not acquired:
                    logger.warning("Another pipeline run holds the database lock")
                    return 3
                report = store.record(
                    args.revision_id, args.label, (args.note or "").strip() or None
                )
            if report is None:
                logger.error("Article revision not found")
                return 2
        elif args.command == "review":
            report = store.review(
                revision_id=args.revision,
                limit=args.limit,
                needs_review=args.needs_review,
                related_only=args.related,
            )
            if args.revision and not report:
                logger.error("Article revision not found")
                return 2
        else:
            report = store.evaluate(common_only=args.common_only)
    except (SQLAlchemyError, sqlite3.Error, OSError, ImportError) as exc:
        logger.error(
            "Database operation failed", extra={"fields": {"error_type": type(exc).__name__}}
        )
        return 1
    finally:
        if engine is not None:
            engine.dispose()
    _print_json(report)
    return 0


def _evaluate_synthetic(runtime: RuntimeSettings, project: ProjectSettings, path: Path) -> int:
    if project.classification is None:
        logger.error("Add a [classification] section to the project TOML")
        return 2
    try:
        with httpx.Client() as client:
            key = runtime.llm_api_key
            classifier = build_classifier(
                project.classification, client, key.get_secret_value() if key else None
            )
            report, code = evaluate_fixtures(path, classifier)
    except ClassificationError as exc:
        logger.error("Invalid classifier setup", extra={"fields": {"reason": exc.code}})
        return 2
    except (OSError, ValueError) as exc:
        logger.error(
            "Could not load evaluation inputs", extra={"fields": {"error_type": type(exc).__name__}}
        )
        return 2
    _print_json(report)
    return code


def _status(runtime: RuntimeSettings, project: ProjectSettings) -> int:
    try:
        database = database_status(runtime.database_url.get_secret_value())
    except (SQLAlchemyError, ImportError, OSError) as exc:
        logger.error("Database check failed", extra={"fields": {"error_type": type(exc).__name__}})
        return 1

    if not database["revisions"]:
        logger.info("Initialize the database with: uv run alembic upgrade head")

    print(
        json.dumps(
            {
                "schedule": {
                    "timezone": project.schedule.timezone,
                    "times": [
                        slot.isoformat(timespec="minutes") for slot in project.schedule.times
                    ],
                },
                "sources": [
                    {
                        "slug": source.slug,
                        "name": source.name,
                        "enabled": source.enabled,
                        "allow_classification": source.allow_classification,
                    }
                    for source in project.sources
                ],
                "database": database,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _run_stages(
    runtime: RuntimeSettings,
    project: ProjectSettings,
    args: argparse.Namespace,
    *,
    check_only: bool = False,
) -> int:
    command = "run" if args.command == "schedule" else args.command
    requested = getattr(args, "source", None)
    sources = [
        source
        for source in project.sources
        if source.enabled and (not requested or source.slug in requested)
    ]
    if command in {"collect", "extract"} and not sources:
        _print_json([])
        return 0

    engine = None
    history = None
    run_id = None
    report = None
    code = 1
    error = None
    try:
        engine = create_database_engine(runtime.database_url.get_secret_value())
        schema = inspect(engine)
        if not schema.has_table("notifications") or not any(
            column["name"] == "lease_until" for column in schema.get_columns("notifications")
        ):
            logger.error("Apply the database migrations with: uv run alembic upgrade head")
            return 1
        sessions = sessionmaker(engine, class_=Session)
        if command == "run":
            if not schema.has_table("pipeline_runs"):
                logger.error("Apply the database migrations with: uv run alembic upgrade head")
                return 1
            if not check_only:
                history = RunHistory(sessions)
                run_id = history.start("scheduled" if args.command == "schedule" else "manual")
        with httpx.Client() as client, pipeline_lock(engine) as acquired:
            if not acquired and not check_only:
                logger.warning("Another pipeline run holds the database lock")
                code, error = 3, "pipeline_busy"
            else:
                workflow = Workflow(project, runtime, client, sessions)
                report, code = workflow.run(
                    sources,
                    command=command,
                    limit=getattr(args, "limit", None),
                    metadata_only=getattr(args, "metadata_only", False),
                    retry_failed=getattr(args, "retry_failed", False),
                    retry_unknown=getattr(args, "retry_unknown", None),
                    check_only=check_only,
                )
    except ConfigurationError as exc:
        logger.error(str(exc))
        code, error = 2, str(exc)
    except ClassificationError as exc:
        logger.error("Invalid classifier setup", extra={"fields": {"reason": exc.code}})
        code, error = 2, exc.code
    except (SQLAlchemyError, sqlite3.Error, OSError, UnicodeError, ImportError) as exc:
        error = type(exc).__name__
        code = 1
        logger.error("Pipeline run aborted", extra={"fields": {"error_type": error}})
    finally:
        try:
            if history is not None and run_id is not None:
                history.finish(run_id, code, report if isinstance(report, dict) else None, error)
        except (SQLAlchemyError, OSError) as exc:
            logger.error(
                "Could not save run outcome", extra={"fields": {"error_type": type(exc).__name__}}
            )
            code = code or 1
        finally:
            if engine is not None:
                engine.dispose()

    if not check_only and report is not None:
        _print_json(report)
        if command == "notify" and code == 2:
            logger.error("No eligible unconfirmed delivery for that article ID")
    return code
