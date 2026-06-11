# Alumni Outreach PRD and Level Plan

Date: June 10, 2026

Owner: Alexandros Chalvatzakis

Reviewer: Arsh Shah Dilbagi

## Executive Summary

The alumni outreach project will build a repeatable pipeline for identifying alumni, enriching their contact and professional data, and preparing reliable outbound email infrastructure for investment and relationship-building workflows.

The project has three sequential modules:

1. Scraping: collect structured alumni profile data from authorized alumni directories.
2. Enrichment: normalize the scraped data and enrich it with LinkedIn profiles, work emails, personal emails, phone numbers, company metadata, and confidence scores.
3. Deliverability: create and operate outbound email infrastructure that can safely send outreach without damaging SWC/Tiger domains or landing in spam.

The first implementation target is Princeton TigerNet. After Princeton, the same system should be adaptable to St. Paul's and Columbia, then additional schools as access becomes available.

The key product requirement is repeatability: Arsh or another teammate should be able to clone the repo, configure credentials, run the documented commands, and produce auditable CSV outputs without relying on one-off manual steps.

## Goals

- Build a maintainable alumni outreach data pipeline that AI coding agents and human engineers can extend.
- Produce high-quality CSV exports for alumni outreach and investment research workflows.
- Preserve raw source data where allowed so parsers can be improved without re-scraping.
- Support long-running scraping jobs with restart, token-expiration recovery, and progress durability.
- Create clear module boundaries so scraping, enrichment, and deliverability can evolve independently.
- Establish a practical 8-week plan with staged milestones, acceptance criteria, and operational runbooks.

## Non-Goals

- Do not bypass access controls, MFA, CAPTCHA, rate limits, or site security mechanisms.
- Do not send production outreach before data authorization, compliance expectations, and unsubscribe/suppression handling are confirmed.
- Do not make the CSV the only system of record for scraping progress.
- Do not hard-code school-specific logic into the shared pipeline when an adapter boundary is appropriate.
- Do not store credentials, tokens, or sensitive exports in Git.

## Users and Stakeholders

- Investment team: uses alumni data to find relevant founders, executives, operators, researchers, and domain experts.
- Research team: filters, enriches, and validates target lists for outreach.
- Alexandros: primary builder and operator during the internship.
- Arsh: reviewer, product sponsor, and daily check-in owner.
- Future AI agents and engineers: maintain and extend school adapters, enrichment providers, and outbound workflows.

## Product Principles

- Durable over clever: every long-running job should be resumable.
- Modular over bespoke: school-specific behavior belongs in adapters.
- Observable by default: every run should expose counts, failures, auth state, and exports.
- Conservative with external systems: use legitimate credentials, modest request rates, and clear cooldowns.
- Replayable parsing: preserve raw responses where permitted, then normalize in repeatable code.
- Agent-maintainable code: clear README files, small modules, tests, and comments where they reduce ambiguity.

## Module 1: Scraping PRD

### Problem

Alumni directories contain useful relationship and professional data, but the data is locked behind authenticated web applications, often with MFA, session expiration, rate limits, and inconsistent profile schemas. Manual collection is not scalable.

The scraping module must reliably collect available alumni profile data from authorized accounts and produce structured, auditable output.

### Initial Scope

Schools:

- Princeton TigerNet: first and current implementation target.
- St. Paul's: second target, platform discovery required.
- Columbia: third target, platform discovery required.

Princeton data targets:

- Basic listing fields: ID, name, class year, location, affiliations, affinity groups, last seen metadata where available.
- Full profile fields: emails, phones, current job, company, education, experience, social URLs, interests, volunteer activity, privacy flags.
- Supplemental profile data from platform-specific secondary endpoints.

### Current State

The repository already supports a Princeton TigerNet proof of concept with:

- Playwright authentication through Princeton CAS and Duo.
- Persistent browser profile at `output/browser-profile/tigernet`.
- JWT/token cache at `output/.token_cache.json`.
- Database-backed seed/work/status/export commands.
- PostgreSQL queue tables for seed rows, profile jobs, results, auth sessions, and heartbeats.
- Raw JSON storage under `output/raw*`.
- CSV export from normalized database results.
- Successful 100-profile E2E test with forced token-cache expiration and restart recovery.

### Functional Requirements

- Seed alumni IDs from listing pages into durable storage.
- Create one idempotent profile job per alumni ID.
- Fetch full profile and supplemental data for each queued profile.
- Save raw responses and normalized results immediately after each profile.
- Resume after interruption without duplicate completed rows.
- Detect expired auth and stop or refresh without corrupting job state.
- Support small smoke tests, medium E2E tests, and full production runs.
- Export completed results to CSV with stable priority columns and dynamic additional fields.
- Support school adapters for new alumni platforms.

### Reliability Requirements

- A worker crash must not lose completed profiles.
- Leased jobs must become claimable again after lease expiry.
- Completed jobs must remain complete across restarts.
- Auth-required states must release incomplete jobs back to pending or retry.
- Token cache expiration must trigger re-auth through the remembered browser profile when possible.
- Rate limiting or transient network failures must use retry and backoff.
- Raw and normalized output must be tied to a run ID.

### Data Model

Core entities:

- `schools`: one row per school/platform.
- `scrape_runs`: one row per scrape run.
- `accounts`: metadata for credentials, without raw secrets.
- `auth_sessions`: session state, token expiry, profile path, and failure reason.
- `alumni_seed`: canonical discovered alumni IDs and listing payloads.
- `profile_jobs`: queue state for profile fetching.
- `profile_results`: normalized output plus raw payload references.
- `worker_heartbeats`: liveness and current worker state.

### Key Interfaces

CLI:

```powershell
python main.py init-db
python main.py auth-check --login-if-needed
python main.py seed --school princeton --max-pages 3 --per-page 50
python main.py work --run-id 1 --max-jobs 100 --batch-size 5
python main.py status --run-id 1
python main.py export-db --run-id 1 --output output\e2e_tigernet_fresh.csv
```

Repeatable E2E:

```powershell
python scripts\e2e_scrape_100.py --reset-db --profiles 100 --first-chunk 5 --batch-size 5 --per-page 50 --output output\e2e_tigernet_fresh.csv
```

School adapter methods:

- Build listing URL.
- Extract listing users.
- Extract total users.
- Extract external user ID.
- Build full profile URL.
- Build supplemental profile-data URL.
- Normalize merged profile data.

### Acceptance Criteria

- 10-profile smoke test completes with zero errors and exports a CSV.
- 100-profile E2E completes with zero errors.
- 100-profile E2E force-expires the cached access token, restarts, refreshes auth, and continues.
- Status command reports seed, pending, complete, retry, and failed counts.
- Export command produces a CSV with expected row count and non-empty core columns.
- Worker can be stopped and restarted without losing completed results.
- Princeton runbook documents initial Duo setup and remembered-device assumptions.

### Risks

- Duo remembered-device state expires or is invalidated.
- Alumni platform changes endpoints or response shapes.
- Aggressive request rates cause 403, 429, cooldowns, or account issues.
- New schools use different vendors or HTML-only pages.
- Personal data handling requires stricter retention or access controls than local development provides.

### Mitigations

- Preserve persistent browser profiles and test token refresh explicitly.
- Keep school-specific logic behind adapters.
- Store raw responses for replayable parsing.
- Use conservative request rates and account-level cooldowns.
- Add platform discovery memos before implementing new schools.
- Keep credentials and exports out of Git.

## Module 2: Enrichment PRD

### Problem

Scraped alumni data is useful but incomplete, inconsistent, and not directly ready for outreach. Names, job titles, companies, emails, phones, and social profiles may be missing, stale, duplicated, or privacy-limited. The enrichment module must turn scraped profiles into reliable contact and targeting records.

### Scope

Input:

- Normalized scraping output.
- Raw profile references where permitted.
- School, class year, location, company, role, education, activity, and social URL fields.

