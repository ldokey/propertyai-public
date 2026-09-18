# Cleaner Multi-Job DB Acceptance Matrix

Status: **DRAFT_REVIEW**. This matrix is intentionally broader than the 19 currently automated PostgreSQL contract tests.

Legend:
- **PASS-AUTO**: currently covered by executable review-DB contract test.
- **DESIGNED**: schema/command supports it; dedicated automated case should still be added.
- **OPEN-POLICY**: business decision required before asserting expected behavior.
- **FUTURE-SLICE**: outside the current migration slice but must be tested before full cutover.

| ID | Scenario | Expected contract | Status |
|---|---|---|---|
| TIER-01 | Campaign starts with Tier 1 | Only Tier 1 may accept initially | DESIGNED |
| TIER-02 | 24h elapsed, Tier 2 opens | Tier 1 + Tier 2 both remain eligible | PASS-AUTO |
| TIER-03 | Tier 1 accepts after Tier 2 opened | Acceptance allowed if all fresh checks pass | PASS-AUTO |
| TIER-04 | Tier 2 attempts before Tier 2 opened | Reject `ACCEPT_TIER_NOT_OPEN` | PASS-AUTO |
| TIER-05 | Tier opens | No Telegram/outbox message solely for opening | PASS-AUTO |
| TIER-06 | Tier-open scheduled action replay | One immutable opening row only | PASS-AUTO |
| TIER-07 | Cleaner explicitly declines | That candidate remains declined; campaign ownership is not transferred | DESIGNED |
| TIER-08 | Decline should immediately widen tier? | Must be explicitly decided | OPEN-POLICY |
| TIER-09 | Current tier state is 2 but accepted timestamp predates Tier-2 opening | Rejected based on opening audit at accepted time | DESIGNED |
| TIER-10 | Campaign reaches max tier | No phantom additional tier | DESIGNED |
| OFFER-01 | Work duration absent | Campaign cannot open | PASS-AUTO |
| OFFER-02 | Old Telegram button remains visible after cutoff | Fresh acceptance rejects it | PASS-AUTO |
| OFFER-03 | Campaign already closed by another winner | Late candidate cannot accept | DESIGNED |
| OFFER-04 | Schedule revision changed after offer | Old campaign/candidates superseded | PASS-AUTO |
| OFFER-05 | Candidate snapshot says eligible but Cleaner became inactive | Fresh acceptance rejects | DESIGNED |
| OFFER-06 | Candidate snapshot says eligible but Property became inactive | Fresh acceptance rejects | DESIGNED |
| OFFER-07 | Candidate snapshot says eligible but administrative Property access is REVOKED | Current draft rejects new acceptance | PASS-AUTO / REVIEW-SEMANTICS |
| OFFER-08 | Access revoked only after HARD_BOOKED | Existing hard booking is preserved | PASS-AUTO |
| ID-01 | One Party gets second active Telegram identity | DB unique invariant rejects | PASS-AUTO |
| ID-02 | Same Telegram user assigned to another active Party | DB unique invariant rejects | DESIGNED |
| ID-03 | Same Telegram chat assigned to another active Party | DB unique invariant rejects | DESIGNED |
| ID-04 | Identity revoked then replaced | Exactly one new active identity allowed | DESIGNED |
| SLOT-01 | Duration belongs to Cleaning request | No Property-duration default required | PASS-AUTO |
| SLOT-02 | Proposed slot shorter/longer than required duration | Reject | DESIGNED |
| SLOT-03 | Proposed slot outside service window | Reject | DESIGNED |
| SLOT-04 | Same Cleaner has overlapping hard-booked slots | Reject | PASS-AUTO |
| SLOT-05 | Existing slot ends exactly when next starts, no buffer | Allowed `[start,end)` boundary | PASS-AUTO |
| SLOT-06 | Non-overlap work slots overlap after travel buffer | Reject | PASS-AUTO |
| SLOT-07 | Multiple sequential jobs same day, no daily limit configured | Allowed | PASS-AUTO |
| SLOT-08 | `max_daily_jobs` configured and exceeded | Reject | PASS-AUTO |
| SLOT-09 | `max_daily_work_minutes` configured and exceeded | Reject | DESIGNED |
| SLOT-10 | Cleaner unavailable-window overlaps conflict window | Reject | DESIGNED |
| SLOT-11 | Exact travel buffer policy not configured | NULL imposes no guessed buffer | PASS-AUTO |
| RACE-01 | Two Cleaners accept same Cleaning simultaneously | Exactly one HARD_BOOKED winner | PASS-AUTO |
| RACE-02 | Same Cleaner accepts two overlapping Cleanings simultaneously | Exactly one succeeds | PASS-AUTO |
| RACE-03 | Tier expansion vs acceptance | Acceptance checks exact opening audit/locks; no tier ownership transfer | DESIGNED |
| RACE-04 | Cutoff action vs acceptance | Accepted timestamp + locked campaign define one terminal result | DESIGNED |
| RACE-05 | Schedule revision activation vs acceptance | Old revision cannot become new hard booking after revision wins | DESIGNED |
| RACE-06 | Reservation cancellation vs acceptance | Must preserve existing same-Cleaning serialization in future DB cancellation slice | FUTURE-SLICE |
| REV-01 | Checkout changes before assignment | New revision; stale open offer superseded | PASS-AUTO |
| REV-02 | Checkout changes after HARD_BOOKED | Preserve booking; create explicit reconciliation | PASS-AUTO |
| REV-03 | Schedule activation replay | Idempotent | PASS-AUTO |
| REV-04 | Multiple sequential checkout changes | Monotonic revision chain, one current revision | DESIGNED |
| REV-05 | Duration changes with checkout/schedule revision | New request duration snapshot required before new campaign | DESIGNED |
| REV-06 | Calendar already reflects old date | Future outbox/reconciliation must patch, never silently diverge | FUTURE-SLICE |
| UNAV-01 | Cleaner confirms unavailable | Old HARD_BOOKED becomes RELEASED; durable case recorded | DESIGNED / exercised via reassignment tests |
| UNAV-02 | Unavailable replay | One case/event only | DESIGNED |
| UNAV-03 | Replacement campaign opens | Original released assignment remains history, not effective schedule | DESIGNED |
| REASSIGN-01 | Original Cleaner requests return before replacement wins | Request may be created | PASS-AUTO |
| REASSIGN-02 | Original Cleaner reassigned into free slot | New `ORIGINAL_REASSIGNED` HARD_BOOKED row | PASS-AUTO |
| REASSIGN-03 | Original Cleaner accepted another overlapping Cleaning meanwhile | Reassignment rejected | PASS-AUTO |
| REASSIGN-04 | Replacement accepts first | Late original-reassign attempt rejected | PASS-AUTO |
| REASSIGN-05 | Current schedule revision changed after request | Reassignment rejects stale revision | DESIGNED |
| REASSIGN-06 | Administrative Property access revoked before operator decision | Current draft rejects reassignment | DESIGNED / REVIEW-SEMANTICS |
| PERF-01 | Same incident makes 3 Cleanings unavailable | Whether score is per Cleaning or incident-grouped | OPEN-POLICY |
| PERF-02 | Daily score cap | Deferred | OPEN-POLICY |
| PERF-03 | Urgent accepted/completed score events | Preserve existing policy later; not in this DB slice | FUTURE-SLICE |
| NOTIFY-01 | Day confirmation | Future timing relative to actual assignment start | OPEN-POLICY |
| NOTIFY-02 | Reconfirm/escalation/arrival/start | Lead minutes stored as nullable versioned policy | DESIGNED |
| NOTIFY-03 | No lead policy configured | No guessed default is seeded | PASS-AUTO |
| BATCH-01 | More than 100 due rows | Worker uses keyset pagination `(due_at,id)`, not one fixed query page | DESIGNED |
| BATCH-02 | Worker dies after claiming scheduled action | Lease expiry allows retry | DESIGNED |
| BATCH-03 | External API result uncertain | Outbox enters reconciliation-capable state | DESIGNED |
| BATCH-04 | Same scheduled action produced twice | Unique idempotency key prevents duplicate business timer | DESIGNED |
| BATCH-05 | Same external delivery retried | Same outbox idempotency/effect tracked through attempts | DESIGNED |
| BATCH-06 | Retry repeatedly fails | Dead-letter terminal state available | DESIGNED |
| OPS-01 | Cleaner/profile disabled after already HARD_BOOKED | Assignment remains visible; explicit operational reconciliation policy still needed | OPEN-POLICY |
| OPS-02 | Property access revoked after hard booking | Current DB preserves hard booking; notification/reconciliation UX still needed | OPEN-POLICY |
| OPS-03 | Multiple Cleaning alerts same Cleaner | Future reminder builder should schedule per actual slot and aggregate UX if desired | OPEN-POLICY |
| MIG-01 | Existing Notion assignment imported | Stable IDs + source refs + exact history preservation | FUTURE-SLICE |
| MIG-02 | Notion and DB disagree during transition | One declared source-of-truth per phase; fail closed/reconcile | FUTURE-SLICE |
| MIG-03 | App language changes | Same PostgreSQL schema/commands/constraints remain authoritative | DESIGNED |

## Mandatory pre-live subset

Before another controlled live multi-job test, at minimum automate/close:

- TIER-01 through TIER-10,
- OFFER-01 through OFFER-08,
- ID-01 through ID-04,
- SLOT-01 through SLOT-11,
- RACE-01 through RACE-06 as applicable to implemented slices,
- REV-01 through REV-06 as applicable,
- UNAV-01/02/03,
- REASSIGN-01 through REASSIGN-06,
- BATCH-01 through BATCH-05.

Performance scoring cases may remain a separately gated later review as explicitly requested.
