# TigerNet Alumni Directory Scraper

An automated scraper for Princeton's TigerNet alumni directory (`tigernet.princeton.edu`). Authenticates via Princeton CAS + Duo MFA, paginates through the Hivebrite-powered directory API, and exports all alumni profiles to a clean CSV.

## How It Works

TigerNet runs on [Hivebrite](https://hivebrite.com), a commercial alumni platform. Behind the web interface, it exposes a JSON REST API at `/frontoffice/api/users`. This scraper:

1. **Authenticates** using Playwright to automate the CAS → Duo → Hivebrite login flow, then extracts session cookies and JWT tokens
2. **Scrapes the directory** by paginating through the listing API (`/frontoffice/api/users?page=N`)
3. **Optionally fetches full profiles** for each user via `/users/{id}/users/{id}?full_profile=true`
4. **Exports to CSV** with dynamic field detection (no hardcoded column list)

## Prerequisites

- Python 3.11+
- A Princeton NetID with TigerNet access
- A device configured for Duo MFA push notifications

## Installation

```bash
git clone https://github.com/YOUR_USERNAME/tigerbook-scraper.git
cd tigerbook-scraper

python -m venv venv
source venv/bin/activate        # macOS/Linux
# venv\Scripts\activate         # Windows

pip install -r requirements.txt
playwright install chromium
```

## Configuration

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:
```
PRINCETON_NETID=your_netid
PRINCETON_PASSWORD=your_password
```

**Your credentials are never committed** — `.env` is in `.gitignore`.

## Usage

### Basic scrape (listing data only)

```bash
python main.py
```

This scrapes basic profile data (name, class year, location, affinity groups) for all ~130,000 alumni. Takes approximately 45–60 minutes.

### With full profile details

```bash
python main.py --full-profiles
```

Fetches detailed data for each user (email, phone, work history, education, LinkedIn). **This is slow** — ~1.5 seconds per profile × 130K profiles ≈ 54 hours. Consider using `--max-pages` to limit scope.

### Test run

```bash
python main.py --max-pages 5
```

### Check auth without scraping

```bash
python main.py auth-check --skip-api-check
python main.py auth-check
python main.py auth-check --login-if-needed
```

`auth-check` reports token-cache status and can verify a single listing API call.
The first fresh login uses the persistent browser profile in
`output/browser-profile/tigernet`, so Duo/CAS remembered-device state can be
reused on later token refreshes.

### Resume after interruption

```bash
python main.py --resume
```

Progress is auto-saved every 10 pages. If the script crashes or you Ctrl+C, just re-run with `--resume`.

### All options

```
python main.py --help

  --full-profiles    Fetch detailed profile data (slow)
  --resume           Resume from saved progress
  --max-pages N      Limit number of listing pages
  --per-page N       Results per page (default: 50)
  --output PATH      Output CSV path (default: output/tigernet_alumni.csv)
  --headless         Run browser headless (needs automated Duo)
```

## Output

The CSV is written to `output/tigernet_alumni.csv` with one row per alumnus. Columns include:

| Column | Source | Example |
|--------|--------|---------|
| `id` | Hivebrite user ID | `2364473` |
| `full_name` | Full Name field | `Ms. Charlotte Y. Stanton '00` |
| `class_year` | Extracted from name | `2000` |
| `city`, `state`, `country` | Location | `Oakland`, `CA`, `United States` |
| `preferred_paa` | Regional association | `PC of Northern California` |
| `affinity_groups` | Affinity groups | `Princeton Women's Network` |
| `email`, `email2`, `email3` | Full profile only | `alumni@example.com` |
| `current_job`, `company_name` | Full profile only | `Founder`, `Hortihop` |
| `educations` | Full profile only | `Princeton University — 2016 — Bachelor of Arts (AB) — Music` |
| `experiences` | Full profile only | `Founder at Hortihop (2022-10-01 — present)` |

Missing values are represented as empty cells (not `null` or `NaN`).

## Architecture

```
├── main.py                 # Entry point and CLI
├── src/
│   ├── auth.py             # CAS + Duo login via Playwright
│   ├── scraper.py          # Directory listing pagination
│   ├── profile.py          # Individual full profile fetching
│   ├── exporter.py         # JSON → CSV flattening and export
│   └── utils.py            # HTTP session, retries, logging, progress
├── config/
│   └── settings.py         # Configuration dataclass
├── requirements.txt
├── .env.example
└── .gitignore
```

## Known Limitations

- **Duo MFA requires manual approval** on first login. The browser opens non-headlessly so you can approve the push. Tokens are cached for ~1 hour afterward.
- **Token expiration**: The `api_access_token` JWT expires after ~1 hour. For long scrapes (full profiles), the token may need refreshing. The scraper saves progress so you can re-authenticate and resume.
- **Rate limiting**: The scraper uses a 1–1.5 second delay between requests. If TigerNet rate-limits you (429), it backs off automatically using the `Retry-After` header.
- **`per_page` maximum**: The API default is 18. We request 50; the actual limit may vary.
- **Privacy-restricted profiles** may return limited data.

## Rate Limiting Strategy

- Listing pages: 1.0s between requests
- Full profiles: 1.5s between requests
- On 429 (rate limit): honor `Retry-After` header
- On 5xx (server error): exponential backoff (2s, 4s, 8s)
- On network timeout: exponential backoff with 3 retries
