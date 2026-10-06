# Company, IPO and Trade Research Monitor

Tracks existing public companies and IPO evidence, samples public internet commentary, reads world news, and includes government contracts as supplementary evidence. Produces conditional trade research over **5–15 trading sessions**. Government awards and penny-stock thresholds are not required for public-company research or the broad IPO tracker.

The local Hermes deployment emails only new or materially changed conditional opportunities approved by an evidence analyst, through Gmail OAuth to a confirmed recipient. Hourly collection does not produce hourly email. Trade ideas contain entry conditions and risk levels; no brokerage orders are placed. The original contract-qualified SMTP route remains a separate optional feature and is disabled in this deployment.

## What it tracks

- **SEC IPO evidence:** S-1/F-1/S-11 registrations and amendments, relevant Reg A and transaction filings, EFFECT notices, final prospectuses, and withdrawals. A registration alone is not an IPO. Resale registrations and historical IPO references are rejected. Lifecycle changes are scoped to CIK plus SEC registration file number; effectiveness/prospectus filing never means trading has begun.
- **Existing companies:** a reviewed global-issuer shortlist selected from comparable reported total-revenue growth and positive GAAP/IFRS net income. Latest issuer releases are bundled with their actual amounts, fiscal periods, currencies, source links and limitations. SEC company facts refresh complete comparable contexts every seven days. Quarter/YTD/year comparisons are kept distinct; older SEC results cannot replace newer reviewed quarters.
- **New listed-company discovery:** a bounded scan of current SEC 10-Q/10-K filings and the SEC ticker/exchange directory, with up to five new issuer fact requests per collection. A comparable fresh quarter with at least 10% reported total-revenue growth and positive reported GAAP profit produces a `qualified_review` finding. This remains a wait for security selection, trading currency, price, liquidity, valuation and company-news review; it is not an automatically tradable addition to the curated list. Exact financial source contexts and their original report dates survive checkpoints.
- **Daily prices and trade ideas:** explicitly mapped listing symbols, venues and currencies, at least 60 completed daily bars, a global equity benchmark, trend/relative strength, liquidity and volatility. Freshness, issuer identity, financial provenance and corporate-action checks gate every setup. A conditional buy includes an entry trigger, invalidation and a 2R research target; weak trends can suggest reducing exposure only if already owned. Missing or contradictory evidence produces `wait` with reasons.
- **World news:** bounded BBC, Guardian, Al Jazeera and New York Times feeds. Explicit rates, inflation, geopolitics, energy, export-controls, supply-chain and regulation themes are associated with configured company exposures. Fresh reports from at least two publishers are required; relevant risk reports tighten entry limits. Headline selection and relevance are disclosed interpretations, without inventing a verified event or market direction.
- **Online discourse:** general company-news RSS summaries, public Hacker News stories and comments, approved Reddit OAuth posts/comments, configurable public forum RSS/Atom, configured YouTube videos, and optional API-based YouTube discovery. Accessible English caption text is analyzed when available. Video title/description alone is excluded from sentiment. Exact cashtags supplement company names without treating short ticker words as ordinary mentions.
- **Government contracts:** USAspending awards and optional SAM.gov reconciliation. Unsupported procurement sources and missing keys are disclosed, not presented as nationwide coverage.
- **Sentiment and bias:** an auditable English lexicon, negation handling, content deduplication, equal weighting by origin and then platform, and flags for sponsorship, financial interests, hype, speculation, sparse evidence, and selection bias. Engagement counts do not increase credibility. Insufficient evidence is `unknown`.

The October 5, 2026 public-company review includes **MU, NVDA, PLTR, AVGO, MELI, TSM, META, ASML, MSFT and 0700.HK**. The filter is at least 10% year-over-year total-revenue growth and positive reported profit; the entry screen further requires a 5% net margin. Ranking is by growth, then margin. This is a researched shortlist concentrated in technology/AI, not an exhaustive screen or a claim to identify the world's fastest-growing or most-profitable companies. Source links, exclusions and accounting limitations are in each report.

