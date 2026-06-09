# TigerNet Scale and Reliability Plan

Date: June 9, 2026

## Executive Summary

The current TigerNet scraper proves the hard parts are possible: it can authenticate through Princeton CAS and Duo, run inside a real browser context, call the Hivebrite-backed JSON endpoints, collect listing data, fetch full profile data, fetch the supplemental `/data` endpoint, and export results. The next version should not primarily be a better scraper. It should be a reliable scraping system.

The central change is to move from a local script with local progress files to a durable cloud-run job system:

1. Seed the full alumni universe from the fast listing endpoint.
2. Store every discovered alumni ID in a central database.
3. Create one idempotent profile job per alumni ID.
4. Run one or more cloud workers that claim jobs safely, fetch profile endpoints, save raw and normalized output, and checkpoint constantly.
5. Make authentication a first-class state machine with session validation, pre-expiration refresh, cooldowns, re-authentication, and alerting.
6. Export from the database after the run instead of treating a CSV as the system of record.

Recommended first implementation: AWS EC2 worker instance(s) plus managed PostgreSQL, S3, Secrets Manager, and CloudWatch. This is the best near-term path because the scraper depends on a stateful Playwright browser profile, CAS/Duo session persistence, and possibly one-time human MFA setup. A full ECS/Fargate implementation is attractive later, but EC2 is more practical for the first production Princeton run because headed browser/VNC access can be available for initial authentication or rare re-auth recovery.

The system should be designed so Princeton works with one credential and becomes faster with more credentials. Multiple accounts should be an optional scaling mechanism, not a requirement.

## Current State of the Repository

The repository currently contains:

- `main.py`: CLI entry point.
- `src/auth.py`: Princeton CAS + Duo login through Playwright, token/cache extraction, browser session restoration, token refresh.
- `src/scraper.py`: listing scraper plus async full-profile workers using multiple Playwright tabs.
- `src/exporter.py`: dynamic JSON-to-CSV export logic.
- `src/utils.py`: logging, progress file persistence, HTTP retry helpers.
- `config/settings.py`: static settings for endpoints, delays, retries, tab count, and paths.

The current product shape is correct:

- The listing endpoint can seed IDs quickly.
- Profile enrichment is separated from listing collection.
- Playwright fetches are made from inside an authenticated browser context, which matches the current Cloudflare/session behavior.
- Async Playwright tabs are the right local concurrency primitive.
- The scraper already understands that token/session expiration is the long-run bottleneck.

The current weaknesses are also clear:

- Progress is stored in a local JSON file, which is not durable enough for a week-long cloud process.
- Session state is implicit, not centrally tracked.
- Worker state, account state, and job state are not modeled separately.
- Resume works only at script granularity, not at distributed worker/job granularity.
- Parallelism is limited to tabs in one process; multiple credentials or machines would risk duplicate work without a central queue.
- Results are exported at the end instead of written safely as each profile completes.
- There is no cloud deployment, structured monitoring, or operational alerting.

## Constraints and Principles

### Functional Requirements

- Scrape approximately 120,000 to 130,000 Princeton alumni records.
- Use the fast listing endpoint first to collect IDs and basic data.
- Fetch richer detail from full profile and supplemental data endpoints.
- Run in the cloud for days without a laptop staying open.
- Resume automatically after crashes, token expiry, process restarts, VM reboots, and transient network failures.
- Support one credential and optionally multiple credentials.
- Avoid duplicate work across workers.
- Produce a final clean CSV or spreadsheet-ready export.
- Preserve raw responses so parser/export bugs can be fixed without re-scraping.

### Reliability Requirements

- Every profile job must be idempotent.
- A completed profile should never be lost because a process crashes later.
- A worker crash should return its leased jobs to the queue automatically.
- Session expiration should pause only the affected account, not corrupt global progress.
- Rate-limit events should slow the system down automatically.
- The system should be observable enough to answer:
  - How many IDs have been seeded?
  - How many profile jobs are pending, leased, complete, failed, or cooling down?
  - Which account is active or blocked?
  - What is the estimated completion time?
  - What caused recent failures?

### Compliance and Operational Guardrails

This plan assumes SWC has appropriate authorization to access and process the alumni data through the accounts being used. Before a production run, the team should confirm acceptable use, data retention, access control, and downstream handling requirements.

The system should not try to bypass MFA, CAPTCHA, access controls, or rate limits. It should use legitimate authenticated sessions, conservative request rates, backoff behavior, and explicit permissions. If Duo requires human approval for every new session, that is a real product constraint; the engineering solution is to persist legitimate sessions, refresh before expiry where supported, alert when approval is required, and pursue an official export/API/service-account path if fully unattended access is mandatory.

