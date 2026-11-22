# Job Application Agent — v0.9

A Python + LangGraph job **discovery** agent. It searches public job feeds,
employer career portals and a web search engine across eleven IT career tracks,
scores each advert against your verified evidence with an LLM, and writes the
result to your existing Google Sheets tracker.

**It never submits an application.** There is no send code path anywhere in this
project, and every write to the tracker requires an explicit `--write` flag.

Implemented: skill-based discovery across six source families, employer-direct
ATS boards, career-page reading via schema.org JobPosting, Brave Search with
verification at the employer's own page, cross-source deduplication,
deterministic eligibility pre-screening, evidence-based AI matching behind a
pluggable provider interface, a persistent assessment cache, per-source rate
limits, robots.txt compliance, budgets, retries and a safe daily runner.
Not implemented: CV/cover-letter generation, application submission, email access.

Existing tracker: [Google Sheets](https://docs.google.com/spreadsheets/d/1pN3Y1090EVvfTal_-1N1bU-h-3TCWQ2POt-vYdcOmCs/edit)

## Setup on Ubuntu / WSL2

Prerequisites: Python 3.11+, `uv` ([installation](https://docs.astral.sh/uv/getting-started/installation/)), a Google Account and a Google Cloud project.

```bash
unzip job-application-agent.zip
cd job-application-agent
cp .env.example .env
uv sync
```

### Google Cloud OAuth (recommended for a personal Sheet)

1. Visit [Google Cloud Console](https://console.cloud.google.com/), create/select a project.
2. Enable the **Google Sheets API** in APIs & Services > Library.
3. Configure **Google Auth platform** (Branding / Audience). If it is an external test app, add your account as a test user. For an agent-specific OAuth client, Google's UI may offer **Use this client for an AI-powered agent**.
4. Under Google Auth platform > Clients, create an OAuth client of type **Desktop app**.
5. Download its JSON and save as `credentials.json` in this project folder. **Never commit it.**
6. From your normal Windows/Ubuntu desktop session (browser access required), run `uv run job-agent check` and sign in to the Google account that owns/edits the existing Sheet. The refresh token is stored in `token.json` (git-ignored).

The ChatGPT Google Drive connection does **not** authenticate your local Python process. OAuth credentials are separate.

**Alternative:** set `GOOGLE_AUTH_MODE=service_account`, download the service-account JSON as `service-account.json`, and explicitly **share the existing Sheet with the JSON's `client_email` as Editor**. Do not grant domain-wide delegation. Store the secret outside git. This is convenient for unattended cron jobs.

Google setup documentation: https://developers.google.com/workspace/sheets/api/quickstart/python

## Commands

```bash
# Offline: show career tracks and today's search plan
uv run job-agent tracks

# Offline: the employer registry; --probe re-checks each board read-only
uv run job-agent employers
uv run job-agent employers --probe

# Offline: the web-search queries built from your verified skills
uv run job-agent search-queries --limit 12

# Offline: LLM matching providers, readiness and API costs
uv run job-agent providers

# Read-only: discovery across all tracks and sources, no model calls, no writes
uv run job-agent daily

# Read-only + LLM: also score each advert. Costs tokens with Gemini.
uv run job-agent daily --ai

# Write scores and new rows to the real tracker (never submits anything)
uv run job-agent daily --ai --write

# Narrow a run to specific tracks or sources
uv run job-agent daily --tracks ai_engineering,python_automation
uv run job-agent daily --sources ba
uv run job-agent daily --sources greenhouse,ashby --remote-germany

# Optional: add the Eligibility column (writes T1 only; A:S untouched)
uv run job-agent migrate-schema
uv run job-agent migrate-schema --write

# Read-only: test login, existing column names, and preferences
uv run job-agent check

# Read-only: list saved jobs
uv run job-agent list --limit 20

# Read-only: pre-screen a sample job with score 88 (score is synthetic)
uv run job-agent ingest sample_job.json

# Write an explicitly requested test record to the actual Google Sheet
uv run job-agent ingest sample_job.json --write

# Read-only: returns only rows with Status=Approved AND Approval=Approved
uv run job-agent approved

# Offline tests (no Google credentials required)
uv run pytest -q
```

The real sheet currently contains an earlier `DEMO-001` row. The `sample_job.json` creates a *different* clearly labeled demonstration row only with `--write`; delete demo rows before live operation.

## Career tracks and sources

Eleven configurable career tracks (AI/LLM, Python/automation, Java/Spring, PHP/web,
C/C++, Linux/DevOps, data engineering, SAP/ABAP, IT consulting, graduate/trainee and
generic software titles), each with German and English query variants. No query
requires the word "Junior": entry-level roles are often advertised as plain
"Software Engineer". Search budget is shared round-robin across tracks and rotates
daily, and a per-track result quota stops internship listings dominating a run.

Sources are modular adapters over a normalized schema: Bundesagentur für Arbeit and
Arbeitnow are enabled by default; Greenhouse and Lever board APIs are implemented but
make no requests until you configure `GREENHOUSE_BOARDS` / `LEVER_COMPANIES`. Only
documented JSON endpoints are used — no HTML scraping, no logins, no CAPTCHA bypass.

See `docs/SKILL_BASED_DISCOVERY.md` for the full design and
`docs/SCHEDULING.md` for Windows Task Scheduler + WSL setup.

## Existing spreadsheet schema

`Vacancies` columns A:S are fixed. v0.8 adds one **optional** column T,
`Eligibility`, via `job-agent migrate-schema --write`; a 19-column sheet stays
fully supported and nothing is written to T until you migrate. The repository validates all 19 headers before writing. A stable `Job ID` is mandatory. New candidate rows are created with `Status=Review` if score >= the `Profile` threshold, otherwise `Skip`, and `Approval=Not reviewed`. Existing rows update only **C:L**; column M (Status), N (Approval), O:R (documents and timestamps) and S (Notes) stay untouched. `RAW` mode prevents a job-title or employer name from being interpreted as a spreadsheet formula.

`Profile` contains your configured roles (junior Python, software, Linux/DevOps, SAP/ABAP, internship), Frankfurt/Stuttgart/Heilbronn, threshold 80, daily search and human approval. Phase 1 actually reads the **Min match score** from this tab. Other preferences are present for later modules; the scheduling and role/location filters are not active yet.

There is **no send method** in this project. The `approved` CLI only displays rows and never contacts an employer. When implementing submission later, require both explicit manual approvals **and** a supported authorized submission channel; do not silently mark a job `Applied`.

## Troubleshooting

- `FileNotFoundError ... credentials.json`: create OAuth Desktop credentials in Google Cloud; `cp .env.example .env` is not enough.
- `403 access_denied`: Google Auth platform/test user issue; check that the signed-in Google account can edit the Sheet.
- `403 insufficientPermissions`: enable Sheets API, verify OAuth scope, and remove an outdated `token.json` before re-authorizing if you changed scopes.
- `404 not found`: wrong spreadsheet ID/account, or service-account email was not added to the Sheet.
- Browser cannot open from remote Linux: perform first OAuth sign-in on a machine with browser, or use service account with explicit sheet sharing.

## Known limitations

- The BA endpoint is community-documented, not a supported public API; it may
  change without notice.
- **ATS boards barely cover your cities.** Verified read-only on 2026-11-21:
  across 1,425 postings on 13 live boards, only 4 were in Frankfurt, Stuttgart
  or Heilbronn (34 including remote-Germany). The Greenhouse/Lever/Ashby
  ecosystem is Berlin/Munich-centric, so `ALLOW_REMOTE_GERMANY=true` matters.
- **The Lever adapter is unverified**: no live board was reachable to confirm it.
- Career pages require schema.org JobPosting data. JavaScript-rendered portals
  (most SAP SuccessFactors and Workday instances) cannot be read this way.
- Brave's free tier allows 2,000 queries/month; a lead without structured job
  data is dropped, so some genuine vacancies are missed. A miss is preferred to
  an invented vacancy.
- Arbeitnow's feed is weighted towards Berlin, London and remote roles, so it
  rarely yields anything in Frankfurt, Stuttgart or Heilbronn.
- Match scores are advisory heuristics, not a probability of an interview.
- The pipeline is a single writer: never run two schedulers against one sheet.

## Future phases

1. CV/Anschreiben generation and human review.
2. One-time approved submission integration and response tracking.
3. Dashboards and response monitoring.

See `docs/ARCHITECTURE.md` for the full diagram and
`docs/SKILL_BASED_DISCOVERY.md` for the v0.8 pipeline.
# job-application-agent
