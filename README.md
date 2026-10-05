# IPO and Online Sentiment Monitor

Tracks company IPO evidence across company sizes, samples public internet commentary, and includes government contracts as supplementary evidence. Government awards and penny-stock thresholds are **not required** for the broad IPO tracker.

The original contract-qualified email route is retained as an optional feature. No trading or automatic investment decisions are implemented.

## What it tracks

- **SEC IPO evidence:** S-1/F-1/S-11 registrations and amendments, relevant Reg A and transaction filings, EFFECT notices, final prospectuses, and withdrawals. A registration alone is not an IPO. Resale registrations and historical IPO references are rejected. Lifecycle changes are scoped to CIK plus SEC registration file number; effectiveness/prospectus filing never means trading has begun.
- **Online discourse:** company IPO news RSS summaries, public Reddit search posts, configured YouTube videos, and optional API-based YouTube discovery. Accessible English caption text is analyzed when available. Video title/description alone is excluded from sentiment.
- **Government contracts:** USAspending awards and optional SAM.gov reconciliation. Unsupported procurement sources and missing keys are disclosed, not presented as nationwide coverage.
- **Sentiment and bias:** an auditable English lexicon, negation handling, content deduplication, equal weighting by origin and then platform, and flags for sponsorship, financial interests, hype, speculation, sparse evidence, and selection bias. Engagement counts do not increase credibility. Insufficient evidence is `unknown`.

Default research watchlist: Anduril, SpaceX, OpenAI, Anthropic, Databricks, and Stripe, plus recent active IPO issuers discovered from filings. Inclusion on the watchlist does not assert an IPO is planned. Configure `WATCH_COMPANIES` to change it.

The supplied Anduril video, https://youtu.be/0BE2AAOlYWI, is a default seed. If captions cannot be retrieved, the report explicitly states that its spoken content was not analyzed. Optional `YOUTUBE_API_KEY` enables bounded discovery; official captions downloads for arbitrary third-party videos cannot be assumed available.

## Run locally

Python 3.12+:

```text
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env`, identify a monitored SEC contact, and configure optional sources. SMTP is disabled by default; no email password is required to collect and review reports.

```text
gov-contract-ipo-monitor check-config
gov-contract-ipo-monitor run-once --report-dir data/reports --checkpoint data/checkpoint/monitor.db
gov-contract-ipo-monitor status
gov-contract-ipo-monitor report --output data/reports
gov-contract-ipo-monitor run
```

`run-once` writes `latest.md` and `latest.json`, saves a durable run receipt, and returns exit code 3 if the required SEC or USAspending collector is incomplete. Optional source gaps remain visible in a degraded report. A SQLite backup captures committed WAL data for recovery.

`run` polls configured sources continuously, publishes a report every minute, and serves:

- `/dashboard`: IPO evidence, sentiment samples, coverage, contract evaluations, and collection status.
- `/api/ipos`, `/api/research`, `/api/collectors`, `/api/candidates`, `/api/rejections`, `/api/alerts`.
- `/healthz`: liveness and component status; `/readyz`: required collectors have succeeded and remain fresh.

The dashboard is intended for a trusted local/private environment. Bind `HEALTH_HOST=127.0.0.1` for local-only access. Do not expose the unauthenticated operational API publicly.

## Hourly hosted job

`.github/workflows/monitor.yml` runs at minute 17 of every hour, on relevant updates to `main`, and through **Run workflow**. GitHub scheduling can be delayed; this is not a real-time guarantee.

Each job:

1. Restores the newest retained `ipo-monitor-state` artifact from this workflow on `main`.
2. Validates and restores only the expected SQLite file; corrupt checkpoints fail visibly.
3. Collects evidence, records errors without erasing previous verified evidence, and writes a research report.
4. Publishes the report to the Actions run summary and `ipo-monitor-report` artifact.
5. Saves a consistent SQLite checkpoint as `ipo-monitor-state` for the next run, including runs with collection gaps.

Jobs are serialized. Reports are retained for 30 days and checkpoints for 90 days. If all checkpoints expire or are deleted, collection history starts afresh and the restore step states this. Download a checkpoint for longer-term archival. GitHub may disable scheduled workflows after 60 days of repository inactivity; inspect the Actions workflow status if collection stops.

Configure repository **Variables** for `SEC_USER_AGENT`, `WATCH_COMPANIES`, `YOUTUBE_VIDEO_URLS`, and `NEWS_FEED_URLS`. Configure optional **Secrets** for `YOUTUBE_API_KEY`, `SAM_API_KEY`, and `TWELVE_DATA_API_KEY`. The hosted job intentionally leaves SMTP disabled. No secrets are committed or written into reports.

## Important limits

SEC discovery currently samples bounded current-feed pages, with durable processed-accession receipts and explicit truncation errors. It covers U.S. public filings, not confidential submissions or every international exchange. A page-limit gap is not proof there are no other IPOs.

News feeds are publisher summaries, Reddit communities are self-selected, and YouTube creators may have sponsorships or financial incentives. Blocked requests, missing English captions, source limits, stale items, and missing API keys are reported. This sample cannot represent all internet sentiment or validate an investment claim.

USAspending is periodically refreshed with overlap and pagination protection; its award search does not expose every cancellation or older modification. SAM reconciliation requires a key. State/local coverage remains incomplete; see `docs/state-local/README.md`.

## Optional contract-qualified email alerts

Set `SMTP_ENABLED=true` and configure the SMTP fields to enable the existing durable outbox. This route requires actual award evidence, deterministic legal-entity identity, substantiated listing evidence, and the configured small-company thresholds (`MAX_PRICE`, `MAX_MARKET_CAP`, `QUOTE_MAX_AGE_HOURS`). These thresholds affect only contract-qualified emails, not the independent IPO tracker.

Corrections cancel unsent originals and are tied to exact identity/evidence. Partial recipient acceptance is retained so only refused recipients are retried. SMTP acceptance is recorded separately from inbox delivery; SMTP cannot guarantee exactly-once delivery across a process crash after remote acceptance.

## Verification

```text
python -m pytest
python -m compileall -q src scripts
```

Tests cover IPO/resale discrimination, scoped lifecycle, large issuers, withdrawal suppression, API normalization/pagination, durable receipts/checkpoints, biased and duplicate discourse, unavailable captions, unsafe source URLs, retries, corrections, SMTP partial acceptance, dashboard/report status, and restart recovery.

Container builds are published after CI. Publication receipts are saved as Actions artifacts rather than committing back to `main`, preventing recursive build workflows. `deployments/container.json` is a historical receipt, not proof of a running service.