## Recommended Architecture

### High-Level Components

1. **PostgreSQL database**
   - Source of truth for alumni IDs, job state, run state, account state, and normalized profile data.
   - Enables parallel workers without duplicate scraping.
   - Makes resume independent of any one machine.

2. **Object storage**
   - S3 bucket for raw endpoint responses, compressed JSON, screenshots, logs that should survive worker deletion, and final exports.
   - Raw storage is important because it lets us change parsers later without re-hitting TigerNet.

3. **Worker process**
   - Runs Playwright.
   - Owns one account session at a time.
   - Maintains a persistent browser profile directory per account.
   - Claims jobs from PostgreSQL.
   - Fetches profile endpoints.
   - Saves raw and normalized results immediately.
   - Heartbeats periodically.

4. **Session manager**
   - Responsible for authentication, session validation, token expiry prediction, refresh/re-auth, cooldowns, and browser profile restoration.
   - Treats auth as a state machine rather than a one-time startup step.

5. **Seeder**
   - Runs the fast listing endpoint.
   - Inserts or upserts alumni IDs and basic listing payloads.
   - Can be re-run safely.

6. **Exporter**
   - Reads normalized profile rows and raw parsed fields from the database.
   - Produces CSV/Parquet/JSON exports.
   - Uploads final artifacts to S3.

7. **Monitoring and alerting**
   - CloudWatch logs and metrics.
   - Optional Slack/email alerts for MFA required, repeated 403/429, stalled queues, or run completion.

### Data Flow

1. Operator starts a Princeton scrape run.
2. Seeder authenticates and fetches `/frontoffice/api/users` pages.
3. Seeder writes:
   - `alumni_seed` row per user ID.
   - `profile_jobs` row per user ID.
   - raw listing page snapshots to S3 or PostgreSQL JSONB.
4. Profile workers claim jobs in batches.
5. For each claimed user ID, a worker fetches:
   - full profile endpoint.
   - supplemental `/data` endpoint.
6. Worker writes:
   - raw responses to S3.
   - normalized result to PostgreSQL.
   - job completion status.
7. Exporter generates final deliverables from database state.

## Database Model

The exact schema can be refined during implementation, but the system should start with these concepts.

### `schools`

Stores one row per school/platform.

Important fields:

- `id`
- `slug`: `princeton`, `nyu`, `st_pauls`, `columbia`, `penn`, `uva`
- `platform`: `hivebrite`, `custom`, `graduway`, `unknown`, etc.
- `base_url`
- `enabled`
- `created_at`

### `scrape_runs`

Stores one row per run.

Important fields:

- `id`
- `school_id`
- `mode`: `seed`, `profiles`, `full`
- `status`: `created`, `running`, `paused`, `complete`, `failed`
- `started_at`
- `finished_at`
- `target_count`
- `completed_count`
- `failed_count`
- `notes`

### `accounts`

Stores metadata about credentials without storing raw secrets directly.

Important fields:

- `id`
- `school_id`
- `label`
- `secret_ref`
- `status`: `available`, `authenticating`, `active`, `cooling_down`, `mfa_required`, `disabled`
- `last_auth_at`
- `last_success_at`
- `last_failure_at`
- `cooldown_until`
- `max_concurrency`
- `request_rate_per_second`

Credentials themselves should live in AWS Secrets Manager or an equivalent secret store.

### `auth_sessions`

Stores session metadata and validation state.

Important fields:

- `id`
- `account_id`
- `status`: `valid`, `expiring`, `refreshing`, `expired`, `invalid`, `mfa_required`
- `browser_profile_path`
- `token_expires_at`
- `last_validated_at`
- `failure_reason`
- `created_at`
- `updated_at`

Do not store sensitive tokens unencrypted in ordinary database columns. Use encrypted storage or rely on the persistent browser profile path on encrypted disk.

### `alumni_seed`

Stores the canonical alumni universe discovered from listing endpoints.

Important fields:

- `school_id`
- `external_user_id`
- `listing_payload`
- `full_name`
- `class_year`
- `source_page`
- `first_seen_at`
- `last_seen_at`

Constraint:

- Unique `(school_id, external_user_id)`.

### `profile_jobs`

Stores one durable unit of profile work per alumni ID.

Important fields:

