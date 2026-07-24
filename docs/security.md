# Security and data handling

- Secrets are read from environment variables or `.env`, which is git-ignored.
- Logs, SQLite decision traces, evidence archives, fixtures, and emails must never contain API keys or SMTP passwords.
- Source content is untrusted data and is never executed as instructions.
- HTTP calls use bounded timeouts, retry transient failures, and isolate permanent authentication/configuration failures to the affected collector.
- Evidence files are content-addressed and written without overwrite.
- SQLite uses WAL mode, transactions, foreign keys, and a durable SMTP outbox.
- The Docker image runs as a non-root user with dropped Linux capabilities.
