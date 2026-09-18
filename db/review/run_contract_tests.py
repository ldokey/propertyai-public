#!/usr/bin/env python3
"""Behavioral contract tests for the PropertyAI Cleaner scheduling review DB.

Review-only harness. It never connects to the legacy Production runtime.
Requires psycopg 3 and a fresh database with db/migration/*.sql applied.
"""
from __future__ import annotations

import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import psycopg

DSN = os.environ.get(
    "PROPERTYAI_REVIEW_DSN",
    "host=/Users/kate/DKATE/propertyai-db-review/postgres18/socket port=55432 dbname=propertyai_review",
)
UTC = timezone.utc
BASE = datetime(2026, 9, 4, 0, 0, tzinfo=UTC)


def uid() -> uuid.UUID:
    return uuid.uuid4()


def assert_eq(actual, expected, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected={expected!r} actual={actual!r}")


def expect_error(conn: psycopg.Connection, expected: str, sql: str, params: tuple) -> None:
    try:
        with conn.transaction():
            conn.execute(sql, params).fetchone()
    except Exception as exc:
        if expected not in str(exc):
            raise AssertionError(f"expected error {expected!r}, got {exc!r}") from exc
    else:
        raise AssertionError(f"expected error {expected!r}, statement succeeded")


@dataclass
class Candidate:
    party_id: uuid.UUID
    candidate_id: uuid.UUID


class World:
    def __init__(self, conn: psycopg.Connection, label: str):
        self.conn = conn
        self.label = label
        self.property_id = uid()
        self.conn.execute(
            "INSERT INTO propertyai.property(property_id,property_code,display_name) VALUES (%s,%s,%s)",
            (self.property_id, f"PROP-{label}-{self.property_id.hex[:8]}", f"Property {label}"),
        )

    def cleaner(self, label: str, tier: int = 1) -> uuid.UUID:
        party = uid()
        self.conn.execute(
            "INSERT INTO propertyai.party(party_id,party_code,display_name) VALUES (%s,%s,%s)",
            (party, f"PTY-{label}-{party.hex[:8]}", f"Cleaner {label}"),
        )
        self.conn.execute(
            "INSERT INTO propertyai.cleaner_profile(cleaner_party_id) VALUES (%s)", (party,)
        )
        self.conn.execute(
            """INSERT INTO propertyai.external_identity(
                   party_id,provider,provider_user_id,provider_chat_id)
               VALUES (%s,'TELEGRAM',%s,%s)""",
            (party, f"u-{party.hex}", f"c-{party.hex}"),
        )
        self.conn.execute(
            """INSERT INTO propertyai.cleaner_property_access(
                   cleaner_party_id,property_id,status,offer_tier,effective_from)
               VALUES (%s,%s,'APPROVED',%s,%s)""",
            (party, self.property_id, tier, BASE - timedelta(days=1)),
        )
        return party

    def job(
        self,
        label: str,
        *,
        window_start: datetime,
        window_end: datetime,
        work_minutes: int,
        cleaners: list[uuid.UUID],
        current_open_tier: int = 1,
        max_tier: int = 2,
        tier_expand_after_minutes: int | None = 1440,
        opened_at: datetime = BASE,
        cutoff_at: datetime | None = None,
    ) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, dict[uuid.UUID, uuid.UUID]]:
        cleaning = uid()
        revision = uid()
        campaign = uid()
        cutoff_at = cutoff_at or window_start
        self.conn.execute(
            """INSERT INTO propertyai.cleaning_job(
                   cleaning_id,cleaning_code,property_id,cleaning_status)
               VALUES (%s,%s,%s,'OFFERING')""",
            (cleaning, f"CLN-{label}-{cleaning.hex[:8]}", self.property_id),
        )
        self.conn.execute(
            """INSERT INTO propertyai.cleaning_schedule_revision(
                   schedule_revision_id,cleaning_id,revision_no,
                   service_window_start_at,service_deadline_at,required_work_minutes,
                   source_checkout_at,change_reason_code,source_event_key)
               VALUES (%s,%s,1,%s,%s,%s,%s,'INITIAL','evt-' || %s)""",
            (revision, cleaning, window_start, window_end, work_minutes, window_start, label),
        )
        self.conn.execute(
            "SELECT propertyai.activate_cleaning_schedule_revision(%s,%s,%s,'INITIAL','review-fixture',%s)",
            (cleaning, revision, opened_at, f"activate:{cleaning}"),
        ).fetchone()
        self.conn.execute(
            """INSERT INTO propertyai.cleaning_offer_campaign(
                   campaign_id,cleaning_id,schedule_revision_id,campaign_version,
                   campaign_status,current_open_tier,max_tier,tier_expand_after_minutes,
                   next_tier_open_at,acceptance_cutoff_at,base_fee_krw,
                   replacement_urgency,urgent_premium_krw,total_agreed_fee_krw,
                   opened_at,idempotency_key)
               VALUES (%s,%s,%s,1,'OPEN',%s,%s,%s,%s,%s,60000,'NORMAL',0,60000,%s,%s)""",
            (
                campaign,
                cleaning,
                revision,
                current_open_tier,
                max_tier,
                tier_expand_after_minutes,
                (opened_at + timedelta(minutes=tier_expand_after_minutes))
                if tier_expand_after_minutes and current_open_tier < max_tier
                else None,
                cutoff_at,
                opened_at,
                f"campaign:{campaign}",
            ),
        )
        self.conn.execute(
            """INSERT INTO propertyai.cleaning_offer_tier_opening(
                   campaign_id,tier_no,opened_at,opening_reason_code,idempotency_key)
               VALUES (%s,1,%s,'INITIAL',%s)""",
            (campaign, opened_at, f"tier-initial:{campaign}"),
        )
        out: dict[uuid.UUID, uuid.UUID] = {}
        for party in cleaners:
            tier = self.conn.execute(
                """SELECT offer_tier FROM propertyai.cleaner_property_access
                   WHERE cleaner_party_id=%s AND property_id=%s AND status='APPROVED'""",
                (party, self.property_id),
            ).fetchone()[0]
            cid = uid()
            self.conn.execute(
                """INSERT INTO propertyai.cleaning_offer_candidate(
                       offer_candidate_id,campaign_id,cleaner_party_id,tier_no)
                   VALUES (%s,%s,%s,%s)""",
                (cid, campaign, party, tier),
            )
            out[party] = cid
        return cleaning, revision, campaign, out

    def second_revision(
        self, cleaning: uuid.UUID, start: datetime, end: datetime, work_minutes: int, label: str
    ) -> uuid.UUID:
        rev = uid()
        self.conn.execute(
            """INSERT INTO propertyai.cleaning_schedule_revision(
                   schedule_revision_id,cleaning_id,revision_no,service_window_start_at,
                   service_deadline_at,required_work_minutes,source_checkout_at,
                   change_reason_code,source_event_key)
               VALUES (%s,%s,2,%s,%s,%s,%s,'RESERVATION_CHECKOUT_CHANGED',%s)""",
            (rev, cleaning, start, end, work_minutes, start, f"checkout-change:{label}"),
        )
        return rev

    def replacement_campaign(
        self,
        cleaning: uuid.UUID,
        revision: uuid.UUID,
        label: str,
        cleaners: list[uuid.UUID],
        *,
        campaign_version: int = 2,
        opened_at: datetime = BASE + timedelta(hours=2),
        cutoff_at: datetime | None = None,
    ) -> tuple[uuid.UUID, dict[uuid.UUID, uuid.UUID]]:
        campaign = uid()
        revision_row = self.conn.execute(
            "SELECT service_window_start_at FROM propertyai.cleaning_schedule_revision WHERE cleaning_id=%s AND schedule_revision_id=%s",
            (cleaning, revision),
        ).fetchone()
        if not revision_row:
            raise AssertionError("replacement campaign revision missing")
        cutoff_at = cutoff_at or revision_row[0]
        self.conn.execute(
            """INSERT INTO propertyai.cleaning_offer_campaign(
                   campaign_id,cleaning_id,schedule_revision_id,campaign_version,
                   campaign_status,current_open_tier,max_tier,tier_expand_after_minutes,
                   next_tier_open_at,acceptance_cutoff_at,base_fee_krw,
                   replacement_urgency,urgent_premium_krw,total_agreed_fee_krw,
                   urgent_premium_policy_version,opened_at,idempotency_key)
               VALUES (%s,%s,%s,%s,'OPEN',1,2,1440,%s,%s,60000,'URGENT',10000,70000,
                       'P0-CLEANER-PERFORMANCE-V1',%s,%s)""",
            (campaign, cleaning, revision, campaign_version, opened_at + timedelta(days=1), cutoff_at, opened_at, f"replacement-campaign:{campaign}"),
        )
        self.conn.execute(
            """INSERT INTO propertyai.cleaning_offer_tier_opening(
                   campaign_id,tier_no,opened_at,opening_reason_code,idempotency_key)
               VALUES (%s,1,%s,'INITIAL',%s)""",
            (campaign, opened_at, f"replacement-tier-initial:{campaign}"),
        )
        out = {}
        for party in cleaners:
            tier = self.conn.execute(
                "SELECT offer_tier FROM propertyai.cleaner_property_access WHERE cleaner_party_id=%s AND property_id=%s AND status='APPROVED'",
                (party, self.property_id),
            ).fetchone()[0]
            cid = uid()
            self.conn.execute(
                "INSERT INTO propertyai.cleaning_offer_candidate(offer_candidate_id,campaign_id,cleaner_party_id,tier_no) VALUES (%s,%s,%s,%s)",
                (cid, campaign, party, tier),
            )
            out[party] = cid
        return campaign, out


