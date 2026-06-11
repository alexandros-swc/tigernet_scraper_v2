"""Run a fresh TigerNet DB-backed E2E scrape with an auth refresh check.

This is intentionally an orchestration script around the production runtime:
it resets local scraper tables, seeds enough listing rows, processes a first
small chunk, makes the cached access token look expired, then restarts the
worker to prove the remembered browser profile can refresh auth and continue.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import time
from pathlib import Path
from pprint import pprint
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv

from src.auth import TOKEN_CACHE_PATH, inspect_token_cache
from src.runtime.export_db import export_results_to_csv
from src.runtime.seeder import seed_school
from src.runtime.status import get_status
from src.runtime.worker import work_school
from src.storage.db import connection, ensure_schema
from src.utils import setup_logging


TABLES = (
    "worker_heartbeats",
    "profile_results",
    "profile_jobs",
    "alumni_seed",
    "auth_sessions",
    "accounts",
    "scrape_runs",
    "schools",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fresh E2E scrape for N TigerNet profiles with auth-refresh coverage."
    )
    parser.add_argument("--profiles", type=int, default=100)
    parser.add_argument("--first-chunk", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--seed-pages", type=int, default=None)
    parser.add_argument("--school", default="princeton")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--raw-root", default="output/raw_e2e_100")
    parser.add_argument("--output", default="output/e2e_tigernet_100.csv")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Required: truncate local scraper tables before running.",
    )
    return parser.parse_args()


def reset_database(database_url: str | None) -> None:
    with connection(database_url) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {', '.join(TABLES)} RESTART IDENTITY CASCADE")


def _b64url_decode(data: str) -> dict:
    padded = data + ("=" * (-len(data) % 4))
    return json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))


def _b64url_encode(payload: dict) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def force_expire_token_cache() -> dict:
    path = Path(TOKEN_CACHE_PATH)
    if not path.exists():
        raise RuntimeError(f"Token cache does not exist: {path}")

    tokens = json.loads(path.read_text(encoding="utf-8"))
    access_token = tokens.get("cookies", {}).get("api_access_token")
    if not access_token:
        raise RuntimeError("Token cache does not contain api_access_token.")

    parts = access_token.split(".")
    if len(parts) != 3:
        raise RuntimeError("api_access_token did not look like a JWT.")

    payload = _b64url_decode(parts[1])
    original_exp = payload.get("exp")
    payload["exp"] = int(time.time()) - 600
    expired_token = ".".join((parts[0], _b64url_encode(payload), parts[2]))

    tokens["cookies"]["api_access_token"] = expired_token
    for cookie in tokens.get("cookie_jar") or []:
        if cookie.get("name") == "api_access_token":
            cookie["value"] = expired_token

    path.write_text(json.dumps(tokens), encoding="utf-8")
    return {
        "path": str(path),
        "original_exp": original_exp,
        "forced_exp": payload["exp"],
    }


def csv_row_count(path: str) -> int:
    with open(path, newline="", encoding="utf-8") as f:
        return sum(1 for _ in csv.DictReader(f))


def assert_completed(result: dict, expected: int, label: str) -> None:
    if result["completed"] != expected or result["errors"] or result["auth_required"]:
        raise RuntimeError(f"{label} failed: {result}")


def main() -> int:
    args = parse_args()
    setup_logging()
    load_dotenv()

    if not args.reset_db:
        raise SystemExit("Refusing to run without --reset-db.")

    print("Resetting local scraper tables...")
    reset_database(args.database_url)

    seed_pages = args.seed_pages or max(1, math.ceil(args.profiles / args.per_page) + 1)
    print(f"Seeding {seed_pages} listing pages...")
    seed_result = seed_school(
        school_slug=args.school,
        database_url=args.database_url,
        per_page=args.per_page,
        max_pages=seed_pages,
        headless=args.headless,
        raw_root=args.raw_root,
    )
    run_id = seed_result["run_id"]
    status_after_seed = get_status(args.school, args.database_url, run_id)
    pending_after_seed = status_after_seed["jobs"].get("pending", 0)
    if pending_after_seed < args.profiles:
        raise RuntimeError(
            f"Seeded only {pending_after_seed} pending jobs; need {args.profiles}. "
            "Rerun with a larger --seed-pages."
        )

    first_chunk = min(args.first_chunk, args.profiles)
    print(f"Processing first chunk ({first_chunk} profiles)...")
    first_work = work_school(
        school_slug=args.school,
        database_url=args.database_url,
        run_id=run_id,
        batch_size=args.batch_size,
        max_jobs=first_chunk,
        headless=args.headless,
        raw_root=args.raw_root,
    )
    assert_completed(first_work, first_chunk, "first worker chunk")

    cache_before_expiry = inspect_token_cache()
    print("Forcing cached token to look expired...")
    expiry_patch = force_expire_token_cache()
    cache_after_forced_expiry = inspect_token_cache()
    if cache_after_forced_expiry.get("valid_for_startup"):
        raise RuntimeError("Token cache still appears valid after forced expiry.")

    remaining = args.profiles - first_chunk
    print(f"Restarting worker for remaining {remaining} profiles...")
    second_work = work_school(
        school_slug=args.school,
        database_url=args.database_url,
        run_id=run_id,
        batch_size=args.batch_size,
        max_jobs=remaining,
        headless=args.headless,
        raw_root=args.raw_root,
    )
    assert_completed(second_work, remaining, "post-expiry worker chunk")

    cache_after_refresh = inspect_token_cache()
    if not cache_after_refresh.get("valid_for_startup"):
        raise RuntimeError("Token cache did not refresh after post-expiry worker restart.")

    final_status = get_status(args.school, args.database_url, run_id)
    if final_status["result_count"] != args.profiles:
        raise RuntimeError(f"Expected {args.profiles} results; got {final_status}")

    export_result = export_results_to_csv(
        output_path=args.output,
        school_slug=args.school,
        database_url=args.database_url,
        run_id=run_id,
    )
    rows = csv_row_count(args.output)
    if rows != args.profiles:
        raise RuntimeError(f"Expected {args.profiles} CSV rows; got {rows}.")

    summary = {
        "run_id": run_id,
        "seed": seed_result,
        "status_after_seed": status_after_seed,
        "first_work": first_work,
        "expiry_patch": expiry_patch,
        "cache_before_expiry": cache_before_expiry,
        "cache_after_forced_expiry": cache_after_forced_expiry,
        "second_work": second_work,
        "cache_after_refresh": cache_after_refresh,
        "final_status": final_status,
        "export": export_result,
        "csv_rows": rows,
    }
    pprint(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
