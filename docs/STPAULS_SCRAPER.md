# St. Paul's Scraper

The St. Paul's Alumni Network scraper is intentionally separate from the
Princeton TigerNet scraper.

Use `stpauls.py` for St. Paul's:

```powershell
python stpauls.py auth-check --skip-api-check
python stpauls.py auth-check --login-if-needed
python stpauls.py smoke --count 3
python stpauls.py seed --max-pages 5
python stpauls.py work --run-id <RUN_ID> --max-jobs 25 --batch-size 5
python stpauls.py status --run-id <RUN_ID>
python stpauls.py export-db --run-id <RUN_ID> --output output\stpauls\stpauls.csv
```

Use `main.py` for Princeton TigerNet:

```powershell
python main.py auth-check
python main.py seed --school princeton
python main.py work --school princeton --run-id <RUN_ID>
python main.py export-db --school princeton --run-id <RUN_ID>
```

## Isolation

- Princeton auth remains in `src/auth.py`.
- St. Paul's auth lives in `src/stpauls/auth.py`.
- Princeton's browser profile stays in `output/browser-profile/tigernet`.
- St. Paul's browser profile stays in `output/browser-profile/stpauls`.
- Princeton's token cache stays in `output/.token_cache.json`.
- St. Paul's token cache stays in `output/stpauls/.token_cache.json`.
- Raw St. Paul's API payloads are stored under `output/raw/stpauls`.

St. Paul's only runs when you call `python stpauls.py ...`.

## First Login

Run this once with a visible browser:

```powershell
python stpauls.py auth-check --login-if-needed
```

After you finish login in the browser, the scraper reads `authToken` and
`authTokenExpires` from St. Paul's local storage and caches them separately.

## Smoke Test

This fetches the first directory page, pulls a few full profiles, and writes a
CSV without touching the database queue:

```powershell
python stpauls.py smoke --count 3
```

Output:

```text
output/stpauls/smoke_profiles.csv
```
