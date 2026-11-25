"""Serialize pipeline runs targeting the same database."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Lock
from typing import Literal

from sqlalchemy import Engine, text

_MEMORY_LOCKS = {"pipeline": Lock(), "scheduler": Lock()}
_POSTGRES_KEY = 0x4952414E4E455753


@contextmanager
def pipeline_lock(
    engine: Engine,
    *,
    scope: Literal["pipeline", "scheduler"] = "pipeline",
) -> Iterator[bool]:
    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            filename = next(
                path
                for _, name, path in connection.exec_driver_sql("PRAGMA database_list")
                if name == "main"
            )
        if not filename:
            memory = _MEMORY_LOCKS[scope]
            acquired = memory.acquire(blocking=False)
            try:
                yield acquired
            finally:
                if acquired:
                    memory.release()
            return

        database = Path(filename).resolve()
        sidecar = database.with_name(f"{database.name}.{scope}.lock")
        with closing(sqlite3.connect(sidecar, timeout=0, autocommit=True)) as lock:
            # Lock the sidecar so article transactions can still commit during the run.
            try:
                lock.execute("BEGIN IMMEDIATE").close()
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                yield False
            else:
                yield True
        return

    with engine.connect() as connection:
        key = {"key": _POSTGRES_KEY + int(scope == "scheduler")}
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), key)
        connection.commit()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                try:
                    connection.execute(text("SELECT pg_advisory_unlock(:key)"), key)
                except BaseException:
                    connection.invalidate()
                    raise