- `id`
- `run_id`
- `school_id`
- `external_user_id`
- `status`: `pending`, `leased`, `complete`, `retry`, `failed`, `skipped`
- `leased_by`
- `lease_expires_at`
- `attempt_count`
- `next_attempt_at`
- `last_error_code`
- `last_error_message`
- `created_at`
- `updated_at`

Key behavior:

- Workers claim jobs with a lease.
- If a worker dies, jobs become claimable again after `lease_expires_at`.
- Completion is an upsert, so re-running a job cannot create duplicate final rows.

### `profile_results`

Stores normalized profile output.

Important fields:

- `school_id`
- `external_user_id`
- `profile_payload_ref`
- `data_payload_ref`
- `normalized_json`
- `field_count`
- `profile_fetched_at`
- `data_fetched_at`
- `parser_version`

Constraint:

- Unique `(school_id, external_user_id)`.

### `worker_heartbeats`

Tracks live workers.

Important fields:

- `worker_id`
- `run_id`
- `account_id`
- `hostname`
- `status`
- `current_job_id`
- `completed_count`
- `error_count`
- `last_heartbeat_at`

## Session Restart and Authentication Strategy

This is the most important subsystem.

### Desired State Machine

Each account should move through explicit states:

1. `idle`
2. `authenticating`
3. `active`
4. `refreshing`
5. `cooling_down`
6. `mfa_required`
7. `disabled`

### Startup Flow

1. Worker loads account metadata.
2. Worker opens the persistent browser profile directory for that account.
3. Worker navigates to TigerNet.
4. Worker validates that it can call a small API endpoint such as:
   - `/frontoffice/api/header_data`
   - `/frontoffice/api/users?page=1&per_page=1`
5. If valid, worker starts claiming profile jobs.
6. If invalid, worker runs the auth flow.
7. If Duo approval is required and cannot be completed headlessly, worker marks `mfa_required`, emits an alert, and stops claiming jobs for that account.

### Proactive Expiry Handling

The current code decodes the `api_access_token` JWT and checks its `exp` claim. Keep this, but move it into the session manager.

New behavior:

- Decode token expiry whenever a token is available.
- Refresh or re-auth 10 to 15 minutes before expiry.
- Stop claiming new jobs while refresh is in progress.
- Let already leased jobs finish if the session is still valid.
- If refresh fails, return leased but incomplete jobs to the queue.

### Reactive Expiry Handling

The worker should treat these as auth/session signals:

- HTTP 401.
- HTTP 403 when validation endpoint also fails.
- Redirect to login page.
- JSON fetch returning HTML login page.
- Consecutive failures across multiple known-good endpoints.

On auth failure:

1. Stop claiming new jobs.
2. Save progress for the current job as failed/retry, not complete.
3. Mark the account session invalid.
4. Attempt refresh/re-auth.
5. If re-auth succeeds, resume.
6. If Duo or manual approval is required, set account to `mfa_required`, alert, and leave jobs pending for another available account or later retry.

### Cooldowns

The system should distinguish normal token expiry from rate limiting or defensive blocks.

- 429: honor `Retry-After` when present; otherwise exponential backoff.
- Repeated 403 with valid login page access: account cooldown and lower request rate.
- Network timeout: short retry with jitter.
- 5xx: retry with exponential backoff and jitter.
- Too many consecutive failures: pause the account, not the whole run.

### Persistent Browser Profiles

For Princeton, persistent browser profiles are important because Duo "remember this device" behavior may depend on browser storage and cookies. Each account should have its own profile directory:

- `profiles/princeton/account_001/`
- `profiles/princeton/account_002/`

In cloud:

- EC2: encrypted EBS volume is enough for v1.
- ECS/Fargate: use EFS if persistent browser profiles must survive task replacement.

## Parallelism Strategy

### Single Account Mode

Single account mode must work because other schools may have only one available credential.

Recommended default:

- One worker process for the account.
- 2 to 4 async Playwright tabs inside one browser context.
- A per-account token bucket rate limiter.
- Conservative initial rate: 0.25 to 0.5 profile jobs per second, then tune upward only after stable endurance tests.

The current settings use multiple tabs and short delays. For production reliability, we should centralize rate limiting instead of letting each tab independently sleep. The account, not the tab, should own the request budget.

### Multiple Account Mode

Multiple credentials can reduce elapsed time, but should be used carefully.

Recommended model:

- One browser context/profile per account.
- One account-level worker per credential.
- Shared PostgreSQL queue.
- Each worker claims jobs independently.
- Unique constraints and leases prevent duplicate profile completion.
- Each account has its own rate limit and cooldown.

This allows:

- Princeton with one account.
- Princeton with several accounts.
- NYU/Columbia/Penn/etc. with one account each.
- Multi-school runs without changing the core scheduler.