`WATCH_SYMBOLS` optionally selects from that reviewed catalogue. New instruments require explicit listing/price-provider metadata and sourced comparable financial results; unknown symbols fail configuration rather than guessing an exchange or currency. Reported financial currency and trading currency are preserved separately. Tencent's native HKD history is supported, but trade ideas wait for a comparable HKD benchmark or validated FX-adjusted relative strength. Default U.S. listings cover issuers from multiple countries and do not imply native exchange execution worldwide.

Anduril, SpaceX, OpenAI, Anthropic, Databricks and Stripe, plus recent active IPO issuers, remain supplementary private-company research targets. Inclusion does not assert an IPO is planned. Configure `WATCH_COMPANIES` to change those names. Public companies enter commentary queries first; the default cap is 30 companies per run.

Discovered issuers retain their legal names. Conservative aliases remove trailing legal suffixes from names with multiple words, so articles about “TRex Bio” can match “TRex Bio, Inc.” without matching unrelated substrings.

Social-profile URL words and subsidiary navigation labels alone do not establish a parent-company mention; for example, a promoter's Instagram link is not treated as Meta investment commentary.

The supplied Anduril video, https://youtu.be/0BE2AAOlYWI, is a default seed. If captions cannot be retrieved, the report explicitly states that its spoken content was not analyzed. Optional `YOUTUBE_API_KEY` enables bounded discovery; official captions downloads for arbitrary third-party videos cannot be assumed available.

Hacker News collection uses the public Algolia API without an account or key, samples one recent page per watched company, and retains comment text, timestamps, and author-account origins. Set `HACKER_NEWS_ENABLED=false` to disable it. Search selection, community selection, quoted opinions, and unverified authors remain explicit limitations.

Reddit requires approved API access: set `REDDIT_ACCESS_TOKEN`, or approved `REDDIT_CLIENT_ID`/`REDDIT_CLIENT_SECRET` with an optional `REDDIT_REFRESH_TOKEN`. Authentication uses Reddit's official token and read endpoints. Per-company posts, per-post comments and total run comments are bounded; tokens never appear in source URLs or receipts. Anonymous requests may be blocked and are reported as gaps. The collector does not bypass access controls. `FORUM_FEED_URLS` defaults to the public ValuePickr investing forum feed; add or replace public RSS/Atom URLs as needed. Only content matching watched identities is retained, with timestamps, author/site origins and forum selection/quotation bias flags.

Default daily histories use the public Nasdaq site endpoint for supported U.S. listings and public Yahoo charts for explicitly mapped alternatives. These undocumented site endpoints have no API SLA and may be blocked or changed. `TWELVE_DATA_API_KEY` enables an optional documented provider when the reviewed listing has an explicit mapping and the account plan covers it. No intraday/executable quote is inferred from daily history. Corporate-action adjustment is unknown for public series; material raw-price jumps require review.

Current informational quotes are collected separately from completed daily bars. Before analyst review and again before publication, the quote must match the security, trading currency and reviewed venue, and its observation must be no more than five minutes old. Open-session provider timestamps must also fall within the reviewed delay plus freshness tolerance. The latest completed session close can support explicitly labelled research while the regular market is closed; it does not become a live or executable quote. Missing timestamps, failed refreshes and unsupported venue/delay coverage withhold a setup. A live broker quote and cost preview are still required before entry.

Each candidate has short evidence briefs with source links and dates: comparable reported revenue/profit, completed price trends, relevant company/world-news summaries and available commentary. Missing commentary stays unknown. Headlines are publisher reports, sampled tone is not an opinion poll, and neither is independently established truth. The analyst may judge these inputs insufficient and return `wait`; even an approval can be mistaken.

