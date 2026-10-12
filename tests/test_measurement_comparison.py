"""Closed retained-measurement comparison behavior with fixture-only inputs."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import (
    GtmMeasurementComparisonCompleteness,
    MeasurementComparisonRequest,
    OpenAIMeasurementComparisonCompleteness,
    compare_measurements,
)
from adhelm.measurement_records import (
    Ga4MeasurementSnapshot,
    GtmMeasurementSnapshot,
    OpenAIMeasurementSnapshot,
)
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
    GtmWorkspaceMetadata,
)
from adhelm.providers.openai_ads import OpenAIAdsIdentity
from adhelm.providers.openai_conversions import OpenAIAdsConversionSetting
from adhelm.saved_measurements import retain_measurement

START = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def _hex(value: int) -> str:
    return f"{value:064x}"


def _ga4_snapshot(
    *,
    rows: tuple[tuple[int, str], ...] = ((700, "ONCE_PER_EVENT"),),
    stream: str = "456",
    with_default_value: bool = False,
) -> Ga4MeasurementSnapshot:
    identity = Ga4AdminIdentity(
        property_ref="ga4_admin.property.123",
        property_client_ref="123",
        web_stream_ref=f"ga4_admin.web_stream.{stream}",
        web_stream_client_ref=stream,
    )
    return Ga4MeasurementSnapshot(
        inventory=Ga4AdminInventory(
            identity=identity,
            key_events=tuple(
                Ga4AdminKeyEvent(
                    resource_ref=f"ga4_admin.key_event.{resource_id}",
                    resource_client_ref=str(resource_id),
                    property_ref=identity.property_ref,
                    configuration_fingerprint=_hex(resource_id),
                    event_name_fingerprint=_hex(resource_id + 1000),
                    counting_method=counting_method,  # type: ignore[arg-type]
                    default_value=(
                        Ga4AdminDefaultValue(numeric_value=Decimal("1.0"), currency="USD")
                        if with_default_value
                        else None
                    ),
                    custom=True,
                    deletable=True,
                )
                for resource_id, counting_method in rows
            ),
        )
    )


def _openai_snapshot(
    *,
    settings: tuple[tuple[str, int, int], ...] = (("setting_a", 0, 1),),
) -> OpenAIMeasurementSnapshot:
    identity = OpenAIAdsIdentity(
        account_ref="openai_ads.account.acct_fixture",
        account_client_ref="acct_fixture",
        timezone="UTC",
        currency="USD",
        status="active",
    )
    return OpenAIMeasurementSnapshot(
        identity=identity,
        settings=tuple(
            OpenAIAdsConversionSetting(
                account_ref=identity.account_ref,
                conversion_setting_ref=f"openai_ads.conversion_setting.{setting_id}",
                conversion_setting_client_ref=setting_id,
                event_type="order_created",
                attribution_window_days=30,
                status="NOT_ARCHIVED",
                campaign_count=campaign_count,
                version=version,
            )
            for setting_id, campaign_count, version in settings
        ),
    )


def _gtm_counts(*, tags: int = 0) -> GtmResourceCounts:
    return GtmResourceCounts(
        tags=tags,
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


def _gtm_snapshot(*, opaque_count: int = 1, live_version: str = "7") -> GtmMeasurementSnapshot:
    identity = GtmReadIdentity(
        account_ref="gtm.account.1",
        account_client_ref="1",
        container_ref="gtm.container.1.2",
        container_client_ref="2",
    )
    marker = GtmOpaqueCodeMetadata(
        kind="CUSTOM_HTML_TAG",
        configuration_fingerprint=_hex(11),
    )
    discovery = GtmIdentityDiscovery(
        identity=identity,
        account_features=GtmAccountFeatureAvailability(support_multiple_containers="FALSE"),
        container_features=GtmContainerFeatureAvailability(support_workspaces="TRUE"),
    )
    inventory = GtmReadInventory(
        identity=identity,
        live=GtmVersionSnapshot(
            context="live",
            version_ref=f"gtm.container_version.1.2.{live_version}",
            configuration_fingerprint=_hex(12 + int(live_version)),
            resource_counts=_gtm_counts(tags=opaque_count),
            opaque_code=(marker,) * opaque_count,
        ),
        latest=GtmVersionSnapshot(
            context="latest",
            version_ref="gtm.container_version.1.2.8",
            configuration_fingerprint=_hex(13),
            resource_counts=_gtm_counts(),
        ),
        latest_header_count_status=GtmLatestHeaderCountStatus(status="ALL_HEADER_COUNTS_MATCH"),
        workspaces=(
            GtmWorkspaceMetadata(
                workspace_ref="gtm.workspace.1.2.3",
                configuration_fingerprint=_hex(14),
            ),
        ),
    )
    return GtmMeasurementSnapshot(discovery=discovery, inventory=inventory)


def _retain(
    ledger: Ledger,
    snapshot: Ga4MeasurementSnapshot | OpenAIMeasurementSnapshot | GtmMeasurementSnapshot,
    *,
    profile_ref: str = "measurement.fixture",
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    return retain_measurement(
        ledger,
        snapshot,
        profile_ref=profile_ref,
        read_started_at=start,
        read_finished_at=end,
    )


def _comparison_records(
    ledger: Ledger,
    before_snapshot: Ga4MeasurementSnapshot | OpenAIMeasurementSnapshot | GtmMeasurementSnapshot,
    after_snapshot: Ga4MeasurementSnapshot | OpenAIMeasurementSnapshot | GtmMeasurementSnapshot,
    *,
    before_profile: str = "measurement.fixture",
    after_profile: str = "measurement.fixture",
    before_start: datetime = START,
    before_end: datetime = START + timedelta(seconds=4),
    after_start: datetime = START + timedelta(seconds=9),
    after_end: datetime = START + timedelta(seconds=13),
) -> tuple[dict[str, Any], dict[str, Any]]:
    before = _retain(
        ledger,
        before_snapshot,
        profile_ref=before_profile,
        start=before_start,
        end=before_end,
    )
    after = _retain(
        ledger,
        after_snapshot,
        profile_ref=after_profile,
        start=after_start,
        end=after_end,
    )
    return before, after


def test_ga4_comparison_binds_outer_interval_and_only_typed_absence(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(
        ledger,
        _ga4_snapshot(rows=((700, "ONCE_PER_EVENT"), (701, "ONCE_PER_EVENT"))),
        _ga4_snapshot(rows=((700, "ONCE_PER_SESSION"), (702, "ONCE_PER_EVENT"))),
    )

    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])

    assert comparison.checkpoint_digest == ledger.checkpoint()["checkpoint_digest"]
    assert comparison.observation_interval_start == START
    assert comparison.observation_interval_end == START + timedelta(seconds=13)
    assert comparison.observed_actor == "UNKNOWN"
    assert comparison.exact_change_time is None
    assert comparison.execution_authority is comparison.remote_mutations is False
    assert comparison.implementation_evidence == "FIXTURE_VERIFIED_LIVE_UNVERIFIED"
    assert comparison.fact_basis == "NORMALIZED_OBSERVATION_DIFFERENCE"
    assert comparison.fingerprint_key_continuity == "NOT_EVIDENCED"
    assert [(fact.resource_ref, fact.status, fact.changed_fields) for fact in comparison.facts] == [
        ("ga4_admin.key_event.700", "CHANGED", ("counting_method",)),
        ("ga4_admin.key_event.701", "OBSERVED_ABSENT_FROM_LIST", ()),
        ("ga4_admin.key_event.702", "ADDED", ()),
    ]
    assert (
        comparison.changed_count == comparison.added_count == comparison.observed_absent_count == 1
    )
    assert comparison.comparison_digest == digest(comparison)
    assert "DELETED" not in comparison.model_dump_json()


def test_comparison_requires_same_profile_scope_and_nonoverlapping_reads(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(
        ledger,
        _ga4_snapshot(),
        _ga4_snapshot(),
        after_profile="another.fixture",
    )
    with pytest.raises(AdhelmError) as profile_mismatch:
        compare_measurements(ledger, before["record_digest"], after["record_digest"])
    assert profile_mismatch.value.code == "MEASUREMENT_COMPARISON_SCOPE_MISMATCH"

    ledger = Ledger(tmp_path / "overlap")
    before, after = _comparison_records(
        ledger,
        _ga4_snapshot(),
        _ga4_snapshot(),
        after_start=START + timedelta(seconds=3),
    )
    with pytest.raises(AdhelmError) as overlap:
        compare_measurements(ledger, before["record_digest"], after["record_digest"])
    assert overlap.value.code == "MEASUREMENT_COMPARISON_INTERVAL_INVALID"

    ledger = Ledger(tmp_path / "stream")
    before, after = _comparison_records(ledger, _ga4_snapshot(), _ga4_snapshot(stream="457"))
    with pytest.raises(AdhelmError) as stream_mismatch:
        compare_measurements(ledger, before["record_digest"], after["record_digest"])
    assert stream_mismatch.value.code == "MEASUREMENT_COMPARISON_SCOPE_MISMATCH"


def test_openai_comparison_retains_fixture_caveat_when_no_rows_changed(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(ledger, _openai_snapshot(), _openai_snapshot())

    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])

    assert comparison.facts == ()
    assert (
        comparison.changed_count == comparison.added_count == comparison.observed_absent_count == 0
    )
    completeness = [
        item
        for item in comparison.source_completeness
        if isinstance(item, OpenAIMeasurementComparisonCompleteness)
    ]
    assert len(completeness) == 2
    assert [item.list_response_conformance for item in completeness] == [
        "FIXTURE_CONTRACT_LIVE_UNVERIFIED",
        "FIXTURE_CONTRACT_LIVE_UNVERIFIED",
    ]
    assert all(item.pagination_verified is True for item in completeness)
    assert comparison.fingerprint_key_continuity == "NOT_APPLICABLE"


def test_openai_absence_is_only_observed_from_complete_retained_list(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(
        ledger,
        _openai_snapshot(settings=(("setting_a", 0, 1), ("setting_b", 0, 1))),
        _openai_snapshot(settings=(("setting_a", 1, 2),)),
    )

    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])

    assert [(fact.resource_ref, fact.status) for fact in comparison.facts] == [
        ("openai_ads.conversion_setting.setting_a", "CHANGED"),
        ("openai_ads.conversion_setting.setting_b", "OBSERVED_ABSENT_FROM_LIST"),
    ]
    changed = comparison.facts[0]
    assert changed.changed_fields == ("campaign_count", "version")
    assert comparison.absence_semantics == "OBSERVED_ABSENT_FROM_LIST"


def test_gtm_comparison_preserves_partial_coverage_caveats_without_source_text(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(
        ledger, _gtm_snapshot(opaque_count=1), _gtm_snapshot(opaque_count=1)
    )

    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])

    assert comparison.facts == ()
    completeness = [
        item
        for item in comparison.source_completeness
        if isinstance(item, GtmMeasurementComparisonCompleteness)
    ]
    assert len(completeness) == 2
    assert [item.inventory_coverage for item in completeness] == [
        "VERSION_COUNTS_AND_WORKSPACE_METADATA",
        "VERSION_COUNTS_AND_WORKSPACE_METADATA",
    ]
    assert [item.dependency_completeness for item in completeness] == ["NOT_PROVEN", "NOT_PROVEN"]
    assert [item.observation_status for item in completeness] == [
        "SEQUENTIALLY_REREAD_NONATOMIC",
        "SEQUENTIALLY_REREAD_NONATOMIC",
    ]
    assert [item.staging_eligibility for item in completeness] == ["NOT_EVALUATED", "NOT_EVALUATED"]
    assert comparison.fingerprint_key_continuity == "NOT_EVIDENCED"
    serialized = comparison.model_dump_json()
    assert "opaque_code" not in serialized and "raw_source" not in serialized


def test_gtm_opaque_marker_compares_only_hmac_backed_observed_counts(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(
        ledger, _gtm_snapshot(opaque_count=1), _gtm_snapshot(opaque_count=2)
    )

    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])

    opaque = next(
        fact for fact in comparison.facts if fact.resource_type == "GTM_OPAQUE_CODE_MARKER"
    )
    assert opaque.status == "CHANGED"
    assert opaque.changed_fields == ("observed_count",)
    assert (opaque.before_observed_count, opaque.after_observed_count) == (1, 2)
    assert opaque.resource_ref.startswith("gtm.opaque_marker.live.CUSTOM_HTML_TAG.")


def test_gtm_version_slot_change_is_a_changed_slot_not_added_or_absent(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(
        ledger,
        _gtm_snapshot(live_version="7"),
        _gtm_snapshot(live_version="9"),
    )

    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])

    live = next(fact for fact in comparison.facts if fact.resource_type == "GTM_LIVE_VERSION")
    assert live.status == "CHANGED"
    assert live.resource_ref == "gtm.container.1.2.live_version_slot"
    assert "version_ref" in live.changed_fields
    assert not any(
        fact.resource_type == "GTM_LIVE_VERSION"
        and fact.status in {"ADDED", "OBSERVED_ABSENT_FROM_LIST"}
        for fact in comparison.facts
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), True),
        (("execution_authority",), 0),
        (("snapshot", "inventory", "pagination_verified"), 1),
    ],
)
def test_self_consistent_rehashed_source_rejects_literal_type_coercion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: tuple[str, ...],
    value: object,
) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(ledger, _ga4_snapshot(), _ga4_snapshot())
    checkpoint = deepcopy(ledger.checkpoint())
    records = checkpoint["records_by_stream"]["changes"]
    altered = next(
        record for record in records if record["record_digest"] == after["record_digest"]
    )
    target: dict[str, Any] = altered["payload"]
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value
    altered["record_digest"] = digest(
        {name: item for name, item in altered.items() if name != "record_digest"}
    )
    checkpoint["streams"]["changes"]["head_digest"] = altered["record_digest"]
    checkpoint["checkpoint_digest"] = digest(
        {name: checkpoint["streams"][name] for name in ("changes", "creative", "performance")}
    )
    monkeypatch.setattr(ledger, "checkpoint", lambda: checkpoint)

    with pytest.raises(AdhelmError) as raised:
        compare_measurements(ledger, before["record_digest"], altered["record_digest"])
    assert raised.value.code == "MEASUREMENT_COMPARISON_SOURCE_INVALID"


@pytest.mark.parametrize("raw_numeric", [1, 1.0])
def test_rehashed_ga4_default_value_rejects_numeric_json_before_decimal_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_numeric: int | float,
) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(
        ledger,
        _ga4_snapshot(with_default_value=True),
        _ga4_snapshot(with_default_value=True),
    )
    checkpoint = deepcopy(ledger.checkpoint())
    records = checkpoint["records_by_stream"]["changes"]
    altered = next(
        record for record in records if record["record_digest"] == after["record_digest"]
    )
    snapshot_payload = altered["payload"]["snapshot"]
    snapshot_payload["inventory"]["key_events"][0]["default_value"]["numeric_value"] = raw_numeric
    normalized_snapshot = Ga4MeasurementSnapshot.model_validate_json(
        canonical_bytes(snapshot_payload)
    )
    altered["payload"]["snapshot_digest"] = digest(normalized_snapshot)
    altered["record_digest"] = digest(
        {name: item for name, item in altered.items() if name != "record_digest"}
    )
    checkpoint["streams"]["changes"]["head_digest"] = altered["record_digest"]
    checkpoint["checkpoint_digest"] = digest(
        {name: checkpoint["streams"][name] for name in ("changes", "creative", "performance")}
    )
    monkeypatch.setattr(ledger, "checkpoint", lambda: checkpoint)

    with pytest.raises(AdhelmError) as raised:
        compare_measurements(ledger, before["record_digest"], altered["record_digest"])
    assert raised.value.code == "MEASUREMENT_COMPARISON_SOURCE_INVALID"


def test_request_and_result_schema_versions_and_control_booleans_are_exact(tmp_path: Path) -> None:
    selector = "a" * 64
    other_selector = "b" * 64
    with pytest.raises(ValidationError):
        MeasurementComparisonRequest.model_validate_json(
            f'{{"schema_version":true,"before_record_digest":"{selector}",'
            f'"after_record_digest":"{other_selector}"}}'
        )
    with pytest.raises(ValidationError):
        MeasurementComparisonRequest(
            before_record_digest=selector,
            after_record_digest=selector,
        )
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(
        ledger,
        _ga4_snapshot(rows=((700, "ONCE_PER_EVENT"),)),
        _ga4_snapshot(rows=((700, "ONCE_PER_SESSION"),)),
    )
    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])
    for field, value in (("schema_version", True), ("execution_authority", 0)):
        payload = comparison.model_dump(mode="json")
        payload[field] = value
        with pytest.raises(ValidationError):
            type(comparison).model_validate_json(json.dumps(payload))
    payload = comparison.model_dump(mode="json")
    payload["fingerprint_key_continuity"] = "NOT_APPLICABLE"
    with pytest.raises(ValidationError):
        type(comparison).model_validate_json(json.dumps(payload))
    payload = comparison.model_dump(mode="json")
    payload["source_completeness"][0]["pagination_verified"] = 1
    with pytest.raises(ValidationError):
        type(comparison).model_validate_json(json.dumps(payload))
    payload = comparison.model_dump(mode="json")
    payload["before"]["read_finished_at"] = (START + timedelta(seconds=10)).isoformat()
    with pytest.raises(ValidationError):
        type(comparison).model_validate_json(json.dumps(payload))
    payload = comparison.model_dump(mode="json")
    payload["facts"][0]["resource_type"] = "OPENAI_ACCOUNT_SETTINGS"
    payload["facts"][0]["changed_fields"] = ["status"]
    with pytest.raises(ValidationError):
        type(comparison).model_validate_json(json.dumps(payload))
    payload = comparison.model_dump(mode="json")
    payload["before"]["provider"] = "openai_ads"
    with pytest.raises(ValidationError):
        type(comparison).model_validate_json(json.dumps(payload))


def test_valid_comparison_reads_one_checkpoint_and_does_not_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = Ledger(tmp_path)
    before, after = _comparison_records(ledger, _openai_snapshot(), _openai_snapshot())
    expected_checkpoint = ledger.checkpoint()
    before_bytes = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / "ledger").rglob("*.jsonl")
    }
    calls = 0

    def one_checkpoint() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return expected_checkpoint

    monkeypatch.setattr(ledger, "checkpoint", one_checkpoint)
    comparison = compare_measurements(ledger, before["record_digest"], after["record_digest"])

    assert calls == 1
    assert comparison.checkpoint_digest == expected_checkpoint["checkpoint_digest"]
    assert {
        path.relative_to(tmp_path): path.read_bytes()
        for path in (tmp_path / "ledger").rglob("*.jsonl")
    } == before_bytes


def test_invalid_selector_fails_before_checkpoint_and_comparison_bound_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = Ledger(tmp_path)
    checkpoint_calls = 0

    def forbidden_checkpoint() -> dict[str, object]:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        raise AssertionError("invalid selection read history")

    monkeypatch.setattr(ledger, "checkpoint", forbidden_checkpoint)
    with pytest.raises(AdhelmError) as invalid:
        compare_measurements(ledger, "not-a-digest", "a" * 64)
    assert invalid.value.code == "INVALID_MEASUREMENT_COMPARISON_SELECTION"
    assert checkpoint_calls == 0

    ledger = Ledger(tmp_path / "bound")
    before, after = _comparison_records(
        ledger,
        _ga4_snapshot(rows=((700, "ONCE_PER_EVENT"), (701, "ONCE_PER_EVENT"))),
        _ga4_snapshot(rows=((702, "ONCE_PER_EVENT"), (703, "ONCE_PER_EVENT"))),
    )
    monkeypatch.setattr("adhelm.measurement_comparison.MAX_COMPARISON_FACTS", 1)
    with pytest.raises(AdhelmError) as oversized:
        compare_measurements(ledger, before["record_digest"], after["record_digest"])
    assert oversized.value.code == "MEASUREMENT_COMPARISON_TOO_LARGE"
