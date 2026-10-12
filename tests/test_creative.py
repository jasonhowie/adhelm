from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from pydantic import ValidationError

from adhelm.canonical import canonical_bytes
from adhelm.creative import (
    AssetAnalysisRecord,
    CreativeAssetVersion,
    CreativeComponent,
    CreativeDeployment,
    CreativeFamilyRecord,
    CreativeMessage,
    CreativeRendition,
    CreativeVersion,
    CreativeVersionRecord,
    ImageAnalysis,
    ImageFingerprintRecord,
    PaletteColor,
    ServingCreativeManifest,
    ServingRendition,
    analyze_image,
    join_performance_window,
    validate_creative_history_graph,
)
from adhelm.errors import AdhelmError


def _proven_rendition(*, provider_format: str, locale: str) -> CreativeRendition:
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
    asset = CreativeAssetVersion.from_image(b"creative-test-image", analysis)
    return CreativeRendition.from_asset(asset, provider_format=provider_format, locale=locale)


def manifest(headline: str = "Control paid media") -> ServingCreativeManifest:
    rendition = _proven_rendition(provider_format="RESPONSIVE_DISPLAY", locale="en-US")
    return ServingCreativeManifest(
        provider_format="RESPONSIVE_DISPLAY",
        locale="en-US",
        destination_ref="destination:landing",
        rendition_refs=(
            ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
        ),
        components=(
            CreativeComponent(
                component_id="headline",
                component_role="HEADLINE",
                ordinal=0,
                exact_text=headline,
                normalized_text=headline.lower(),
                language="en",
                provider_slot="headline_1",
                provenance="DECLARED",
                confidence_basis_points=10_000,
                extraction_status="DECLARED",
            ),
            CreativeComponent(
                component_id="cta",
                component_role="CTA_BUTTON",
                ordinal=1,
                exact_text="Learn more",
                normalized_text="learn more",
                language="en",
                provider_slot="cta",
                normalized_cta_intent="LEARN_MORE",
                provenance="DECLARED",
                confidence_basis_points=10_000,
                extraction_status="DECLARED",
            ),
        ),
    )


def test_serving_digest_changes_for_served_content_but_not_annotations() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    first = CreativeVersion(
        creative_family_id="family:one",
        manifest=manifest(),
        message=CreativeMessage(hook="proof"),
        analyzer_refs=("fingerprint:one",),
        provenance="CUSTOMER_LOCAL",
        created_at=created,
    )
    annotation_only = CreativeVersion(
        creative_family_id="family:other",
        manifest=manifest(),
        message=CreativeMessage(hook="offer"),
        analyzer_refs=("fingerprint:two",),
        provenance="HUMAN_ANNOTATED",
        created_at=created + timedelta(days=1),
    )
    changed_copy = CreativeVersion(
        creative_family_id="family:one",
        manifest=manifest("A different headline"),
        provenance="CUSTOMER_LOCAL",
        created_at=created,
    )
    assert first.creative_version_id == annotation_only.creative_version_id
    assert first.creative_version_id != changed_copy.creative_version_id
    payload = first.to_ledger_payload()
    assert payload["destination_ref"] == "destination:landing"
    assert payload["rendition_refs"]
    persisted = CreativeVersionRecord.model_validate_json(canonical_bytes(payload))
    assert persisted.creative_version_id == first.creative_version_id