Entry setups use completed regular sessions, with a 15-minute close buffer and reviewed U.S. exchange holidays/early closes for 2026–2028. An unsupported venue/calendar cannot authorize a buy. Strategies include an entry cap, five-session entry expiry, the quoted invalidation/target, and conditional regular-session windows shown in Nashville time (`America/Chicago`). These windows are scheduling references, not predicted profitable times. Review after five sessions and exit/reassess after fifteen sessions are anchored to an actual verified fill; displayed future dates are illustrative until a fill exists. Position size requires a chosen loss budget and cash limit; no order quantity or fill is inferred. Risk/reward is recalculated after tick rounding. Frozen paper reference plans can report an entry reference, target, invalidation or time review against later completed closes, always distinguishing those references from actual trades. Daily closes cannot provide intraday execution alerts.

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

`run-once` writes `latest.md` and `latest.json`, saves a durable run receipt, and returns exit code 3 if the required SEC or USAspending collector is incomplete. Optional source gaps remain visible in a degraded report. A SQLite backup captures committed WAL data for recovery. Publishing and restoring checkpoints enforce the same 250 MB bound, SQLite integrity and the original required schema; an invalid checkpoint cannot replace existing history.

`run` polls configured sources continuously, publishes a report every minute, and serves:

- `/dashboard`: conditional trade ideas, researched companies, world news, IPO evidence, sentiment samples, coverage, contract evaluations and collection status.
- `/api/companies`, `/api/trades`, `/api/world-news`, `/api/ipos`, `/api/research`, `/api/collectors`, `/api/candidates`, `/api/rejections`, `/api/alerts`. These read saved reports and never place orders.
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

The hosted job uses the supported 50 MiB SEC document bound so larger current filings can be archived; override `SEC_MAX_DOCUMENT_BYTES` through a repository variable if needed. Local defaults remain 20 MiB unless configured, and the maximum accepted bound is 50 MiB.

Jobs are serialized. Reports are retained for 30 days and checkpoints for 90 days. If all checkpoints expire or are deleted, collection history starts afresh and the restore step states this. Download a checkpoint for longer-term archival. GitHub may disable scheduled workflows after 60 days of repository inactivity; inspect the Actions workflow status if collection stops.

Configure repository **Variables** for `SEC_USER_AGENT`, `WATCH_SYMBOLS`, `WATCH_COMPANIES`, `YOUTUBE_VIDEO_URLS`, `NEWS_FEED_URLS` and `FORUM_FEED_URLS`. Configure optional **Secrets** for the Reddit access fields, `YOUTUBE_API_KEY`, `SAM_API_KEY` and `TWELVE_DATA_API_KEY`. The hosted job intentionally leaves SMTP disabled. No secrets are committed or written into reports. Price histories, company financial observations, world headlines and stable trade-idea versions are compressed/versioned inside the same checkpoint as IPO and commentary evidence.

## Important limits

SEC discovery saves exact-form current-feed pages to a durable filing queue, then catches up from official published daily indexes. The initial index scope is frozen at seven calendar days before the first catalogue poll; later runs resume that scope across checkpoints. Each run captures up to three daily indexes and discovers every configured bounded current feed before processing documents. It then processes up to `SEC_MAX_PAGES × 40` queued documents per configured form, oldest first, with a work budget of 80% of the hard source deadline (capped at one hour). Reaching this budget between completed documents leaves a visible durable backlog; an in-flight HTTP failure or hard deadline still fails collection. SEC publishes these indexes nightly after 10 p.m. Eastern, so index catch-up does not promise immediate discovery of every filing. Configured form scope is saved with the catalogue; newly enabled forms replay verified saved index bytes, while retained disabled forms do not count as active backlog. Index-only dates are explicitly marked as date-only. Official Filing Date and exact Accepted timestamps are stored separately; an acceptance after the dissemination cutoff may precede its filing date. Uncertain lifecycle order cannot authorize an active IPO.

A full current-feed page window is reported as a bounded real-time sample; it no longer discards discovered filings or fails a run solely because older work remains queued. Reports show pending documents, captured/published index dates and catch-up status. HTTP, malformed-source, document, archive and checkpoint failures still fail visibly. Earlier historical coverage flags remain unresolved. This scope covers configured U.S. public filing forms, not earlier complete history, confidential submissions or every international exchange.