### Expected Throughput

Approximate profile workload:

- 120,000 alumni.
- 2 profile-detail calls per alumnus.
- About 240,000 profile-detail API calls after the listing seed.
- Listing seed is small by comparison, likely under a few thousand calls.

Conservative throughput estimates:

| Effective rate | One account elapsed time | Notes |
|---|---:|---|
| 0.25 detail requests/sec | 11.1 days | Extremely conservative |
| 0.5 detail requests/sec | 5.6 days | Good reliability target |
| 1.0 detail request/sec | 2.8 days | Reasonable if stable |
| 2.0 detail requests/sec | 1.4 days | Only after careful testing |

If each profile requires 2 detail requests, then 0.5 detail requests/sec equals roughly 0.25 completed profiles/sec.

With 2 independent accounts at the same safe rate, wall-clock time is roughly cut in half. With 3 accounts, roughly one third, bounded by target-site behavior and operational risk.

## Cloud Deployment Options

### Option 1: Single VPS or EC2 Instance With Local Docker Compose

Architecture:

- One cloud VM.
- Docker Compose runs:
  - scraper worker
  - PostgreSQL
  - optional local admin UI
- Encrypted disk stores browser profiles and database.
- S3 or cloud storage stores backups and exports.

Pros:

- Fastest to implement.
- Lowest cost.
- Easy to SSH/VNC into for initial Duo setup.
- Very practical for one-week Princeton run.
- Minimal cloud complexity.

Cons:

- VM is a single point of failure unless backed up aggressively.
- Local Postgres is less reliable than managed Postgres.
- Scaling to multiple machines requires more migration work.

Best use:

- Quick MVP and controlled test runs.
- Not the best final architecture unless simplicity matters more than managed durability.

### Option 2: EC2 Worker(s) With Managed PostgreSQL and S3

Architecture:

- One or more EC2 worker instances.
- RDS PostgreSQL as durable queue and result store.
- S3 for raw responses and exports.
- Secrets Manager for credentials.
- CloudWatch for logs/alarms.
- Encrypted EBS stores browser profile directories.

Pros:

- Best balance for Princeton v1.
- Supports initial headed browser auth or VNC setup.
- Durable database independent of worker machine.
- Easy to add more EC2 workers later.
- Clear migration path to ECS/Fargate.
- Browser profile persistence is straightforward.

Cons:

- More infrastructure than a single VPS.
- EC2 still needs patching and basic operations.
- If an account profile is tied to one EBS volume, moving that account to another worker needs profile sync or re-auth.

Best use:

- Recommended path for the first production Princeton run.

### Option 3: ECS/Fargate Workers With RDS, S3, Secrets Manager, EFS

Architecture:

- Containerized worker runs as ECS/Fargate task.
- RDS PostgreSQL stores queue and results.
- S3 stores raw responses and exports.
- Secrets Manager stores credentials.
- EFS stores persistent browser profile directories.
- EventBridge schedules workers or keeps services running.

Pros:

- More cloud-native.
- Auto-restart and task supervision built in.
- Easy horizontal scaling.
- No VM patching.
- Good long-term foundation once auth behavior is stable.

Cons:

- Harder initial Duo/headed-browser setup.
- Requires EFS if persistent browser profiles are needed.
- More moving parts.
- Debugging Playwright in containers is slower than on EC2.

Best use:

- v2 after the EC2 implementation proves session behavior.
- Good for schools where auth can run fully headless after initial setup.

### Option 4: Serverless Jobs Only

Examples:

- AWS Lambda.
- Cloud Run request handlers.
- Short-lived scheduled jobs.

Pros:

- Cheap for short jobs.
- Low maintenance.

Cons:

- Poor fit for long-lived browser sessions.
- Browser profile persistence is awkward.
- Session-oriented scraping becomes fragmented.
- Time limits and cold starts complicate Playwright.

Best use:

- Export, reporting, status checks, and small helper tasks.
- Not recommended for the core scraper worker.

## Recommended Path

Build v1 on AWS with:

- EC2 worker instance(s).
- RDS PostgreSQL.
- S3 bucket.
- Secrets Manager.
- CloudWatch logs and alarms.
- Dockerized scraper runtime.
- Persistent encrypted browser profile directories on EBS.

Why this is the best first path:

- It respects the existing Playwright/browser-context requirement.
- It handles Duo setup more realistically than Fargate.
- It gives us durable queue semantics immediately.
- It lets us run Princeton with one credential.
- It lets us add credentials without changing the design.
- It avoids premature orchestration complexity.
- It still uses production-grade storage, secrets, and observability.