def test_digest_excludes_component_annotations_but_covers_serving_changes() -> None:
    original = manifest()
    headline, cta = original.components
    annotations_changed = ServingCreativeManifest(
        provider_format=original.provider_format,
        locale=original.locale,
        destination_ref=original.destination_ref,
        rendition_refs=original.rendition_refs,
        components=(
            headline.model_copy(
                update={
                    "normalized_text": "different normalization",
                    "provenance": "HUMAN_ANNOTATED",
                    "confidence_basis_points": 1,
                    "extraction_status": "NOT_SUPPORTED_BY_ANALYZER",
                }
            ),
            cta,
        ),
    )
    changed_cta_destination = ServingCreativeManifest(
        provider_format=original.provider_format,
        locale=original.locale,
        destination_ref=original.destination_ref,
        rendition_refs=original.rendition_refs,
        components=(headline, cta.model_copy(update={"cta_destination_ref": "destination:cta"})),
    )
    changed_cta = ServingCreativeManifest(
        provider_format=original.provider_format,
        locale=original.locale,
        destination_ref=original.destination_ref,
        rendition_refs=original.rendition_refs,
        components=(headline, cta.model_copy(update={"exact_text": "Book demo"})),
    )
    changed_pinning = ServingCreativeManifest(
        provider_format=original.provider_format,
        locale=original.locale,
        destination_ref=original.destination_ref,
        rendition_refs=original.rendition_refs,
        components=(headline.model_copy(update={"pinned_position": "1"}), cta),
    )
    reordered = ServingCreativeManifest(
        provider_format=original.provider_format,
        locale=original.locale,
        destination_ref=original.destination_ref,
        rendition_refs=original.rendition_refs,
        components=(
            cta.model_copy(update={"ordinal": 0}),
            headline.model_copy(update={"ordinal": 1}),
        ),
    )
    changed_destination = original.model_copy(update={"destination_ref": "destination:other"})
    assert annotations_changed.creative_version_id == original.creative_version_id
    assert changed_cta_destination.creative_version_id != original.creative_version_id
    assert changed_cta.creative_version_id != original.creative_version_id
    assert changed_pinning.creative_version_id != original.creative_version_id
    assert reordered.creative_version_id != original.creative_version_id
    assert changed_destination.creative_version_id != original.creative_version_id


def test_text_only_manifest_allows_no_renditions_and_message_taxonomy_is_closed() -> None:
    text = ServingCreativeManifest(
        provider_format="RESPONSIVE_SEARCH",
        locale="en-US",
        destination_ref="destination:landing",
        surface_kind="TEXT_ONLY",
        rendition_refs=(),
        components=(manifest().components[0],),
    )
    assert text.rendition_refs == ()
    with pytest.raises(ValidationError, match="Text-only"):
        ServingCreativeManifest(
            provider_format="RESPONSIVE_SEARCH",
            locale="en-US",
            destination_ref="destination:landing",
            surface_kind="TEXT_ONLY",
            rendition_refs=manifest().rendition_refs,
            components=(manifest().components[0],),
        )
    with pytest.raises(ValidationError, match="Text-only"):
        CreativeVersion(
            creative_family_id="family:text",
            manifest=text,
            analyzer_refs=("asset-analysis:sha256:" + "a" * 64,),
            provenance="CUSTOMER_LOCAL",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        ).to_ledger_payload()
    with pytest.raises(ValidationError):
        CreativeMessage(hook="not_a_taxonomy_value")  # type: ignore[arg-type]


