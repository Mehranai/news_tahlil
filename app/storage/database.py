"""SQLAlchemy engine setup shared by the app and migrations."""

import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import ConnectionPoolEntry


def create_database_engine(database_url: str, *, create_directory: bool = False) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        return create_engine(url, hide_parameters=True)

    if create_directory and url.database and url.database != ":memory:":
        if url.query.get("uri") != "true":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        connect_args={"autocommit": False, "timeout": 10},
        hide_parameters=True,
    )
    event.listen(engine, "connect", _enable_sqlite_foreign_keys)
    return engine


def _enable_sqlite_foreign_keys(
    connection: sqlite3.Connection,
    _record: ConnectionPoolEntry,
) -> None:
    autocommit = connection.autocommit
    connection.autocommit = True
    try:
        connection.execute("PRAGMA foreign_keys = ON").close()
    finally:
        connection.autocommit = autocommit