def accept(conn, candidate: uuid.UUID, start: datetime, end: datetime, key: str, before=None, after=None, accepted_at=None):
    accepted_at = accepted_at or (BASE + timedelta(hours=1))
    return conn.execute(
        "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,%s,%s)",
        (candidate, start, end, accepted_at, key, before, after),
    ).fetchone()[0]


def run_sequential_contracts(conn: psycopg.Connection) -> list[str]:
    passed: list[str] = []
    w = World(conn, "core")
    t1 = w.cleaner("tier1", 1)
    t2 = w.cleaner("tier2", 2)
    t3 = w.cleaner("schedule", 1)
    t4 = w.cleaner("buffer", 1)
    t5 = w.cleaner("revoked-before", 1)
    t6 = w.cleaner("revoked-after", 1)
    t7 = w.cleaner("daily-limit", 1)
    t8 = w.cleaner("schedule-change", 1)
    t9 = w.cleaner("schedule-change-booked", 1)
    t10 = w.cleaner("reassign-success", 1)
    t11 = w.cleaner("reassign-conflict", 1)
    t12 = w.cleaner("replacement-winner", 1)
    t13 = w.cleaner("replacement-worker", 1)

    # 0. A Cleaning cannot enter an offer campaign before per-request work duration exists.
    no_duration_cleaning = uid()
    no_duration_revision = uid()
    conn.execute(
        "INSERT INTO propertyai.cleaning_job(cleaning_id,cleaning_code,property_id,cleaning_status) VALUES (%s,%s,%s,'OFFERING')",
        (no_duration_cleaning, f"CLN-NODUR-{no_duration_cleaning.hex[:8]}", w.property_id),
    )
    conn.execute(
        """INSERT INTO propertyai.cleaning_schedule_revision(
               schedule_revision_id,cleaning_id,revision_no,service_window_start_at,
               service_deadline_at,required_work_minutes,change_reason_code)
           VALUES (%s,%s,1,%s,%s,NULL,'INITIAL')""",
        (no_duration_revision, no_duration_cleaning, BASE + timedelta(days=3), BASE + timedelta(days=3, hours=4)),
    )
    conn.execute(
        "SELECT propertyai.activate_cleaning_schedule_revision(%s,%s,%s,'INITIAL','review-fixture',%s)",
        (no_duration_cleaning, no_duration_revision, BASE, f"activate:{no_duration_cleaning}"),
    )
    expect_error(
        conn,
        "OFFER_CAMPAIGN_WORK_MINUTES_REQUIRED",
        """INSERT INTO propertyai.cleaning_offer_campaign(
               cleaning_id,schedule_revision_id,campaign_version,current_open_tier,max_tier,
               acceptance_cutoff_at,base_fee_krw,total_agreed_fee_krw,opened_at,idempotency_key)
           VALUES (%s,%s,1,1,1,%s,60000,60000,%s,%s) RETURNING campaign_id""",
        (no_duration_cleaning, no_duration_revision, BASE + timedelta(days=3), BASE, f"nodur:{uid()}"),
    )
    passed.append("WORK_DURATION_REQUIRED_BEFORE_OFFER_CAMPAIGN")

    # No reminder lead values or daily capacity limits are seeded by the migration.
    assert_eq(conn.execute("SELECT count(*) FROM propertyai.cleaning_notification_policy").fetchone()[0], 0, "notification policy not guessed")
    assert_eq(conn.execute("SELECT count(*) FROM propertyai.cleaner_capacity_policy").fetchone()[0], 0, "capacity policy not guessed")
    passed.append("UNDECIDED_NOTIFICATION_AND_CAPACITY_VALUES_ARE_NOT_GUESSED")

    # 1. 24h means cumulative tier expansion; Tier 1 remains eligible and no alert is emitted.
    ws = datetime(2026, 9, 10, 2, 0, tzinfo=UTC)  # 11:00 KST
    we = datetime(2026, 9, 10, 6, 0, tzinfo=UTC)  # 15:00 KST
    _, _, campaign, candidates = w.job(
        "tier-expand", window_start=ws, window_end=we, work_minutes=120, cleaners=[t1, t2]
    )
    opened = conn.execute(
        "SELECT propertyai.open_next_offer_tier(%s,%s,%s)",
        (campaign, BASE + timedelta(days=1), f"open2:{campaign}"),
    ).fetchone()[0]
    assert_eq(opened, 2, "tier expanded to 2")
    opened_replay = conn.execute(
        "SELECT propertyai.open_next_offer_tier(%s,%s,%s)",
        (campaign, BASE + timedelta(days=1, minutes=5), f"open2:{campaign}"),
    ).fetchone()[0]
    assert_eq(opened_replay, 2, "tier expansion idempotent replay")
    assert_eq(
        conn.execute("SELECT count(*) FROM propertyai.cleaning_offer_tier_opening WHERE campaign_id=%s AND tier_no=2", (campaign,)).fetchone()[0],
        1,
        "tier expansion replay does not duplicate opening row",
    )
    rows = conn.execute(
        "SELECT cleaner_party_id,candidate_status FROM propertyai.cleaning_offer_candidate WHERE campaign_id=%s",
        (campaign,),
    ).fetchall()
    assert_eq({r[1] for r in rows}, {"ELIGIBLE"}, "tier opening keeps lower tier eligible")
    assert_eq(
        conn.execute("SELECT count(*) FROM propertyai.integration_outbox").fetchone()[0],
        0,
        "tier opening does not notify",
    )
    # Accepted_at remains BASE+1h by helper; campaign tier is already open as DB state.
    accept(conn, candidates[t1], ws, ws + timedelta(minutes=120), f"accept-tier1-after-open:{campaign}", accepted_at=BASE + timedelta(days=1, minutes=1))
    passed.append("TIER_EXPANSION_IS_CUMULATIVE_AND_SILENT")

    # 2. Tier 2 cannot accept before it is open.
    _, _, _, cands = w.job(
        "tier-closed", window_start=ws + timedelta(days=1), window_end=we + timedelta(days=1),
        work_minutes=60, cleaners=[t2], current_open_tier=1, max_tier=2
    )
    expect_error(
        conn,
        "ACCEPT_TIER_NOT_OPEN",
        "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,NULL,NULL)",
        (cands[t2], ws + timedelta(days=1), ws + timedelta(days=1, hours=1), BASE + timedelta(hours=1), f"tier-closed:{uid()}"),
    )
    passed.append("CLOSED_TIER_CANNOT_ACCEPT")

    # 3. Same Cleaner: overlap rejected, exact [end,start) boundary accepted; NULL limits allow >1 job/day.
    day = datetime(2026, 9, 12, 2, 0, tzinfo=UTC)
    _, _, _, c1 = w.job("overlap-a", window_start=day, window_end=day + timedelta(hours=4), work_minutes=120, cleaners=[t3])
    accept(conn, c1[t3], day, day + timedelta(hours=2), f"overlap-a:{uid()}")
    _, _, _, c2 = w.job("overlap-b", window_start=day, window_end=day + timedelta(hours=4), work_minutes=120, cleaners=[t3])
    expect_error(
        conn,
        "ACCEPT_CLEANER_SCHEDULE_CONFLICT",
        "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,NULL,NULL)",
        (c2[t3], day + timedelta(hours=1), day + timedelta(hours=3), BASE + timedelta(hours=1), f"overlap-b:{uid()}"),
    )
    _, _, _, c3 = w.job("boundary", window_start=day, window_end=day + timedelta(hours=4), work_minutes=120, cleaners=[t3])
    accept(conn, c3[t3], day + timedelta(hours=2), day + timedelta(hours=4), f"boundary:{uid()}")
    assert_eq(
        conn.execute(
            "SELECT count(*) FROM propertyai.cleaning_assignment WHERE cleaner_party_id=%s AND assignment_status='HARD_BOOKED'",
            (t3,),
        ).fetchone()[0],
        2,
        "null daily limits allow two sequential jobs",
    )
    passed.append("CROSS_CLEANING_OVERLAP_BLOCKED_BOUNDARY_ALLOWED")

    # 4. Configured travel buffer can create a conflict even when raw work slots do not overlap.
    bday = datetime(2026, 9, 13, 2, 0, tzinfo=UTC)
    _, _, _, b1 = w.job("buffer-a", window_start=bday, window_end=bday + timedelta(hours=5), work_minutes=120, cleaners=[t4])
    accept(conn, b1[t4], bday, bday + timedelta(hours=2), f"buffer-a:{uid()}")
    _, _, _, b2 = w.job("buffer-b", window_start=bday, window_end=bday + timedelta(hours=5), work_minutes=60, cleaners=[t4])
    expect_error(
        conn,
        "ACCEPT_CLEANER_SCHEDULE_CONFLICT",
        "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,%s,%s)",
        (b2[t4], bday + timedelta(hours=2, minutes=10), bday + timedelta(hours=3, minutes=10), BASE + timedelta(hours=1), f"buffer-b:{uid()}", 20, None),
    )
    passed.append("TRAVEL_BUFFER_PARTICIPATES_IN_CONFLICT")

    # 5. Revoked access blocks a NEW acceptance.
    rday = datetime(2026, 9, 14, 2, 0, tzinfo=UTC)
    _, _, _, rc = w.job("revoke-before", window_start=rday, window_end=rday + timedelta(hours=4), work_minutes=60, cleaners=[t5])
    conn.execute(
        "UPDATE propertyai.cleaner_property_access SET status='REVOKED',effective_until=%s WHERE cleaner_party_id=%s AND property_id=%s AND status='APPROVED'",
        (BASE, t5, w.property_id),
    )
    expect_error(
        conn,
        "ACCEPT_PROPERTY_ACCESS_NOT_APPROVED",
        "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,NULL,NULL)",
        (rc[t5], rday, rday + timedelta(hours=1), BASE + timedelta(hours=1), f"revoke-before:{uid()}"),
    )
    passed.append("REVOKED_ACCESS_BLOCKS_NEW_ACCEPTANCE")

    # 6. Revocation after HARD_BOOKED does not cancel the existing assignment.
    aday = datetime(2026, 9, 15, 2, 0, tzinfo=UTC)
    _, _, _, ac = w.job("revoke-after", window_start=aday, window_end=aday + timedelta(hours=4), work_minutes=60, cleaners=[t6])
    aid = accept(conn, ac[t6], aday, aday + timedelta(hours=1), f"revoke-after:{uid()}")
    conn.execute(
        "UPDATE propertyai.cleaner_property_access SET status='REVOKED',effective_until=%s WHERE cleaner_party_id=%s AND property_id=%s AND status='APPROVED'",
        (BASE + timedelta(hours=2), t6, w.property_id),
    )
    assert_eq(
        conn.execute("SELECT assignment_status FROM propertyai.cleaning_assignment WHERE assignment_id=%s", (aid,)).fetchone()[0],
        "HARD_BOOKED",
        "post-accept revocation does not cascade",
    )
    passed.append("POST_ACCEPT_REVOCATION_PRESERVES_HARD_BOOKING")

    # 7. Exactly one active Telegram identity per Party is a physical DB invariant.
    expect_error(
        conn,
        "uq_active_telegram_identity_per_party",
        "INSERT INTO propertyai.external_identity(party_id,provider,provider_user_id,provider_chat_id) VALUES (%s,'TELEGRAM',%s,%s) RETURNING external_identity_id",
        (t6, f"u2-{uid()}", f"c2-{uid()}"),
    )
    passed.append("ACTIVE_TELEGRAM_IDENTITY_ONE_TO_ONE")

    # 8. NULL daily limits mean unrestricted; configured max_daily_jobs is enforced.
    conn.execute(
        """INSERT INTO propertyai.cleaner_capacity_policy(
               cleaner_party_id,policy_version,max_daily_jobs,effective_from)
           VALUES (%s,1,1,%s)""",
        (t7, BASE - timedelta(days=1)),
    )
    dday = datetime(2026, 9, 16, 2, 0, tzinfo=UTC)
    _, _, _, d1 = w.job("limit-a", window_start=dday, window_end=dday + timedelta(hours=4), work_minutes=60, cleaners=[t7])
    accept(conn, d1[t7], dday, dday + timedelta(hours=1), f"limit-a:{uid()}")
    _, _, _, d2 = w.job("limit-b", window_start=dday, window_end=dday + timedelta(hours=4), work_minutes=60, cleaners=[t7])
    expect_error(
        conn,
        "ACCEPT_DAILY_JOB_LIMIT_EXCEEDED",
        "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,NULL,NULL)",
        (d2[t7], dday + timedelta(hours=2), dday + timedelta(hours=3), BASE + timedelta(hours=1), f"limit-b:{uid()}"),
    )
    passed.append("NULL_LIMIT_IS_UNRESTRICTED_CONFIGURED_DAILY_LIMIT_ENFORCED")

    # 9. Schedule/checkout revision invalidates open offers but does not require reconciliation when unassigned.
    sday = datetime(2026, 9, 17, 2, 0, tzinfo=UTC)
    cleaning, _, camp, sc = w.job("schedule-change", window_start=sday, window_end=sday + timedelta(hours=4), work_minutes=60, cleaners=[t8])
    rev2 = w.second_revision(cleaning, sday + timedelta(days=1), sday + timedelta(days=1, hours=4), 60, "unassigned")
    rec = conn.execute(
        "SELECT propertyai.activate_cleaning_schedule_revision(%s,%s,%s,'RESERVATION_CHECKOUT_CHANGED','review-change',%s)",
        (cleaning, rev2, BASE + timedelta(hours=3), f"rev-change:{cleaning}"),
    ).fetchone()[0]
    assert_eq(rec, None, "unassigned schedule change has no hard-booked reconciliation")
    rec_replay = conn.execute(
        "SELECT propertyai.activate_cleaning_schedule_revision(%s,%s,%s,'RESERVATION_CHECKOUT_CHANGED','review-change',%s)",
        (cleaning, rev2, BASE + timedelta(hours=3, minutes=1), f"rev-change:{cleaning}"),
    ).fetchone()[0]
    assert_eq(rec_replay, None, "schedule activation replay is idempotent")
    assert_eq(conn.execute("SELECT campaign_status FROM propertyai.cleaning_offer_campaign WHERE campaign_id=%s", (camp,)).fetchone()[0], "SUPERSEDED", "old campaign superseded")
    assert_eq(conn.execute("SELECT candidate_status FROM propertyai.cleaning_offer_candidate WHERE offer_candidate_id=%s", (sc[t8],)).fetchone()[0], "SUPERSEDED", "old candidate superseded")
    passed.append("SCHEDULE_REVISION_SUPERSEDES_STALE_OPEN_OFFER")
    passed.append("SCHEDULE_REVISION_ACTIVATION_IS_IDEMPOTENT")

    # 10. Schedule revision after booking preserves old hard booking and creates explicit reconciliation work.
    hday = datetime(2026, 9, 18, 2, 0, tzinfo=UTC)
    cleaning, _, _, hc = w.job("schedule-change-booked", window_start=hday, window_end=hday + timedelta(hours=4), work_minutes=60, cleaners=[t9])
    old_assignment = accept(conn, hc[t9], hday, hday + timedelta(hours=1), f"booked-rev:{uid()}")
    rev2 = w.second_revision(cleaning, hday + timedelta(days=1), hday + timedelta(days=1, hours=4), 60, "booked")
    rec = conn.execute(
        "SELECT propertyai.activate_cleaning_schedule_revision(%s,%s,%s,'RESERVATION_CHECKOUT_CHANGED','review-change',%s)",
        (cleaning, rev2, BASE + timedelta(hours=4), f"booked-rev-change:{cleaning}"),
    ).fetchone()[0]
    if rec is None:
        raise AssertionError("hard-booked schedule change must create reconciliation")
    assert_eq(conn.execute("SELECT assignment_status FROM propertyai.cleaning_assignment WHERE assignment_id=%s", (old_assignment,)).fetchone()[0], "HARD_BOOKED", "old assignment preserved")
    assert_eq(conn.execute("SELECT reconciliation_status FROM propertyai.cleaning_schedule_reconciliation WHERE schedule_reconciliation_id=%s", (rec,)).fetchone()[0], "PENDING", "reconciliation pending")
    passed.append("BOOKED_SCHEDULE_CHANGE_IS_FAIL_CLOSED_RECONCILIATION")

    # 11. Cutoff is authoritative even if a local/UI offer were still displayed.
    cday = datetime(2026, 9, 19, 2, 0, tzinfo=UTC)
    _, _, _, cc = w.job("cutoff", window_start=cday, window_end=cday + timedelta(hours=4), work_minutes=60, cleaners=[t1], cutoff_at=BASE + timedelta(minutes=30))
    expect_error(
        conn,
        "ACCEPT_AFTER_CUTOFF",
        "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,NULL,NULL)",
        (cc[t1], cday, cday + timedelta(hours=1), BASE + timedelta(hours=1), f"cutoff:{uid()}"),
    )
    passed.append("STALE_UI_OFFER_CANNOT_BYPASS_ACCEPTANCE_CUTOFF")

    # 12. Unavailable -> original reassignment succeeds through the same hard-booking authority.
    uday = datetime(2026, 9, 22, 2, 0, tzinfo=UTC)
    cleaning, revision, _, uc = w.job("reassign-success", window_start=uday, window_end=uday + timedelta(hours=4), work_minutes=60, cleaners=[t10])
    original = accept(conn, uc[t10], uday, uday + timedelta(hours=1), f"reassign-original:{uid()}")
    unavailable_id = conn.execute(
        "SELECT propertyai.record_cleaner_unavailable(%s,%s,'SAME_DAY_UNAVAILABLE','URGENT',%s)",
        (original, BASE + timedelta(hours=2), f"unavailable:{uid()}"),
    ).fetchone()[0]
    request_id = conn.execute(
        "SELECT propertyai.request_original_cleaner_reassignment(%s,%s,%s)",
        (unavailable_id, BASE + timedelta(hours=3), f"reassign-request:{uid()}"),
    ).fetchone()[0]
    reassigned = conn.execute(
        "SELECT propertyai.reassign_original_cleaner(%s,%s,%s,%s,%s,NULL,NULL)",
        (request_id, uday + timedelta(hours=1), uday + timedelta(hours=2), BASE + timedelta(hours=4), f"reassign-decision:{uid()}"),
    ).fetchone()[0]
    assert_eq(conn.execute("SELECT assignment_status FROM propertyai.cleaning_assignment WHERE assignment_id=%s", (original,)).fetchone()[0], "RELEASED", "old assignment stays released")
    assert_eq(conn.execute("SELECT assignment_status FROM propertyai.cleaning_assignment WHERE assignment_id=%s", (reassigned,)).fetchone()[0], "HARD_BOOKED", "new reassigned assignment is hard booked")
    assert_eq(conn.execute("SELECT assignment_source FROM propertyai.cleaning_assignment WHERE assignment_id=%s", (reassigned,)).fetchone()[0], "ORIGINAL_REASSIGNED", "reassignment source")
    passed.append("ORIGINAL_REASSIGNMENT_USES_HARD_BOOKING_AUTHORITY")

    # 13. Original Cleaner cannot reassign into a slot occupied by another Cleaning accepted meanwhile.
    cday2 = datetime(2026, 9, 23, 2, 0, tzinfo=UTC)
    cleaning, _, _, uc = w.job("reassign-conflict-original", window_start=cday2, window_end=cday2 + timedelta(hours=4), work_minutes=60, cleaners=[t11])
    original = accept(conn, uc[t11], cday2, cday2 + timedelta(hours=1), f"reassign-conflict-original:{uid()}")
    unavailable_id = conn.execute(
        "SELECT propertyai.record_cleaner_unavailable(%s,%s,'SAME_DAY_UNAVAILABLE','URGENT',%s)",
        (original, BASE + timedelta(hours=2), f"unavailable-conflict:{uid()}"),
    ).fetchone()[0]
    request_id = conn.execute(
        "SELECT propertyai.request_original_cleaner_reassignment(%s,%s,%s)",
        (unavailable_id, BASE + timedelta(hours=3), f"reassign-conflict-request:{uid()}"),
    ).fetchone()[0]
    _, _, _, other = w.job("reassign-conflict-other", window_start=cday2, window_end=cday2 + timedelta(hours=4), work_minutes=60, cleaners=[t11])
    accept(conn, other[t11], cday2 + timedelta(hours=1), cday2 + timedelta(hours=2), f"other-job:{uid()}")
    expect_error(
        conn,
        "REASSIGN_CLEANER_SCHEDULE_CONFLICT",
        "SELECT propertyai.reassign_original_cleaner(%s,%s,%s,%s,%s,NULL,NULL)",
        (request_id, cday2 + timedelta(hours=1), cday2 + timedelta(hours=2), BASE + timedelta(hours=4), f"reassign-conflict-decision:{uid()}"),
    )
    passed.append("ORIGINAL_REASSIGNMENT_CANNOT_BYPASS_CROSS_CLEANING_CONFLICT")

    # 14. Replacement acceptance wins over a later operator attempt to restore original Cleaner.
    wday = datetime(2026, 9, 24, 2, 0, tzinfo=UTC)
    cleaning, revision, _, wc = w.job("replacement-wins-original", window_start=wday, window_end=wday + timedelta(hours=4), work_minutes=60, cleaners=[t12])
    original = accept(conn, wc[t12], wday, wday + timedelta(hours=1), f"replacement-wins-original:{uid()}")
    unavailable_id = conn.execute(
        "SELECT propertyai.record_cleaner_unavailable(%s,%s,'SAME_DAY_UNAVAILABLE','URGENT',%s)",
        (original, BASE + timedelta(hours=2), f"replacement-wins-unavailable:{uid()}"),
    ).fetchone()[0]
    request_id = conn.execute(
        "SELECT propertyai.request_original_cleaner_reassignment(%s,%s,%s)",
        (unavailable_id, BASE + timedelta(hours=3), f"replacement-wins-request:{uid()}"),
    ).fetchone()[0]
    _, replacement_candidates = w.replacement_campaign(cleaning, revision, "replacement-wins", [t13], opened_at=BASE + timedelta(hours=2))
    replacement_assignment = conn.execute(
        "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,NULL,NULL)",
        (replacement_candidates[t13], wday + timedelta(hours=1), wday + timedelta(hours=2), BASE + timedelta(hours=3, minutes=30), f"replacement-accepted:{uid()}"),
    ).fetchone()[0]
    expect_error(
        conn,
        "REASSIGN_CLEANING_NOT_REASSIGNABLE",
        "SELECT propertyai.reassign_original_cleaner(%s,%s,%s,%s,%s,NULL,NULL)",
        (request_id, wday + timedelta(hours=1), wday + timedelta(hours=2), BASE + timedelta(hours=4), f"replacement-wins-reassign:{uid()}"),
    )
    assert_eq(conn.execute("SELECT assignment_status FROM propertyai.cleaning_assignment WHERE assignment_id=%s", (replacement_assignment,)).fetchone()[0], "HARD_BOOKED", "replacement stays winner")
    passed.append("REPLACEMENT_ACCEPTANCE_WINS_OVER_LATE_ORIGINAL_REASSIGN")

    return passed


