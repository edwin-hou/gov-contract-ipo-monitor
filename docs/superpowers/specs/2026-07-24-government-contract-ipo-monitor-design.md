# Government Contract + Near-Term IPO Monitor

**Status:** Approved architecture, implementation pending  
**Date:** 2026-07-24  
**Primary objective:** Send fast, evidence-backed email alerts when a small company both (1) receives an actual government contract and (2) has a high-confidence near-term path to becoming publicly traded.

## 1. Scope and non-goals

The service monitors U.S. federal, state, and local government award sources together with SEC and issuer listing signals. It prioritizes companies that meet the agreed small-company profile:

- current or proposed share price below **$5**, when available; and
- current or proposed market capitalization below **$300 million**, or an equivalent disclosed valuation/offering-size proxy for a private issuer.

The system is an information and research tool. It does not place trades, recommend purchases, predict price appreciation, or claim that a government contract makes a security attractive.

The first production release provides complete federal collectors and a framework for state/local adapters. State and local procurement is fragmented; coverage is reported per adapter and must never be described as nationwide-complete unless every relevant jurisdiction has a functioning, tested source.

## 2. Alert invariant

An alert may enter the SMTP outbox only when all four gates pass:

1. **Actual-contract gate** — documentary evidence proves an award was made to a named legal entity.
2. **Entity-resolution gate** — the award recipient is deterministically linked to the IPO candidate or public shell.
3. **Near-term-listing gate** — the candidate satisfies the approved high-confidence IPO/listing rule.
4. **Small-company gate** — available evidence supports the agreed price/valuation thresholds.

A solicitation, request for proposals, bid submission, “selected vendor” statement without an award, company-only press release, social post, anonymous report, or fuzzy company-name match cannot pass the gates by itself.

## 3. Contract evidence policy

### 3.1 Accepted contract evidence

A contract is considered actual when at least one of the following evidence classes is present:

**Class A — Structured official award record**

- USAspending prime-award or transaction record;
- SAM.gov Contract Awards record;
- an official state/local award feed with an award identifier and named recipient.

**Class B — Official documentary award evidence**

- signed contract or notice of award hosted on a government domain;
- approved board/council minutes naming the recipient and approving the award;
- official agency award announcement that includes the recipient, awarding body, award date, and contract scope, with either a contract identifier or an amount/ceiling.

Class B exists for the publication interval before a structured database receives the record. It is not an “unverified lead”: the evidence must originate from the awarding government body and state that the award occurred. Company press releases and news reports may be included as secondary context but cannot establish the contract.

### 3.2 Rejected evidence

The validator rejects:

- solicitations, presolicitations, sources-sought notices, RFIs, RFQs, and RFPs;
- apparent awards that are actually grants, loans, subsidies, purchase forecasts, framework eligibility, or vendor registrations unless explicitly enabled in a later product version;
- subcontract claims without a prime-award document or official prime/government confirmation;
- indefinite-delivery vehicles with zero obligation unless a funded task/order is also present;
- awards to an unrelated affiliate, distributor, reseller, or similarly named company;
- records whose award status was cancelled, deleted, rescinded, or superseded before alerting.

### 3.3 Contract facts stored

Every accepted contract evidence object stores:

- source system and canonical source URL;
- retrieval and source-publication timestamps;
- award identifier, modification number, and transaction identifier when present;
- award status and award date;
- awarding agency, subagency, and office;
- recipient legal name, UEI, CAGE code, address, and parent identifiers when present;
- prime/subcontract classification;
- obligated amount, current value, potential ceiling, and whether each amount is disclosed;
- contract type, pricing type, start/end dates, option years, and description;
- raw source payload hash and immutable archived payload.

## 4. Near-term public-listing policy

### 4.1 Automatically qualifying signals

A company passes the near-term-listing gate when one of these primary signals exists and is active rather than withdrawn or terminated:

- public **S-1** or **F-1** registration statement for an initial listing;
- public **Form 1-A** offering statement intended to create a publicly traded security;
- definitive de-SPAC business-combination agreement disclosed in an SEC filing;
- definitive reverse-merger agreement disclosed in an SEC filing that would make the contractor or its parent a public operating company.

### 4.2 High-confidence non-filing route

The approved “Option B” also permits an alert before a registration statement only when at least **two** of the following are independently documented in official issuer, exchange, or underwriter materials:

