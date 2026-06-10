#!/usr/bin/env python3
"""
TigerNet Alumni Directory Scraper
==================================
Scrapes the Princeton TigerNet alumni directory (tigernet.princeton.edu)
and exports the data as a clean CSV.

Usage:
    python main.py                    # Scrape listing data for all alumni
    python main.py --full-profiles    # Also fetch full profile details
    python main.py --resume           # Resume a previously interrupted scrape
    python main.py --max-pages 10     # Limit to first 10 pages (for testing)
"""

import argparse
import logging
import sys
import os
import pprint

from src.auth import authenticate
from src.scraper import scrape_directory, fetch_full_profiles
from src.exporter import export_to_csv
from src.utils import setup_logging, load_progress
from config.settings import Settings


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape the TigerNet alumni directory"
    )

    subparsers = parser.add_subparsers(dest="command")

    init_db = subparsers.add_parser(
        "init-db",
        help="Initialize the durable PostgreSQL schema",
    )
    init_db.add_argument("--database-url", default=None)

    seed = subparsers.add_parser(
        "seed",
        help="Seed alumni IDs and durable profile jobs from listing pages",
    )
    seed.add_argument("--school", default="princeton")
    seed.add_argument("--database-url", default=None)
    seed.add_argument("--run-id", type=int, default=None)
    seed.add_argument("--per-page", type=int, default=100)
    seed.add_argument("--max-pages", type=int, default=None)
    seed.add_argument("--headless", action="store_true")
    seed.add_argument("--raw-root", default="output/raw")

    work = subparsers.add_parser(
        "work",
        help="Process queued profile jobs from PostgreSQL",
    )
    work.add_argument("--school", default="princeton")
    work.add_argument("--database-url", default=None)
    work.add_argument("--run-id", type=int, default=None)
    work.add_argument("--worker-id", default=None)
    work.add_argument("--batch-size", type=int, default=5)
    work.add_argument("--max-jobs", type=int, default=None)
    work.add_argument("--lease-seconds", type=int, default=900)
    work.add_argument("--headless", action="store_true")
    work.add_argument("--raw-root", default="output/raw")

    status = subparsers.add_parser(
        "status",
        help="Print durable scrape run status",
    )
    status.add_argument("--school", default="princeton")
    status.add_argument("--database-url", default=None)
    status.add_argument("--run-id", type=int, default=None)

    export_db = subparsers.add_parser(
        "export-db",
        help="Export normalized database results to CSV",
    )
    export_db.add_argument("--school", default="princeton")
    export_db.add_argument("--database-url", default=None)
    export_db.add_argument("--run-id", type=int, default=None)
    export_db.add_argument("--output", default="output/tigernet_alumni_db.csv")

    auth_check = subparsers.add_parser(
        "auth-check",
        help="Inspect cached TigerNet auth and optionally test an API call",
    )
    auth_check.add_argument("--headless", action="store_true")
    auth_check.add_argument(
        "--login-if-needed",
        action="store_true",
        help="Perform a fresh login if no valid token cache exists",
    )
    auth_check.add_argument(
        "--skip-api-check",
        action="store_true",
        help="Only inspect the token cache; do not open a browser/API session",
    )

    parser.add_argument(
        "--full-profiles",
        action="store_true",
        help="Fetch full profile details for each user (slow, ~1.5s per user)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from last saved progress",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Maximum number of listing pages to scrape (for testing)",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=None,
        help="Number of results per page (default: 100, max may vary)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="output/tigernet_alumni.csv",
        help="Output CSV file path",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser headless (requires valid cached or remembered-device auth)",
    )
    return parser.parse_args()


def run_production_command(args) -> None:
    """Dispatch database-backed production runtime commands."""
    if args.command == "init-db":
        from src.runtime.init_db import initialize_database

        result = initialize_database(database_url=args.database_url)
        pprint.pp(result)
        return

    if args.command == "seed":
        from src.runtime.seeder import seed_school

        result = seed_school(
            school_slug=args.school,
            database_url=args.database_url,
            run_id=args.run_id,
            per_page=args.per_page,
            max_pages=args.max_pages,
            headless=args.headless,
            raw_root=args.raw_root,
        )
        pprint.pp(result)
        return

    if args.command == "work":
        from src.runtime.worker import work_school

        result = work_school(
            school_slug=args.school,
            database_url=args.database_url,
            run_id=args.run_id,
            worker_id=args.worker_id,
            batch_size=args.batch_size,
            max_jobs=args.max_jobs,
            lease_seconds=args.lease_seconds,
            headless=args.headless,
            raw_root=args.raw_root,
        )
        pprint.pp(result)
        return

    if args.command == "status":
        from src.runtime.status import get_status

        result = get_status(
            school_slug=args.school,
            database_url=args.database_url,
            run_id=args.run_id,
        )
        pprint.pp(result)
        return

    if args.command == "export-db":
        from src.runtime.export_db import export_results_to_csv

        result = export_results_to_csv(
            output_path=args.output,
            school_slug=args.school,
            database_url=args.database_url,
            run_id=args.run_id,
        )
        pprint.pp(result)
        return

    if args.command == "auth-check":
        from src.runtime.auth_check import check_auth

        result = check_auth(
            headless=args.headless,
            login_if_needed=args.login_if_needed,
            api_check=not args.skip_api_check,
        )
        pprint.pp(result)
        return

    raise ValueError(f"Unknown command: {args.command}")


def main():
    args = parse_args()
    setup_logging()

    if args.command:
        run_production_command(args)
        return

    settings = Settings(
        max_pages=args.max_pages,
        output_path=args.output,
        headless=args.headless,
    )
    # Only override per_page if explicitly passed on command line
    if args.per_page is not None:
        settings.per_page = args.per_page

    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info("TigerNet Alumni Directory Scraper")
    logger.info("=" * 60)

    # Ensure output directory exists
    os.makedirs(os.path.dirname(settings.output_path), exist_ok=True)

    # Step 1: Authenticate
    logger.info("Step 1: Authenticating with TigerNet via CAS + Duo...")
    tokens = authenticate(headless=settings.headless)
    if not tokens:
        logger.error("Authentication failed. Exiting.")
        sys.exit(1)
    logger.info("Authentication successful!")

    # Step 2: Scrape directory listings
    logger.info("Step 2: Scraping directory listings...")
    progress = load_progress() if args.resume else {}
    users = scrape_directory(
        tokens=tokens,
        settings=settings,
        progress=progress,
    )
    logger.info(f"Scraped {len(users)} users from directory listing.")

    # Step 3: Optionally fetch full profiles
    if args.full_profiles:
        logger.info("Step 3: Fetching full profile details...")
        users = fetch_full_profiles(
            tokens=tokens,
            users=users,
            settings=settings,
            progress=progress,
        )
        logger.info(f"Fetched full profiles for {len(users)} users.")

    # Step 4: Export to CSV
    logger.info(f"Step 4: Exporting to {settings.output_path}...")
    export_to_csv(users, settings.output_path, full_profiles=args.full_profiles)
    logger.info(f"Done! Exported {len(users)} alumni records to {settings.output_path}")


if __name__ == "__main__":
    main()
