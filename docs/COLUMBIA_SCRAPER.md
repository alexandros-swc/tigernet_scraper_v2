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

The DB-backed Columbia path is API-first after login:

- `seed` opens the global search page to obtain an authenticated browser
  session, then calls Columbia's Coveo search endpoint from that browser
  context. It stores API rows in `alumni_seed` and enqueues `profile_jobs`.
- `work` claims queued jobs and writes the normalized Coveo/Salesforce fields
  to `profile_results`.
- For LinkedIn, `work` first checks Coveo raw fields, then fetches the
  Salesforce UI API record fields `LinkedIn_Profile_URL__c`,
  `LinkedIn_Profile_Link__c`, and related visibility fields. If those API
  fields do not expose a personal `/in/` URL, it falls back to the already
  proven profile-icon click/intercept path.
- `export-db` writes completed normalized rows to CSV.

The old rendered-search extraction remains as a fallback if the Coveo API is
unavailable during a run. Login is still UI-based through Columbia CAS/Duo.
