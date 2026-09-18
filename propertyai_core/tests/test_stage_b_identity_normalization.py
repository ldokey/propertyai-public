from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from propertyai_core.stage_b.identity import (
    ROOT_NAMESPACE,
    IdentityRegistry,
    assignment_identity,
    deterministic_id,
    exact_unique_match,
    notion_identity,
    telegram_identity,
)
from propertyai_core.stage_b.models import AmbiguousIdentityError, ImmutableIdentityConflict
from propertyai_core.stage_b.normalize import (
    canonical_json,
    normalize_datetime,
    normalize_page_uuid,
    normalize_status,
    semantic_hash,
)


def test_root_namespace_and_exact_uuid5_are_frozen():
    assert ROOT_NAMESPACE == UUID("af69a66b-3c14-5f73-9544-78da052ee5c3")
    assert deterministic_id("organization:PROPERTYAI_PRODUCTION") == deterministic_id(
        "organization:PROPERTYAI_PRODUCTION"
    )
    with pytest.raises(ValueError):
        deterministic_id("organization:propertyai_production")


def test_notion_assignment_and_telegram_identities_use_only_durable_values():
    page = "12345678-1234-4234-9234-123456789abc"
    assert notion_identity("property", page) == notion_identity(
        "property", "https://notion.so/title-12345678123442349234123456789abc"
    )
    assert assignment_identity(page) == deterministic_id(f"assignment:notion19:{page}")
    assert telegram_identity("123") == deterministic_id("external-identity:telegram:user:123")
    with pytest.raises(ValueError):
        telegram_identity("00123")


def test_notion_uuid_v8_is_exactly_supported_without_permissive_substring_parsing():
    page = "12345678-1234-8234-9234-123456789abc"
    assert normalize_page_uuid(page) == page
    assert normalize_page_uuid(page.replace("-", "")) == page
    assert normalize_page_uuid(f"https://notion.so/title-{page.replace('-', '')}") == page

    for invalid in (
        f"prefix-{page}",
        f"{page}-suffix",
        "12345678-1234-9234-9234-123456789abc",
        "12345678-1234-8234-7234-123456789abc",
        "12345678-1234-0234-9234-123456789abc",
        "https://example.com/title-12345678123482349234123456789abc",
    ):
        with pytest.raises(ValueError):
            normalize_page_uuid(invalid)


def test_uuid_v8_is_accepted_across_every_durable_notion_identity_kind():
    page = "12345678-1234-8234-9234-123456789abc"
    notion_kinds = ("property", "rental-unit", "party", "reservation", "cleaning", "roster")
    notion19_kinds = ("assignment", "unavailability", "reassignment")

    for kind in notion_kinds:
        seed = f"{kind}:notion:{page}"
        assert deterministic_id(seed) == notion_identity(kind, page)
    for kind in notion19_kinds:
        assert deterministic_id(f"{kind}:notion19:{page}")
    assert assignment_identity(page) == deterministic_id(f"assignment:notion19:{page}")


def test_durable_notion_identity_uuid_contract_remains_canonical_and_fail_closed():
    for version in range(1, 9):
        assert deterministic_id(
            f"property:notion:12345678-1234-{version}234-9234-123456789abc"
        )

    rejected_pages = (
        "12345678-1234-8234-7234-123456789abc",  # non-RFC variant
        "12345678-1234-0234-9234-123456789abc",  # version zero
        "12345678-1234-9234-9234-123456789abc",  # unsupported version nine
        "12345678123482349234123456789abc",  # non-canonical compact seed
        "12345678-1234-8234-9234-123456789ABC",  # non-canonical case
        "prefix-12345678-1234-8234-9234-123456789abc",
        "12345678-1234-8234-9234-123456789abc-suffix",
    )
    for page in rejected_pages:
        with pytest.raises(ValueError):
            deterministic_id(f"property:notion:{page}")


def test_mutable_or_unsupported_identity_seeds_are_rejected():
    with pytest.raises(ValueError):
        deterministic_id("nickname:cleaner-one")
    with pytest.raises(ValueError):
        deterministic_id("no-namespace")
    with pytest.raises(ValueError):
        normalize_page_uuid("not-a-page")


def test_identity_registry_detects_ambiguity_and_immutable_collision():
    registry = IdentityRegistry()
    first = deterministic_id("party:notion:12345678-1234-4234-9234-123456789abc")
    second = deterministic_id("party:notion:22345678-1234-4234-9234-123456789abc")
    registry.register("notion:row-1", first, {"page": "one"})
    with pytest.raises(AmbiguousIdentityError):
        registry.register("notion:row-1", second, {"page": "two"})
    with pytest.raises(ImmutableIdentityConflict):
        registry.register("notion:row-2", first, {"page": "different"})


def test_exact_matching_never_falls_back_to_fuzzy_values():
    assert exact_unique_match("A", ["A", "a"]) == "A"
    assert exact_unique_match("A ", ["A"]) is None
    with pytest.raises(AmbiguousIdentityError):
        exact_unique_match("A", ["A", "A"])


def test_canonical_normalization_is_timezone_and_order_stable():
    first = {
        "when": datetime(2026, 9, 5, 9, tzinfo=timezone.utc),
        "set": {"b", "a"},
    }
    second = {"set": {"a", "b"}, "when": "2026-09-05T09:00:00.000000Z"}
    assert canonical_json(first) == canonical_json(second)
    assert semantic_hash(first) == semantic_hash(second)
    assert normalize_datetime("2026-09-05T18:00:00+09:00").hour == 9
    assert normalize_status("hard-booked") == "HARD_BOOKED"


def test_naive_datetimes_fail_closed():
    with pytest.raises(ValueError):
        normalize_datetime(datetime(2026, 9, 5))


def test_durable_identity_parser_requires_exact_suffix_and_provider_contract():
    page = "12345678-1234-4234-9234-123456789abc"
    accepted = (
        f"property:notion:{page}",
        f"rental-unit:notion:{page}",
        f"party:notion:{page}",
        f"reservation:notion:{page}",
        f"cleaning:notion:{page}",
        f"roster:notion:{page}",
        f"assignment:notion19:{page}",
        "external-identity:telegram:user:123456",
    )
    for seed in accepted:
        assert deterministic_id(seed)

    rejected = (
        f"property:notion19:{page}",
        f"assignment:notion:{page}",
        f"assignment:notion19:{page}:suffix",
        f"party:notion:{page}:extra",
        "external-identity:telegram:chat:123456",
        "external-identity:telegram:user:00123456",
        "external-identity:telegram:user:-1",
        "external-identity:slack:user:123456",
    )
    for seed in rejected:
        with pytest.raises(ValueError):
            deterministic_id(seed)
