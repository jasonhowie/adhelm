"""Independent adversarial coverage for creative-history reconstruction."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import adhelm.ledger as ledger_module
from adhelm.canonical import canonical_bytes, digest
from adhelm.creative import (
    AssetAnalysisRecord,
    CreativeAssetVersion,
    CreativeComponent,
    CreativeDeployment,
    CreativeFamilyRecord,
    CreativeRendition,
    CreativeVersion,
    CreativeVersionRecord,
    IdentityTransformation,
    ImageAnalysis,
    ImageFingerprintRecord,
    PaletteColor,
    ServingCreativeManifest,
    ServingRendition,
    join_performance_window,
    revalidate_creative_deployment,
    revalidate_creative_payload,
    revalidate_creative_version_record,
    revalidate_image_fingerprint_record,
    validate_creative_history_graph,
)
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger

_MediaRecords = tuple[
    CreativeAssetVersion,
    AssetAnalysisRecord,
    CreativeRendition,
    CreativeVersion,
    ImageFingerprintRecord,
]


def _version(*, family_id: str = "family:one") -> CreativeVersion:
    manifest = ServingCreativeManifest(
        provider_format="RESPONSIVE_SEARCH",
        locale="en-US",
        destination_ref="destination:landing",
        surface_kind="TEXT_ONLY",
        rendition_refs=(),
        components=(
            CreativeComponent(
                component_id="headline",
                component_role="HEADLINE",
                ordinal=0,
                exact_text="A bounded headline",
                normalized_text="a bounded headline",
                language="en",
                provider_slot="headline_1",
                provenance="DECLARED",
                confidence_basis_points=10_000,
                extraction_status="DECLARED",
            ),
        ),
    )
    return CreativeVersion(
        creative_family_id=family_id,
        manifest=manifest,
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def _image_analysis() -> ImageAnalysis:
    return ImageAnalysis(
        mime_type="image/png",
        width_px=1,
        height_px=1,
        aspect_ratio="1:1",
        palette=(PaletteColor(hex="#FF0000", proportion_basis_points=10_000),),
        mean_lightness_basis_points=2_126,
        mean_saturation_basis_points=10_000,
        contrast_basis_points=0,
        colorfulness_basis_points=0,
    )


def _unknown_source_rendition(*, width_px: int, height_px: int) -> CreativeRendition:
    transformation = IdentityTransformation()
    transformation_digest = digest(transformation)
    return CreativeRendition(
        rendition_id="rendition:sha256:"
        + digest(
            {
                "content_digest": "f" * 64,
                "provider_format": "DISPLAY",
                "locale": "en-US",
                "width_px": width_px,
                "height_px": height_px,
                "transformation_digest": transformation_digest,
            }
        ),
        source_asset_version_ref=None,
        source_relation_status="UNKNOWN_SOURCE_ASSET",
        content_digest="f" * 64,
        provider_format="DISPLAY",
        locale="en-US",
        width_px=width_px,
        height_px=height_px,
        transformation_spec=transformation,
        transformation_digest=transformation_digest,
    )


def _fingerprint_with_legacy_payload() -> tuple[ImageFingerprintRecord, dict[str, object]]:
    """Create a valid v2 fingerprint and its deliberately read-only v1 wire form."""
    analysis = _image_analysis()
    asset = CreativeAssetVersion.from_image(b"legacy-fingerprint", analysis)
    rendition = CreativeRendition.from_asset(asset, provider_format="DISPLAY", locale="en-US")
    fingerprint = ImageFingerprintRecord.from_analysis(
        analysis,
        creative_version_id="creative:sha256:" + "a" * 64,
        asset_version_id=asset.asset_version_id,
        rendition_id=rendition.rendition_id,
        analysis_surface="FINAL_RENDITION",
    )
    legacy = dict(fingerprint.model_dump(mode="json", warnings="error"))
    legacy["schema_version"] = 1
    legacy.pop("mime_type")
    return fingerprint, legacy


def _deployment_with_legacy_payload() -> tuple[CreativeDeployment, dict[str, object]]:
    """Create a v2 association and the schema-less M27 representation."""
    observed = datetime(2026, 1, 1, tzinfo=UTC)
    deployment = CreativeDeployment.from_association(
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        creative_family_id="family:one",
        creative_version_id="creative:sha256:" + "a" * 64,
        rendition_refs=("rendition:sha256:" + "b" * 64,),
        effective_from=observed,
        effective_to=observed + timedelta(days=1),
        observed_from=observed,
        observed_to=observed,
        association_status="ACTIVE",
    )
    legacy = dict(deployment.model_dump(mode="json", warnings="error"))
    legacy.pop("schema_version")
    return deployment, legacy


@pytest.mark.parametrize("alias", [True, 1.0])
def test_revalidator_rejects_raw_serving_manifest_schema_version_alias(alias: object) -> None:
    raw = _version().to_ledger_payload()
    raw["serving_manifest_schema_version"] = alias

    with pytest.raises(AdhelmError) as raised:
        revalidate_creative_version_record(raw)

    assert raised.value.code == "INVALID_CREATIVE_HISTORY"


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("creative_version_id", "creative:sha256:" + "0" * 64),
        ("serving_manifest_digest", "0" * 64),
        ("serving_manifest_schema_version", True),
    ],
)
def test_constructed_version_cannot_bypass_semantic_reconstruction(
    field: str, replacement: object
) -> None:
    raw = _version().to_ledger_payload()
    checked = CreativeVersionRecord.model_validate_json(canonical_bytes(raw))
    forged = checked.model_copy(update={field: replacement})

    with pytest.raises(AdhelmError) as raised:
        revalidate_creative_version_record(forged)

    assert raised.value.code == "INVALID_CREATIVE_HISTORY"


@pytest.mark.parametrize("operation", ["verify", "records", "checkpoint"])
def test_physical_creative_reload_rechecks_cross_record_graph(
    operation: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid-shaped old record cannot evade the new graph invariant on reload."""
    ledger = Ledger(tmp_path)
    original = ledger_module.validate_creative_history_graph
    monkeypatch.setattr(ledger_module, "validate_creative_history_graph", lambda _records: None)
    payload = _version(family_id="family:missing").to_ledger_payload()
    ledger.append("creative", "CREATIVE_VERSION", payload)
    monkeypatch.setattr(ledger_module, "validate_creative_history_graph", original)

    with pytest.raises(AdhelmError) as raised:
        if operation == "verify":
            ledger.verify("creative")
        elif operation == "records":
            ledger.records("creative")
        else:
            ledger.checkpoint()

    assert raised.value.code == "CHECKPOINT_MISSING_REFERENCE"


