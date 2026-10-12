"""Independent adversarial checks for retained measurement comparison."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adhelm import measurement_cli
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import compare_measurements
from adhelm.measurement_records import Ga4MeasurementSnapshot, GtmMeasurementSnapshot
from adhelm.providers.ga4_admin import (
    Ga4AdminDefaultValue,
    Ga4AdminIdentity,
    Ga4AdminInventory,
    Ga4AdminKeyEvent,
)
from adhelm.providers.gtm_identity import (
    GtmAccountFeatureAvailability,
    GtmContainerFeatureAvailability,
    GtmIdentityDiscovery,
)
from adhelm.providers.gtm_read import (
    GtmLatestHeaderCountStatus,
    GtmOpaqueCodeMetadata,
    GtmReadIdentity,
    GtmReadInventory,
    GtmResourceCounts,
    GtmVersionSnapshot,
)
from adhelm.saved_measurements import retain_measurement

_START = datetime(2026, 9, 5, 16, 0, tzinfo=UTC)


def _ga4_snapshot(
    *,
    configuration_fingerprint: str = "a" * 64,
    event_name_fingerprint: str = "b" * 64,
    default_value: Ga4AdminDefaultValue | None = None,
) -> Ga4MeasurementSnapshot:
    identity = Ga4AdminIdentity(
        property_ref="ga4_admin.property.123",
        property_client_ref="123",
        web_stream_ref="ga4_admin.web_stream.456",
        web_stream_client_ref="456",
    )
    return Ga4MeasurementSnapshot(
        inventory=Ga4AdminInventory(
            identity=identity,
            key_events=(
                Ga4AdminKeyEvent(
                    resource_ref="ga4_admin.key_event.700",
                    resource_client_ref="700",
                    property_ref=identity.property_ref,
                    configuration_fingerprint=configuration_fingerprint,
                    event_name_fingerprint=event_name_fingerprint,
                    counting_method="ONCE_PER_EVENT",
                    default_value=default_value,
                    custom=True,
                    deletable=True,
                ),
            ),
        )
    )


def _gtm_snapshot(*, opaque_count: int) -> GtmMeasurementSnapshot:
    identity = GtmReadIdentity(
        account_ref="gtm.account.1",
        account_client_ref="1",
        container_ref="gtm.container.1.2",
        container_client_ref="2",
    )
    marker = GtmOpaqueCodeMetadata(
        kind="CUSTOM_HTML_TAG",
        configuration_fingerprint="c" * 64,
    )
    counts = GtmResourceCounts(
        tags=opaque_count,
        triggers=0,
        variables=0,
        folders=0,
        built_in_variables=0,
        zones=0,
        custom_templates=0,
        clients=0,
        gtag_configs=0,
        transformations=0,
    )
    return GtmMeasurementSnapshot(
        discovery=GtmIdentityDiscovery(
            identity=identity,
            account_features=GtmAccountFeatureAvailability(support_multiple_containers="FALSE"),
            container_features=GtmContainerFeatureAvailability(support_workspaces="TRUE"),
        ),
        inventory=GtmReadInventory(
            identity=identity,
            live=GtmVersionSnapshot(
                context="live",
                version_ref="gtm.container_version.1.2.7",
                configuration_fingerprint="d" * 64,
                resource_counts=counts,
                opaque_code=(marker,) * opaque_count,
            ),
            latest=GtmVersionSnapshot(
                context="latest",
                version_ref="gtm.container_version.1.2.8",
                configuration_fingerprint="e" * 64,
                resource_counts=GtmResourceCounts(
                    tags=0,
                    triggers=0,
                    variables=0,
                    folders=0,
                    built_in_variables=0,
                    zones=0,
                    custom_templates=0,
                    clients=0,
                    gtag_configs=0,
                    transformations=0,
                ),
            ),
            latest_header_count_status=GtmLatestHeaderCountStatus(status="ALL_HEADER_COUNTS_MATCH"),
        ),
    )


def _retain_pair(
    ledger: Ledger,
    before: Ga4MeasurementSnapshot | GtmMeasurementSnapshot,
    after: Ga4MeasurementSnapshot | GtmMeasurementSnapshot,
) -> tuple[dict[str, Any], dict[str, Any]]:
    first = retain_measurement(
        ledger,
        before,
        profile_ref="measurement.ultra.fixture",
        read_started_at=_START,
        read_finished_at=_START + timedelta(seconds=2),
    )
    second = retain_measurement(
        ledger,
        after,
        profile_ref="measurement.ultra.fixture",
        read_started_at=_START + timedelta(seconds=4),
        read_finished_at=_START + timedelta(seconds=6),
    )
    return first, second


def test_hmac_rotation_shaped_difference_preserves_not_evidenced_continuity(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path)
    before, after = _retain_pair(
        ledger,
        _ga4_snapshot(),
        _ga4_snapshot(configuration_fingerprint="f" * 64, event_name_fingerprint="e" * 64),
    )
    checkpoint_digest = ledger.checkpoint()["checkpoint_digest"]

    first = compare_measurements(ledger, before["record_digest"], after["record_digest"])
    second = compare_measurements(ledger, before["record_digest"], after["record_digest"])

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint_digest
    assert first.fact_basis == "NORMALIZED_OBSERVATION_DIFFERENCE"
    assert first.fingerprint_key_continuity == "NOT_EVIDENCED"
    assert first.observed_actor == "UNKNOWN" and first.exact_change_time is None
    assert [(fact.status, fact.changed_fields) for fact in first.facts] == [
        ("CHANGED", ("configuration_fingerprint", "event_name_fingerprint")),
    ]
    assert "DELETED" not in first.model_dump_json()


def test_gtm_opaque_marker_absence_is_not_deletion_or_raw_custom_code(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path)
    before, after = _retain_pair(
        ledger,
        _gtm_snapshot(opaque_count=1),
        _gtm_snapshot(opaque_count=0),
    )

    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])

    opaque_fact = next(
        fact for fact in comparison.facts if fact.resource_type == "GTM_OPAQUE_CODE_MARKER"
    )
    assert opaque_fact.status == "OBSERVED_ABSENT_FROM_LIST"
    assert (opaque_fact.before_observed_count, opaque_fact.after_observed_count) == (1, None)
    assert comparison.absence_semantics == "OBSERVED_ABSENT_FROM_LIST"
    assert comparison.fingerprint_key_continuity == "NOT_EVIDENCED"
    assert all(
        item.dependency_completeness == "NOT_PROVEN"
        and item.observation_status == "SEQUENTIALLY_REREAD_NONATOMIC"
        for item in comparison.source_completeness
    )
    serialized = comparison.model_dump_json()
    assert "DELETED" not in serialized
    assert all(name not in serialized for name in ("raw_source", "source_code", "request_body"))


def test_cli_redacts_gtm_native_references_and_hmac_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = Ledger(tmp_path)
    before, after = _retain_pair(
        ledger,
        _gtm_snapshot(opaque_count=1),
        _gtm_snapshot(opaque_count=0),
    )
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "compare",
            before["record_digest"],
            after["record_digest"],
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["fingerprint_key_continuity"] == "NOT_EVIDENCED"
    assert data["fact_basis"] == "NORMALIZED_OBSERVATION_DIFFERENCE"
    assert data["scope"]["account_ref"].startswith("opaque:")
    assert all(fact["resource_ref"].startswith("opaque:") for fact in data["facts"])
    for private_value in (
        "measurement.ultra.fixture",
        "gtm.account.1",
        "gtm.container.1.2",
        "c" * 64,
        "d" * 64,
        "e" * 64,
    ):
        assert private_value not in result.output


@pytest.mark.parametrize("numeric_value", [1, 1.0])
def test_rehashed_ga4_decimal_json_number_is_rejected_before_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    numeric_value: int | float,
) -> None:
    ledger = Ledger(tmp_path)
    default_value = Ga4AdminDefaultValue(numeric_value=Decimal("1.0"), currency="USD")
    before, after = _retain_pair(
        ledger,
        _ga4_snapshot(default_value=default_value),
        _ga4_snapshot(default_value=default_value),
    )
    forged_checkpoint = deepcopy(ledger.checkpoint())
    forged_record = next(
        record
        for record in forged_checkpoint["records_by_stream"]["changes"]
        if record["record_digest"] == after["record_digest"]
    )
    raw_snapshot = forged_record["payload"]["snapshot"]
    raw_snapshot["inventory"]["key_events"][0]["default_value"]["numeric_value"] = numeric_value
    # Simulate a fully self-consistent rehash. The comparison boundary must
    # still reject the noncanonical scalar before `canonical_bytes` can turn
    # an integer-looking float into an integer.
    forged_record["payload"]["snapshot_digest"] = digest(
        Ga4MeasurementSnapshot.model_validate_json(canonical_bytes(raw_snapshot))
    )
    forged_record["record_digest"] = digest(
        {name: value for name, value in forged_record.items() if name != "record_digest"}
    )
    forged_checkpoint["streams"]["changes"]["head_digest"] = forged_record["record_digest"]
    forged_checkpoint["checkpoint_digest"] = digest(
        {
            stream: forged_checkpoint["streams"][stream]
            for stream in ("changes", "creative", "performance")
        }
    )
    monkeypatch.setattr(ledger, "checkpoint", lambda: forged_checkpoint)

    with pytest.raises(AdhelmError) as raised:
        compare_measurements(ledger, before["record_digest"], forged_record["record_digest"])

    assert raised.value.code == "MEASUREMENT_COMPARISON_SOURCE_INVALID"
    assert "numeric_value" not in str(raised.value)