After Princeton succeeds, we can decide whether to keep EC2 workers or move workers to ECS/Fargate. The queue, schema, adapters, and exporter should not need major changes.

## Code-Wise Implementation Plan

No code should be changed in this planning pass. When implementation begins, I would proceed in this order.

### 1. Introduce School Adapter Boundary

Add a school adapter interface so Princeton-specific logic is separated from generic job orchestration.

Proposed files:

- `src/schools/base.py`
- `src/schools/princeton.py`
- `src/schools/__init__.py`

Adapter responsibilities:

- Base URL.
- Login/auth implementation hook.
- Listing endpoint builder.
- Profile endpoint builders.
- Response parsers.
- Known validation endpoints.
- Default rate limits.

Princeton adapter methods:

- `build_listing_url(page, per_page)`
- `extract_listing_users(response)`
- `build_full_profile_url(my_user_id, target_user_id)`
- `build_profile_data_url(target_user_id)`
- `normalize_profile(listing_payload, full_profile_payload, data_payload)`

This gives us a clean path for NYU, St. Paul's, Columbia, Penn, and UVA later.

### 2. Replace Local Progress Files With PostgreSQL

Add database infrastructure:

- SQLAlchemy or psycopg repository layer.
- Alembic migrations or a simple controlled migration script.
- Tables described above.
- Repository methods for seed upsert, job claiming, completion, retry, and status.

Proposed files:

- `src/storage/db.py`
- `src/storage/repositories.py`
- `src/storage/migrations/`
- `src/storage/raw_store.py`

Important behavior:

- Use unique constraints on `(school_id, external_user_id)`.
- Use job leases with `lease_expires_at`.
- Use database transactions for claim/complete.
- Use `SELECT ... FOR UPDATE SKIP LOCKED` or equivalent to safely claim jobs from multiple workers.

### 3. Add Durable Raw Response Storage

Add S3-backed raw response storage:

- Store compressed JSON by school/run/user/endpoint.
- Save references in `profile_results`.
- For local development, allow filesystem storage with the same interface.

Path convention:

- `raw/princeton/run_<run_id>/listing/page_<n>.json.gz`
- `raw/princeton/run_<run_id>/profiles/<user_id>/full_profile.json.gz`
- `raw/princeton/run_<run_id>/profiles/<user_id>/data.json.gz`

### 4. Build the Session Manager

Move authentication lifecycle out of `main.py` and into a reusable session manager.

Proposed files:

- `src/runtime/session_manager.py`
- `src/runtime/account_state.py`

Responsibilities:

- Load credentials from local `.env` or AWS Secrets Manager.
- Create or restore a persistent browser profile.
- Validate session before work.
- Decode token expiry when available.
- Refresh/re-auth before expiry.
- Detect auth failure from responses.
- Mark account states in PostgreSQL.
- Stop workers gracefully when MFA/manual action is required.

The existing `src/auth.py` code can be refactored into this rather than thrown away.

### 5. Build the Worker Runtime

Add a long-running worker command.

Proposed file:

- `src/runtime/worker.py`

Worker loop:

1. Start session manager.
2. Validate account.
3. Claim a small batch of pending profile jobs.
4. Fetch endpoints with adapter.
5. Save raw responses.
6. Normalize result.
7. Mark job complete.
8. Heartbeat.
9. On retryable failure, update attempt count and `next_attempt_at`.
10. On auth failure, pause account and re-auth.
11. On shutdown, release leases cleanly.

Important change:

- The worker should not hold thousands of in-memory users as the source of truth.
- The database is the source of truth.

### 6. Centralize Rate Limiting

Add rate limiter:

- Per account.
- Per school.
- Optional global cap.
- Configurable from database/settings.

Proposed file:

- `src/runtime/rate_limiter.py`

Behavior:

- Token bucket or leaky bucket.
- Jittered sleeps.
- Dynamic adjustment when 429/403 events occur.
- Conservative defaults.

### 7. Add Seeder Command

Add a seed command that populates `alumni_seed` and `profile_jobs`.

Proposed file:

- `src/runtime/seeder.py`

Behavior:

- Resume from last completed listing page.
- Upsert listing users.
- Create missing profile jobs.
- Save raw listing pages.
- Re-runnable without duplicates.

### 8. Add CLI Subcommands

Replace the current single flow with subcommands.

Proposed commands:

