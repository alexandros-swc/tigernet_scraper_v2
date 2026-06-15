# Scraper Module Execution Plan - Columbia Update

Source plan reviewed: `C:\Users\User\Downloads\module1 (1).docx`.

Target date: June 20, 2026.

## Primary Objective

Log into Princeton TigerNet, St. Paul's, and Columbia with authorized accounts;
collect all available alumni fields that each platform exposes; store raw source
payloads for audit/replay; export schema-compatible CSVs; and make long-running
scrapes restartable across machines.

Columbia is no longer just an unknown or placeholder milestone. It now has a
separate scraper module, a DB-backed seed/work/export path, Salesforce
community UI extraction, Columbia CAS/Duo authentication, profile opening, and
personal LinkedIn URL capture from profile-level LinkedIn buttons.

## Current Columbia Implementation

### Isolation

Columbia follows the St. Paul's isolation strategy so fixes cannot accidentally
cross into TigerNet or St. Paul's:

- Entrypoint: `columbia.py`
- Package: `src/columbia/`
- Auth module: `src/columbia/auth.py`
- CLI/runtime: `src/columbia/cli.py`, `src/columbia/runtime.py`
- Docs: `docs/COLUMBIA_SCRAPER.md`
- Browser profile: `output/browser-profile/columbia`
- Token cache: `output/columbia/.token_cache.json`
- Raw payload root: `output/raw/columbia`

Princeton remains under `main.py` and `src/auth.py`. St. Paul's remains under
`stpauls.py` and `src/stpauls/`.

### Columbia Directory And Platform

The Columbia directory starts from:

```text
https://community.alumni.columbia.edu/s/global-search/%40uri#t=All&sort=relevancy
```

This value should be quoted in `.env` because the `#` fragment can otherwise be
treated as a comment by dotenv-aware editors or parsers:

```text
COLUMBIA_START_URL="https://community.alumni.columbia.edu/s/global-search/%40uri#t=All&sort=relevancy"
```

Columbia is a Salesforce Experience Cloud community with a Coveo-backed search
experience, not a Hivebrite/TigerNet-style `/frontoffice/api/users` integration.
The implemented Columbia path is therefore UI-driven:

- `seed` opens the Salesforce global search page.
- It sets the visible result count when possible, such as 10, 25, 50, or 100.
- It extracts visible alumni result rows from the rendered result page.
- It filters obvious non-person rows and malformed lines.
- It stores each listing row in `alumni_seed`.
- It enqueues each row into `profile_jobs`.
- It stores raw listing captures under
  `output/raw/columbia/run_<id>/listing/page_<page>.json.gz`.

The current `src/columbia/adapter.py` still contains legacy Hivebrite naming and
helper methods. Before the final handoff, rename the platform metadata to
Salesforce/Coveo and either remove or clearly quarantine unused Hivebrite-style
helpers so future operators do not confuse Columbia with TigerNet.

### Columbia Auth And Reauthentication

Columbia follows the Princeton auth durability model but uses separate files and
paths:

- Auth is handled by Playwright and a persistent Columbia browser profile.
- The login flow starts at the Columbia community and redirects to Columbia CAS.
- CAS fields are filled from `COLUMBIA_UNI` and `COLUMBIA_PASSWORD`.
- Duo is handled legitimately. A fresh Duo approval cannot be bypassed, but the
  persistent profile should preserve remembered-device state where Columbia
  permits it.
- Cached Salesforce community cookies are stored in
  `output/columbia/.token_cache.json`.
- `auth-check --login-if-needed` validates cached cookies against the real
  community search page, not just the existence of a cache file.
- When worker session restore fails, the worker refreshes tokens through a
  subprocess, reopens the browser session, records auth-session state, releases
  leased jobs, and continues if refresh succeeds.
- The worker exposes:
  - `--max-auth-refreshes`
  - `--auth-refresh-delay`
  - `--simulate-auth-expiry-after-jobs`

