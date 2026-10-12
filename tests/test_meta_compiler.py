from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from adhelm.canonical import digest
from adhelm.creative import (
    CreativeAssetVersion,
    CreativeComponent,
    CreativeRendition,
    CreativeVersion,
    ServingCreativeManifest,
    ServingRendition,
)
from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.providers.meta_compiler import (
    MetaAdsCampaignBrief,
    MetaAdsCreativeAssociation,
    MetaAdsDestinationPolicy,
    MetaAdsImageReferenceEvidence,
    MetaAdsNativeObjectStorySpec,
    MetaAdsNativeTargeting,
    MetaAdsSingleImageLinkCreative,
    compile_meta_ads_campaign,
)


def _brief(
    *, destination_url: str = "https://example.test/demo?utm_source=meta"
) -> MetaAdsCampaignBrief:
    content_digest = hashlib.sha256(b"customer-image").hexdigest()
    asset = CreativeAssetVersion(
        asset_version_id="asset:sha256:"
        + digest(
            {
                "content_digest": content_digest,
                "mime_type": "image/png",
                "width_px": 1200,
                "height_px": 628,
            }
        ),
        content_digest=content_digest,
        media_type="IMAGE",
        mime_type="image/png",
        byte_size=8,
        width_px=1200,
        height_px=628,
        aspect_ratio="300:157",
    )
    rendition = CreativeRendition.from_asset(
        asset, provider_format="META_ADS_SINGLE_IMAGE_LINK", locale="en-US"
    )
    version = CreativeVersion(
        creative_family_id="family:meta-traffic",
        manifest=ServingCreativeManifest(
            provider_format="META_ADS_SINGLE_IMAGE_LINK",
            locale="en-US",
            destination_ref="destination:sha256:" + digest({"target_url": destination_url}),
            rendition_refs=(
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            ),
            components=(
                CreativeComponent(
                    component_id="body",
                    component_role="PRIMARY_TEXT",
                    ordinal=0,
                    exact_text="Customer supplied primary text",
                    normalized_text="customer supplied primary text",
                    language="en",
                    provider_slot="message",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
                CreativeComponent(
                    component_id="headline",
                    component_role="HEADLINE",
                    ordinal=1,
                    exact_text="Customer supplied headline",
                    normalized_text="customer supplied headline",
                    language="en",
                    provider_slot="name",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
                CreativeComponent(
                    component_id="cta",
                    component_role="CTA_BUTTON",
                    ordinal=2,
                    exact_text="Learn More",
                    normalized_text="learn more",
                    language="en",
                    provider_slot="call_to_action.type",
                    normalized_cta_intent="LEARN_MORE",
                    cta_destination_ref="destination:sha256:"
                    + digest({"target_url": destination_url}),
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
            ),
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return MetaAdsCampaignBrief(
        account_ref="meta_ads.account.act_123",
        account_timezone="America/Chicago",
        campaign_handle="launch",
        adset_handle="us-feed",
        ad_handle="image-link",
        campaign_name="Customer campaign",
        adset_name="Customer ad set",
        ad_name="Customer ad",
        lifetime_budget=Money(minor_units=25_000, currency="USD"),
        start_time=datetime(2026, 10, 1, tzinfo=UTC),
        stop_time=datetime(2026, 10, 31, tzinfo=UTC),
        destination_policy=MetaAdsDestinationPolicy(
            allowed_hosts=("example.test",), allowed_paths=("/demo",)
        ),
        creative=MetaAdsSingleImageLinkCreative(
            page_id="12345",
            primary_text="Customer supplied primary text",
            headline="Customer supplied headline",
            destination_url=destination_url,
            association=MetaAdsCreativeAssociation(
                creative_version=version, asset=asset, rendition=rendition
            ),
            image_reference=MetaAdsImageReferenceEvidence(
                account_ref="meta_ads.account.act_123",
                image_hash="opaque-image-hash",
                asset_content_digest=content_digest,
                evidence_ref="evidence:sha256:" + "a" * 64,
            ),
        ),
    )


def test_compiles_paused_traffic_hierarchy_with_lifetime_cap_and_feed_only() -> None:
    spec = compile_meta_ads_campaign(_brief())
    assert spec.campaign_request.model_dump(mode="json") == {
        "name": "Customer campaign",
        "objective": "OUTCOME_TRAFFIC",
        "buying_type": "AUCTION",
        "special_ad_categories": [],
        "lifetime_budget": 25_000,
        "start_time": "2026-10-01T00:00:00Z",
        "stop_time": "2026-10-31T00:00:00Z",
        "status": "PAUSED",
    }
    assert spec.adset_request.targeting.model_dump(mode="json") == {
        "geo_locations": {"countries": ["US"]},
        "publisher_platforms": ["facebook"],
        "facebook_positions": ["feed"],
    }
    assert spec.adset_request.optimization_goal == "LINK_CLICKS"
    assert spec.creative_request.object_story_spec.link_data.image_hash == "opaque-image-hash"
    assert spec.ad_request.status == "PAUSED"
    assert "status" not in spec.creative_request.model_dump()
    assert spec.adset_parent_campaign_handle == "launch"
    assert spec.ad_parent_adset_handle == "us-feed"
    assert spec.ad_creative_handle == spec.creative_handle
    assert spec.creative_consumed_by_ad_handle == "image-link"
    assert spec.maximum_spend.minor_units == 25_000
    assert spec.planning_boundary.status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"


def test_candidate_is_deterministic_and_reviewable_output_is_not_raw_serving_data() -> None:
    first, second = compile_meta_ads_campaign(_brief()), compile_meta_ads_campaign(_brief())
    assert first.spec_digest == second.spec_digest
    rendered = json.dumps(first.redacted_review.model_dump(mode="json"), sort_keys=True)
    assert "opaque-image-hash" not in rendered
    assert "example.test" not in rendered
    assert "Customer supplied" not in rendered
    assert "12345" not in rendered


def test_destination_and_creative_identity_are_fail_closed() -> None:
    with pytest.raises(AdhelmError) as raised:
        compile_meta_ads_campaign(_brief(destination_url="https://example.test/demo?coupon=yes"))
    assert raised.value.code == "SENSITIVE_DATA_RISK"

    original = _brief()
    forged = original.model_copy(
        update={"creative": original.creative.model_copy(update={"headline": "Other"})}
    )
    with pytest.raises(ValidationError, match="creative version"):
        compile_meta_ads_campaign(forged)


def test_unreviewed_country_special_category_and_account_image_binding_fail() -> None:
    with pytest.raises(ValidationError, match="reviewed US"):
        compile_meta_ads_campaign(_brief().model_copy(update={"country_codes": ("CA",)}))
    with pytest.raises(ValidationError, match="literal"):
        compile_meta_ads_campaign(_brief().model_copy(update={"special_ad_category": "CREDIT"}))
    original = _brief()
    forged_image = original.creative.image_reference.model_copy(
        update={"account_ref": "meta_ads.account.act_9"}
    )
    with pytest.raises(ValidationError, match="bound to the compiled account"):
        compile_meta_ads_campaign(
            original.model_copy(
                update={
                    "creative": original.creative.model_copy(
                        update={"image_reference": forged_image}
                    )
                }
            )
        )


def test_revalidates_model_copy_schedule_and_unknown_input_is_rejected() -> None:
    brief = _brief()
    with pytest.raises(ValidationError, match="increasing"):
        compile_meta_ads_campaign(brief.model_copy(update={"stop_time": brief.start_time}))
    with pytest.raises(ValidationError, match="Extra inputs"):
        MetaAdsCampaignBrief.model_validate(
            {**brief.model_dump(mode="python"), "custom_audience": "no"}
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        MetaAdsNativeTargeting.model_validate(
            {
                "geo_locations": {"countries": ["US"]},
                "custom_audiences": ["audience-id"],
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        MetaAdsNativeObjectStorySpec.model_validate(
            {
                "page_id": "12345",
                "link_data": {
                    "image_hash": "opaque-image-hash",
                    "link": "https://example.test/demo",
                    "message": "Customer supplied primary text",
                    "name": "Customer supplied headline",
                    "call_to_action": {"type": "LEARN_MORE"},
                },
                "template_data": {"dynamic": "no"},
            }
        )
