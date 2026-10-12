from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adhelm.canonical import canonical_bytes, digest
from adhelm.creative import (
    AssetAnalysisRecord,
    CreativeAssetVersion,
    CreativeComponent,
    CreativeDeployment,
    CreativeFamilyRecord,
    ImageAnalysis,
    PaletteColor,
)
from adhelm.creative_registration_closure import (
    CreativeIdentityRegistrationInput,
    CreativeTextRegistrationInput,
    build_creative_registration_closure,
    build_text_creative_registration_closure,
    revalidate_creative_registration_closure,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger


def _analysis() -> ImageAnalysis:
    return ImageAnalysis(
        mime_type="image/png",
        width_px=2,
        height_px=1,
        aspect_ratio="2:1",
        palette=(PaletteColor(hex="#FF0000", proportion_basis_points=10_000),),
        mean_lightness_basis_points=1,
        mean_saturation_basis_points=2,
        contrast_basis_points=3,
        colorfulness_basis_points=4,
    )


def _component(text: str = "Learn more") -> CreativeComponent:
    return CreativeComponent(
        component_id="headline",
        component_role="HEADLINE",
        ordinal=0,
        exact_text=text,
        normalized_text=text.lower(),
        language="en",
        provider_slot="headline",
        provenance="DECLARED",
        confidence_basis_points=10_000,
        extraction_status="DECLARED",
    )


def _image_input(
    *, family_created_at: datetime, version_created_at: datetime
) -> CreativeIdentityRegistrationInput:
    return CreativeIdentityRegistrationInput(
        creative_family_id="family:local-image",
        family_label="Local image",
        provider_format="RESPONSIVE_DISPLAY",
        locale="en-US",
        destination_ref="destination:landing",
        rendition_role="HERO",
        components=(_component(),),
        family_created_at=family_created_at,
        version_created_at=version_created_at,
    )


def _durable_source(
    ledger: Ledger, data: bytes
) -> tuple[CreativeAssetVersion, AssetAnalysisRecord, str, str]:
    analysis = _analysis()
    asset = CreativeAssetVersion.from_image(data, analysis)
    asset_analysis = AssetAnalysisRecord.from_analysis(asset, analysis)
    asset_record = ledger.append(
        "creative", "CREATIVE_ASSET_VERSION", asset.model_dump(mode="json")
    )
    analysis_record = ledger.append(
        "creative", "CREATIVE_ASSET_ANALYSIS", asset_analysis.model_dump(mode="json")
    )
    return asset, asset_analysis, asset_record["record_digest"], analysis_record["record_digest"]


def test_image_registration_appends_a_closed_graph_and_reuses_a_family(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    asset, analysis, asset_digest, analysis_digest = _durable_source(ledger, b"first-image")
    first = build_creative_registration_closure(
        _image_input(family_created_at=base, version_created_at=base),
        source_asset_record_digest=asset_digest,
        source_analysis_record_digest=analysis_digest,
        asset=asset,
        asset_analysis=analysis,
    )
    first_result = ledger.append_creative_registration(first)
    assert first_result["status"] == "REGISTERED"
    assert set(first_result["record_digests"]) == {
        "CREATIVE_FAMILY",
        "CREATIVE_RENDITION",
        "CREATIVE_VERSION",
        "CREATIVE_FINGERPRINT",
    }

    next_asset, next_analysis, next_asset_digest, next_analysis_digest = _durable_source(
        ledger, b"second-image"
    )
    second = build_creative_registration_closure(
        _image_input(
            family_created_at=base,
            version_created_at=base + timedelta(seconds=1),
        ),
        source_asset_record_digest=next_asset_digest,
        source_analysis_record_digest=next_analysis_digest,
        asset=next_asset,
        asset_analysis=next_analysis,
        existing_family=first.family,
    )
    second_result = ledger.append_creative_registration(second)
    assert second_result["status"] == "REGISTERED"
    family_records = [
        item for item in ledger.records("creative") if item["record_kind"] == "CREATIVE_FAMILY"
    ]
    assert len(family_records) == 1
    assert family_records[0]["payload"]["created_at"] == "2026-01-01T00:00:00Z"
    assert first.version.creative_version_id != second.version.creative_version_id


def test_text_registration_uses_the_same_fixed_ledger_operation(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    closure = build_text_creative_registration_closure(
        CreativeTextRegistrationInput(
            creative_family_id="family:text",
            family_label="Text",
            provider_format="RESPONSIVE_SEARCH",
            locale="en-US",
            destination_ref="destination:landing",
            components=(_component("Text only"),),
            family_created_at=created,
            version_created_at=created,
        )
    )
    result = ledger.append_creative_registration(closure)
    assert result["status"] == "REGISTERED"
    assert set(result["record_digests"]) == {"CREATIVE_FAMILY", "CREATIVE_VERSION"}


def test_registration_rejects_stale_source_or_constructed_fingerprint(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    asset, analysis, asset_digest, analysis_digest = _durable_source(ledger, b"first-image")
    closure = build_creative_registration_closure(
        _image_input(family_created_at=created, version_created_at=created),
        source_asset_record_digest=asset_digest,
        source_analysis_record_digest=analysis_digest,
        asset=asset,
        asset_analysis=analysis,
    )
    forged = closure.model_copy(
        update={"fingerprint": closure.fingerprint.model_copy(update={"width_px": 99})}
    )
    with pytest.raises(AdhelmError, match="invalid"):
        revalidate_creative_registration_closure(forged)
    stale = closure.model_copy(update={"source_asset_record_digest": "f" * 64})
    with pytest.raises(AdhelmError, match="unavailable or changed"):
        ledger.append_creative_registration(stale)
    assert [item["record_kind"] for item in ledger.records("creative")] == [
        "SEGMENT_STARTED",
        "CREATIVE_ASSET_VERSION",
        "CREATIVE_ASSET_ANALYSIS",
    ]


def test_registration_maps_post_append_checkpoint_failure_to_inspect_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = Ledger(tmp_path)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    asset, analysis, asset_digest, analysis_digest = _durable_source(ledger, b"first-image")
    closure = build_creative_registration_closure(
        _image_input(family_created_at=created, version_created_at=created),
        source_asset_record_digest=asset_digest,
        source_analysis_record_digest=analysis_digest,
        asset=asset,
        asset_analysis=analysis,
    )

    def fail_checkpoint() -> dict[str, object]:
        raise AdhelmError("PRIVATE_POST_APPEND", "private failure", exit_code=2)

    monkeypatch.setattr(ledger, "checkpoint", fail_checkpoint)
    with pytest.raises(AdhelmError) as raised:
        ledger.append_creative_registration(closure)

    assert raised.value.code == "CREATIVE_REGISTRATION_UNVERIFIED"
    assert raised.value.exit_code == 6
    # The fixed status deliberately does not claim rollback: all child
    # records may be present and must be inspected before a deliberate retry.
    assert [item["record_kind"] for item in ledger.records("creative")][-4:] == [
        "CREATIVE_FAMILY",
        "CREATIVE_RENDITION",
        "CREATIVE_VERSION",
        "CREATIVE_FINGERPRINT",
    ]


def test_legacy_fingerprint_and_deployment_are_read_without_rewriting(
    tmp_path: Path,
) -> None:
    """M27 shapes remain readable, but new writes need their explicit M28 forms."""
    ledger = Ledger(tmp_path)
    created = datetime(2026, 1, 1, tzinfo=UTC)
    asset, analysis, asset_digest, analysis_digest = _durable_source(ledger, b"first-image")
    closure = build_creative_registration_closure(
        _image_input(family_created_at=created, version_created_at=created),
        source_asset_record_digest=asset_digest,
        source_analysis_record_digest=analysis_digest,
        asset=asset,
        asset_analysis=analysis,
    )
    ledger.append_creative_registration(closure)

    segment = next((tmp_path / "ledger" / "creative").glob("*.jsonl"))
    lines = segment.read_bytes().splitlines()
    fingerprint_record = json.loads(lines[-1])
    legacy_fingerprint = dict(fingerprint_record["payload"])
    legacy_fingerprint["schema_version"] = 1
    legacy_fingerprint.pop("mime_type")
    fingerprint_record["payload"] = legacy_fingerprint
    fingerprint_record["record_digest"] = digest(
        {key: value for key, value in fingerprint_record.items() if key != "record_digest"}
    )
    lines[-1] = canonical_bytes(fingerprint_record)
    segment.write_bytes(b"\n".join(lines) + b"\n")
    assert ledger.records("creative")[-1]["payload"] == legacy_fingerprint

    deployment = CreativeDeployment.from_association(
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        creative_family_id=closure.family.creative_family_id,
        creative_version_id=closure.version.creative_version_id,
        rendition_refs=(closure.rendition.rendition_id,),
        effective_from=created,
        effective_to=created,
        observed_from=created,
        observed_to=created,
        association_status="ACTIVE",
    )
    ledger.append(
        "creative",
        "CREATIVE_DEPLOYMENT",
        deployment.model_dump(mode="json", warnings="error"),
        provider="google_ads",
        account_ref="account:one",
    )
    lines = segment.read_bytes().splitlines()
    deployment_record = json.loads(lines[-1])
    legacy_deployment = dict(deployment_record["payload"])
    legacy_deployment.pop("schema_version")
    deployment_record["payload"] = legacy_deployment
    deployment_record["record_digest"] = digest(
        {key: value for key, value in deployment_record.items() if key != "record_digest"}
    )
    lines[-1] = canonical_bytes(deployment_record)
    segment.write_bytes(b"\n".join(lines) + b"\n")

    records = ledger.records("creative")
    assert records[-1]["payload"] == legacy_deployment
    assert segment.read_bytes().splitlines()[-1] == canonical_bytes(deployment_record)
    with pytest.raises(AdhelmError) as fingerprint_write:
        ledger.append("creative", "CREATIVE_FINGERPRINT", legacy_fingerprint)
    assert fingerprint_write.value.code == "CREATIVE_HISTORY_MIGRATION_REQUIRED"
    with pytest.raises(AdhelmError) as deployment_write:
        ledger.append(
            "creative",
            "CREATIVE_DEPLOYMENT",
            legacy_deployment,
            provider="google_ads",
            account_ref="account:one",
        )
    assert deployment_write.value.code == "CREATIVE_HISTORY_MIGRATION_REQUIRED"


def test_existing_family_must_preserve_its_original_timestamp() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    existing = CreativeFamilyRecord(
        creative_family_id="family:text",
        family_label="Text",
        created_at=base,
        provenance="CUSTOMER_LOCAL",
    )
    with pytest.raises(AdhelmError, match="family"):
        build_text_creative_registration_closure(
            CreativeTextRegistrationInput(
                creative_family_id="family:text",
                family_label="Text",
                provider_format="RESPONSIVE_SEARCH",
                locale="en-US",
                destination_ref="destination:landing",
                components=(_component(),),
                family_created_at=base + timedelta(seconds=1),
                version_created_at=base + timedelta(seconds=1),
            ),
            existing_family=existing,
        )