The production expectation is: one initial interactive login/Duo approval may
be needed, then long runs should continue automatically while the remembered
browser profile remains trusted. If Duo forces a new phone approval, the worker
must stop cleanly with retryable jobs, not corrupt progress.

### Columbia Profile And LinkedIn Extraction

Columbia profile work is also rendered-page driven:

- `work` claims jobs from the shared DB queue.
- It navigates back to the source result page for that job.
- It opens the matching visible profile.
- It extracts profile URL and profile-level details currently visible to the
  scraper.
- If the profile has an `in` LinkedIn icon, the scraper clicks the profile-level
  icon and captures the outbound LinkedIn URL.
- LinkedIn requests are intercepted and aborted so the scraper records the URL
  without actually loading LinkedIn.
- Only personal LinkedIn profile URLs are accepted. The alumni association group
  URL, such as `https://www.linkedin.com/groups/55739/`, is rejected because it
  is not a person profile.
- If a profile cannot be opened, the job is completed as listing-only with
  `profile_open_error=could_not_open_profile` so it does not retry forever.

### Columbia Durable Data Path

The Columbia commands use the same durable DB tables as the other scrapers:

- `schools`
- `scrape_runs`
- `accounts`
- `auth_sessions`
- `alumni_seed`
- `profile_jobs`
- `profile_results`
- `worker_heartbeats`

Because Salesforce visible results do not expose a clean stable user ID in the
current UI path, Columbia creates a deterministic external ID from:

```text
full_name | class_tag | raw_text | profile_url
```

The ID is a short SHA1-derived value. This is sufficient for queue idempotency
within the current result ordering, but the final plan should still evaluate
whether a true Salesforce ID can be recovered from the rendered links, Coveo
payloads, or Aura profile calls before full production scraping.

## Updated Definition Of Done For Columbia

Columbia is done when:

- The scraper can authenticate from a clean token cache using
  `COLUMBIA_UNI`/`COLUMBIA_PASSWORD`, Columbia CAS, and Duo remembered-device
  state where available.
- `auth-check --login-if-needed` validates a real authenticated community page.
- `seed` can enumerate at least the first production-sized page window with
  `--per-page 100` and continue across pages without malformed rows polluting
  the queue.
- `work` can open profiles, capture listing-only fallbacks, extract personal
  LinkedIn URLs where present, save raw profile records, and continue after
  simulated auth expiry.
- `status` shows meaningful queued, leased, completed, retry, failed, heartbeat,
  and auth state.
- `export-db` emits a CSV with stable columns first, including `source`,
  `external_id`, `full_name`, `class_tag`, `profile_url`,
  `linkedin_profile_url`, `profile_open_error`, raw payload refs, and
  `scraped_at` or equivalent DB timestamps where available.
- Raw listing/profile captures exist under deterministic paths.
- Princeton and St. Paul's compile/smoke checks still pass after Columbia
  integration.

## Columbia Acceptance Criteria

