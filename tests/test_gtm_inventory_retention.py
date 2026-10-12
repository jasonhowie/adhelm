"""Retention, review, and refusal boundaries for the complete GTM Web inventory.

These tests use `httpx.MockTransport` and the real installed parsers. They never
reach a live provider, and they assert that no custom-code source or credential
value reaches canonical history, a review envelope, or an export.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_gtm_inventory import (
    _PARENT,
    _SENTINEL,
    _Fixture,
    _install_workflow_fixture,
)
from typer.testing import CliRunner

from adhelm import measurement_cli
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import compare_measurements
from adhelm.measurement_records import (
    Ga4MeasurementSnapshot,
    GtmFullInventoryMeasurementSnapshot,
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
from adhelm.saved_measurements import load_measurement, measurement_rows

# Golden digests recorded from the exact snapshot models that existed before the
# additive `gtm_web_inventory` union member. A change here means an already
# retained observation would no longer match its recorded snapshot digest.
_GOLDEN_GA4 = "3ec70f88247990c83d69cda8fe7e635e3b56f6f711f2f8c2fe0520f95d3cf8c1"
_GOLDEN_OPENAI = "2dd4f894f8f459ea3667891735fa4e3e2da7b1c1e72e82ff737c099a01bb9e59"
_GOLDEN_GTM = "bf422c204f5bec769142f949676a82601144c9af8d5f3c0889933f463f531d00"


def _hex(value: int) -> str:
    return f"{value:064x}"


def _golden_ga4() -> Ga4MeasurementSnapshot:
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
                    configuration_fingerprint=_hex(700),
                    event_name_fingerprint=_hex(1700),
                    counting_method="ONCE_PER_EVENT",
                    default_value=Ga4AdminDefaultValue(
                        numeric_value=Decimal("1.0"), currency="USD"
                    ),
                    custom=True,
                    deletable=True,
                ),
            ),
        )
    )


def _golden_openai() -> OpenAIMeasurementSnapshot:
    identity = OpenAIAdsIdentity(
        account_ref="openai_ads.account.acct_fixture",
        account_client_ref="acct_fixture",
        timezone="UTC",
        currency="USD",
        status="active",
    )
    return OpenAIMeasurementSnapshot(
        identity=identity,
        settings=(
            OpenAIAdsConversionSetting(
                account_ref=identity.account_ref,
                conversion_setting_ref="openai_ads.conversion_setting.setting_a",
                conversion_setting_client_ref="setting_a",
                event_type="order_created",
                attribution_window_days=30,
                status="NOT_ARCHIVED",
                campaign_count=0,
                version=1,
            ),
        ),
    )


def _counts(tags: int = 0) -> GtmResourceCounts:
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


def _golden_gtm() -> GtmMeasurementSnapshot:
    identity = GtmReadIdentity(
        account_ref="gtm.account.1",
        account_client_ref="1",
        container_ref="gtm.container.1.2",
        container_client_ref="2",
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
                configuration_fingerprint=_hex(19),
                resource_counts=_counts(1),
                opaque_code=(
                    GtmOpaqueCodeMetadata(
                        kind="CUSTOM_HTML_TAG", configuration_fingerprint=_hex(11)
                    ),
                ),
            ),
            latest=GtmVersionSnapshot(
                context="latest",
                version_ref="gtm.container_version.1.2.8",
                configuration_fingerprint=_hex(13),
                resource_counts=_counts(),
            ),
            latest_header_count_status=GtmLatestHeaderCountStatus(status="ALL_HEADER_COUNTS_MATCH"),
            workspaces=(
                GtmWorkspaceMetadata(
                    workspace_ref="gtm.workspace.1.2.3", configuration_fingerprint=_hex(14)
                ),
            ),
        ),
    )


def test_existing_measurement_snapshot_digests_survive_the_additive_union() -> None:
    assert digest(_golden_ga4()) == _GOLDEN_GA4
    assert digest(_golden_openai()) == _GOLDEN_OPENAI
    assert digest(_golden_gtm()) == _GOLDEN_GTM
    # The existing GTM snapshot must not have gained a field of any kind.
    assert set(_golden_gtm().model_dump(mode="json")) == {
        "provider",
        "capability_version",
        "discovery",
        "inventory",
        "inventory_coverage",
        "dependency_completeness",
    }


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: _Fixture) -> CliRunner:
    _install_workflow_fixture(monkeypatch, tmp_path, fixture)
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)
    monkeypatch.setattr("adhelm.credentials.export_key", lambda: b"k" * 32)
    return CliRunner()


def _retain(tmp_path: Path, runner: CliRunner, *, full: bool) -> dict[str, Any]:
    arguments = [
        "--data-dir",
        str(tmp_path),
        "measurement",
        "gtm",
        "gtm-read",
        "2",
        "--ci-enabled",
    ]
    if full:
        arguments.append("--full-inventory")
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    return dict(json.loads(result.output)["data"])


def test_full_inventory_flag_retains_a_verified_observation_without_leaking_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture()
    runner = _configure(tmp_path, monkeypatch, fixture)
    receipt = _retain(tmp_path, runner, full=True)

    assert receipt["provider"] == "gtm_web_inventory"
    assert receipt["persisted"] is True
    assert receipt["execution_authority"] is False
    assert receipt["remote_mutations"] is False
    assert receipt["row_count"] > 0

    observation, provenance = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    snapshot = observation.snapshot
    assert isinstance(snapshot, GtmFullInventoryMeasurementSnapshot)
    assert snapshot.provider == "gtm_web_inventory"
    assert snapshot.capability_version == "gtm-full-fixed-inventory-1"
    assert snapshot.dependency_completeness == "NOT_PROVEN"
    assert snapshot.inventory.counts.resources == len(snapshot.inventory.resources)
    assert observation.snapshot_digest == digest(snapshot)
    assert provenance["checkpoint_digest"]
    assert snapshot.row_count == receipt["row_count"]

    history = next((tmp_path / "ledger/changes").glob("*.jsonl")).read_text()
    for private in (_SENTINEL, "fixture-token-not-for-output", "customer container"):
        assert private not in history

    verified = runner.invoke(app, ["--data-dir", str(tmp_path), "history", "verify"])
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.output)["data"]["unresolved_apply_starts"] == 0

    indexed = runner.invoke(app, ["--data-dir", str(tmp_path), "history", "index"])
    assert indexed.exit_code == 0, indexed.output
    tailed = runner.invoke(app, ["--data-dir", str(tmp_path), "history", "tail", "--limit", "1"])
    assert tailed.exit_code == 0, tailed.output
    assert _SENTINEL not in tailed.output

    export_root = tmp_path / "export"
    exported = runner.invoke(
        app, ["--data-dir", str(tmp_path), "history", "export", "--output", str(export_root)]
    )
    assert exported.exit_code == 0, exported.output
    exported_files = [path for path in export_root.rglob("*") if path.is_file()]
    assert exported_files
    for path in exported_files:
        assert _SENTINEL not in path.read_text(errors="ignore")


def test_catalog_and_show_review_the_new_snapshot_kind_without_rereading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture()
    runner = _configure(tmp_path, monkeypatch, fixture)
    receipt = _retain(tmp_path, runner, full=True)
    requests_after_read = len(fixture.requests)

    catalog = runner.invoke(app, ["--data-dir", str(tmp_path), "measurement", "catalog"])
    assert catalog.exit_code == 0, catalog.output
    listed = json.loads(catalog.output)["data"]["measurements"]
    assert [item["provider"] for item in listed] == ["gtm_web_inventory"]

    shown = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "show",
            receipt["record_digest"],
            "--limit",
            "5",
        ],
    )
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.output)["data"]
    assert data["capability_version"] == "gtm-full-fixed-inventory-1"
    assert data["dependency_completeness"] == "NOT_PROVEN"
    assert data["ui_reconciliation"] == "NOT_PERFORMED"
    assert data["dependency_graph"] == "RAW_EDGES_ONLY_NOT_A_GRAPH"
    assert data["custom_code_source"] == "CLASSIFIED_AND_HASHED_NEVER_PERSISTED"
    assert data["execution_authority"] is False
    assert data["live_latest"]["staging_eligibility"] == "NOT_EVALUATED"
    assert _SENTINEL not in shown.output
    assert len(fixture.requests) == requests_after_read

    observation, _ = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    kinds = {row["resource_type"] for row in measurement_rows(observation)}
    assert kinds == {"GTM_COLLECTION_COVERAGE", "GTM_RESOURCE", "GTM_REFERENCE_EDGE"}


def test_default_gtm_command_still_retains_the_existing_snapshot_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture()
    runner = _configure(tmp_path, monkeypatch, fixture)
    receipt = _retain(tmp_path, runner, full=False)
    assert receipt["provider"] == "gtm"
    observation, _ = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    assert isinstance(observation.snapshot, GtmMeasurementSnapshot)
    assert observation.snapshot.capability_version == "gtm-versions-workspace-metadata-1"
    assert [request.url.path for request in fixture.requests] == [
        "/tagmanager/v2/accounts/1",
        f"/tagmanager/v2/{_PARENT}",
        f"/tagmanager/v2/{_PARENT}/version_headers:latest",
        f"/tagmanager/v2/{_PARENT}/versions:live",
        f"/tagmanager/v2/{_PARENT}/versions/8",
        f"/tagmanager/v2/{_PARENT}/workspaces",
        f"/tagmanager/v2/{_PARENT}/version_headers:latest",
        f"/tagmanager/v2/{_PARENT}/versions:live",
    ]


def test_comparison_refuses_the_full_inventory_with_an_explicit_typed_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture()
    runner = _configure(tmp_path, monkeypatch, fixture)
    first = _retain(tmp_path, runner, full=True)
    second = _retain(tmp_path, runner, full=True)

    with pytest.raises(AdhelmError) as refusal:
        compare_measurements(Ledger(tmp_path), first["record_digest"], second["record_digest"])
    assert refusal.value.code == "NOT_COMPARABLE_FULL_INVENTORY"
    assert refusal.value.exit_code == 2

    compared = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "compare",
            first["record_digest"],
            second["record_digest"],
        ],
    )
    assert compared.exit_code == 2, compared.output
    envelope = json.loads(compared.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert envelope["diagnostics"][0]["code"] == "NOT_COMPARABLE_FULL_INVENTORY"


def test_discover_still_reports_the_gtm_subset_as_fixture_verified_and_unproven(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(app, ["--data-dir", str(tmp_path), "discover"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert "measurement gtm" in data["commands"]
    assert "measurement-gtm-read" in data["schemas"]
    gtm = next(item for item in data["providers"] if item["provider"] == "gtm")
    assert gtm["implementation"] == "FIXTURE_VERIFIED_READ_SUBSET"
    assert gtm["live_verified"] is False


def test_full_inventory_request_schema_stays_additive_and_defaults_off(
    tmp_path: Path,
) -> None:
    result = CliRunner().invoke(
        app, ["--data-dir", str(tmp_path), "schema", "measurement-gtm-read"]
    )
    assert result.exit_code == 0, result.output
    properties = json.loads(result.output)["data"]["schema"]["properties"]
    assert properties["full_inventory"]["default"] is False
    assert properties["schema_version"]["const"] == 1


def test_retained_full_inventory_binds_its_container_scoped_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture()
    runner = _configure(tmp_path, monkeypatch, fixture)
    receipt = _retain(tmp_path, runner, full=True)
    observation, _ = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    payload = observation.snapshot.model_dump(mode="json")
    payload["inventory"]["resources"][0]["scope_ref"] = "gtm.workspace.9.9.9"
    with pytest.raises(ValueError):
        GtmFullInventoryMeasurementSnapshot.model_validate(payload)


def test_full_inventory_read_failure_retains_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(status={"/zones": 403})
    runner = _configure(tmp_path, monkeypatch, fixture)
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "gtm",
            "gtm-read",
            "2",
            "--ci-enabled",
            "--full-inventory",
        ],
    )
    assert result.exit_code == 4, result.output
    assert json.loads(result.output)["diagnostics"][0]["code"] == "GTM_ACCESS_NOT_PERMITTED"
    catalog = runner.invoke(app, ["--data-dir", str(tmp_path), "measurement", "catalog"])
    assert json.loads(catalog.output)["data"]["measurements"] == []
    assert isinstance(httpx.Response, type)


def test_retention_refuses_an_unresolved_name_selector_that_still_carries_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger boundary re-checks the rule; it does not trust its producer.

    A name-form selector is the only free-text channel a reference edge has. If
    it names no resource this read saw, it is arbitrary customer configuration,
    often a fragment of custom code, and it must not enter append-only history.
    """

    fixture = _Fixture()
    runner = _configure(tmp_path, monkeypatch, fixture)
    receipt = _retain(tmp_path, runner, full=True)
    observation, _ = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    payload = observation.snapshot.model_dump(mode="json")

    edge = next(
        item
        for item in payload["inventory"]["edges"]
        if item["selector_form"] == "NAME" and item["resolution"] != "RESOLVED"
    )
    assert edge["selector"] is None
    assert edge["selector_status"] == "WITHHELD_UNRESOLVED_REFERENCE"
    edge["selector"] = "Missing Teardown"
    edge["selector_status"] = "PRESENT"
    with pytest.raises(ValueError):
        GtmFullInventoryMeasurementSnapshot.model_validate(payload)