`SEC_MAX_DOCUMENT_BYTES` applies the same limit to SEC response streaming, decompression, and filing text. It defaults to 20 MiB (20971520 bytes), may be configured up to 50 MiB, and reports larger filings as collection gaps. Other sources retain their own limits.

Relevant SEC receipts require valid archived filing and index documents inside the checkpoint. Missing or damaged archives are fetched again while retaining prior evidence versions. Explicit truncation errors from older saved runs migrate to unresolved historical coverage flags; a successful current poll does not erase them.

News feeds are publisher summaries, Hacker News and Reddit communities are self-selected, and YouTube creators may have sponsorships or financial incentives. Author accounts do not establish independent people. Blocked requests, missing English captions, source limits, stale items, and missing API keys are reported. This sample cannot represent all internet sentiment or validate an investment claim.

The trade method is an auditable screening rule with unmeasured predictive accuracy. It does not estimate fair valuation, reconcile every corporate action, check a personal portfolio or establish guaranteed returns. Financial evidence expires 120 days after reporting and must describe a recent period; expired results remain visible but cannot qualify a trade. SEC custom XBRL tags can prevent automatic refresh, especially for foreign issuers; reviewed issuer snapshots need periodic primary-source research updates. A new reviewed bundle can replace older checkpoint results, while reopening the same bundle cannot refresh their report dates. World-news associations are keyword/exposure inferences and may miss important events. A future gap, spread, fee or slippage can exceed an invalidation level. Fresh market quotes and linked evidence must be reviewed before acting.

USAspending is periodically refreshed with overlap and pagination protection; its award search does not expose every cancellation or older modification. SAM reconciliation requires a key. State/local coverage remains incomplete; see `docs/state-local/README.md`.

## Optional contract-qualified email alerts

Set `SMTP_ENABLED=true` and configure the SMTP fields to enable the existing durable outbox. This route requires actual award evidence, deterministic legal-entity identity, substantiated listing evidence, and the configured small-company thresholds (`MAX_PRICE`, `MAX_MARKET_CAP`, `QUOTE_MAX_AGE_HOURS`). These thresholds affect only contract-qualified emails, not the independent IPO tracker.

Corrections cancel unsent originals and are tied to exact identity/evidence. Partial recipient acceptance is retained so only refused recipients are retried. SMTP acceptance is recorded separately from inbox delivery; SMTP cannot guarantee exactly-once delivery across a process crash after remote acceptance.

Unknown SMTP DATA outcomes and expired delivery leases are held as `unknown` rather than blindly resent. Explicitly rejected or pre-send transient failures can retry, while permanent rejections stop.

## Background Hermes opportunity alerts

The local deployment uses the existing hourly Hermes `no_agent` script job at minute 45 under its existing watchdog. GitHub collects at minute 17. The script downloads actual current-main report artifacts and verifies recency, source coverage and report identity, then refreshes informational quotes. These are separate collection and review steps; neither requires a Codex chat automation. Local review and Gmail delivery require this computer and its Hermes scheduler to be running. A delayed hosted schedule permits at most one catch-up dispatch per source report, never behind queued/running or failed collection jobs. Deployment instructions and public helper templates are in [deploy/hermes/README.md](deploy/hermes/README.md); reuse the existing scheduler rather than installing a duplicate.

Only eligible candidates enter an isolated analyst request. This monitor pins **GPT-5.6 Sol (`gpt-5.6-sol`) with medium reasoning**, verified in the authenticated account's model catalogue, using the existing Hermes Codex subscription credentials. It does not change Hermes' global model or other jobs, enable a provider fallback, or give the analyst tools or order access. All supplied web/forum text is treated as evidence rather than instructions. A durable call claim precedes each attempt, with at most **four attempts per UTC day**, including failed or ambiguous attempts. Unchanged substantive evidence reuses a cached decision; expired reviews cannot authorize mail. No eligible candidate means no model call.