| Priority | Criterion | Confirmation Test | Evidence |
| --- | --- | --- | --- |
| P0 | Columbia stays isolated from Princeton and St. Paul's. | `python -m compileall main.py stpauls.py src\auth.py src\runtime src\stpauls src\columbia`; grep that Columbia imports do not appear in Princeton/St. Paul's runtime paths. | Compile transcript and grep output. |
| P0 | Columbia CAS/Duo login works from a cleared token cache. | Delete only `output\columbia\.token_cache.json`, keep `output\browser-profile\columbia`, then run `python columbia.py auth-check --login-if-needed`. | Auth transcript showing credentials entered, Duo/remembered-device path, cookies cached, and visible result count. |
| P0 | Columbia DB seed/work/export works. | `python columbia.py seed --max-pages 1 --per-page 10`; `python columbia.py work --run-id <RUN_ID> --max-jobs 10 --batch-size 2`; `python columbia.py export-db --run-id <RUN_ID> --output output\columbia\columbia_e2e_10.csv`. | DB status, CSV, raw listing/profile paths, row count equals completed jobs. |
| P0 | LinkedIn capture records personal profile URLs only. | Run smoke/work on known profiles with and without LinkedIn buttons. Verify `linkedin_profile_url` contains `/in/` when present and never contains `/groups/55739/`. | CSV rows showing personal LinkedIn URLs and blank values for non-person/group links. |
| P0 | Auth refresh works during the worker loop. | `python columbia.py work --run-id <RUN_ID> --max-jobs 5 --batch-size 1 --simulate-auth-expiry-after-jobs 2 --auth-refresh-delay 0`. | Output has `auth_refreshes: 1`, `auth_required: False`, and additional jobs complete after refresh. |
| P0 | Crash/restart does not lose or duplicate work. | Start `work`, interrupt with Ctrl+C after several completions, run `status`, then rerun `work` on same run ID. | Completed count only increases; duplicate external IDs are zero; leased jobs become claimable. |
| P1 | Pagination is stable at production settings. | `python columbia.py seed --max-pages 5 --per-page 100`, then inspect page counts and malformed-row rate. | Five raw listing pages, about 500 visible rows subject to page availability, no location/filter rows enqueued. |
| P1 | Long-run browser stability is acceptable. | Run a 500-profile worker pilot with `--batch-size 5 --request-delay 1`. | Worker transcript, heartbeat activity, memory/browser stability notes, failure classification. |
| P1 | Field coverage is measured. | Export after 100 and 500 completed jobs, run CSV coverage validation. | Coverage report listing non-empty counts for `full_name`, `class_tag`, `profile_url`, `linkedin_profile_url`, and error fields. |
| P1 | Documentation is operator-ready. | Follow `docs/COLUMBIA_SCRAPER.md` from a clean shell using only `.env` and the persistent profile. | Runbook gap list is empty or tracked in the unknown register. |

## Updated Unknowns Register

| Unknown | Current Status | Closeout Task | Due |
| --- | --- | --- | --- |
| Columbia platform/vendor | Retired: Salesforce Experience Cloud plus Coveo, not Hivebrite. | Update adapter metadata/docs so runtime does not advertise Columbia as Hivebrite. | Jun 16 |
| Clean Columbia JSON/API path | Partially open. Coveo and Aura endpoints are known, but current production implementation is rendered UI extraction. | Decide whether to keep UI path for June 20 or add a second API-backed extractor for stable IDs and richer fields. | Jun 17 |
| True stable Columbia profile ID | Open. Current external ID is deterministic from visible listing fields. | Inspect rendered profile links, Coveo `sfid`/`permanentid`, or Aura profile calls during a 20-profile sample. Use Salesforce ID if available. | Jun 17 |
| Profile field inventory | Partially open. Current implementation captures profile URL and LinkedIn; visible overview/detail sections need full inventory. | For 20 sample profiles, compare visible profile sections, raw HTML, Coveo/Aura data, and export columns. | Jun 18 |
| LinkedIn completeness | Partially open. Known working for Ki-Min Baek-style profile icon. | Run a 100-profile sample, count profiles with `in` icons, captured `/in/` URLs, blank values, and false group links. | Jun 18 |
| Duo remembered-device reliability | Open. Automatic refresh depends on Columbia/Duo trust duration. | Run a multi-hour worker pilot and a fresh token-cache test while preserving browser profile. Record whether Duo approval is needed. | Jun 18 |
| Rate limits/cooldowns | Open. UI-driven scraping is slower but still needs conservative pacing. | Run 500-profile pilot with `--request-delay 1`; record 403/429/timeout behavior and adjust defaults. | Jun 19 |
| Pagination completeness | Open. Salesforce visible pagination may change or skip under filters/sorting. | Seed 5, 25, then 50 pages at `--per-page 100`; verify page raw files, page numbers, and duplicate IDs. | Jun 19 |
| Cross-machine portability | Open. First login requires local credentials and possibly Duo approval. | Repeat auth-check plus 10-profile E2E on a clean environment without copying token cache. | Jun 19 |

## Updated Milestones Through June 20

### June 15 - Columbia Baseline Integration

Tasks:

