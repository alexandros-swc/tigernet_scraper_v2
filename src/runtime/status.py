"""Status helpers for scrape runs."""

from __future__ import annotations

from src.storage.db import connection, ensure_schema
from src.storage.repositories import ScrapeRepository


def get_status(
    school_slug: str,
    database_url: str | None = None,
    run_id: int | None = None,
) -> dict:
    with connection(database_url) as conn:
        ensure_schema(conn)
        repo = ScrapeRepository(conn)
        run = repo.get_run(run_id) if run_id else repo.latest_run_for_school(school_slug)
        if not run:
            raise RuntimeError("No scrape run found.")
        return repo.summary(run["id"])

