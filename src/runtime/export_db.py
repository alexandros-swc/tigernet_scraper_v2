"""Export normalized database results to CSV."""

from __future__ import annotations

import csv
import logging
import os

from src.exporter import PRIORITY_COLUMNS
from src.storage.db import connection, ensure_schema
from src.storage.repositories import ScrapeRepository

logger = logging.getLogger(__name__)


def export_results_to_csv(
    output_path: str,
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

        rows = [row["normalized_json"] for row in repo.completed_results(run["id"])]

    if not rows:
        raise RuntimeError("No completed profile results to export.")

    all_keys = set()
    for row in rows:
        all_keys.update(row.keys())
    ordered = [col for col in PRIORITY_COLUMNS if col in all_keys]
    fieldnames = ordered + sorted(all_keys - set(ordered))

    directory = os.path.dirname(output_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, restval="", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Exported %s rows with %s columns", len(rows), len(fieldnames))
    return {
        "run_id": run["id"],
        "output_path": output_path,
        "rows": len(rows),
        "columns": len(fieldnames),
    }