- `python main.py seed --school princeton`
- `python main.py work --school princeton --account princeton_001`
- `python main.py run --school princeton`
- `python main.py export --school princeton --run-id <id>`
- `python main.py status --school princeton`
- `python main.py validate-session --school princeton --account princeton_001`

For compatibility, the current simple `python main.py --max-pages 5` path can remain during transition.

### 9. Add Cloud Deployment Files

Proposed files:

- `Dockerfile`
- `docker-compose.yml` for local integration tests.
- `deploy/aws/README.md`
- `deploy/aws/cloud-init-worker.sh`
- Optional Terraform later:
  - `deploy/aws/terraform/`

Initial AWS v1 can be simple:

- RDS created manually or with Terraform.
- S3 bucket created manually or with Terraform.
- EC2 instance created with a setup script.
- Worker launched under systemd or Docker Compose.

### 10. Add Observability

Implement structured logs and counters:

- Jobs complete per minute.
- Error rate.
- Auth refresh count.
- Account cooldown count.
- Queue depth.
- Estimated completion time.
- Consecutive failures by account.

Alerts:

- `mfa_required`.
- No job completion for 30 minutes while jobs remain.
- Error rate above threshold.
- Account disabled/cooling down too often.
- Run complete.

### 11. Add Tests

Test layers:

- Unit tests for URL builders and parsers.
- Unit tests for job state transitions.
- Database integration tests for job claiming and lease expiry.
- Session manager tests with mocked responses.
- Exporter tests using captured JSON fixtures.
- A small live smoke test:
  - seed 1 to 2 pages.
  - fetch 10 profiles.
  - force restart worker.
  - verify no duplicate completion.

## Timeline

Assuming implementation starts immediately after this planning pass on June 9, 2026.

### Phase 0: Access, Policy, and Production Parameters

Estimate: 0.5 to 1 day

Target dates: June 9 to June 10, 2026

Tasks:

- Confirm data authorization and acceptable use.
- Confirm which Princeton credentials can be used.
- Confirm whether the account owner can approve initial Duo on the cloud VM.
- Decide allowed request rate.
- Decide whether raw responses may be stored and for how long.
- Decide final export format.

Deliverable:

- Approved production run parameters.

### Phase 1: Database and Job Queue Foundation

Estimate: 2 days

Target dates: June 10 to June 12, 2026

Tasks:

- Add PostgreSQL connection/config.
- Add migrations/schema.
- Add repository layer.
- Implement seed/job/result tables.
- Implement job claim/lease/complete/retry logic.
- Add local Docker Compose Postgres for dev tests.

Deliverable:

- Local queue-backed job system.

### Phase 2: Princeton Seeder

Estimate: 1 day

Target date: June 12, 2026

Tasks:

- Refactor listing logic into Princeton adapter.
- Insert listing results into `alumni_seed`.
- Create one profile job per user.
- Make seeding fully resumable and re-runnable.

Deliverable:

- Full Princeton alumni ID list in PostgreSQL.

### Phase 3: Worker Runtime and Session Manager

Estimate: 3 to 4 days

Target dates: June 13 to June 17, 2026

Tasks:

- Refactor current Playwright auth into session manager.
- Add persistent browser profile support.
- Add validation endpoint checks.
- Add JWT expiry monitoring.
- Add account/session state records.
- Add worker loop.
- Add auth failure handling.
- Add per-account cooldowns.
- Add rate limiter.

Deliverable:

- Local long-running worker that can stop/restart without losing work.

### Phase 4: Cloud Deployment

Estimate: 1 to 2 days

Target dates: June 17 to June 19, 2026

Tasks:

- Create AWS resources:
  - EC2 worker.
  - RDS PostgreSQL.
  - S3 bucket.
  - Secrets Manager entries.
  - CloudWatch log group and alarms.
- Install Docker/Playwright dependencies.
- Configure encrypted EBS for browser profiles.
- Configure systemd or Docker Compose worker service.
- Run headed/VNC initial Duo setup if needed.

Deliverable:

- Cloud worker can seed and fetch small profile batches.

### Phase 5: Endurance Test and Tuning

Estimate: 2 to 3 days

Target dates: June 19 to June 22, 2026

Tasks:

- Run 500-profile test.
- Run 5,000-profile test.
- Force worker restart.
- Force token expiry/re-auth scenario.
- Tune rate limits.
- Verify no duplicate jobs.
- Verify raw payload storage.
- Verify export quality.
- Verify alerting.

Deliverable:

- Production readiness sign-off for Princeton.

### Phase 6: Princeton Production Run

Estimate: 3 to 8 days, depending on rate limit and account count

Target dates: June 22 to June 30, 2026