The request, output text, stream and runtime are bounded. The Codex OAuth endpoint does not accept a strict provider-side `max_output_tokens` cap: the worker enforces its output bound locally and records actual usage. Four attempts, medium reasoning and timeouts reduce avoidable allowance use; they do not guarantee a fixed amount of usage. The model's opinion is unvalidated and can be wrong. An approval is a conditional research judgement, without guaranteed profitability or calibrated odds.

The `ai_approved_only` delivery policy suppresses routine reports, waits and unapproved candidates. A new or materially changed approved opportunity includes a brief rationale, counterargument, linked evidence, quote date/type, exchange/currency, entry cap, invalidation, target and approximate strategy. The email presents readable HTML cards with a plain-text fallback. A PDF attachment contains the detailed plan, clickable evidence and coverage limitations; the exact JSON audit copy is also attached. No remote images, trackers or external stylesheets are embedded. The deployment records **zero holdings**: it never presumes a buy, position or fill, and does not send an unrequested short or an owner-only sell instruction. Fill-anchored exit references become relevant only after an independently verified purchase.

`notifications.EmailOutbox` persists immutable recipient-bound MIME bytes, expiry, delivery leases and receipts in SQLite. Pending alerts must retain current analyst and quote authority; stale or invalidated messages cannot be sent as fresh opportunities. `gmail_delivery.GmailOAuthTransport` reuses the owning Hermes runtime's native locked token refresh helper, without committing credentials or using SMTP. It verifies the authenticated sender and approved recipient before sending. Provider acceptance alone is insufficient: decoded plain/HTML message text, the alternative MIME structure and the exact PDF/JSON attachments must match readback. Versioned content digests preserve historical delivery receipts without rewriting sent messages. A linked provider message ID permits auditing Gmail's transport-generated Message-ID while preserving both identities. Ambiguous sends remain fenced for read-only reconciliation; durable sent rows recover notice history after a crash.

## Verification

Checkpoint-selection tests execute the production helper with Node.js; install Node alongside Python when running the full development suite. Hosted checkpoint selection uses verified upload and workflow creation times rather than assuming artifact IDs are chronological.

```text
python -m pytest
python -m compileall -q src scripts
```

Tests cover IPO/resale discrimination, scoped lifecycle, large issuers, withdrawal suppression, API normalization/pagination, durable receipts/checkpoints, biased and duplicate discourse, unavailable captions, unsafe source URLs, retries, corrections, SMTP partial acceptance, dashboard/report status and restart recovery. Public-market checks cover quarter/year contexts, restatements, financial/quote identity, currencies and venues, incomplete/current/future bars, mismatched benchmark sessions, raw-price jumps, macro risk tightening, wait states, ordered trade levels and market checkpoint restoration.

Delivery tests inject lost provider acknowledgements, crashes after acceptance, wrong recipient/content readback, rewritten Message-ID, expiry, permanent rejection, safe transient retries and repeated polling. Reference-plan tests cover entry gaps, frozen levels, evidence withdrawal, expiry and conditional target/invalidation/time reviews without inventing holdings.

Analyst tests cover real evidence types, quote identity/freshness, material-context cache changes, durable attempt limits, malformed/duplicate model JSON, unknown evidence references, pending-alert authority and delivered-notice recovery. Worker protocol tests mock provider responses and check a single tools-free request, bounded streams, refusals, incomplete responses, redacted errors and no automatic retry. Passing these checks establishes software behaviour, not live provider availability or investment accuracy.

Container builds are published after successful same-repository `main` push CI. Each revision tag and immutable digest reference use the checked-out source commit and verified registry digest. Only a source commit still matching the current `main` head can promote that digest to `latest`, with registry readback and concurrent main advancement recorded. Publication receipts are saved as Actions artifacts rather than committing back to `main`, preventing recursive build workflows. `deployments/container.json` is a historical receipt, not proof of a running service.