- named lead underwriter or bookrunner;
- named intended U.S. exchange;
- announced submission of a listing application;
- declared expected offering/listing window within 180 days;
- executed underwriting, merger, or listing agreement.

At least one item must be supported by a source other than the issuer itself. Anonymous media sourcing, executive aspirations, hiring activity, trademark filings, and “considering an IPO” language do not qualify.

### 4.3 Signal invalidation

A previously qualifying listing signal becomes inactive after any of the following:

- SEC withdrawal filing or abandoned offering notice;
- termination of a definitive merger/business-combination agreement;
- exchange rejection or delisting before the contemplated transaction;
- passage of 365 days without an amendment or other confirming event, unless the source gives a longer active period;
- evidence that the transaction already closed and the company is no longer “about to” list. A closing event may be reported only as a final update to an existing alert thread, not as a new candidate alert.

## 5. Small-company and penny-stock qualification

### 5.1 Existing public shell or listed issuer

The candidate qualifies when the latest sufficiently fresh market data shows:

- share price `< $5.00`; and
- market capitalization `< $300,000,000`.

The service records quote time, market session, exchange/OTC venue, shares-outstanding source, and market-data provider. A stale quote cannot silently pass the gate. Default maximum staleness is one trading day for an initial release; providers may enforce tighter limits.

### 5.2 Private operating company pursuing an IPO

A private candidate qualifies when:

- disclosed proposed/estimated equity valuation is below $300 million; or
- maximum disclosed offering size is below $300 million; or
- a recent financing or transaction value from a primary filing is below $300 million.

A disclosed proposed share price below $5 is reported, but lack of a proposed per-share price does not disqualify an otherwise confirmed sub-$300 million candidate. If neither price nor any defensible valuation/offering proxy is available, the candidate remains stored but no email is sent.

### 5.3 Data-provider policy

Market data is obtained through a pluggable provider interface. The free default is **Twelve Data Basic** for current U.S. exchange-listed quotes and volume, combined with SEC filings/company facts for shares outstanding and issuer fundamentals. The provider budget is eight API credits per minute and 800 per day; requests occur only after the contract and listing gates have produced a plausible matched candidate, so the service does not attempt to stream the whole market.

OTC candidates remain in scope, but the service does not automate extraction from OTC Markets web pages because their published terms restrict database construction and redistribution. An OTC candidate may pass only when a configured lawful market-data provider covers the symbol or primary issuer/transaction documents provide a sufficiently current price and share-count basis. Missing OTC price coverage prevents the small-company gate from passing; it never produces a guessed quote or market cap.

Every provider exposes source timestamps, venue, coverage status, and usage terms. Provider failure, stale data, or inconsistent symbols fail closed.

## 6. Architecture

The repository is a Python 3.12 service runnable directly or in Docker. The initial deployment uses a single process with isolated asynchronous workers and SQLite in WAL mode. Interfaces are designed so collectors or the outbox can later move to separate processes without changing validation rules.

### 6.1 Components

1. **Scheduler**
   - launches collectors at source-appropriate intervals;
   - adds jitter to avoid synchronized traffic;
   - enforces per-host rate limits and backoff.

2. **Collectors**
   - `sec_edgar`: current filings and filing documents;
   - `usaspending`: new/modified federal contract awards and transactions;
   - `sam_contract_awards`: strategic confirmation and fields not present elsewhere, respecting API-key quotas;
   - `state_local/*`: one adapter per official portal or feed;
   - `market_data/*`: price, shares, market cap, volume, and venue metadata.

3. **Normalizer**
   - converts source payloads into versioned internal records;
   - preserves raw payloads and hashes;
   - assigns stable source-level deduplication keys.

4. **Contract validator**
   - classifies evidence as Class A, Class B, or rejected;
   - distinguishes base awards, modifications, funded orders, IDVs, solicitations, grants, and cancellations;
   - computes amount semantics without treating a ceiling as current revenue.

5. **Entity resolver**
   - links award recipients to SEC filers, private IPO issuers, shells, parents, and subsidiaries;
   - prioritizes UEI, CAGE, SEC CIK, EIN, exact legal name, address, and disclosed corporate relationships;
   - forbids alerting on fuzzy-name similarity alone.

