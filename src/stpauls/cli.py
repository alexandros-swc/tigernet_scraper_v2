"""Separate CLI for the St. Paul's scraper."""

from __future__ import annotations

import argparse
import pprint

from src.utils import setup_logging


def parse_args():
    parser = argparse.ArgumentParser(
        description="Scrape the St. Paul's School Alumni Network"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth-check", help="Inspect St. Paul's auth cache")
    auth.add_argument("--headless", action="store_true")
    auth.add_argument("--login-if-needed", action="store_true")
    auth.add_argument("--skip-api-check", action="store_true")

    seed = subparsers.add_parser("seed", help="Seed St. Paul's profile jobs")
    seed.add_argument("--database-url", default=None)
    seed.add_argument("--run-id", type=int, default=None)
    seed.add_argument("--per-page", type=int, default=None)
    seed.add_argument("--max-pages", type=int, default=None)
    seed.add_argument("--headless", action="store_true")
    seed.add_argument("--raw-root", default="output/raw")

    work = subparsers.add_parser("work", help="Process St. Paul's queued jobs")
    work.add_argument("--database-url", default=None)
    work.add_argument("--run-id", type=int, default=None)
    work.add_argument("--worker-id", default=None)
    work.add_argument("--batch-size", type=int, default=5)
    work.add_argument("--max-jobs", type=int, default=None)
    work.add_argument("--lease-seconds", type=int, default=900)
    work.add_argument("--headless", action="store_true")
    work.add_argument("--raw-root", default="output/raw")
    work.add_argument("--request-delay", type=float, default=0.5)

    status = subparsers.add_parser("status", help="Print St. Paul's run status")
    status.add_argument("--database-url", default=None)
    status.add_argument("--run-id", type=int, default=None)

    export = subparsers.add_parser(
        "export-db",
        help="Export St. Paul's normalized DB results to CSV",
    )
    export.add_argument("--database-url", default=None)
    export.add_argument("--run-id", type=int, default=None)
    export.add_argument("--output", default="output/stpauls/stpauls_alumni_db.csv")

    smoke = subparsers.add_parser("smoke", help="Fetch a few profiles without DB")
    smoke.add_argument("--count", type=int, default=3)
    smoke.add_argument("--headless", action="store_true")
    smoke.add_argument("--output", default="output/stpauls/smoke_profiles.csv")

    return parser.parse_args()


def main() -> None:
    args = parse_args()
    setup_logging()

    from src.stpauls import runtime

    if args.command == "auth-check":
        result = runtime.auth_check(
            headless=args.headless,
            login_if_needed=args.login_if_needed,
            api_check=not args.skip_api_check,
        )
    elif args.command == "seed":
        result = runtime.seed(
            database_url=args.database_url,
            run_id=args.run_id,
            per_page=args.per_page,
            max_pages=args.max_pages,
            headless=args.headless,
            raw_root=args.raw_root,
        )
    elif args.command == "work":
        result = runtime.work(
            database_url=args.database_url,
            run_id=args.run_id,
            worker_id=args.worker_id,
            batch_size=args.batch_size,
            max_jobs=args.max_jobs,
            lease_seconds=args.lease_seconds,
            headless=args.headless,
            raw_root=args.raw_root,
            request_delay=args.request_delay,
        )
    elif args.command == "status":
        result = runtime.status(
            database_url=args.database_url,
            run_id=args.run_id,
        )
    elif args.command == "export-db":
        result = runtime.export_db(
            output_path=args.output,
            database_url=args.database_url,
            run_id=args.run_id,
        )
    elif args.command == "smoke":
        result = runtime.smoke(
            count=args.count,
            headless=args.headless,
            output_path=args.output,
        )
    else:
        raise ValueError(f"Unknown St. Paul's command: {args.command}")

    pprint.pp(result)


if __name__ == "__main__":
    main()
