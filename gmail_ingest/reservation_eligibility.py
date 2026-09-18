"""Canonical Reservation eligibility helpers shared by read-only consumers."""

from __future__ import annotations

from datetime import date


def select_name(page: dict, name: str) -> str | None:
    return (page.get("properties", {}).get(name, {}).get("select") or {}).get("name")


def date_start(page: dict, name: str) -> str | None:
    return (page.get("properties", {}).get(name, {}).get("date") or {}).get("start")


def relation_ids(page: dict, name: str) -> list[str]:
    return [item["id"] for item in page.get("properties", {}).get(name, {}).get("relation", [])]


def is_canonical_confirmed_reservation(page: dict) -> bool:
    """Existing canonical rule used by live reservation/operation validation."""
    return (
        select_name(page, "데이터 환경") == "PRODUCTION"
        and select_name(page, "등록 상태") == "APPROVED"
        and select_name(page, "상태") == "확정"
    )


def is_cleaning_reconciliation_eligible(page: dict, *, as_of: date) -> bool:
    """Current/future canonical Reservation requiring a turnover-cleaning check."""
    if not is_canonical_confirmed_reservation(page):
        return False
    checkout = date_start(page, "체크아웃")
    if not checkout:
        return False
    try:
        checkout_date = date.fromisoformat(checkout[:10])
    except ValueError:
        return False
    return checkout_date >= as_of