def test_late_fingerprint_binds_its_own_version_analyzer_evidence() -> None:
    """A complete older version can receive final evidence after a newer version."""
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
    family = CreativeFamilyRecord(
        creative_family_id="family:late-evidence",
        family_label="Late evidence",
        created_at=created,
        provenance="CUSTOMER_LOCAL",
    )

    def version_graph(
        data: bytes, headline: str
    ) -> tuple[
        CreativeAssetVersion,
        AssetAnalysisRecord,
        CreativeRendition,
        CreativeVersionRecord,
    ]:
        asset = CreativeAssetVersion.from_image(data, analysis)
        asset_analysis = AssetAnalysisRecord.from_analysis(asset, analysis)
        rendition = CreativeRendition.from_asset(asset, provider_format="DISPLAY", locale="en-US")
        version = CreativeVersion(
            creative_family_id=family.creative_family_id,
            manifest=ServingCreativeManifest(
                provider_format="DISPLAY",
                locale="en-US",
                destination_ref="destination:landing",
                rendition_refs=(
                    ServingRendition(
                        rendition_id=rendition.rendition_id,
                        ordinal=0,
                        role="HERO",
                    ),
                ),
                components=(
                    CreativeComponent(
                        component_id="headline",
                        component_role="HEADLINE",
                        ordinal=0,
                        exact_text=headline,
                        normalized_text=headline.lower(),
                        language="en",
                        provider_slot="headline",
                        provenance="DECLARED",
                        confidence_basis_points=10_000,
                        extraction_status="DECLARED",
                    ),
                ),
            ),
            analyzer_refs=(asset_analysis.asset_analysis_id,),
            provenance="CUSTOMER_LOCAL",
            created_at=created,
        )
        return (
            asset,
            asset_analysis,
            rendition,
            CreativeVersionRecord.model_validate_json(canonical_bytes(version.to_ledger_payload())),
        )

    first_asset, first_analysis, first_rendition, first_version = version_graph(b"first", "First")
    second_asset, second_analysis, second_rendition, second_version = version_graph(
        b"second", "Second"
    )
    late_first_fingerprint = ImageFingerprintRecord.from_analysis(
        analysis,
        creative_version_id=first_version.creative_version_id,
        asset_version_id=first_asset.asset_version_id,
        rendition_id=first_rendition.rendition_id,
        analysis_surface="FINAL_RENDITION",
    )
    records = (
        ("CREATIVE_FAMILY", family),
        ("CREATIVE_ASSET_VERSION", first_asset),
        ("CREATIVE_ASSET_ANALYSIS", first_analysis),
        ("CREATIVE_RENDITION", first_rendition),
        ("CREATIVE_VERSION", first_version),
        ("CREATIVE_ASSET_VERSION", second_asset),
        ("CREATIVE_ASSET_ANALYSIS", second_analysis),
        ("CREATIVE_RENDITION", second_rendition),
        ("CREATIVE_VERSION", second_version),
        ("CREATIVE_FINGERPRINT", late_first_fingerprint),
    )
    validate_creative_history_graph(
        [
            {
                "record_kind": kind,
                "payload": model.model_dump(mode="json", warnings="error"),
            }
            for kind, model in records
        ]
    )


def test_sensitive_creative_copy_is_rejected() -> None:
    with pytest.raises(AdhelmError, match="Prohibited"):
        manifest("Email patient@example.com")


def test_join_marks_mixed_and_unknown_effective_windows_without_fanout() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    first_manifest = manifest()
    first_version = CreativeVersion(
        creative_family_id="family:one",
        manifest=first_manifest,
        provenance="CUSTOMER_LOCAL",
        created_at=base,
    )
    second_manifest = manifest("Second version")
    second_version = CreativeVersion(
        creative_family_id="family:one",
        manifest=second_manifest,
        provenance="CUSTOMER_LOCAL",
        created_at=base,
    )
    first = CreativeDeployment.from_association(
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        creative_family_id="family:one",
        creative_version_id=first_version.creative_version_id,
        rendition_refs=tuple(item.rendition_id for item in first_manifest.rendition_refs),
        effective_from=base,
        effective_to=base + timedelta(days=1),
        observed_from=base,
        observed_to=base + timedelta(days=1),
        association_status="ACTIVE",
    )
    second = CreativeDeployment.from_association(
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        creative_family_id="family:one",
        creative_version_id=second_version.creative_version_id,
        rendition_refs=tuple(item.rendition_id for item in second_manifest.rendition_refs),
        effective_from=base + timedelta(days=1),
        effective_to=base + timedelta(days=2),
        observed_from=base + timedelta(days=1),
        observed_to=base + timedelta(days=2),
        association_status="ACTIVE",
    )
    mixed = join_performance_window(
        metric_id="metric:one",
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        window_start=base,
        window_end=base + timedelta(days=2),
        deployments=[first, second],
    )
    assert mixed.join_status == "MIXED_CREATIVE_VERSION"
    # A carousel still yields one ad-level association, never one row per rendition.
    exact = join_performance_window(
        metric_id="metric:two",
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        window_start=base,
        window_end=base + timedelta(hours=12),
        deployments=[first],
    )
    assert exact.join_status == "EXACT"
    assert exact.creative_version_id == first_version.creative_version_id
    unknown = CreativeDeployment.from_association(
        provider=first.provider,
        account_ref=first.account_ref,
        ad_ref=first.ad_ref,
        creative_family_id=first.creative_family_id,
        creative_version_id=first.creative_version_id,
        rendition_refs=first.rendition_refs,
        effective_from=None,
        effective_to=None,
        observed_from=first.observed_from,
        observed_to=first.observed_to,
        association_status=first.association_status,
    )
    uncertain = join_performance_window(
        metric_id="metric:three",
        provider="google_ads",
        account_ref="account:one",
        ad_ref="ad:one",
        window_start=base,
        window_end=base + timedelta(hours=12),
        deployments=[unknown],
    )
    assert uncertain.join_status == "UNKNOWN_EFFECTIVE_TIME"


