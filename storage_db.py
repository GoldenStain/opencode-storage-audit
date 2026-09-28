"""Open OpenCode's SQLite database read-only and run small introspection queries."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def scalar(connection: sqlite3.Connection, sql: str, parameters=()) -> int:
    row = connection.execute(sql, parameters).fetchone()
    return row[0] if row and row[0] is not None else 0


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def page_metrics(connection: sqlite3.Connection) -> dict:
    """Return SQLite page geometry and freelist bytes for the database."""
    page_size = scalar(connection, "PRAGMA page_size")
    page_count = scalar(connection, "PRAGMA page_count")
    freelist_pages = scalar(connection, "PRAGMA freelist_count")
    return {
        "page_size": page_size,
        "page_count": page_count,
        "physical_bytes": page_size * page_count,
        "freelist_bytes": page_size * freelist_pages,
    }