Output:

- Enriched person records.
- LinkedIn URLs where available.
- Work emails.
- Personal emails where allowed and useful.
- Phone numbers where allowed and useful.
- Company domain, company LinkedIn, industry, headcount, location, and role category.
- Confidence scores and provider provenance.
- Suppression and do-not-contact flags.

Primary enrichment provider:

- Clay.com for workflow orchestration and vendor integrations.

Possible API categories:

- Email discovery.
- Email verification.
- LinkedIn/profile matching.
- Phone discovery.
- Company enrichment.
- Deduplication and identity resolution.

### Functional Requirements

- Import scraper CSV or database export.
- Normalize names, class years, companies, titles, locations, and existing URLs.
- Deduplicate people across schools and repeated runs.
- Match alumni to LinkedIn profiles using deterministic fields first, then enrichment providers.
- Identify current company and role.
- Discover and verify work emails.
- Preserve personal emails from source data only where usage is authorized.
- Add confidence scores for each enriched attribute.
- Track provider, timestamp, and cost per enrichment step.
- Export clean outreach-ready CSV and a richer audit CSV.
- Support re-running enrichment without paying twice for unchanged rows.

### Data Quality Requirements

- Every enriched field must have provenance.
- Ambiguous matches must be flagged rather than silently accepted.
- Emails must distinguish source-provided, guessed, verified, bounced, and unknown.
- Work email confidence must be separate from person-match confidence.
- Duplicate records must preserve all school affiliations.
- Records with privacy or suppression flags must be excluded from outreach exports unless explicitly approved.

### Suggested Enrichment Schema

Core fields:

- `person_id`
- `source_school`
- `source_external_user_id`
- `full_name`
- `first_name`
- `last_name`
- `class_year`
- `current_title`
- `current_company`
- `company_domain`
- `company_linkedin_url`
- `linkedin_profile_url`
- `source_email`
- `work_email`
- `work_email_status`
- `personal_email`
- `phone`
- `location_city`
- `location_state`
- `location_country`
- `industry`
- `seniority`
- `function`
- `match_confidence`
- `email_confidence`
- `enrichment_provider`
- `enriched_at`
- `suppression_reason`

### Workflow

1. Load scraper export into enrichment staging.
2. Normalize and deduplicate records.
3. Split into enrichment batches.
4. Run LinkedIn/company matching.
5. Run email discovery.
6. Verify email deliverability.
7. Add phone/company metadata when useful.
8. Apply suppression rules.
9. Export outreach-ready data and QA reports.

### Acceptance Criteria

- 100 scraped Princeton records can be imported and enriched in a test batch.
- At least 95 percent of rows retain stable source IDs and school provenance.
- Duplicate detection correctly merges known duplicate fixture examples.
- Every email in the outreach export has a status and source.
- Every enriched LinkedIn/company/email field has provider provenance.
- The module can resume a partially completed enrichment batch.
- Export includes a QA summary with coverage rates and provider costs.

### Risks

- Provider costs scale quickly.
- LinkedIn matching can produce false positives.
- Email discovery may create deliverability risk if verification is weak.
- Personal data usage may require policy review.
- Clay workflows can become manual or hard to version-control.

### Mitigations

- Run small test batches before full-school enrichment.
- Store provider response IDs and cache results.
- Require confidence thresholds for outreach exports.
- Keep a human review lane for high-value but ambiguous records.
- Version Clay workflows or mirror logic in documented configs.
- Separate internal research export from outbound-ready export.

## Module 3: Deliverability PRD

### Problem

Even high-quality contact data will fail if outbound infrastructure is poorly configured. Sending from core company domains can damage reputation, trigger spam filtering, and reduce reply rates. The deliverability module must create and operate safe outbound infrastructure for alumni outreach.

### Scope

The deliverability module covers:

- Outbound domain strategy.
- DNS configuration.
- Mailbox creation.
- Warmup.
- Sending limits.
- Bounce and unsubscribe handling.
- Suppression lists.
- Campaign readiness checks.
- Monitoring of domain and mailbox health.