def test_performance_join_revalidates_constructed_deployment_before_exact_output() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    deployment = CreativeDeployment.from_association(
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        creative_family_id="family:one",
        creative_version_id="creative:sha256:" + "a" * 64,
        rendition_refs=("rendition:sha256:" + "b" * 64,),
        effective_from=base,
        effective_to=base + timedelta(days=2),
        observed_from=base,
        observed_to=base + timedelta(days=2),
        association_status="ACTIVE",
    )
    forged = deployment.model_copy(update={"creative_version_id": "creative:sha256:" + "c" * 64})

    with pytest.raises(AdhelmError) as raised:
        join_performance_window(
            metric_id="metric:one",
            provider="google_ads",
            account_ref="account:one",
            ad_ref="ad:one",
            window_start=base,
            window_end=base + timedelta(days=1),
            deployments=(forged,),
        )

    assert raised.value.code == "INVALID_CREATIVE_HISTORY"


def test_unknown_source_rendition_cannot_claim_unrelated_final_asset_analysis() -> None:
    """A final-surface fingerprint needs local content evidence for the rendition itself."""
    analysis = _image_analysis()
    asset = CreativeAssetVersion.from_image(b"unrelated local image", analysis)
    asset_analysis = AssetAnalysisRecord.from_analysis(asset, analysis)
    rendition = _unknown_source_rendition(width_px=123, height_px=456)
    family = CreativeFamilyRecord(
        creative_family_id="family:one",
        family_label="one",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        provenance="CUSTOMER_LOCAL",
    )
    manifest = ServingCreativeManifest(
        provider_format="DISPLAY",
        locale="en-US",
        destination_ref="destination:one",
        rendition_refs=(
            ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
        ),
        components=_version().manifest.components,
    )
    version = CreativeVersion(
        creative_family_id=family.creative_family_id,
        manifest=manifest,
        analyzer_refs=(asset_analysis.asset_analysis_id,),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fingerprint = ImageFingerprintRecord.from_analysis(
        analysis,
        creative_version_id=version.creative_version_id,
        asset_version_id=asset.asset_version_id,
        rendition_id=rendition.rendition_id,
        analysis_surface="FINAL_RENDITION",
    )
    records = (
        ("CREATIVE_ASSET_VERSION", asset),
        ("CREATIVE_ASSET_ANALYSIS", asset_analysis),
        ("CREATIVE_FAMILY", family),
        ("CREATIVE_RENDITION", rendition),
        ("CREATIVE_VERSION", version.to_ledger_payload()),
        ("CREATIVE_FINGERPRINT", fingerprint),
    )

    with pytest.raises(AdhelmError) as raised:
        validate_creative_history_graph(
            [
                {
                    "record_kind": kind,
                    "payload": value if type(value) is dict else value.model_dump(mode="json"),
                }
                for kind, value in records
            ]
        )

    assert raised.value.code == "INVALID_CREATIVE_HISTORY"


def test_unknown_source_rendition_id_binds_retained_intrinsic_dimensions() -> None:
    first = _unknown_source_rendition(width_px=1, height_px=1)
    second = _unknown_source_rendition(width_px=123, height_px=456)

    assert first.rendition_id != second.rendition_id


def test_text_only_raw_or_constructed_versions_cannot_claim_visual_evidence() -> None:
    """A text-only label must not hide a rendition or its image analysis."""
    analysis = _image_analysis()
    asset = CreativeAssetVersion.from_image(b"text-only-visual", analysis)
    rendition = CreativeRendition.from_asset(asset, provider_format="DISPLAY", locale="en-US")
    media_version = CreativeVersion(
        creative_family_id="family:one",
        manifest=ServingCreativeManifest(
            provider_format="DISPLAY",
            locale="en-US",
            destination_ref="destination:one",
            rendition_refs=(
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            ),
            components=_version().manifest.components,
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    visual_copy = CreativeVersionRecord.model_validate_json(
        canonical_bytes(media_version.to_ledger_payload())
    ).model_copy(update={"surface_kind": "TEXT_ONLY"})
    text_raw = _version().to_ledger_payload()
    text_raw["analyzer_refs"] = ("asset-analysis:sha256:" + "a" * 64,)

    for value in (visual_copy, text_raw):
        with pytest.raises(AdhelmError) as raised:
            revalidate_creative_version_record(value)
        assert raised.value.code == "INVALID_CREATIVE_HISTORY"


def test_provider_preview_cannot_be_fabricated_from_local_analysis() -> None:
    """A provider preview needs its own future capture-evidence contract."""
    analysis = _image_analysis()
    asset = CreativeAssetVersion.from_image(b"local-only-preview", analysis)
    rendition = CreativeRendition.from_asset(asset, provider_format="DISPLAY", locale="en-US")
    final = ImageFingerprintRecord.from_analysis(
        analysis,
        creative_version_id="creative:sha256:" + "a" * 64,
        asset_version_id=asset.asset_version_id,
        rendition_id=rendition.rendition_id,
        analysis_surface="FINAL_RENDITION",
    )
    forged = final.model_copy(update={"analysis_surface": "PROVIDER_PREVIEW"})

    with pytest.raises(AdhelmError) as raised:
        revalidate_image_fingerprint_record(forged)

    assert raised.value.code == "INVALID_CREATIVE_HISTORY"


@pytest.mark.parametrize("alias", [True, 2.0])
def test_v2_fingerprint_schema_version_rejects_raw_boolean_and_float_aliases(
    alias: object,
) -> None:
    """V2 must not be downgraded through Literal equality before MIME binding runs."""
    _fingerprint, raw = _fingerprint_with_legacy_payload()
    raw["schema_version"] = alias
    raw["mime_type"] = "image/png"

    with pytest.raises(AdhelmError) as raised:
        revalidate_image_fingerprint_record(raw)

    assert raised.value.code == "INVALID_CREATIVE_HISTORY"


def test_v2_fingerprint_copy_cannot_change_mime_under_prior_content_identity() -> None:
    """The v2 MIME field is in the historical fingerprint identity domain."""
    fingerprint, _legacy = _fingerprint_with_legacy_payload()
    forged = fingerprint.model_copy(update={"mime_type": "image/jpeg"})

    with pytest.raises(AdhelmError) as raised:
        revalidate_image_fingerprint_record(forged)

    assert raised.value.code == "INVALID_CREATIVE_HISTORY"


def test_legacy_fingerprint_is_scan_only_and_cannot_be_reused_or_appended(tmp_path) -> None:
    """The M27 parser is only for physical history reconstruction, never new use."""
    _fingerprint, legacy = _fingerprint_with_legacy_payload()
    scanned = revalidate_creative_payload("CREATIVE_FINGERPRINT", legacy)
    assert scanned.model_dump(mode="json", warnings="error") == legacy

    with pytest.raises(AdhelmError) as direct:
        revalidate_image_fingerprint_record(legacy)
    assert direct.value.code == "CREATIVE_HISTORY_MIGRATION_REQUIRED"

    ledger = Ledger(tmp_path)
    with pytest.raises(AdhelmError) as appended:
        ledger.append("creative", "CREATIVE_FINGERPRINT", legacy)
    assert appended.value.code == "CREATIVE_HISTORY_MIGRATION_REQUIRED"
    assert not (tmp_path / "ledger" / "creative").exists()


def test_legacy_deployment_is_scan_only_and_cannot_join_current_performance() -> None:
    """Schema-less associations have no M28 content identity and cannot become exact joins."""
    _deployment, legacy = _deployment_with_legacy_payload()
    scanned = revalidate_creative_payload("CREATIVE_DEPLOYMENT", legacy)
    assert scanned.model_dump(mode="json", warnings="error") == legacy

    with pytest.raises(AdhelmError) as direct:
        revalidate_creative_deployment(legacy)
    assert direct.value.code == "CREATIVE_HISTORY_MIGRATION_REQUIRED"

    observed = datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(AdhelmError) as joined:
        join_performance_window(
            metric_id="metric:one",
            provider="google_ads",
            account_ref="account:one",
            ad_ref="ad:one",
            window_start=observed,
            window_end=observed + timedelta(hours=1),
            deployments=(legacy,),  # type: ignore[arg-type]
        )
    assert joined.value.code == "CREATIVE_HISTORY_MIGRATION_REQUIRED"


def test_legacy_fingerprint_cannot_substitute_parent_analysis_mime() -> None:
    """The omitted M27 MIME must be recovered only from its exact retained analysis."""
    analysis = _image_analysis()
    asset = CreativeAssetVersion.from_image(b"png-parent-analysis", analysis)
    asset_analysis = AssetAnalysisRecord.from_analysis(asset, analysis)
    rendition = CreativeRendition.from_asset(asset, provider_format="DISPLAY", locale="en-US")
    family = CreativeFamilyRecord(
        creative_family_id="family:one",
        family_label="one",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        provenance="CUSTOMER_LOCAL",
    )
    version = CreativeVersion(
        creative_family_id=family.creative_family_id,
        manifest=ServingCreativeManifest(
            provider_format="DISPLAY",
            locale="en-US",
            destination_ref="destination:one",
            rendition_refs=(
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            ),
            components=_version().manifest.components,
        ),
        analyzer_refs=(asset_analysis.asset_analysis_id,),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    foreign_mime = analysis.model_copy(update={"mime_type": "image/jpeg"})
    fingerprint = ImageFingerprintRecord.from_analysis(
        foreign_mime,
        creative_version_id=version.creative_version_id,
        asset_version_id=asset.asset_version_id,
        rendition_id=rendition.rendition_id,
        analysis_surface="FINAL_RENDITION",
    )
    legacy = dict(fingerprint.model_dump(mode="json", warnings="error"))
    legacy["schema_version"] = 1
    legacy.pop("mime_type")

    with pytest.raises(AdhelmError) as raised:
        validate_creative_history_graph(
            [
                {"record_kind": "CREATIVE_ASSET_VERSION", "payload": asset.model_dump(mode="json")},
                {
                    "record_kind": "CREATIVE_ASSET_ANALYSIS",
                    "payload": asset_analysis.model_dump(mode="json"),
                },
                {"record_kind": "CREATIVE_FAMILY", "payload": family.model_dump(mode="json")},
                {"record_kind": "CREATIVE_RENDITION", "payload": rendition.model_dump(mode="json")},
                {"record_kind": "CREATIVE_VERSION", "payload": version.to_ledger_payload()},
                {"record_kind": "CREATIVE_FINGERPRINT", "payload": legacy},
            ]
        )

    assert raised.value.code == "INVALID_CREATIVE_HISTORY"


def test_late_fingerprint_uses_its_own_version_analyzer_evidence() -> None:
    """A later version must not change the evidence binding of an earlier fingerprint."""
    family = CreativeFamilyRecord(
        creative_family_id="family:one",
        family_label="one",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        provenance="CUSTOMER_LOCAL",
    )

    def media_records(data: bytes, text: str) -> _MediaRecords:
        analysis = _image_analysis()
        asset = CreativeAssetVersion.from_image(data, analysis)
        asset_analysis = AssetAnalysisRecord.from_analysis(asset, analysis)
        rendition = CreativeRendition.from_asset(asset, provider_format="DISPLAY", locale="en-US")
        component = CreativeComponent(
            component_id=f"headline:{text}",
            component_role="HEADLINE",
            ordinal=0,
            exact_text=text,
            normalized_text=text.casefold(),
            language="en-US",
            provider_slot="headline",
            provenance="DECLARED",
            confidence_basis_points=10_000,
            extraction_status="DECLARED",
        )
        version = CreativeVersion(
            creative_family_id=family.creative_family_id,
            manifest=ServingCreativeManifest(
                provider_format="DISPLAY",
                locale="en-US",
                destination_ref="destination:one",
                rendition_refs=(
                    ServingRendition(
                        rendition_id=rendition.rendition_id,
                        ordinal=0,
                        role="HERO",
                    ),
                ),
                components=(component,),
            ),
            analyzer_refs=(asset_analysis.asset_analysis_id,),
            provenance="CUSTOMER_LOCAL",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        fingerprint = ImageFingerprintRecord.from_analysis(
            analysis,
            creative_version_id=version.creative_version_id,
            asset_version_id=asset.asset_version_id,
            rendition_id=rendition.rendition_id,
            analysis_surface="FINAL_RENDITION",
        )
        return asset, asset_analysis, rendition, version, fingerprint

    first = media_records(b"first", "First")
    second = media_records(b"second", "Second")
    rows: list[dict[str, object]] = [
        {"record_kind": "CREATIVE_FAMILY", "payload": family.model_dump(mode="json")}
    ]
    for asset, asset_analysis, rendition, version, _fingerprint in (first, second):
        rows.extend(
            [
                {
                    "record_kind": "CREATIVE_ASSET_VERSION",
                    "payload": asset.model_dump(mode="json"),
                },
                {
                    "record_kind": "CREATIVE_ASSET_ANALYSIS",
                    "payload": asset_analysis.model_dump(mode="json"),
                },
                {
                    "record_kind": "CREATIVE_RENDITION",
                    "payload": rendition.model_dump(mode="json"),
                },
                {"record_kind": "CREATIVE_VERSION", "payload": version.to_ledger_payload()},
            ]
        )
    rows.append(
        {"record_kind": "CREATIVE_FINGERPRINT", "payload": first[4].model_dump(mode="json")}
    )

    validate_creative_history_graph(rows)
