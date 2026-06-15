# Columbia Scraper

The Columbia scraper is intentionally separate from both Princeton TigerNet and
St. Paul's.

Use `columbia.py` for Columbia:

```powershell
python columbia.py auth-check --skip-api-check
python columbia.py auth-check --login-if-needed
python columbia.py smoke --count 3
python columbia.py seed --max-pages 5 --per-page 100
python columbia.py work --run-id <RUN_ID> --max-jobs 25 --batch-size 5
python columbia.py status --run-id <RUN_ID>
python columbia.py export-db --run-id <RUN_ID> --output output\columbia\columbia.csv
```

Isolation rules:

- Princeton auth remains in `src/auth.py`.
- St. Paul's auth remains in `src/stpauls/auth.py`.
- Columbia auth lives in `src/columbia/auth.py`.
- Columbia's browser profile stays in `output/browser-profile/columbia`.
- Columbia's token cache stays in `output/columbia/.token_cache.json`.
- Raw Columbia API payloads are stored under `output/raw/columbia`.

Environment variables:

```text
COLUMBIA_BASE_URL=https://community.alumni.columbia.edu
COLUMBIA_START_URL=https://community.alumni.columbia.edu/s/global-search/%40uri#t=All&sort=relevancy
COLUMBIA_LOGIN_PATH=/cas/auth
COLUMBIA_UNI=your_uni
COLUMBIA_PASSWORD=your_password
COLUMBIA_MY_USER_ID=optional_numeric_profile_id
```

The auth flow follows the Princeton strategy: use a persistent Playwright
profile so Columbia CAS/Duo can remember the device, then restore browser
sessions from cached Salesforce community cookies for scraping.

`auth-check --login-if-needed` validates cached cookies against the real
community search page. If the cache is stale, it refreshes through the durable
browser profile. Columbia login automation fills `COLUMBIA_UNI` and
`COLUMBIA_PASSWORD`, advances through common login buttons, and clicks common
Duo/remembered-device prompts when they appear. Headless refreshes can work
only when Columbia CAS/Duo still trusts the remembered device; if Duo requires
a fresh phone approval, run without `--headless` once so you can approve it and
preserve the remembered browser state.

The DB-backed Columbia path is Salesforce UI-driven:

- `seed` opens the global search page, captures visible alumni result rows,
  stores them in `alumni_seed`, and enqueues `profile_jobs`.
- `work` claims queued jobs, navigates back to the saved result page number,
  opens each profile, captures profile details and the personal LinkedIn URL
  when available, and writes normalized rows to `profile_results`.
- `export-db` writes completed normalized rows to CSV.

Unlike TigerNet, Columbia does not currently expose a clean Hivebrite listing
API here, so pagination is driven by clicking the Salesforce community result
controls.
