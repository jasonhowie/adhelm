"""Contract tests for customer-declared video identities."""

from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError
from test_tiktok_compiler import _brief

from adhelm.creative import CreativeVersion, ServingRendition
from adhelm.providers.tiktok_compiler import (
    TikTokAdsCreativeAssociation,
    TikTokAdsInFeedVideoCreative,
    TikTokAdsMediaReferenceEvidence,
    compile_tiktok_ads_campaign,
)
from adhelm.video_creative import CustomerDeclaredVideoAsset, CustomerDeclaredVideoRendition


def _asset(
    *,
    content: bytes = b"declared-video-v1",
    width: int = 1080,
    height: int = 1920,
    duration_ms: int = 30_000,
    evidence_suffix: str = "a",
) -> CustomerDeclaredVideoAsset:
    return CustomerDeclaredVideoAsset.create(
        declared_content_digest=hashlib.sha256(content).hexdigest(),
        declared_byte_size=2_000_000,
        declared_width_px=width,
        declared_height_px=height,
        declared_duration_ms=duration_ms,
        declared_frame_rate_milli=30_000,
        evidence_ref="evidence:sha256:" + evidence_suffix * 64,
    )


def _brief_with_asset(asset: CustomerDeclaredVideoAsset):
    original = _brief()
    previous = original.creative
    rendition = CustomerDeclaredVideoRendition.from_video_asset(
        asset,
        provider_format="TIKTOK_ADS_IN_FEED_VIDEO",
        locale=previous.association.creative_version.manifest.locale,
    )
    manifest = previous.association.creative_version.manifest.model_copy(
        update={
            "rendition_refs": (
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            )
        }
    )
    creative_version = CreativeVersion(
        creative_family_id=previous.association.creative_version.creative_family_id,
        manifest=manifest,
        provenance=previous.association.creative_version.provenance,
        created_at=previous.association.creative_version.created_at,
    )
    association = TikTokAdsCreativeAssociation(
        creative_version=creative_version,
        video_asset=asset,
        video_rendition=rendition,
    )
    media_reference = TikTokAdsMediaReferenceEvidence(
        account_ref=previous.media_reference.account_ref,
        video_id=previous.media_reference.video_id,
        video_asset_version_id=asset.video_asset_version_id,
        declared_video_content_digest=asset.declared_content_digest,
        evidence_ref=previous.media_reference.evidence_ref,
    )
    creative = TikTokAdsInFeedVideoCreative(
        ad_text=previous.ad_text,
        destination_url=previous.destination_url,
        association=association,
        media_reference=media_reference,
        identity=previous.identity,
    )
    return original.model_copy(update={"creative": creative})


def test_declared_video_is_closed_and_explicit_about_missing_analysis() -> None:
    asset = _asset()
    rendition = CustomerDeclaredVideoRendition.from_video_asset(
        asset, provider_format="TIKTOK_ADS_IN_FEED_VIDEO", locale="en-US"
    )
    assert asset.media_type == "VIDEO"
    assert asset.local_media_status == "DECLARED_UNVERIFIED_NO_LOCAL_DECODE"
    assert asset.execution_eligibility == "REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"
    assert asset.decoded_frame_status == "NOT_DECODED"
    assert asset.palette_analysis_status == "NOT_ANALYZED_NO_VIDEO_DECODE"
    assert asset.ocr_status == "NOT_ANALYZED_NO_VIDEO_DECODE"
    assert asset.transcript_status == "NOT_ANALYZED_NO_AUDIO_EXTRACTION"
    assert asset.audio_analysis_status == "NOT_ANALYZED_NO_AUDIO_EXTRACTION"
    assert asset.component_performance_status == "NOT_INFERRED"
    assert rendition.media_type == "VIDEO"
    assert rendition.decoded_frame_status == "NOT_DECODED"
    with pytest.raises(ValidationError, match="Extra inputs"):
        CustomerDeclaredVideoAsset.model_validate({**asset.model_dump(), "path": "/tmp/a.mp4"})


def test_intrinsic_changes_propagate_through_asset_rendition_and_creative_identity() -> None:
    base_asset = _asset()
    base_rendition = CustomerDeclaredVideoRendition.from_video_asset(
        base_asset, provider_format="TIKTOK_ADS_IN_FEED_VIDEO", locale="en-US"
    )
    base_spec = compile_tiktok_ads_campaign(_brief_with_asset(base_asset))
    variants = (
        _asset(content=b"declared-video-v2"),
        _asset(width=720, height=1280),
        _asset(duration_ms=31_000),
    )
    for variant in variants:
        rendition = CustomerDeclaredVideoRendition.from_video_asset(
            variant, provider_format="TIKTOK_ADS_IN_FEED_VIDEO", locale="en-US"
        )
        spec = compile_tiktok_ads_campaign(_brief_with_asset(variant))
        assert variant.video_asset_version_id != base_asset.video_asset_version_id
        assert rendition.rendition_id != base_rendition.rendition_id
        assert spec.creative_version_id != base_spec.creative_version_id
        assert spec.creative_content_digest != base_spec.creative_content_digest


def test_evidence_and_locale_do_not_claim_new_media_bytes() -> None:
    original = _asset(evidence_suffix="a")
    same_declared_media = _asset(evidence_suffix="b")
    assert same_declared_media.video_asset_version_id == original.video_asset_version_id
    en = CustomerDeclaredVideoRendition.from_video_asset(
        original, provider_format="TIKTOK_ADS_IN_FEED_VIDEO", locale="en-US"
    )
    fr = CustomerDeclaredVideoRendition.from_video_asset(
        original, provider_format="TIKTOK_ADS_IN_FEED_VIDEO", locale="fr-FR"
    )
    assert en.rendition_id != fr.rendition_id
    assert original.video_asset_version_id == same_declared_media.video_asset_version_id


def test_reparse_rejects_forged_intrinsic_and_rendition_bindings() -> None:
    asset = _asset()
    forged_asset = asset.model_copy(update={"declared_duration_ms": 31_000})
    with pytest.raises(ValidationError, match="intrinsic metadata"):
        CustomerDeclaredVideoAsset.model_validate(forged_asset.model_dump(mode="python"))
    rendition = CustomerDeclaredVideoRendition.from_video_asset(
        asset, provider_format="TIKTOK_ADS_IN_FEED_VIDEO", locale="en-US"
    )
    forged_rendition = rendition.model_copy(update={"locale": "fr-FR"})
    with pytest.raises(ValidationError, match="declared video rendition identity"):
        CustomerDeclaredVideoRendition.model_validate(forged_rendition.model_dump(mode="python"))
