"""Stable headers cannot conceal a contradictory latest-version resource inventory."""

import pytest
from test_gtm_read_ultra_review import _client, _complete_handler, _header, _identity, _version

from adhelm.errors import AdhelmError


@pytest.mark.parametrize(
    "field",
    [
        "numTags",
        "numTriggers",
        "numVariables",
        "numZones",
        "numCustomTemplates",
        "numClients",
        "numGtagConfigs",
        "numTransformations",
    ],
)
def test_stable_header_count_mismatch_rejects_latest_snapshot(field: str) -> None:
    header = _header()
    header[field] = "1"
    with pytest.raises(AdhelmError, match="header counts differ"):
        _client(_complete_handler(header=header)).inventory(identity=_identity())


def test_matching_counts_do_not_claim_dependency_or_atomic_completeness() -> None:
    inventory = _client(_complete_handler()).inventory(identity=_identity())
    assert inventory.latest.resource_counts.tags == 0
    assert inventory.latest_header_count_status.status == "ALL_HEADER_COUNTS_MATCH"
    assert inventory.latest_header_count_status.missing_counter_fields == ()
    assert inventory.observation_status == "SEQUENTIALLY_REREAD_NONATOMIC"
    assert inventory.staging_eligibility == "NOT_EVALUATED"
    assert inventory.latest.browser_status == "NOT_PROVEN_BY_INVENTORY"


def test_omitted_and_null_header_counts_stay_explicitly_unavailable_not_zero() -> None:
    header = _header()
    del header["numTags"]
    header["numTriggers"] = None
    latest = _version("8", tags=[{"tagId": "1"}])

    inventory = _client(_complete_handler(header=header, latest=latest)).inventory(
        identity=_identity()
    )

    assert inventory.latest.resource_counts.tags == 1
    assert inventory.latest_header_count_status.status == "HEADER_COUNTS_UNAVAILABLE"
    assert inventory.latest_header_count_status.missing_counter_fields == (
        "numTags",
        "numTriggers",
    )


def test_unavailable_header_counts_do_not_bypass_a_supplied_mismatch() -> None:
    header = _header()
    del header["numTags"]
    header["numTriggers"] = "1"

    with pytest.raises(AdhelmError) as raised:
        _client(_complete_handler(header=header)).inventory(identity=_identity())

    assert raised.value.code == "INVENTORY_INCONSISTENT"


@pytest.mark.parametrize("value", ["", "-1", "1.0", "4001", 1])
def test_malformed_supplied_header_count_fails_closed(value: object) -> None:
    header = _header()
    header["numTags"] = value

    with pytest.raises(AdhelmError) as raised:
        _client(_complete_handler(header=header)).inventory(identity=_identity())

    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
