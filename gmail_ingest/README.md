# Gmail read-only ingest + active local booking bridge

- Gmail connector searches and reads messages without modifying Gmail.
- `airbnb_parser.py` deterministically extracts booking fields.
- Raw bodies, guest names, recipient addresses, and confirmation codes are not persisted.
- Message and booking identifiers are persisted only as SHA-256 hashes during shadow mode.
- Gmail itself remains read-only. Mapped, exact Airbnb confirmations can create
  idempotent Notion Reservation/Cleaning records, a cleaning Calendar event,
  and a detailed operator Telegram door-code prompt.

Local OAuth uses `gmail.readonly` plus Google Calendar access. `poll_once.py`
performs an in-memory read, persists only redacted normalized results, and the
LaunchAgent runs it every five minutes. Gmail is never modified.

Every newly parsed event creates a PII-free queue item. The bridge re-reads the
source from Gmail in memory, checks exact Notion and Calendar idempotency keys,
and resumes from recorded downstream IDs after partial failures. Only listing
IDs present in `listing_mappings.json` are eligible; unknown listings become
`REVIEW_REQUIRED`.

## Reservation changes and cancellations

- A change request is observed but never applied before Airbnb confirms it.
- A change request is held locally without raw text or guest identity. If a
  later Airbnb change confirmation matches exactly one request by hashed actor,
  hashed listing, and a seven-day window, explicitly stated adult-count changes
  are applied. Unmatched or unsupported changes lock the Reservation as
  `REVIEW_REQUIRED`; dates and amounts are never guessed.
- A cancellation requires both an Airbnb platform notice and an exact booking
  code match. Telegram approval is mandatory before downstream changes.
- Approval marks the Reservation and unfinished Cleaning rows as cancelled,
  marks the cleaning Calendar event `[취소]`, immediately sends the cleaner a
  cancellation notice, and creates an idempotent Finance adjustment review row.
  Records and events are not deleted.
- A guest full-refund notice does not prove the host payout adjustment amount.
  The Finance row therefore has no invented amount and remains `확인필요` until
  platform settlement evidence is reconciled.
- `lifecycle_event_start_at` prevents historical lifecycle mail from being
  replayed as live operations when the broader Gmail query is first enabled.

## Runtime configuration authority

`gmail_ingest/config.json` is runtime-managed configuration, not immutable Product
source or release content. W01 resolves it only from the absolute
`PROPERTYAI_GMAIL_INGEST_CONFIG_PATH` launcher environment value. Missing,
relative, symlinked, unreadable, or invalid JSON authorities fail closed; there
is no release-relative or current-working-directory fallback. The source plist
contains a cutover placeholder so deployment can bind the already-authorized
external runtime path without baking configuration or credential bytes into a
release.
