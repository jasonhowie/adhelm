"""Hermetic conformance checks for the TikTok offline compiler."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from adhelm.canonical import digest
from adhelm.creative import (
    CreativeComponent,
    CreativeVersion,
    ServingCreativeManifest,
    ServingRendition,
)
from adhelm.models import Money
from adhelm.providers.tiktok_compiler import (
    TikTokAdsCampaignBrief,
    TikTokAdsCreativeAssociation,
    TikTokAdsDestinationPolicy,
    TikTokAdsIdentityEvidence,
    TikTokAdsInFeedVideoCreative,
    TikTokAdsMediaReferenceEvidence,
    TikTokAdsNativeAdGroupPayload,
    compile_tiktok_ads_campaign,
)
from adhelm.video_creative import CustomerDeclaredVideoAsset, CustomerDeclaredVideoRendition

_DESTINATION = "https://example.test/demo?utm_source=tiktok&utm_medium=paid"
_DESTINATION_REF = "destination:sha256:" + digest({"target_url": _DESTINATION})


def _component(
    component_id: str,
    role: str,
    ordinal: int,
    text: str,
    slot: str,
    *,
    cta: bool = False,
) -> CreativeComponent:
    return CreativeComponent(
        component_id=component_id,
        component_role=role,  # type: ignore[arg-type]
        ordinal=ordinal,
        exact_text=text,
        normalized_text=text.casefold(),
        language="en-US",
        provider_slot=slot,
        normalized_cta_intent="LEARN_MORE" if cta else None,
        cta_destination_ref=_DESTINATION_REF if cta else None,
        provenance="DECLARED",
        confidence_basis_points=10_000,
        extraction_status="DECLARED",
    )


def _video_asset(*, content_digest: str | None = None) -> CustomerDeclaredVideoAsset:
    return CustomerDeclaredVideoAsset.create(
        declared_content_digest=content_digest
        or hashlib.sha256(b"tiktok-fixture-video").hexdigest(),
        declared_byte_size=2_000_000,
        declared_width_px=1080,
        declared_height_px=1920,
        declared_duration_ms=30_000,
        declared_frame_rate_milli=30_000,
        evidence_ref="evidence:sha256:" + "c" * 64,
    )


def _brief() -> TikTokAdsCampaignBrief:
    video_asset = _video_asset()
    video_rendition = CustomerDeclaredVideoRendition.from_video_asset(
        video_asset, provider_format="TIKTOK_ADS_IN_FEED_VIDEO", locale="en-US"
    )
    manifest = ServingCreativeManifest(
        provider_format="TIKTOK_ADS_IN_FEED_VIDEO",
        locale="en-US",
        destination_ref=_DESTINATION_REF,
        rendition_refs=(
            ServingRendition(
                rendition_id=video_rendition.rendition_id,
                ordinal=0,
                role="HERO",
            ),
        ),
        components=(
            _component("caption", "CAPTION", 0, "Fixture video caption", "ad_text"),
            _component("cta", "CTA_BUTTON", 1, "Learn More", "call_to_action", cta=True),
        ),
    )
    version = CreativeVersion(
        creative_family_id="family:tiktok-fixture",
        manifest=manifest,
        provenance="IMPORTED_MANIFEST",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return TikTokAdsCampaignBrief(
        account_ref="tiktok_ads.account.123",
        account_timezone="America/Chicago",
        campaign_handle="traffic-campaign",
        adgroup_handle="traffic-group",
        ad_handle="traffic-video",
        campaign_name="Fixture TikTok campaign",
        adgroup_name="Fixture TikTok group",
        ad_name="Fixture TikTok ad",
        total_budget=Money(minor_units=25_000, currency="USD"),
        start_time=datetime(2026, 10, 1, tzinfo=UTC),
        stop_time=datetime(2026, 10, 31, tzinfo=UTC),
        destination_policy=TikTokAdsDestinationPolicy(
            allowed_hosts=("example.test",), allowed_paths=("/demo",)
        ),
        creative=TikTokAdsInFeedVideoCreative(
            ad_text="Fixture video caption",
            destination_url=_DESTINATION,
            association=TikTokAdsCreativeAssociation(
                creative_version=version,
                video_asset=video_asset,
                video_rendition=video_rendition,
            ),
            media_reference=TikTokAdsMediaReferenceEvidence(
                account_ref="tiktok_ads.account.123",
                video_id="opaque-tiktok-video",
                video_asset_version_id=video_asset.video_asset_version_id,
                declared_video_content_digest=video_asset.declared_content_digest,
                evidence_ref="evidence:sha256:" + "a" * 64,
            ),
            identity=TikTokAdsIdentityEvidence(
                account_ref="tiktok_ads.account.123",
                identity_id="opaque-tiktok-identity",
                evidence_ref="evidence:sha256:" + "b" * 64,
            ),
        ),
    )


def test_compiles_disabled_native_hierarchy_with_exact_lifetime_limit() -> None:
    spec = compile_tiktok_ads_campaign(_brief())
    assert spec.campaign_request.model_dump() == {
        "campaign_name": "Fixture TikTok campaign",
        "objective_type": "TRAFFIC",
        "budget_optimize_on": False,
        "operation_status": "DISABLE",
    }
    assert spec.adgroup_request.budget_mode == "BUDGET_MODE_TOTAL"
    assert spec.adgroup_request.budget == Decimal("250.00")
    assert spec.adgroup_request.schedule_type == "SCHEDULE_START_END"
    assert spec.adgroup_request.schedule_start_time == "2026-10-01 00:00:00"
    assert spec.adgroup_request.schedule_end_time == "2026-10-31 00:00:00"
    assert spec.adgroup_request.optimization_goal == "CLICK"
    assert spec.adgroup_request.billing_event == "CPC"
    assert spec.adgroup_request.placements == ("PLACEMENT_TIKTOK",)
    assert spec.ad_request.identity_type == "CUSTOMIZED_USER"
    assert spec.ad_request.ad_format == "SINGLE_VIDEO"
    assert spec.ad_request.operation_status == "DISABLE"
    assert spec.maximum_spend == Money(minor_units=25_000, currency="USD")
    assert spec.planning_boundary.status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert spec.planning_boundary.fixture_only_status == "FIXTURE_ONLY_NOT_LIVE_VALIDATED"


def test_candidate_is_deterministic_and_review_has_no_destination_copy_or_native_refs() -> None:
    one, two = compile_tiktok_ads_campaign(_brief()), compile_tiktok_ads_campaign(_brief())
    assert one.spec_digest == two.spec_digest
    review = json.dumps(one.redacted_review.model_dump(mode="json"), sort_keys=True)
    for value in (
        "example.test",
        "Fixture video caption",
        "opaque-tiktok-video",
        "opaque-tiktok-identity",
    ):
        assert value not in review


def test_forged_model_copy_and_unknown_native_fields_fail_closed() -> None:
    brief = _brief()
    with pytest.raises(ValidationError, match="increasing"):
        compile_tiktok_ads_campaign(brief.model_copy(update={"stop_time": brief.start_time}))
    forged = brief.model_copy(
        update={"creative": brief.creative.model_copy(update={"ad_text": "Changed caption"})}
    )
    with pytest.raises(ValidationError, match="exactly match"):
        compile_tiktok_ads_campaign(forged)
    with pytest.raises(ValidationError, match="Extra inputs"):
        TikTokAdsNativeAdGroupPayload.model_validate(
            {
                "adgroup_name": "x",
                "targeting": {"location_ids": ["6252001"]},
                "budget": Decimal("1.00"),
                "schedule_start_time": "2026-10-01 00:00:00",
                "schedule_end_time": "2026-10-02 00:00:00",
                "budget_minor_units": 100,
            }
        )


def test_account_binding_handles_targeting_and_destination_are_strict() -> None:
    brief = _brief()
    with pytest.raises(ValidationError, match="bound to the compiled account"):
        compile_tiktok_ads_campaign(
            brief.model_copy(
                update={
                    "creative": brief.creative.model_copy(
                        update={
                            "identity": brief.creative.identity.model_copy(
                                update={"account_ref": "tiktok_ads.account.9"}
                            )
                        }
                    )
                }
            )
        )
    with pytest.raises(ValidationError, match="6252001"):
        compile_tiktok_ads_campaign(brief.model_copy(update={"location_ids": ("1",)}))
    unsafe_url = "https://example.test/login?utm_source=tiktok"
    unsafe_ref = "destination:sha256:" + digest({"target_url": unsafe_url})
    unsafe_manifest = brief.creative.association.creative_version.manifest.model_copy(
        update={
            "destination_ref": unsafe_ref,
            "components": (
                brief.creative.association.creative_version.manifest.components[0],
                brief.creative.association.creative_version.manifest.components[1].model_copy(
                    update={"cta_destination_ref": unsafe_ref}
                ),
            ),
        }
    )
    unsafe_association = brief.creative.association.model_copy(
        update={
            "creative_version": brief.creative.association.creative_version.model_copy(
                update={"manifest": unsafe_manifest}
            )
        }
    )
    with pytest.raises(Exception, match="undeclared or sensitive"):
        compile_tiktok_ads_campaign(
            brief.model_copy(
                update={
                    "creative": brief.creative.model_copy(
                        update={"destination_url": unsafe_url, "association": unsafe_association}
                    )
                }
            )
        )


def test_serialized_graph_revalidates_exposure_and_hierarchy() -> None:
    spec = compile_tiktok_ads_campaign(_brief())
    forged = spec.model_copy(
        update={
            "maximum_spend": Money.model_construct(minor_units=1, currency="USD"),
            "ad_parent_adgroup_handle": "other",
        }
    )
    with pytest.raises(ValidationError, match="exposure limit"):
        type(spec).model_validate(forged.model_dump(mode="python"))