def run_concurrent_same_cleaning(conn: psycopg.Connection) -> str:
    w = World(conn, "race-cleaning")
    a = w.cleaner("A", 1)
    b = w.cleaner("B", 1)
    day = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
    _, _, _, c = w.job("one-winner", window_start=day, window_end=day + timedelta(hours=4), work_minutes=60, cleaners=[a, b])
    conn.commit()

    barrier = threading.Barrier(3)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker(name: str, candidate: uuid.UUID):
        with psycopg.connect(DSN, autocommit=True) as cconn:
            barrier.wait()
            try:
                aid = cconn.execute(
                    "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,NULL,NULL)",
                    (candidate, day, day + timedelta(hours=1), BASE + timedelta(hours=1), f"race-cleaning:{name}:{uid()}"),
                ).fetchone()[0]
                result = f"SUCCESS:{aid}"
            except Exception as exc:
                result = f"ERROR:{exc}"
            with lock:
                outcomes.append((name, result))

    ts = [threading.Thread(target=worker, args=("A", c[a])), threading.Thread(target=worker, args=("B", c[b]))]
    for t in ts: t.start()
    barrier.wait()
    for t in ts: t.join(10)
    if any(t.is_alive() for t in ts):
        raise AssertionError("same-cleaning race deadlocked")
    success = [x for x in outcomes if x[1].startswith("SUCCESS:")]
    errors = [x for x in outcomes if x[1].startswith("ERROR:")]
    assert_eq(len(success), 1, "same cleaning race winner count")
    assert_eq(len(errors), 1, "same cleaning race loser count")
    assert_eq(
        conn.execute("SELECT count(*) FROM propertyai.cleaning_assignment WHERE cleaning_id=(SELECT cleaning_id FROM propertyai.cleaning_offer_campaign WHERE campaign_id=(SELECT campaign_id FROM propertyai.cleaning_offer_candidate WHERE offer_candidate_id=%s)) AND assignment_status='HARD_BOOKED'", (c[a],)).fetchone()[0],
        1,
        "same cleaning has one hard booking",
    )
    return "CONCURRENT_SAME_CLEANING_HAS_ONE_WINNER"


