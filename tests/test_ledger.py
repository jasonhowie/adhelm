from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import pytest

from adhelm.canonical import canonical_bytes, digest
from adhelm.creative import (
    AssetAnalysisRecord,
    CreativeAssetVersion,
    CreativeComponent,
    CreativeDeployment,
    ImageAnalysis,
    PaletteColor,
)
from adhelm.creative_registration_closure import (
    CreativeIdentityRegistrationInput,
    CreativeTextRegistrationInput,
    build_creative_registration_closure,
    build_text_creative_registration_closure,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import SCHEMA_URI, Ledger
from adhelm.reporting import LedgerCheckpoint, NormalizedReport


def change(event_type: str = "APPLY_STARTED", **extra: object) -> dict[str, object]:
    return {
        "event_type": event_type,
        "operation": "CREATE",
        "origin": "LOCAL_CLI",
        "resource_type": "CAMPAIGN",
        "resource_ref": "campaign:one",
        "effect_status": "NO_REMOTE_WRITE",
        "idempotency_ref": "attempt:one",
        **extra,
    }


def test_chain_unresolved_and_rebuildable_index(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    started = ledger.append("changes", "CHANGE_EVENT", change(), provider="google_ads")
    assert ledger.unresolved_starts() == [started]
    ledger.append("changes", "CHANGE_EVENT", change("APPLY_RESULT"), provider="google_ads")
    assert ledger.unresolved_starts() == [started]
    ledger.append(
        "changes",
        "CHANGE_EVENT",
        change(
            "VERIFICATION_RESULT",
            effect_status="APPLIED_VERIFIED",
            evidence_refs=["evidence:" + "a" * 64],
        ),
        provider="google_ads",
    )
    verified = ledger.verify("changes")
    assert verified["streams"]["changes"]["high_watermark"] == 4
    assert ledger.unresolved_starts() == []
    rebuilt = ledger.rebuild_index()
    assert rebuilt["status"] == "REBUILT"
    assert (tmp_path / "ledger" / "index.sqlite3").exists()


def test_tamper_and_torn_tail_fail_closed(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.append("changes", "CHANGE_EVENT", change())
    segment = next((tmp_path / "ledger" / "changes").glob("*.jsonl"))
    segment.write_bytes(segment.read_bytes()[:-1])
    with pytest.raises(AdhelmError, match="torn"):
        ledger.verify("changes")


def test_concurrent_append_has_strict_sequences(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)

    def append(index: int) -> None:
        ledger.append(
            "changes", "CHANGE_EVENT", change("PLAN_CREATED", idempotency_ref=f"attempt:{index}")
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(20)))
    records = ledger.records("changes")
    assert [record["stream_sequence"] for record in records] == list(range(1, 22))
    assert len({record["record_id"] for record in records}) == len(records)


def test_payload_is_closed_and_sensitive_data_is_rejected(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    with pytest.raises(AdhelmError, match="payload"):
        ledger.append("changes", "CHANGE_EVENT", change(unexpected="no"))
    with pytest.raises(AdhelmError, match="Prohibited"):
        ledger.append("changes", "CHANGE_EVENT", change(resource_ref="person@example.com"))


def test_snapshot_diff_preserves_honest_observation_interval(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    old = {
        "snapshot_id": "snapshot:1",
        "provider_api_version": "v1",
        "connector_version": "v1",
        "resource_type": "CAMPAIGN",
        "resource_ref": "campaign:one",
        "remote_fingerprint": "a",
        "normalized_state": {"status": "PAUSED"},
        "normalized_state_digest": digest({"status": "PAUSED"}),
        "effective_status": "PAUSED",
    }
    ledger.append("changes", "RESOURCE_SNAPSHOT", old, provider="google_ads")
    changed = {
        **old,
        "snapshot_id": "snapshot:2",
        "remote_fingerprint": "b",
        "normalized_state": {"status": "ENABLED"},
        "normalized_state_digest": digest({"status": "ENABLED"}),
    }
    diff = ledger.snapshot_diff(changed)
    assert diff["changed"] is True
    event = diff["change_payload"]
    assert event["origin"] == "UNKNOWN"
    assert event["observation_interval_start"]
    assert event["observation_interval_end"]


def test_policy_reservation_is_available_only_for_started_apply(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.append(
        "changes",
        "CHANGE_EVENT",
        change(policy_id="policy:one", policy_reservation=[{"currency": "USD", "minor_units": 9}]),
    )
    with pytest.raises(AdhelmError):
        ledger.append("changes", "CHANGE_EVENT", change("APPLY_RESULT", policy_id="policy:one"))


def _report_payload(ledger: Ledger, *, checkpoints: list[LedgerCheckpoint]) -> dict[str, object]:
    report = NormalizedReport.model_validate_json(
        canonical_bytes(
            {
                "envelope": {
                    "report_id": "report:one",
                    "provider": "google_ads",
                    "source_account_ref": "account:one",
                    "query_contract_hash": "a" * 64,
                    "window_start": "2026-01-01",
                    "window_end": "2026-01-01",
                    "source_timezone": "UTC",
                    "report_timezone": "UTC",
                    "currency": "USD",
                    "attribution_model": "LAST_CLICK",
                    "observed_at": "2026-01-02T00:00:00Z",
                    "provider_api_version": "v1",
                    "connector_version": "v1",
                    "source_ledger_checkpoints": [],
                },
                "performance_rows": [],
                "conversion_rows": [],
            }
        )
    )
    report = report.model_copy(
        update={
            "envelope": report.envelope.model_copy(
                update={"source_ledger_checkpoints": checkpoints}
            )
        }
    )
    return report.to_ledger_payload(
        observation_key="observation:one",
        observation_revision=1,
        source_report_digest=digest(report),
        query_as_of=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_checkpoint_includes_empty_streams_and_binds_report_provenance(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    initial = ledger.checkpoint()
    assert initial["streams"] == {
        stream: {
            "stream_id": f"default:{stream}",
            "records": 0,
            "high_watermark": 0,
            "head_digest": None,
            "segments": 0,
        }
        for stream in ("changes", "creative", "performance")
    }
    source = [
        LedgerCheckpoint(
            **{
                "stream_id": item["stream_id"],
                "high_watermark_sequence": item["high_watermark"],
                "head_digest": item["head_digest"],
            }
        )
        for item in initial["streams"].values()
    ]
    payload = _report_payload(ledger, checkpoints=source)
    ledger.append("performance", "PERFORMANCE_OBSERVATION", payload, provider="google_ads")
    assert ledger.checkpoint()["streams"]["performance"]["high_watermark"] == 2

    tampered = dict(payload)
    tampered["source_report_digest"] = "b" * 64
    with pytest.raises(AdhelmError, match="source digest"):
        ledger.append("performance", "PERFORMANCE_OBSERVATION", tampered, provider="google_ads")


def test_checkpoint_rejects_unresolved_creative_references(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    deployment = CreativeDeployment.from_association(
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        creative_family_id="family:missing",
        creative_version_id="creative:sha256:" + "a" * 64,
        rendition_refs=("rendition:sha256:" + "b" * 64,),
        effective_from=None,
        effective_to=None,
        observed_from=datetime(2026, 1, 1, tzinfo=UTC),
        observed_to=datetime(2026, 1, 2, tzinfo=UTC),
        association_status="ACTIVE",
    )
    with pytest.raises(AdhelmError, match="Deployment"):
        ledger.append(
            "creative",
            "CREATIVE_DEPLOYMENT",
            deployment.model_dump(mode="json"),
            provider="google_ads",
        )


def test_performance_creative_references_must_match_one_deployment(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    analysis = ImageAnalysis(
        mime_type="image/png",
        width_px=1,
        height_px=1,
        aspect_ratio="1:1",
        palette=(PaletteColor(hex="#FF0000", proportion_basis_points=10_000),),
        mean_lightness_basis_points=1,
        mean_saturation_basis_points=2,
        contrast_basis_points=3,
        colorfulness_basis_points=4,
    )
    asset = CreativeAssetVersion.from_image(b"creative-image", analysis)
    asset_analysis = AssetAnalysisRecord.from_analysis(asset, analysis)
    asset_record = ledger.append(
        "creative", "CREATIVE_ASSET_VERSION", asset.model_dump(mode="json")
    )
    analysis_record = ledger.append(
        "creative", "CREATIVE_ASSET_ANALYSIS", asset_analysis.model_dump(mode="json")
    )
    component = CreativeComponent(
        component_id="headline",
        component_role="HEADLINE",
        ordinal=0,
        exact_text="One",
        normalized_text="one",
        language="en",
        provider_slot="headline",
        provenance="DECLARED",
        confidence_basis_points=10_000,
        extraction_status="DECLARED",
    )
    image = build_creative_registration_closure(
        CreativeIdentityRegistrationInput(
            creative_family_id="family:one",
            family_label="One",
            provider_format="DISPLAY",
            locale="en-US",
            destination_ref="destination:one",
            rendition_role="HERO",
            components=(component,),
            family_created_at=created,
            version_created_at=created,
        ),
        source_asset_record_digest=asset_record["record_digest"],
        source_analysis_record_digest=analysis_record["record_digest"],
        asset=asset,
        asset_analysis=asset_analysis,
    )
    ledger.append_creative_registration(image)
    text = build_text_creative_registration_closure(
        CreativeTextRegistrationInput(
            creative_family_id="family:one",
            family_label="One",
            provider_format="RESPONSIVE_SEARCH",
            locale="en-US",
            destination_ref="destination:one",
            components=(
                component.model_copy(update={"exact_text": "Two", "normalized_text": "two"}),
            ),
            family_created_at=created,
            version_created_at=created,
        ),
        existing_family=image.family,
    )
    ledger.append_creative_registration(text)
    deployment = CreativeDeployment.from_association(
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        creative_family_id=image.family.creative_family_id,
        creative_version_id=image.version.creative_version_id,
        rendition_refs=(image.rendition.rendition_id,),
        effective_from=created,
        effective_to=created,
        observed_from=created,
        observed_to=created,
        association_status="ACTIVE",
    )
    ledger.append(
        "creative",
        "CREATIVE_DEPLOYMENT",
        deployment.model_dump(mode="json"),
        provider="google_ads",
        account_ref="account:one",
    )
    checkpoint = ledger.checkpoint()
    checkpoints = [
        LedgerCheckpoint(
            stream_id=state["stream_id"],
            high_watermark_sequence=state["high_watermark"],
            head_digest=state["head_digest"],
        )
        for state in checkpoint["streams"].values()
    ]

    def payload_for(version_id: str) -> dict[str, object]:
        report = NormalizedReport.model_validate_json(
            canonical_bytes(
                {
                    "envelope": {
                        "report_id": "report:creative",
                        "provider": "google_ads",
                        "source_account_ref": "account:one",
                        "query_contract_hash": "a" * 64,
                        "window_start": "2026-01-01",
                        "window_end": "2026-01-01",
                        "source_timezone": "UTC",
                        "report_timezone": "UTC",
                        "currency": "USD",
                        "attribution_model": "LAST_CLICK",
                        "observed_at": "2026-01-02T00:00:00Z",
                        "provider_api_version": "v1",
                        "connector_version": "v1",
                        "source_ledger_checkpoints": [
                            item.model_dump(mode="json") for item in checkpoints
                        ],
                    },
                    "performance_rows": [
                        {
                            "schema_version": 1,
                            "report_id": "report:creative",
                            "resource_level": "AD",
                            "metric_grain": "AD",
                            "resource_type": "AD",
                            "resource_ref": "ad:one",
                            "creative_family_id": "family:one",
                            "creative_version_id": version_id,
                            "creative_deployment_id": deployment.deployment_id,
                            "impressions": 1,
                            "data_quality_statuses": ["CREATIVE_NOT_EXPOSED"],
                        }
                    ],
                    "conversion_rows": [],
                }
            )
        )
        return report.to_ledger_payload(
            observation_key="observation:creative",
            observation_revision=1,
            source_report_digest=digest(report),
            query_as_of=datetime(2026, 1, 2, tzinfo=UTC),
        )

    with pytest.raises(AdhelmError, match="deployment"):
        ledger.append(
            "performance",
            "PERFORMANCE_OBSERVATION",
            payload_for(text.version.creative_version_id),
            provider="google_ads",
            account_ref="account:one",
        )
    retained = ledger.append(
        "performance",
        "PERFORMANCE_OBSERVATION",
        payload_for(image.version.creative_version_id),
        provider="google_ads",
        account_ref="account:one",
    )
    assert retained["record_kind"] == "PERFORMANCE_OBSERVATION"


def test_the_ledger_record_schema_uri_is_on_the_product_home() -> None:
    assert SCHEMA_URI == "https://aptsonic.com/adhelm/schema/ledger-record"
