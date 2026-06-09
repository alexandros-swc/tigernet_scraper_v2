"""Database connection helpers."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from dotenv import load_dotenv

from src.storage.schema import SCHEMA_STATEMENTS


def get_database_url(explicit_url: str | None = None) -> str:
    """Resolve the configured PostgreSQL connection string."""
    load_dotenv()
    database_url = (
        explicit_url
        or os.getenv("TIGERNET_DATABASE_URL")
        or os.getenv("DATABASE_URL")
    )
    if not database_url:
        raise RuntimeError(
            "Missing database URL. Set TIGERNET_DATABASE_URL or DATABASE_URL."
        )
    return database_url


def connect(database_url: str | None = None):
    """Open a psycopg connection with dict rows and autocommit enabled."""
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "psycopg is required for database-backed runs. "
            "Install dependencies with: pip install -r requirements.txt"
        ) from exc

    conn = psycopg.connect(get_database_url(database_url), row_factory=dict_row)
    conn.autocommit = True
    return conn


@contextmanager
def connection(database_url: str | None = None) -> Iterator:
    """Context manager for short-lived database work."""
    conn = connect(database_url)
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema(conn) -> None:
    """Create or update the production schema."""
    with conn.cursor() as cur:
        for statement in SCHEMA_STATEMENTS:
            cur.execute(statement)