def run_concurrent_same_cleaner(conn: psycopg.Connection) -> str:
    w = World(conn, "race-cleaner")
    cleaner = w.cleaner("X", 1)
    day = datetime(2026, 9, 21, 2, 0, tzinfo=UTC)
    _, _, _, ca = w.job("race-a", window_start=day, window_end=day + timedelta(hours=4), work_minutes=120, cleaners=[cleaner])
    _, _, _, cb = w.job("race-b", window_start=day, window_end=day + timedelta(hours=4), work_minutes=120, cleaners=[cleaner])
    conn.commit()

    barrier = threading.Barrier(3)
    outcomes: list[str] = []
    out_lock = threading.Lock()

    def worker(candidate: uuid.UUID, key: str):
        with psycopg.connect(DSN, autocommit=True) as cconn:
            barrier.wait()
            try:
                aid = cconn.execute(
                    "SELECT propertyai.accept_cleaning_offer(%s,%s,%s,%s,%s,NULL,NULL)",
                    (candidate, day, day + timedelta(hours=2), BASE + timedelta(hours=1), key),
                ).fetchone()[0]
                value = f"SUCCESS:{aid}"
            except Exception as exc:
                value = f"ERROR:{exc}"
            with out_lock:
                outcomes.append(value)

    ts = [
        threading.Thread(target=worker, args=(ca[cleaner], f"race-cleaner-a:{uid()}")),
        threading.Thread(target=worker, args=(cb[cleaner], f"race-cleaner-b:{uid()}")),
    ]
    for t in ts: t.start()
    barrier.wait()
    for t in ts: t.join(10)
    if any(t.is_alive() for t in ts):
        raise AssertionError("same-cleaner race deadlocked")
    assert_eq(sum(v.startswith("SUCCESS:") for v in outcomes), 1, "same cleaner overlap winner count")
    assert_eq(sum(v.startswith("ERROR:") for v in outcomes), 1, "same cleaner overlap loser count")
    return "CONCURRENT_SAME_CLEANER_OVERLAP_HAS_ONE_WINNER"


def main() -> None:
    with psycopg.connect(DSN, autocommit=False) as conn:
        passed = run_sequential_contracts(conn)
        conn.commit()
        passed.append(run_concurrent_same_cleaning(conn))
        passed.append(run_concurrent_same_cleaner(conn))
        print(f"CONTRACT_TESTS_PASS={len(passed)}")
        for item in passed:
            print(f"PASS {item}")


if __name__ == "__main__":
    main()