It does not cover final message copy or investment thesis content, though it should support campaign metadata and segmentation.

### Functional Requirements

- Buy or configure separate outreach domains.
- Configure SPF, DKIM, DMARC, MX, and tracking-domain settings.
- Create mailboxes for outreach senders.
- Warm up mailboxes gradually before production sends.
- Maintain per-mailbox daily sending limits.
- Track bounces, replies, unsubscribes, and spam complaints.
- Maintain global suppression list across campaigns.
- Validate that enriched contacts are eligible for sending.
- Export deliverability-ready batches to the sending platform.
- Produce daily health reports.

### Deliverability Requirements

- Do not send from core corporate domains unless explicitly approved.
- Do not send to addresses without verification or approved source status.
- Do not send to suppressed, opted-out, bounced, or high-risk contacts.
- Keep initial volume low and ramp gradually.
- Separate cold outreach reputation from normal business email reputation.
- Monitor bounce rate, reply rate, spam complaint rate, and domain health.

### Suggested System Components

- Domain registrar account.
- DNS provider.
- Mailbox provider.
- Warmup/sending platform.
- Suppression database or sheet.
- Campaign export generator.
- Health report and runbook.

### Workflow

1. Choose outreach domain pattern.
2. Purchase or configure domains.
3. Configure DNS records.
4. Create mailboxes.
5. Start warmup.
6. Verify domain health.
7. Import eligible enriched contacts.
8. Export small test campaign.
9. Monitor bounces and replies.
10. Ramp volume only if health metrics remain acceptable.

### Acceptance Criteria

- At least one outreach domain is configured with valid SPF, DKIM, and DMARC.
- Mailboxes pass deliverability setup checks.
- Warmup schedule is documented and active.
- Suppression list is live before any campaign export.
- Campaign export excludes invalid, bounced, suppressed, and low-confidence emails.
- Daily health report includes sends, bounces, replies, complaints, and domain status.
- Runbook documents how to pause all sending immediately.

### Risks

- Poor setup damages domain reputation.
- High bounce rates reduce deliverability.
- Sending too quickly triggers spam filters.
- Incomplete suppression handling creates compliance and relationship risk.
- Outreach domains or mailboxes get suspended.

### Mitigations

- Use separate domains and conservative ramp schedules.
- Verify emails before export.
- Maintain global suppression from day one.
- Start with small, high-confidence batches.
- Keep manual approval before production campaign launch.
- Monitor health daily and pause on threshold breaches.

## Cross-Module Interfaces

### Scraping to Enrichment

Artifact:

- CSV or database export from `profile_results`.

Required fields:

- Stable source ID.
- School.
- Full name.
- Class year.
- Current company/title when available.
- Location.
- Source emails and social URLs.
- Privacy/share flags.
- Raw payload references if allowed.

### Enrichment to Deliverability

Artifact:

- Outreach-ready CSV.

Required fields:

- Person ID.
- Full name.
- Company/title.
- Email.
- Email status.
- Confidence scores.
- Segment/campaign fields.
- Suppression status.
- Source/provenance fields.

### Deliverability Back to Enrichment

Artifact:

- Bounce/reply/unsubscribe/suppression feedback.

Required fields:

- Person ID.
- Email.
- Event type.
- Event timestamp.
- Campaign ID.
- Sender mailbox/domain.

## Milestones and Timeline

Assumption: 8-week internship beginning the week of June 10, 2026, with possible extension through August 8 or August 9, 2026.

### Week 1: PRD, Repo, and Princeton Scraper Stabilization

Target dates: June 10 to June 14, 2026

Deliverables:

- PRD reviewed by Arsh.
- Repo structure and README cleaned up for agent maintainability.
- Princeton 100-profile E2E test passing.
- Token-expiration/restart path documented.
- Initial scraping runbook written.

Acceptance:

- Arsh can review the plan and identify missing assumptions.
- A teammate can run a documented 100-profile test with credentials.

