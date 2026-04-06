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

from src.auth import authenticate
from src.scraper import scrape_directory, fetch_full_profiles
from src.exporter import export_to_csv
from src.utils import setup_logging, load_progress, save_progress
from config.settings import Settings


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape the TigerNet alumni directory"
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
        default=50,
        help="Number of results per page (default: 50, max may vary)",
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
        help="Run browser in headless mode (requires automated Duo approval)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    settings = Settings(
        per_page=args.per_page,
        max_pages=args.max_pages,
        output_path=args.output,
        headless=args.headless,
    )

    setup_logging()
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