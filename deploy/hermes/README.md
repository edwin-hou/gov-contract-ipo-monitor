# Windows Hermes deployment

These helper templates connect the hosted collector to an existing Hermes installation. They do not create another scheduler, change the global Hermes model, place orders or enable SMTP. The existing GitHub workflow collects hourly at minute 17; the existing Hermes `no_agent` job reviews artifacts at minute 45 under its watchdog.

## Private deployment files

Use a private deployment root with `work` and `outputs` directories. Keep the repository checkout at `work/gov-contract-ipo-monitor` and its Python 3.12+ environment at `work/monitor-venv`. Install the project and development dependencies as described in the main README.

Copy the public helper templates from this directory into `work`: `deployment_settings.py`, `github_ops.py`, `hermes_market_monitor.py`, `market_notifications.py`, `hermes_email_worker.py` and `ipo_analyst_worker.py`. Copy `ipo_trade_monitor.py` into the existing Hermes home's `scripts` directory. Beside that deployed bridge, create private `ipo_monitor_deployment.json`:

```json
{
  "work": "<absolute private deployment root>/work"
}
```

Replace the placeholder with the actual absolute Windows work path. The bridge invokes the private monitor environment; the analyst and Gmail workers use the owning Hermes installation's Python environment. Use the existing watchdog and update its existing monitor job if its script path differs. Do not add a second hourly job or a Codex chat automation.

Preserve the existing verified `trade-notifications.json` baseline, analyst state and mail outbox during migration. A fresh installation needs a validated hosted-report baseline before enabling its job; these templates do not invent receipt dates or delivery history.

Create private `work/deployment_settings.json` with exactly the deployment root and explicitly confirmed recipient:

```json
{
  "base": "<absolute private deployment root>",
  "confirmed_recipient": "<confirmed email address>"
}
```

Replace both placeholders. Configure the private `work/market-delivery.json` for the same confirmed sender/recipient, owning Hermes home, workspace outbox, empty holdings and `notification_policy: "ai_approved_only"`. Its analyst settings are:

```json
{
  "enabled": true,
  "model": "gpt-5.6-sol",
  "reasoning_effort": "medium",
  "daily_limit": 4
}
```

This object belongs under the delivery configuration's `analyst` key; it is not a complete delivery configuration. The unconfigured identity `reports@example.invalid` is a non-deliverable placeholder; configure the confirmed recipient privately before enabling delivery. Keep deployment settings, bridge manifest, delivery configuration, credentials, SQLite outboxes/ledgers and downloaded reports outside the Git repository. No real recipient, private deployment path or secret belongs in the public templates.

## Existing authentication

The artifact helper requires authorized GitHub access through the existing Git credential setup. Never put a GitHub token in a report or committed configuration.

The analyst uses the existing authenticated Hermes `openai-codex` route. Verify that its account catalogue contains `gpt-5.6-sol` with medium reasoning before enabling delivery. A saved provider name alone does not establish usable credentials. The isolated worker reads that route and refreshes its existing token when necessary; there is no API-key/provider fallback and no global model change. Reauthenticate through Hermes' normal private login flow if necessary, rather than pasting tokens into chat or templates.

Gmail delivery requires the owning profile's already configured native Google OAuth credentials and `scripts/gmail_sync.py` token-refresh helper. The authenticated sender and confirmed recipient must match the private delivery configuration. Reuse that locked helper; do not copy tokens into the repository, replace another job's credentials or configure SMTP.


Report preparation runs in the monitor’s Python 3.12+ environment. The owning Hermes Gmail worker may use Python 3.11: it imports only the sealed outbox and Gmail transport, then delivers already prepared MIME bytes. It does not import the collector, report renderer or PDF dependencies. Preserve this boundary when adding delivery features; test the actual installed Hermes interpreter as well as the collector versions.

## What runs and what sends

The script job checks actual hosted report artifacts and refreshes candidate quotes. Report age, listing/currency identity, provider timestamp/delay and a quote observation no more than five minutes old gate analyst review. A freshly checked latest-session close is labelled a closed-market research reference and still requires a live broker check before entry.

Only eligible candidates cause a model request. Durable claims limit attempts to four per UTC day, including failures, and unchanged substantive evidence reuses cached decisions. The worker makes one tools-free Codex request with medium reasoning, bounded input/output text and stream size, and a 90-second watchdog. The Codex OAuth route does not support a strict provider-side output-token cap; these limits do not guarantee fixed allowance consumption.

Only new or materially changed AI-approved conditional opportunities can be emailed. Brief evidence, the model's rationale and counterargument, source/quote dates, currency, entry/invalidation/target and approximate strategy accompany the report. Model opinions can be wrong and are not calibrated return probabilities or guarantees. Unknown evidence and waits remain visible in saved reports without routine email.

Holdings start empty. Nashville regular-session windows are conditional scheduling references, while review after five sessions and exit/reassessment by fifteen sessions require an actual verified fill. No account position, quantity, purchase, sell or order is inferred.

Delivery uses an expiring durable outbox. Pending messages must retain current authority, ambiguous sends are reconciled read-only, and successful Gmail readback binds the exact report attachments. Inspect sanitized receipts and saved reports under the private deployment root when diagnosing a failure; a green job alone does not prove collection, analysis or delivery.