Tasks:

- Run final seed.
- Run profile workers.
- Monitor account/session health.
- Adjust rate limits only if stability is proven.
- Let queue drain.

Deliverable:

- All reachable Princeton alumni profiles collected.

### Phase 7: Export, QA, and Handoff

Estimate: 1 to 2 days

Target dates: June 30 to July 2, 2026

Tasks:

- Generate final CSV.
- Generate field coverage report.
- Generate failed/skipped profile report.
- Sample-check raw versus normalized profiles.
- Document operational runbook.

Deliverable:

- Final Princeton dataset and production run report.

### Realistic Completion Estimate

If Duo session persistence works as expected and credentials are ready, a robust Princeton v1 can likely be implemented, deployed, tested, run, and exported by July 2, 2026.

If Duo requires frequent human approval or Princeton invalidates remembered devices in the cloud environment, add 2 to 5 engineering days to design an operator-assisted auth flow or pursue official access/export options.

## Cost Estimate

Costs below are estimates as of June 9, 2026 and should be verified in the AWS/GCP/DigitalOcean calculators before procurement.

### AWS Recommended v1

One-week Princeton run:

| Component | Suggested size | Approx cost |
|---|---:|---:|
| EC2 worker | t3.medium or t3.large Linux | about $7 to $14 per week |
| RDS PostgreSQL | small Single-AZ instance | about $3 to $8 per week if kept only for run; about $12 to $30 per month depending size |
| EBS volume | 30 to 80 GB encrypted gp3 | about $1 to $6 per month |
| S3 | 5 to 30 GB raw/export data | usually under $1 per month at this scale |
| Secrets Manager | a few secrets | about $0.40 per secret per month plus tiny API-call cost |
| CloudWatch logs | low GB ingestion | often free to a few dollars if logs are controlled |

Practical budget:

- One account, one worker: $25 to $75 for the first month.
- Multiple accounts/workers: $50 to $150 for the first month.
- The actual one-week production run should be far cheaper than a month, but keeping RDS and the VM around for testing/QA makes the monthly view more realistic.

### ECS/Fargate v2 Reference

Fargate Linux/x86 in US East is priced by vCPU-second and GB-second. AWS's pricing example lists US East Linux/x86 at `$0.000011244` per vCPU-second and `$0.000001235` per GB-second.

Approximate compute:

- 1 vCPU, 2 GB worker: about $0.049 per hour, or about $8.30 per week.
- 2 vCPU, 4 GB worker: about $0.099 per hour, or about $16.60 per week.

This excludes RDS, S3, CloudWatch, Secrets Manager, public IPv4, and optional EFS.

Fargate is not significantly more expensive for this workload. Its main drawback is operational complexity around Playwright browser profile persistence and Duo setup.

### DigitalOcean Alternative

DigitalOcean has simpler VM pricing:

- 2 GB / 2 vCPU Droplet: $18 per month.
- 4 GB / 2 vCPU Droplet: $24 per month.
- 8 GB / 4 vCPU Droplet: $48 per month.

This is a good simple-cloud option. The main reasons to prefer AWS are managed service maturity, Secrets Manager, RDS, IAM, CloudWatch, and easier future integration with a broader production stack.

## Operational Runbook

### Before the Run

- Confirm credentials.
- Confirm credential owners can handle initial Duo setup if needed.
- Deploy database and worker.
- Run `validate-session`.
- Run a 10-profile smoke test.
- Run a 500-profile endurance test.
- Check logs, job counts, raw storage, and exports.

### During the Run

Monitor:

- Pending jobs.
- Completed jobs per hour.
- Account state.
- Session expiry time.
- 401/403/429 counts.
- Worker heartbeat.
- ETA.

Do not increase concurrency unless:

- Error rate is low.
- No 429s are observed.
- Session remains stable.
- Completion estimates require acceleration.

### If Session Expires

Expected behavior:

- Worker stops claiming jobs.
- Current incomplete jobs go back to retry/pending.
- Session manager refreshes/re-authenticates.
- Worker resumes.

If MFA is required:

- Account goes to `mfa_required`.
- Alert is sent.
- Operator approves through cloud browser/VNC or prepares a new session.
- Worker resumes after validation.

### If Rate Limited

Expected behavior:

- Honor `Retry-After`.
- Cool down account.
- Lower account rate.
- Resume later.

### If Worker Dies

Expected behavior:

- Systemd/Docker restarts worker.
- Worker restores browser profile.
- Expired leases return to queue.
- Completed jobs remain complete.

### If Database Is Unavailable

