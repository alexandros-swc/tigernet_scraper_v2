# THINKING.md — Engineering Journal

## 1. Problem Decomposition

The challenge breaks down into four distinct sub-problems:

### Authentication
TigerNet uses a multi-step auth flow: Princeton CAS → Duo MFA → Hivebrite OAuth JWT. The Duo MFA step makes fully headless automation impractical without TOTP setup, so the first decision was how to handle this. I chose Playwright to automate the browser through the CAS form, allow manual Duo approval, then extract the resulting session tokens for use with direct HTTP requests.

### Discovery / Reconnaissance
Before writing any code, I opened TigerNet with Chrome DevTools and mapped the network requests. Key finding: TigerNet runs on **Hivebrite** (a commercial alumni platform), not a custom-built site. The frontend is a React SPA that communicates with a JSON API at `/frontoffice/api/users`. This meant I could skip HTML parsing entirely and scrape structured JSON.

### Data Collection
The directory API returns paginated results with a `total_items` field (130,070 alumni). Two endpoints are available:
- **Listing**: `/frontoffice/api/users?page=N&per_page=50` — basic data, fast
- **Full profile**: `/users/{id}/users/{id}?full_profile=true` — rich data, one call per user

The listing endpoint alone captures name, class year, location, and affinity groups. Full profiles add contact info, work history, and education.

### Export
The Hivebrite API returns nested JSON with dynamic fields (the `fields` array varies by profile). The exporter needed to flatten this into tabular CSV format while dynamically discovering all columns across 130K profiles.

## 2. Approach Exploration

### API vs. HTML Scraping
**Explored**: BeautifulSoup HTML parsing of rendered directory pages
**Chose**: Direct API calls to `/frontoffice/api/users`
**Why**: The DevTools recon revealed a clean JSON API. Scraping the API is faster, more reliable, and returns structured data. No HTML parsing, no CSS selector fragility.

### Authentication Strategy
**Explored**:
- Raw `requests.Session` to replay the CAS flow
- Selenium WebDriver
- Playwright

**Chose**: Playwright for login, then `requests` for scraping
**Why**: The Duo MFA step requires JavaScript execution and user interaction (push approval). Raw requests can't handle this. Playwright is lighter and faster than Selenium. After login, I extract cookies and switch to `requests` for speed — no need for a browser on every API call.

### Pagination Strategy
**Explored**:
- Scrape listing only (basic data for all 130K users)
- Scrape listing + full profiles for every user
- Scrape listing + full profiles for a targeted subset

**Chose**: Listing-first approach with optional full profile enrichment
**Why**: The listing endpoint returns useful data in ~2,600 API calls (at 50/page). Full profiles require 130,000 individual calls. I implemented both, with `--full-profiles` as an opt-in flag.

## 3. Technical Tradeoffs

| Decision | Option A | Option B | Chose | Rationale |
|----------|----------|----------|-------|-----------|
| Auth mechanism | Raw HTTP | Playwright browser | B | Duo MFA requires JS execution |
| Scraping speed | Parallel requests | Sequential with delays | B | Respect rate limits, avoid getting blocked |
| Data completeness | Listing only | Listing + full profiles | Both | Listing is fast; full profiles are opt-in |
| Field handling | Hardcoded column list | Dynamic field discovery | B | Assessment says "don't hardcode fields" |
| Progress tracking | None | JSON checkpoint file | B | 130K profiles = hours of work, need resumability |

## 4. Obstacles & Solutions

### Obstacle: Identifying the platform
**Problem**: I initially thought TigerNet was built on iModules (the old platform). The assessment document also referenced "TigerBook" which turned out to be a different system entirely.
**Solution**: Browser DevTools recon revealed Hivebrite cookies, JWT tokens, and API patterns. This completely changed the technical approach.

### Obstacle: Duo MFA in the auth flow
**Problem**: Can't fully automate Duo push approval without TOTP configuration.
**Solution**: Run Playwright in non-headless mode for login. User approves Duo once, tokens are cached for ~1 hour. For longer scrapes, the resumable progress system handles token expiration gracefully.

### Obstacle: Token expiration during long scrapes
**Problem**: The JWT access token expires after ~1 hour. A full profile scrape takes ~54 hours.
**Solution**: Implemented progress checkpointing. When the token expires (401 response), the user re-authenticates and resumes with `--resume`.

*(TODO: Add more obstacles as you encounter them during actual scraping)*

## 5. AI Collaboration

*(Document your actual AI usage here. Be specific and honest. Example structure:)*

### Tools Used
- Claude (via claude.ai) for architecture planning, API analysis, and code generation
- *(Add others: Copilot, ChatGPT, etc.)*

### What Worked Well
- Analyzing the Hivebrite API response structure from raw JSON
- Generating the project skeleton and boilerplate code
- Identifying the JWT token structure and extracting user_id

### What Didn't Work / Where I Overrode AI
*(Be honest here — this is where you show engineering judgment)*

### What I Built Myself
*(Highlight the parts where you had to think independently)*
