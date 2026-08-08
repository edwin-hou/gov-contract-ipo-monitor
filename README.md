# Government Contract + Near-Term IPO Monitor

A fail-closed Python service that sends an email only when it can substantiate **both**:

1. a small company received an actual government contract, and
2. that same legal entity has a high-confidence near-term U.S. public-listing path.

The monitor is designed for speed without treating solicitations, corporate press releases, rumors, fuzzy-name matches, or unfunded contract vehicles as investable events.

> Research tool only. This project does not place trades, recommend securities, or predict returns. Penny stocks, shells, SPAC transactions, and pre-IPO offerings can be illiquid, highly dilutive, manipulated, or lost entirely.

## What triggers an email

All four gates must pass in one durable database transaction:

- **Actual contract:** structured USAspending/SAM award evidence or qualifying official government award documentation.
- **Deterministic entity match:** UEI, CAGE, exact legal name plus address, exact legal name independently anchored to a unique official USAspending UEI, or a primary-source corporate relationship. Fuzzy names alone never pass.
- **Near-term listing:** active initial S-1/F-1, qualifying Form 1-A, definitive de-SPAC/reverse-merger agreement, or the approved Option B two-factor route with independent official corroboration.
- **Small company:** public price under `$5` and market cap under `$300M`, or a private primary-filing valuation/offering proxy below `$300M`.

Withdrawals, terminations, cancelled contracts, and material corrections generate correction emails tied to the original alert.

## Included collectors

- SEC EDGAR current-filings Atom feeds and filing documents
- SEC submissions API for official issuer business addresses
- USAspending advanced award search with full pagination
- USAspending recipient lookup for exact-name-to-UEI identity enrichment
- SAM.gov Contract Awards, including deleted-contract reconciliation when a key is configured
- Twelve Data quote/statistics enrichment when a free API key is configured
- Explicit state/local adapter inventory and extension interface

USAspending collection keeps a durable high-water mark and intentionally overlaps recent days on each poll so late-arriving changes can be re-observed without rescanning an arbitrarily large historical window. Source-record hashing and database deduplication make that overlap safe.

State and local procurement is fragmented. The first release does **not** claim nationwide-complete local coverage. Add only official, tested adapters and expose their status through the inventory command.

## Install

Python 3.12 or newer is required.

```bash
python -m venv .venv
# Linux/macOS
source .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -e ".[dev]"
cp .env.example .env
```

The checked-in `.env.example` is prefilled with Edwin's contact address and Gmail SMTP settings, but **not** with any password. Create a Gmail App Password and put it only in your local `.env`.

Configure `.env`, then validate it:

```bash
gov-contract-ipo-monitor check-config
gov-contract-ipo-monitor init-db
gov-contract-ipo-monitor run-once
gov-contract-ipo-monitor run
```

## Health, dashboard, and rejection reasons

The HTTP server is available by default on port `8080`:

- `GET /healthz` — liveness and in-memory component status
- `GET /readyz` — readiness after the database, SEC, and USAspending collectors succeed
- `GET /dashboard` — read-only operational dashboard
- `GET /api/candidates` — recent evaluated contract/listing pairs and gate decisions
- `GET /api/rejections` — recent rejected candidates with the failed gates and reasons
- `GET /api/alerts` — recent confirmed alerts
- `GET /api/collectors` — durable collector status/high-water marks

The dashboard intentionally shows only operational summaries, not raw archived source payloads or credentials.

## Required environment variables

```dotenv
SEC_USER_AGENT=EdwinHouGovContractIPOMonitor/0.2 edwin.s.hou@gmail.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USERNAME=edwin.s.hou@gmail.com
SMTP_PASSWORD=replace-with-google-app-password
SMTP_SENDER=edwin.s.hou@gmail.com
SMTP_RECIPIENTS=edwin.s.hou@gmail.com
```

Optional/configurable:

```dotenv
SAM_API_KEY=
TWELVE_DATA_API_KEY=
DATABASE_PATH=data/monitor.db
EVIDENCE_ARCHIVE_PATH=data/evidence
SEC_INTERVAL_SECONDS=30
USASPENDING_INTERVAL_SECONDS=300
USASPENDING_INITIAL_LOOKBACK_DAYS=7
USASPENDING_OVERLAP_DAYS=2
USASPENDING_MAX_PAGES=250
SAM_INTERVAL_SECONDS=21600
SMTP_POLL_SECONDS=5
MAX_PRICE=5
MAX_MARKET_CAP=300000000
QUOTE_MAX_AGE_HOURS=24
HEALTH_HOST=0.0.0.0
HEALTH_PORT=8080
```

The SEC user agent must include a real monitored contact email. Never commit API keys, SMTP App Passwords, or other credentials.

## Docker

```bash
cp .env.example .env
# add the App Password/API keys only to .env
docker compose up --build -d
docker compose logs -f monitor
```

SQLite and the evidence archive live in the persistent `monitor-data` volume.

## Email contents

Every confirmed alert includes:

- company, CIK, ticker/venue, route, filing/accession, expected exchange and timing;
- agency, award identifier, date, scope, obligation, current value, ceiling, term, and prime/subcontract status;
- price and market-cap or private valuation evidence with timestamps;
- obligation and ceiling relative to valuation and available revenue;
- cancellation, IDIQ/option, concentration, execution, dilution, warrant, convertible, reverse-split, cash, debt, going-concern, auditor, reporting, liquidity, OTC, SPAC-redemption, and manipulation risks when available;
- direct primary-source URLs and evidence timestamps;
- an explicit research-only disclaimer.

## Commands

```bash
gov-contract-ipo-monitor --help
gov-contract-ipo-monitor check-config
gov-contract-ipo-monitor init-db --database data/monitor.db
gov-contract-ipo-monitor run-once
gov-contract-ipo-monitor run
gov-contract-ipo-monitor adapters
```

## Testing

```bash
pytest
python -m compileall -q src
```

The test suite covers the four-gate invariant, adversarial near misses, deterministic resolution, the official UEI identity bridge, stale quotes, initial-vs-resale registration parsing, filing-derived valuation extraction, withdrawals, corrections, USAspending multi-page retrieval and truncation protection, deduplication, SQLite restart recovery, SMTP retries, official-source normalization, HTTP retry behavior, archive immutability, dashboard rejection reasons, health checks, and CLI behavior.

## Architecture

```text
USAspending/SAM ---------> immutable contract evidence -------------------------+
       |                                                                      |
       +--> official recipient/UEI identity bridge                            |
                                                                              v
SEC EDGAR --------------> listing evidence + filing facts -----------> deterministic entity match
                                                                              |
market data -----------------------------------------------------------> qualification gates
                                                                              |
                                                                              v
                                                           candidate + rejection trace
                                                                              |
                                                                   all four gates pass
                                                                              |
                                                                              v
                                                                  durable SMTP outbox
```

SQLite uses WAL mode. Raw source records are append-only and content-addressed evidence files are never overwritten. Derived records are versioned. SMTP acceptance is recorded separately from recipient inbox delivery.

## State/local adapters

See [`docs/state-local/README.md`](docs/state-local/README.md). An adapter must use an official source, identify an awarded contract rather than a solicitation, retain primary-source evidence, and pass the same validation gates as federal records.

## Design specification

The approved design is in [`docs/superpowers/specs/2026-07-24-government-contract-ipo-monitor-design.md`](docs/superpowers/specs/2026-07-24-government-contract-ipo-monitor-design.md).