6. **IPO signal validator**
   - parses filing type, filing items, exhibits, status, and transaction parties;
   - tracks amendments, withdrawals, terminations, and transaction closing;
   - applies the primary-signal and two-factor non-filing rules.

7. **Qualification and risk engine**
   - applies price/valuation thresholds;
   - extracts and scores risks without converting the score into a buy/sell recommendation.

8. **Alert gate**
   - evaluates the four-part invariant in one database transaction;
   - creates a deterministic alert fingerprint from company, award, and listing transaction;
   - suppresses duplicates and low-information modifications.

9. **SMTP outbox worker**
   - renders plain-text and HTML email;
   - sends immediately after commit to the outbox;
   - retries temporary failures with bounded exponential backoff;
   - records SMTP acceptance time without claiming recipient inbox delivery.

10. **Health and audit layer**
    - structured JSON logs;
    - collector heartbeat and last-success timestamps;
    - immutable evidence history and decision trace;
    - `/healthz` and `/readyz` endpoints.

### 6.2 Data flow

```text
Official contract sources ─┐
                           ├─> raw records -> normalize -> contract validator ─┐
SEC / listing sources ─────┤                                                    ├─> entity resolver
                           └─> raw records -> normalize -> IPO validator ──────┘
                                                                                 |
Market data + filings -----------------------------------------------------------┤
                                                                                 v
                                                                  qualification + risks
                                                                                 |
                                                                            alert gate
                                                                                 |
                                                                           SMTP outbox
```

## 7. Polling and latency targets

Polling intervals are configurable and never exceed a source’s published fair-access limits.

- SEC current-filings discovery: every 30 seconds.
- USAspending recent award/transaction discovery: every 5 minutes.
- SAM.gov Contract Awards: quota-aware; used for confirmation and periodic reconciliation rather than wasteful high-frequency polling on a ten-request/day personal key.
- State/local API or RSS adapters: typically every 5–30 minutes.
- HTML/document adapters: source-specific, normally every 15–60 minutes.
- SMTP outbox: event-driven wake-up with a five-second safety poll.

Target internal latency, measured from collector receipt to SMTP acceptance:

- median under 10 seconds for already normalized, automatically validated candidates;
- under 60 seconds when one additional source fetch is required;
- longer only when source throttling, document retrieval, or retry behavior requires it.

Source publication delay is measured separately and shown in the alert.

## 8. Email report specification

### 8.1 Subject

`[CONFIRMED CONTRACT + IPO] <Company> | <Agency> | <Award value> | <Listing route>`

### 8.2 Required sections

1. **Why this alert fired**
   - one-sentence explanation of the contract proof, entity link, and listing proof.

2. **Company and listing status**
   - legal company name, parent/subsidiary relationship, CIK, ticker if any, venue, listing route, expected exchange, filing/accession identifiers, filing status, and expected timing if disclosed.

3. **Contract details**
   - agency, award identifier, award date, recipient, scope, obligated amount, potential ceiling, contract period, options, and prime/subcontract status.

4. **Small-company screen**
   - latest/proposed price, valuation/market cap, source timestamps, shares outstanding, and whether values are delayed or estimated.

5. **Materiality context**
   - contract obligation and ceiling as percentages of market cap/valuation and latest available annual revenue, clearly labeling unavailable or incomparable data.

6. **Risk report**
   - award cancellation and termination rights;
   - IDIQ, task-order, option-year, and ceiling-versus-obligation uncertainty;
   - customer concentration and execution requirements;
   - financing, dilution, warrants, convertibles, preferred shares, and reverse-split history;
   - cash runway, debt, going-concern language, and auditor issues;
   - reporting delinquency, shell-company, SPAC-redemption, and transaction-completion risk;
   - price staleness, low volume, wide spread, OTC status, and manipulation/promotional risk;
   - entity-match limitations and any remaining data gaps.

7. **Evidence and timestamps**
   - direct primary-source links;
   - source publication time, first observed time, validation completion time, and SMTP acceptance time;
   - evidence class and deterministic confidence explanation.

8. **Disclaimer**
   - research alert only; not investment advice; verify independently before trading.

### 8.3 Updates and corrections

The system sends a follow-up in the same logical alert thread only when:

- a material contract amount or status changes;
- an IPO filing is amended, withdrawn, declared effective, or terminated;
- entity mapping is corrected;
- a significant newly disclosed risk changes the original report.

Every correction states what changed and preserves the original evidence trail.

