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
from adhelm.providers.reddit_compiler import (
    RedditAdsCampaignBrief,
    RedditAdsCreativeAssociation,
    RedditAdsDestinationPolicy,
    RedditAdsFundingInstrumentReferenceEvidence,
    RedditAdsNativeTargeting,
    RedditAdsPixelReferenceEvidence,
    RedditAdsPostReferenceEvidence,
    RedditAdsStandardPostCreative,
    compile_reddit_ads_campaign,
)


def _brief(
    *, destination_url: str = "https://example.test/demo?utm_source=reddit"
) -> RedditAdsCampaignBrief:
    content_digest = hashlib.sha256(b"customer-reddit-image").hexdigest()
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
        byte_size=20,
        width_px=1200,
        height_px=628,
        aspect_ratio="300:157",
    )
    rendition = CreativeRendition.from_asset(
        asset, provider_format="REDDIT_ADS_STANDARD_POST", locale="en-US"
    )
    destination_ref = "destination:sha256:" + digest({"target_url": destination_url})
    version = CreativeVersion(
        creative_family_id="family:reddit-traffic",
        manifest=ServingCreativeManifest(
            provider_format="REDDIT_ADS_STANDARD_POST",
            locale="en-US",
            destination_ref=destination_ref,
            rendition_refs=(
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            ),
            components=(
                CreativeComponent(
                    component_id="headline",
                    component_role="HEADLINE",
                    ordinal=0,
                    exact_text="Customer supplied Reddit headline",
                    normalized_text="customer supplied reddit headline",
                    language="en",
                    provider_slot="creative.headline",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
                CreativeComponent(
                    component_id="cta",
                    component_role="CTA_BUTTON",
                    ordinal=1,
                    exact_text="Learn More",
                    normalized_text="learn more",
                    language="en",
                    provider_slot="creative.destination",
                    normalized_cta_intent="LEARN_MORE",
                    cta_destination_ref=destination_ref,
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
            ),
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    # The evidence is deliberately local: it establishes a binding to check,
    # not that the post is eligible or owned by this account.
    post_content_digest = digest(
        {
            "provider_format": version.manifest.provider_format,
            "creative_version_id": version.creative_version_id,
            "destination_ref": version.manifest.destination_ref,
            "components": [
                {
                    "role": item.component_role,
                    "ordinal": item.ordinal,
                    "text": item.exact_text,
                    "provider_slot": item.provider_slot,
                    "cta": item.normalized_cta_intent,
                    "cta_destination_ref": item.cta_destination_ref,
                }
                for item in version.manifest.components
            ],
        }
    )
    return RedditAdsCampaignBrief(
        account_ref="reddit_ads.account.a2_fixture",
        account_timezone="America/Chicago",
        campaign_handle="reddit-launch",
        ad_group_handle="us-feed",
        ad_handle="existing-post",
        campaign_name="Customer Reddit campaign",
        ad_group_name="Customer US feed group",
        ad_name="Customer existing post ad",
        lifetime_budget=Money(minor_units=2_500, currency="USD"),
        start_time=datetime(2026, 10, 1, tzinfo=UTC),
        end_time=datetime(2026, 10, 31, tzinfo=UTC),
        pixel_reference=RedditAdsPixelReferenceEvidence(
            account_ref="reddit_ads.account.a2_fixture",
            pixel_id="p2_fixture_pixel",
            evidence_ref="evidence:sha256:" + "b" * 64,
        ),
        funding_instrument_reference=RedditAdsFundingInstrumentReferenceEvidence(
            account_ref="reddit_ads.account.a2_fixture",
            funding_instrument_id="fi_fixture_funding",
            evidence_ref="evidence:sha256:" + "c" * 64,
        ),
        destination_policy=RedditAdsDestinationPolicy(
            allowed_hosts=("example.test",), allowed_paths=("/demo",)
        ),
        creative=RedditAdsStandardPostCreative(
            destination_url=destination_url,
            association=RedditAdsCreativeAssociation(
                creative_version=version, asset=asset, rendition=rendition
            ),
            post_reference=RedditAdsPostReferenceEvidence(
                account_ref="reddit_ads.account.a2_fixture",
                post_id="t3_fixturepost",
                post_content_digest=post_content_digest,
                evidence_ref="evidence:sha256:" + "a" * 64,
            ),
        ),
    )


def test_compiles_documented_paused_cbo_traffic_hierarchy_with_exact_native_cap() -> None:
    spec = compile_reddit_ads_campaign(_brief())
    assert spec.campaign_request.model_dump(mode="json") == {
        "name": "Customer Reddit campaign",
        "configured_status": "PAUSED",
        "objective": "CLICKS",
        "is_campaign_budget_optimization": True,
        "goal_type": "LIFETIME_SPEND",
        "goal_value": 25_000_000,
        "start_time": "2026-10-01T00:00:00Z",
        "end_time": "2026-10-31T00:00:00Z",
        "bid_strategy": "BIDLESS",
        "bid_type": "CPC",
        "conversion_pixel_id": "p2_fixture_pixel",
        "funding_instrument_id": "fi_fixture_funding",
    }
    assert spec.ad_group_request.targeting.model_dump(mode="json") == {
        "geolocations": ["US"],
        "locations": ["FEED"],
        "platforms": ["ALL"],
        "view_modes": ["ALL"],
    }
    assert spec.ad_group_request.bid_strategy is None
    assert spec.ad_group_request.bid_value is None
    assert spec.ad_request.post_id == "t3_fixturepost"
    assert spec.ad_request.click_url == "https://example.test/demo?utm_source=reddit"
    assert spec.maximum_spend_micros == 25_000_000
    assert spec.maximum_spend == Money(minor_units=2_500, currency="USD")
    assert spec.planning_boundary.status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert spec.planning_boundary.fixture_only_status == "FIXTURE_ONLY_NOT_LIVE_VALIDATED"


def test_output_is_deterministic_and_redacted_review_excludes_native_post_pixel_and_copy() -> None:
    first, second = compile_reddit_ads_campaign(_brief()), compile_reddit_ads_campaign(_brief())
    assert first.spec_digest == second.spec_digest
    review = json.dumps(first.redacted_review.model_dump(mode="json"), sort_keys=True)
    assert "t3_fixturepost" not in review
    assert "p2_fixture_pixel" not in review
    assert "example.test" not in review
    assert "Customer supplied" not in review


def test_revalidates_model_copy_and_construct_and_rejects_unknown_or_audience_data() -> None:
    original = _brief()
    with pytest.raises(ValidationError, match="increasing"):
        compile_reddit_ads_campaign(original.model_copy(update={"end_time": original.start_time}))

    forged_creative = original.creative.model_copy(
        update={"destination_url": "https://example.test/other"}
    )
    with pytest.raises(ValidationError, match="canonical creative"):
        compile_reddit_ads_campaign(original.model_copy(update={"creative": forged_creative}))

    bypassed = RedditAdsCampaignBrief.model_construct(
        **{**original.__dict__, "lifetime_budget": Money(minor_units=0, currency="USD")}
    )
    with pytest.raises(ValidationError):
        compile_reddit_ads_campaign(bypassed)

    with pytest.raises(ValidationError, match="Extra inputs"):
        RedditAdsCampaignBrief.model_validate(
            {**original.model_dump(mode="python"), "custom_audience": "not-supported"}
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        RedditAdsNativeTargeting.model_validate(
            {
                "geolocations": ["US"],
                "locations": ["FEED"],
                "platforms": ["ALL"],
                "view_modes": ["ALL"],
                "custom_audience_ids": ["not-supported"],
            }
        )


def test_rejects_unapproved_destination_and_forged_serialized_graph() -> None:
    with pytest.raises(AdhelmError) as raised:
        compile_reddit_ads_campaign(_brief(destination_url="https://example.test/demo?coupon=yes"))
    assert raised.value.code == "SENSITIVE_DATA_RISK"

    spec = compile_reddit_ads_campaign(_brief())
    serialized = spec.model_dump(mode="json")
    serialized["ad_request"]["click_url"] = "https://example.test/other"
    with pytest.raises(ValidationError, match="compiled Reddit hierarchy"):
        type(spec).model_validate_json(json.dumps(serialized))
