import pytest

from propertyai_core.notion_resources import (
    CLEANING_SOURCE_ID_ENV,
    RESERVATION_SOURCE_ID_ENV,
    NotionResourceBindingError,
    cleaning_source_id,
    reservation_source_id,
)


RESERVATION_SOURCE_ID = "11111111-1111-4111-8111-111111111111"
CLEANING_SOURCE_ID = "22222222-2222-4222-8222-222222222222"


def test_private_resource_bindings_fail_closed_when_missing():
    with pytest.raises(NotionResourceBindingError, match=f"{RESERVATION_SOURCE_ID_ENV}_REQUIRED"):
        reservation_source_id({})
    with pytest.raises(NotionResourceBindingError, match=f"{CLEANING_SOURCE_ID_ENV}_REQUIRED"):
        cleaning_source_id({})


@pytest.mark.parametrize("value", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_private_resource_bindings_reject_invalid_uuid(value):
    with pytest.raises(NotionResourceBindingError, match=f"{RESERVATION_SOURCE_ID_ENV}_INVALID"):
        reservation_source_id({RESERVATION_SOURCE_ID_ENV: value})


def test_private_resource_bindings_accept_synthetic_v4_uuid():
    environment = {
        RESERVATION_SOURCE_ID_ENV: RESERVATION_SOURCE_ID,
        CLEANING_SOURCE_ID_ENV: CLEANING_SOURCE_ID,
    }
    assert reservation_source_id(environment) == RESERVATION_SOURCE_ID
    assert cleaning_source_id(environment) == CLEANING_SOURCE_ID