- Confirm Columbia module remains separate from TigerNet and St. Paul's.
- Confirm `.env.example` contains Columbia variables and document that
  `COLUMBIA_START_URL` should be quoted.
- Validate the auth flow from a cleared token cache while preserving
  `output/browser-profile/columbia`.
- Validate `smoke`, `seed`, `work`, `status`, and `export-db` for a bounded run.
- Validate personal LinkedIn capture and exclusion of the alumni association
  group link.
- Add this Columbia update to the module execution plan.

Exit criteria:

- `python columbia.py auth-check --login-if-needed` succeeds.
- `python columbia.py smoke --count 3` produces rows.
- `python columbia.py seed --max-pages 1 --per-page 10` creates a run.
- `python columbia.py work --run-id <RUN_ID> --max-jobs 10 --batch-size 2`
  completes jobs.
- `python columbia.py export-db --run-id <RUN_ID>` writes a CSV.
- Princeton/St. Paul's compile check still passes.

### June 16 - Columbia Reliability Hardening

Tasks:

- Replace legacy Columbia Hivebrite metadata with Salesforce/Coveo naming or
  clearly mark unused helpers as non-production.
- Harden row filtering so filter labels, locations, year-only rows, and
  malformed class tags are never queued.
- Run `seed --max-pages 5 --per-page 100` and inspect the raw listing files.
- Run the simulated auth-expiry test in the worker loop.
- Run a 100-profile Columbia E2E and export coverage.
- Add or update docs with exact Columbia reset, auth-check, seed, work, status,
  export, and refresh-test commands.

Exit criteria:

- Five-page seed completes without malformed queue rows.
- Simulated auth expiry completes with `auth_refreshes: 1` and
  `auth_required: False`.
- 100-profile export has no duplicate `external_id` values.
- Any profiles that cannot be opened are completed listing-only with
  `profile_open_error`.

### June 17 - Columbia Durability And Restart Testing

Tasks:

- Run a 500-profile Columbia pilot with conservative delay.
- Kill/restart the worker mid-run and verify lease recovery.
- Clear the token cache mid-run while keeping the browser profile and verify
  automatic refresh or safe auth-required stop.
- Inspect `auth_sessions` and `worker_heartbeats`.
- Compare raw payload counts to DB completed counts.

Exit criteria:

- Completed rows are not duplicated after restart.
- Leased jobs become claimable.
- Auth-required conditions release jobs instead of marking them complete.
- `status` reconciles with database counts.

### June 18 - Columbia Field Coverage And Scale Pilot

Tasks:

- Run a 2,500-profile or equivalent time-boxed pilot if 500-profile results are
  healthy.
- Inventory Columbia profile sections visible in the UI.
- Decide whether to add a Coveo/Aura supplemental extractor before June 20.
- Generate field coverage reports for 100-profile and 500-profile exports.
- Record rate-limit, timeout, and profile-open failure rates.

Exit criteria:

- Field coverage report exists.
- Known missing fields are classified as platform-hidden, privacy-restricted,
  not-yet-parsed, or inaccessible.
- Rate-limit defaults are documented.
- Decision recorded on whether API-backed Columbia enrichment is in or out for
  the June 20 delivery.

### June 19 - Three-School Endurance And Runbook QA

Tasks:

- Run bounded endurance tests for Princeton, St. Paul's, and Columbia.
- Re-run compile/regression checks after any Columbia changes.
- Finalize runbooks and unknown register.
- Produce sample exports and coverage files for all three schools.
- Verify fresh-shell commands work with documented env vars only.

Exit criteria:

- Each school has a successful bounded export.
- Columbia has at least one 500-profile or larger successful pilot unless an
  external access/rate-limit blocker is documented.
- Open Columbia risks are documented with owner, severity, and next action.

### June 20 - Review, Handoff, And Full-Run Decision

Tasks:

- Present run IDs, command transcripts, exported CSV paths, coverage reports,
  raw payload counts, auth-refresh evidence, and known gaps.
- Decide whether Columbia is ready for an unattended full run or should proceed
  with a bounded production batch first.