def test_local_image_analysis_is_bounded_and_deterministic() -> None:
    image_module = pytest.importorskip("PIL.Image")
    image = image_module.new("RGB", (2, 1))
    image.putdata([(255, 0, 0), (0, 0, 255)])
    output = BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    first = analyze_image(data)
    second = analyze_image(data)
    assert first == second
    assert first.aspect_ratio == "2:1"
    assert first.mime_type == "image/png"
    assert sum(item.proportion_basis_points for item in first.palette) == 10_000
    assert first.extraction_statuses == ("NOT_SUPPORTED_BY_ANALYZER",)
    asset = CreativeAssetVersion.from_image(data, first)
    asset_analysis = AssetAnalysisRecord.from_analysis(asset, first)
    assert asset_analysis.asset_version_id == asset.asset_version_id
    rendition = CreativeRendition.from_asset(asset, provider_format="DISPLAY", locale="en-US")
    assert rendition.transformation_spec.kind == "IDENTITY"
    version = CreativeVersion(
        creative_family_id="family:one",
        manifest=ServingCreativeManifest(
            provider_format="DISPLAY",
            locale="en-US",
            destination_ref="destination:landing",
            rendition_refs=(
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            ),
            components=(manifest().components[0],),
        ),
        analyzer_refs=(asset_analysis.asset_analysis_id,),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fingerprint = ImageFingerprintRecord.from_analysis(
        first,
        creative_version_id=version.creative_version_id,
        asset_version_id=asset.asset_version_id,
        rendition_id=rendition.rendition_id,
        analysis_surface="FINAL_RENDITION",
    )
    assert fingerprint.palette == first.palette
    with pytest.raises(ValidationError):
        ImageFingerprintRecord.from_analysis(
            first,
            creative_version_id=version.creative_version_id,
            asset_version_id=asset.asset_version_id,
            rendition_id=rendition.rendition_id,
            analysis_surface="PROVIDER_PREVIEW",  # type: ignore[arg-type]
        )
    with pytest.raises(AdhelmError, match="identity"):
        CreativeRendition.from_asset(  # type: ignore[arg-type]
            asset,
            provider_format="DISPLAY",
            locale="en-US",
            transformation_spec={"kind": "CROP"},
        )
    with pytest.raises(AdhelmError, match="bound"):
        analyze_image(data, maximum_bytes=1)


def test_animated_images_fail_closed() -> None:
    image_module = pytest.importorskip("PIL.Image")
    first = image_module.new("RGB", (1, 1), "red")
    second = image_module.new("RGB", (1, 1), "blue")
    output = BytesIO()
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=10)
    with pytest.raises(AdhelmError, match="multi-frame"):
        analyze_image(output.getvalue())
