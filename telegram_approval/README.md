# Telegram operator approval

- Uses Telegram Bot API long polling; no inbound port or webhook.
- Bot token is stored only at `secrets/telegram/bot-token` with mode `600`.
- A one-time local pairing code will bind exactly one Telegram user and private chat.
- Approval callbacks will be HMAC-signed, single-use, expiring, and idempotent.
- Gmail remains read-only. A signed production cancellation approval can invoke
  the local lifecycle executor, which updates Notion and marks (never deletes)
  the linked cleaning Calendar event.
- Door-code prompts accept the authorized operator's reply, preserve the value
  exactly, and store it in the Reservation for both regular and smart locks.
  Smart-lock routing can omit the code from the cleaner-facing message.
- Cleaner identities are added with a 24-hour, single-use Telegram invitation
  and are stored only in the private local roster. The operator always receives
  a button-free observer copy and is the actionable fallback only when no real
  cleaner is registered for the property.
- Cleaner proposals contain property nickname, verified address, work window,
  fee, and the 24-hour response rule. They exclude booking references, guest
  identity, and door codes.
- A decline or 24-hour timeout consumes the current action once and routes to
  the next eligible candidate. If none remains, the Cleaning requires a
  replacement and the operator is notified.