- Confirm whether API-backed Columbia enrichment is required after handoff.

Exit criteria:

- Reviewer can run the Columbia smoke/E2E commands from the docs.
- Columbia scraping is either approved for full run or has a concrete blocker
  with evidence.
- The repo contains final runbook, unknown register, and acceptance evidence.

## Columbia Test Commands

### Auth From Existing Cache/Profile

```powershell
python columbia.py auth-check --skip-api-check
python columbia.py auth-check --login-if-needed
```

### Fresh Token Cache Test

Keep the browser profile so Duo remembered-device state can be reused:

```powershell
Remove-Item -LiteralPath output\columbia\.token_cache.json -ErrorAction SilentlyContinue
python columbia.py auth-check --login-if-needed
```

### Smoke Test

```powershell
python columbia.py smoke --count 3 --output output\columbia\smoke_profiles.csv
```

Pass condition: three visible Columbia rows export, profile rows have names and
class tags, and known profile-level LinkedIn buttons produce `/in/` URLs when
available.

### 10-Profile DB E2E

```powershell
python columbia.py seed --max-pages 1 --per-page 10
python columbia.py status --run-id <RUN_ID>
python columbia.py work --run-id <RUN_ID> --max-jobs 10 --batch-size 2 --request-delay 0.5
python columbia.py status --run-id <RUN_ID>
python columbia.py export-db --run-id <RUN_ID> --output output\columbia\columbia_e2e_10.csv
```

Pass condition: exported rows equal completed jobs, raw listing/profile files
exist, and duplicate `external_id` count is zero.

### Simulated Auth Expiry During Work

```powershell
python columbia.py work --run-id <RUN_ID> --max-jobs 5 --batch-size 1 --simulate-auth-expiry-after-jobs 2 --auth-refresh-delay 0
```

Pass condition: result includes `auth_refreshes: 1`, `auth_required: False`,
and work continues after the simulated refresh.

### Pagination Seed Pilot

```powershell
python columbia.py seed --max-pages 5 --per-page 100
python columbia.py status --run-id <RUN_ID>
```

Pass condition: five raw listing files exist, queue rows are person records, and
page counts are plausible for the visible result count.

### 100/500 Profile Endurance Pilot

```powershell
python columbia.py work --run-id <RUN_ID> --max-jobs 100 --batch-size 5 --request-delay 1
python columbia.py export-db --run-id <RUN_ID> --output output\columbia\columbia_e2e_100.csv

python columbia.py work --run-id <RUN_ID> --max-jobs 500 --batch-size 5 --request-delay 1
python columbia.py export-db --run-id <RUN_ID> --output output\columbia\columbia_e2e_500.csv
```

Pass condition: no runaway retry loop, no duplicate IDs, `status` reconciles
with exported rows, and auth refresh either succeeds automatically or stops
cleanly with retryable jobs.

### Crash/Restart Test

```powershell
python columbia.py work --run-id <RUN_ID> --batch-size 5 --request-delay 1
```

After several completions, press Ctrl+C once, then run:

```powershell
python columbia.py status --run-id <RUN_ID>
python columbia.py work --run-id <RUN_ID> --max-jobs 25 --batch-size 5 --request-delay 1
python columbia.py status --run-id <RUN_ID>
```

Pass condition: completed count only increases, leased jobs return to the queue
after lease expiry, and no completed profiles are duplicated.

## Final Evidence Package

By June 20, the Columbia evidence package should include:

- Auth-check transcript from fresh token cache.
- Smoke CSV with at least one profile-level LinkedIn capture where available.
- 10-profile DB E2E transcript and CSV.
- Simulated auth-expiry transcript.
- Restart test transcript.
- 100-profile and 500-profile exports or a documented external blocker.
- Field coverage report.
- Raw listing/profile directory counts.
- `status` output before, during, and after work.
- Notes on Duo behavior and whether remembered-device state was sufficient.
- Notes on rate limits, timeouts, and profile-open failures.
- Regression evidence that TigerNet and St. Paul's were not affected.
