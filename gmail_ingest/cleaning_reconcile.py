#!/usr/bin/env python3
"""READ-ONLY Reservation -> Cleaning reconciliation scheduled-job consumer."""

from __future__ import annotations

import argparse
import json
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from gmail_ingest.reservation_eligibility import (
    is_cleaning_reconciliation_eligible,
    relation_ids,
)
from propertyai_core.notion_resources import cleaning_source_id, reservation_source_id
from propertyai_core.scheduled_job import (
    JobOutcome,
    MutationAuthorityMode,
    ScheduledJobRunner,
)


ROOT = Path(__file__).resolve().parents[1]
NOTION_VERSION = "2026-03-11"
DEFAULT_NOTION_TOKEN_PATH = ROOT / "secrets" / "notion" / "token"
DEFAULT_LOCK_PATH = ROOT / "gmail_ingest" / "runtime" / "locks" / "cleaning-reconcile.lock"
KST = ZoneInfo("Asia/Seoul")

MATCHED = "MATCHED"
MISSING = "MISSING"
CONFLICT = "CONFLICT"


class ReadOnlyNotionClient:
    """Narrow client exposing only Notion data-source query reads."""

    def __init__(self, token_path: Path) -> None:
        self._token_path = Path(token_path)

    def query_data_source(self, data_source_id: str, payload: dict) -> list[dict]:
        results: list[dict] = []
        cursor = None
        while True:
            body = dict(payload)
            body.setdefault("page_size", 100)
            if cursor:
                body["start_cursor"] = cursor
            token = self._token_path.read_text().strip()
            request = urllib.request.Request(
                f"https://api.notion.com/v1/data_sources/{data_source_id}/query",
                data=json.dumps(body, ensure_ascii=False).encode(),
                method="POST",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": NOTION_VERSION,
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(request, timeout=40) as response:
                page = json.loads(response.read())
            results.extend(page.get("results", []))
            if not page.get("has_more"):
                return results
            cursor = page.get("next_cursor")
            if not cursor:
                raise RuntimeError("Notion query has_more without next_cursor")


@dataclass(frozen=True)
class ReconcileCounts:
    processed_count: int
    matched_count: int
    missing_count: int
    conflict_count: int
    create_candidate_count: int
    business_write_count: int
    skipped_count: int

    def as_result_fields(self) -> dict[str, int]:
        return {
            "PROCESSED_COUNT": self.processed_count,
            "MATCHED_COUNT": self.matched_count,
            "MISSING_COUNT": self.missing_count,
            "CONFLICT_COUNT": self.conflict_count,
            "CREATE_CANDIDATE_COUNT": self.create_candidate_count,
            "BUSINESS_WRITE_COUNT": self.business_write_count,
        }


def reservation_query_filter(
    *,
    as_of: date,
    property_page_id: str | None = None,
    rental_unit_page_id: str | None = None,
) -> dict:
    clauses = [
        {"property": "데이터 환경", "select": {"equals": "PRODUCTION"}},
        {"property": "등록 상태", "select": {"equals": "APPROVED"}},
        {"property": "상태", "select": {"equals": "확정"}},
        {"property": "체크아웃", "date": {"on_or_after": as_of.isoformat()}},
    ]
    if property_page_id:
        clauses.append({"property": "연결 집", "relation": {"contains": property_page_id}})
    if rental_unit_page_id:
        clauses.append({"property": "연결 운영상품", "relation": {"contains": rental_unit_page_id}})
    return {"and": clauses}


def related_cleaning_query_filter(reservation_page_id: str) -> dict:
    return {"property": "관련 Reservation", "relation": {"contains": reservation_page_id}}


def classify_related_cleanings(cleanings: list[dict]) -> str:
    if len(cleanings) == 0:
        return MISSING
    if len(cleanings) == 1:
        return MATCHED
    return CONFLICT


def reconcile_pages(
    reservations: list[dict],
    *,
    as_of: date,
    related_cleaning_lookup: Callable[[str], list[dict]],
    property_page_id: str | None = None,
    rental_unit_page_id: str | None = None,
) -> ReconcileCounts:
    matched = missing = conflict = processed = skipped = 0
    for reservation in reservations:
        if not is_cleaning_reconciliation_eligible(reservation, as_of=as_of):
            skipped += 1
            continue
        if property_page_id and relation_ids(reservation, "연결 집") != [property_page_id]:
            skipped += 1
            continue
        if rental_unit_page_id and relation_ids(reservation, "연결 운영상품") != [rental_unit_page_id]:
            skipped += 1
            continue

        reservation_page_id = reservation.get("id")
        if not reservation_page_id:
            skipped += 1
            continue
        cleanings = related_cleaning_lookup(reservation_page_id)
        classification = classify_related_cleanings(cleanings)
        processed += 1
        if classification == MISSING:
            missing += 1
        elif classification == MATCHED:
            matched += 1
        else:
            conflict += 1

    return ReconcileCounts(
        processed_count=processed,
        matched_count=matched,
        missing_count=missing,
        conflict_count=conflict,
        create_candidate_count=missing,
        business_write_count=0,
        skipped_count=skipped,
    )


def run_read_only(
    client: ReadOnlyNotionClient,
    *,
    as_of: date,
    property_page_id: str | None = None,
    rental_unit_page_id: str | None = None,
) -> ReconcileCounts:
    reservations = client.query_data_source(
        reservation_source_id(),
        {"filter": reservation_query_filter(
            as_of=as_of,
            property_page_id=property_page_id,
            rental_unit_page_id=rental_unit_page_id,
        )},
    )

    def lookup(reservation_page_id: str) -> list[dict]:
        return client.query_data_source(
            cleaning_source_id(),
            {"filter": related_cleaning_query_filter(reservation_page_id)},
        )

    return reconcile_pages(
        reservations,
        as_of=as_of,
        related_cleaning_lookup=lookup,
        property_page_id=property_page_id,
        rental_unit_page_id=rental_unit_page_id,
    )


def load_listing_scope(mapping_path: Path, nickname: str) -> tuple[str, str]:
    mappings = json.loads(Path(mapping_path).read_text()).get("listings", {})
    matches = [value for value in mappings.values() if value.get("nickname") == nickname]
    if len(matches) != 1:
        raise ValueError(f"listing nickname must resolve exactly once: {nickname}")
    mapping = matches[0]
    property_page_id = mapping.get("property_page_id")
    rental_unit_page_id = mapping.get("rental_unit_page_id")
    if not property_page_id or not rental_unit_page_id:
        raise ValueError("listing scope requires property_page_id and rental_unit_page_id")
    return property_page_id, rental_unit_page_id


def main() -> int:
    parser = argparse.ArgumentParser(description="READ_ONLY Reservation -> Cleaning reconciliation")
    parser.add_argument("--notion-token-path", type=Path, default=DEFAULT_NOTION_TOKEN_PATH)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_LOCK_PATH)
    parser.add_argument("--mapping-path", type=Path)
    parser.add_argument("--listing-nickname")
    parser.add_argument("--as-of", type=date.fromisoformat)
    args = parser.parse_args()

    if bool(args.mapping_path) != bool(args.listing_nickname):
        parser.error("--mapping-path and --listing-nickname must be provided together")

    property_page_id = rental_unit_page_id = None
    if args.mapping_path:
        property_page_id, rental_unit_page_id = load_listing_scope(
            args.mapping_path, args.listing_nickname
        )

    as_of = args.as_of or datetime.now(KST).date()
    client = ReadOnlyNotionClient(args.notion_token_path)
    runner = ScheduledJobRunner(
        job_name="reservation_cleaning_reconciliation",
        lock_path=args.lock_path,
        mutation_authority_mode=MutationAuthorityMode.READ_ONLY,
    )

    def work(_context) -> JobOutcome:
        counts = run_read_only(
            client,
            as_of=as_of,
            property_page_id=property_page_id,
            rental_unit_page_id=rental_unit_page_id,
        )
        return JobOutcome(
            processed_count=counts.processed_count,
            changed_count=0,
            skipped_count=counts.skipped_count,
            error_count=0,
            details=counts.as_result_fields(),
        )

    result = runner.run(work)
    print(result.to_json_line())
    return 0 if result.error_count == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