Expected behavior:

- Worker stops claiming work.
- No profile should be marked complete without database write.
- Worker retries database connection.
- Alert fires.

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Duo requires frequent approval | Blocks full autonomy | Persistent browser profiles, proactive refresh, alerts, official access/export discussion |
| TigerNet changes endpoints | Scraper breaks | Adapter boundary, raw fixtures, smoke tests |
| Cloudflare/session behavior changes | Increased 403s | Use legitimate browser context, conservative rates, cooldowns |
| Duplicate profile fetching | Wasted time, possible load increase | PostgreSQL unique constraints and leased jobs |
| Data loss on crash | Incomplete run | Write results per profile, raw S3 storage, DB source of truth |
| Over-aggressive parallelism | Account cooldown or block | Account-level limiter and low initial rates |
| Sensitive data exposure | Compliance/security issue | Secrets Manager, encrypted disks, least privilege, restricted exports |
| Parser misses fields | Incomplete dataset | Store raw responses, parser versioning, dynamic field discovery |

## Approach for Other Schools

The goal after Princeton is not to copy-paste the scraper. It is to reuse the same platform and write school-specific adapters.

### Shared Across All Schools

These should remain common:

- Database schema.
- Job queue.
- Worker lease model.
- Raw response storage.
- Session manager interface.
- Rate limiter.
- Exporter framework.
- Monitoring.
- Runbook.

### School-Specific

Each school adapter needs:

- Authentication flow.
- Session validation endpoint.
- Listing/search discovery method.
- Profile detail endpoints or page parser.
- Normalizer.
- Rate-limit defaults.
- Data coverage tests.

### NYU

Likely constraint:

- Only one account is available.

Strategy:

- Prioritize single-account reliability.
- Lower concurrency.
- Same queue system, but one active worker.
- Research whether the platform exposes a listing endpoint, search endpoint, or requires crawling profile pages.
- Confirm whether MFA/session persistence behaves differently from Princeton.

### St. Paul's

Likely constraint:

- Could be a smaller database but possibly a different alumni platform.

Strategy:

- Start with platform identification.
- If vendor API exists, build adapter around JSON endpoints.
- If HTML-only, use browser DOM extraction but keep the same job model.
- Smaller size makes it a good second-school pilot.

### Columbia

Likely constraint:

- Similar institution-scale alumni directory, potentially different auth and platform.

Strategy:

- DevTools/network discovery first.
- Identify listing/search mechanics.
- Confirm whether profile IDs are stable.
- Reuse the central queue and per-account limiter.

### Penn

Likely constraint:

- Large alumni population and likely institutional SSO/MFA.

Strategy:

- Treat like Princeton in scale.
- Do not assume Hivebrite.
- First deliverable should be an endpoint/platform discovery memo.
- Then implement adapter.

### UVA

Likely constraint:

- Unknown platform and auth behavior.

Strategy:

- Same research sequence:
  1. Manual login and navigation.
  2. Network tab capture.
  3. Platform/vendor identification.
  4. Listing/profile endpoint mapping.
  5. Small proof of concept.
  6. Adapter implementation.

## Immediate Next Steps

1. Review this plan with SWC and confirm the recommended EC2 + RDS + S3 v1 architecture.
2. Confirm data authorization, storage policy, and credential handling.
3. Choose AWS region, likely `us-east-1`.
4. Decide initial request-rate ceiling for Princeton.
5. Create AWS resources.
6. Implement the database schema and job queue.
7. Refactor Princeton into an adapter.
8. Implement session manager.
9. Implement cloud worker.
10. Run staged tests: 10 profiles, 500 profiles, 5,000 profiles.
11. Start production run only after restart/session tests pass.

## Source Links Used for Cloud Cost Context

- AWS EC2 On-Demand pricing: https://aws.amazon.com/ec2/pricing/on-demand/
- AWS Fargate pricing: https://aws.amazon.com/fargate/pricing/
- Amazon RDS for PostgreSQL pricing: https://aws.amazon.com/rds/postgresql/pricing/
- Amazon S3 pricing: https://aws.amazon.com/s3/pricing/
- AWS Secrets Manager pricing: https://aws.amazon.com/secrets-manager/pricing/
- Amazon CloudWatch pricing: https://aws.amazon.com/cloudwatch/pricing/
- DigitalOcean Droplet pricing: https://www.digitalocean.com/pricing/droplets
- DigitalOcean Managed Databases pricing: https://www.digitalocean.com/pricing/managed-databases
- Google Cloud Run pricing reference: https://cloud.google.com/run/pricing
