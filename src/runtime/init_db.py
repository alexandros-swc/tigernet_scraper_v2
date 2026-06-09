"""Database initialization command."""

from __future__ import annotations

from src.schools import get_adapter
from src.storage.db import connection, ensure_schema
from src.storage.repositories import ScrapeRepository


def initialize_database(database_url: str | None = None) -> dict:
    """Create schema and register known school adapters."""
    with connection(database_url) as conn:
        ensure_schema(conn)
        repo = ScrapeRepository(conn)
        adapter = get_adapter("princeton")
        school = repo.ensure_school(
            slug=adapter.slug,
            base_url=adapter.base_url,
            platform=adapter.platform,
        )
        return {"schools": [school]}