### Week 2: Durable Scraping Production Readiness

Target dates: June 15 to June 21, 2026

Deliverables:

- Scraper DB schema finalized.
- Worker restart and lease behavior tested.
- Raw storage conventions finalized.
- Princeton 500-profile and 5,000-profile endurance tests.
- Rate-limit and auth-required behavior documented.

Acceptance:

- Scraper can run unattended for several hours and recover from forced restart.

### Week 3: Princeton Production Run

Target dates: June 22 to June 28, 2026

Deliverables:

- Full Princeton seed.
- Full or large Princeton profile scrape.
- Field coverage report.
- Failed/skipped profile report.
- Final Princeton CSV v1.

Acceptance:

- Dataset is complete enough for enrichment test batches.

### Week 4: Enrichment Prototype

Target dates: June 29 to July 5, 2026

Deliverables:

- Enrichment schema.
- Clay workflow prototype.
- 100-record enrichment test.
- Provider cost and coverage report.
- Confidence scoring rules.

Acceptance:

- Enriched test batch includes LinkedIn/company/email coverage metrics and provenance.

### Week 5: Enrichment Scale-Up and QA

Target dates: July 6 to July 12, 2026

Deliverables:

- 1,000 to 5,000 record enrichment batch.
- Deduplication logic.
- Suppression-aware outreach export.
- QA review workflow for ambiguous records.

Acceptance:

- Outreach-ready export excludes low-confidence and suppressed contacts.

### Week 6: Deliverability Setup

Target dates: July 13 to July 19, 2026

Deliverables:

- Outreach domain strategy.
- DNS setup checklist.
- Mailbox setup.
- Warmup plan.
- Suppression list.
- Deliverability runbook.

Acceptance:

- At least one domain/mailbox set passes setup checks and is warming.

### Week 7: First Controlled Outreach Batch

Target dates: July 20 to July 26, 2026

Deliverables:

- Small high-confidence outreach-ready batch.
- Daily deliverability health report.
- Bounce/reply/unsubscribe feedback loop.
- Campaign export process.

Acceptance:

- Outreach infrastructure can safely support a small controlled campaign.

### Week 8: Additional Schools and Handoff

Target dates: July 27 to August 3, 2026

Deliverables:

- St. Paul's platform discovery memo and adapter plan.
- Columbia platform discovery memo and adapter plan.
- Updated runbooks.
- Final handoff documentation.
- Prioritized backlog.

Acceptance:

- Next school implementation can begin from a clear adapter checklist.

### Extension Window: Polish and Scale

Target dates: August 4 to August 9, 2026

Possible deliverables:

- Start St. Paul's implementation.
- Start Columbia implementation.
- Improve enrichment automation.
- Improve deliverability reporting.
- Package final demo and documentation.

## Daily Operating Cadence

- Daily Slack stand-up: yesterday, today, blockers.
- Daily 15-minute check-in with Arsh.
- Use Codex/Claude for brainstorming, code review, testing, docs, and edge-case discovery.
- Raise blockers immediately: access, credentials, Duo, API providers, billing, compliance, or infrastructure.

## Compliance and Data Handling Questions

Open questions to confirm before production enrichment or outreach:

- What alumni data may be stored, and for how long?
- Which exports may leave the local repo/cloud environment?
- Which fields are allowed for outreach targeting?
- Are personal emails acceptable for outreach, or only work emails?
- What opt-out language and suppression process is required?
- Who may access raw scraped profiles and enriched records?
- What is the approval process before sending any campaign?

## Immediate Next Steps

1. Review this PRD with Arsh.
2. Confirm the repo destination and shared Drive folder.
3. Confirm credentials and access for Princeton, St. Paul's, and Columbia.
4. Confirm enrichment provider budget and Clay access.
5. Confirm outreach domain and mailbox budget.
6. Decide whether the scraping DB reset/E2E helper should remain as a repo test utility.
7. Run a 500-profile Princeton E2E before any larger production run.
8. Draft St. Paul's and Columbia discovery checklists.