## 9. Persistence model

SQLite tables are versioned through migrations and include:

- `source_records`
- `contract_evidence`
- `listing_signals`
- `entities`
- `entity_identifiers`
- `entity_relationships`
- `market_snapshots`
- `risk_findings`
- `candidate_matches`
- `gate_decisions`
- `alerts`
- `outbox_messages`
- `collector_state`
- `dead_letters`

Raw evidence is append-only. Derived records may be superseded but are not destructively overwritten.

## 10. Configuration and deployment

The same repository supports:

- direct execution on Linux, macOS, or Windows;
- Docker and Docker Compose;
- Linux `systemd` service;
- any cloud host capable of running a persistent container and mounting durable storage.

Required configuration is supplied through environment variables or a local `.env` file excluded from version control:

- SMTP host, port, security mode, username, app password/token, sender, and recipients;
- SEC-compliant contact user agent;
- optional SAM.gov API key;
- Twelve Data API key for the default free listed-equity quote provider;
- database path and evidence archive path;
- collector enablement and intervals;
- price/valuation thresholds and staleness limits.

Secrets are never written to logs, database decision traces, fixtures, or email bodies.

## 11. Failure handling

- Network and 5xx failures use exponential backoff with jitter.
- 4xx authentication/configuration failures disable only the affected collector and raise a health error.
- Parsing changes quarantine the payload and create a dead-letter record rather than silently dropping it.
- SMTP failures retain the message in the durable outbox.
- A collector outage cannot cause previously seen records to be re-alerted after restart.
- Clock skew is detected by comparing source timestamps with system time.
- Database writes use transactions; WAL checkpoints and backups are configurable.

## 12. Testing strategy

### 12.1 Unit tests

- contract classification and rejection rules;
- IPO-signal activation/invalidation;
- penny-stock and private-company qualification;
- amount semantics and materiality calculations;
- deterministic entity resolution;
- alert fingerprinting and deduplication;
- risk extraction and email rendering.

### 12.2 Integration tests

- recorded SEC, USAspending, SAM.gov, and state/local fixtures;
- end-to-end candidate that passes all four gates;
- near misses that fail exactly one gate;
- cancellation, withdrawal, and correction paths;
- SMTP test server and retry behavior;
- restart recovery from SQLite outbox and collector cursors.

### 12.3 Adversarial fixtures

- two companies with nearly identical names;
- parent receives award but IPO subsidiary does not benefit;
- zero-obligation IDV advertised as a large contract;
- ceiling amount confused with current obligation;
- company press release preceding official award evidence;
- withdrawn S-1, terminated SPAC, stale quote, reverse split, and delinquent filer;
- award modification that decreases or cancels value.

## 13. Acceptance criteria

The first release is acceptable when:

1. A fixture-backed end-to-end test sends one email only after all four gates pass.
2. Every rejected near miss records a human-readable gate decision.
3. No test built from a solicitation, company-only announcement, fuzzy-name match, zero-obligation IDV, withdrawn listing signal, or missing small-company evidence sends an email.
4. Duplicate source records and process restarts do not duplicate alerts.
5. SMTP acceptance occurs within ten seconds of an already validated outbox insertion under normal test conditions.
6. The email includes all required evidence, timestamps, materiality context, and risk categories.
7. Docker, direct Python execution, and a persistent-volume restart are documented and tested.
8. Federal collectors are operational; state/local coverage is displayed as an explicit adapter inventory rather than an unsupported nationwide claim.

## 14. Primary-source references used by the design

- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- SEC fair-access guidance: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
- USAspending API endpoints: https://api.usaspending.gov/docs/endpoints
- USAspending API tutorial: https://api.usaspending.gov/docs/intro-tutorial
- SAM.gov Contract Awards API: https://open.gsa.gov/api/contract-awards/
- SEC Form S-1 description: https://www.sec.gov/submit-filings/forms-index/aboutformsforms-1pdf
- SEC Form F-1 description: https://www.sec.gov/submit-filings/forms-index/aboutformsformsf-1pdf
- Twelve Data individual pricing and free-tier limits: https://twelvedata.com/pricing
- Twelve Data U.S. equities coverage: https://support.twelvedata.com/en/articles/9935903-us-equities-market-data
- OTC Markets terms of service: https://www.otcmarkets.com/terms-of-service
