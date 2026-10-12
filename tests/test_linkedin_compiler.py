"""Hermetic checks for the LinkedIn existing-dark-image-post offline compiler."""

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
from adhelm.models import Money
from adhelm.providers.linkedin_compiler import (
    LinkedInAdsCampaignBrief,
    LinkedInCreativeAssociation,
    LinkedInExistingDarkImagePost,
    LinkedInNativeCampaignPayload,
    LinkedInNativeCreativeContent,
    LinkedInNativeCreativePayload,
    LinkedInNativeMoney,
    compile_linkedin_ads_campaign,
)

_COMMENTARY = "Customer supplied dark-post commentary"


def _post_digest(
    *, commentary: str = _COMMENTARY, dsc_ad_account_urn: str = "urn:li:sponsoredAccount:45678"
) -> str:
    return digest(
        {
            "post_urn": "urn:li:ugcPost:123456789",
            "author_urn": "urn:li:organization:12345",
            "commentary": commentary,
            "image_urn": "urn:li:image:fixture_image_123",
            "image_title": "Customer supplied image title",
            "image_alt_text": "Customer supplied image alt text",
            "visibility": "PUBLIC",
            "feed_distribution": "NONE",
            "lifecycle_state": "PUBLISHED",
            "dsc_ad_account_urn": dsc_ad_account_urn,
            "is_dsc": True,
            "dsc_ad_type": "STANDARD",
            "dsc_status": "ACTIVE",
            "call_to_action_status": "NOT_APPLICABLE",
            "landing_page_status": "NOT_APPLICABLE",
        }
    )


def _brief() -> LinkedInAdsCampaignBrief:
    content_digest = hashlib.sha256(b"linkedin-fixture-image").hexdigest()
    asset = CreativeAssetVersion(
        asset_version_id="asset:sha256:"
        + digest(
            {
                "content_digest": content_digest,
                "mime_type": "image/png",
                "width_px": 1200,
                "height_px": 627,
            }
        ),
        content_digest=content_digest,
        media_type="IMAGE",
        mime_type="image/png",
        byte_size=20,
        width_px=1200,
        height_px=627,
        aspect_ratio="400:209",
    )
    rendition = CreativeRendition.from_asset(
        asset,
        provider_format="LINKEDIN_ADS_EXISTING_DARK_IMAGE_POST_ENGAGEMENT",
        locale="en-US",
    )
    post = LinkedInExistingDarkImagePost(
        post_urn="urn:li:ugcPost:123456789",
        author_urn="urn:li:organization:12345",
        commentary=_COMMENTARY,
        image_urn="urn:li:image:fixture_image_123",
        image_title="Customer supplied image title",
        image_alt_text="Customer supplied image alt text",
        dsc_ad_account_urn="urn:li:sponsoredAccount:45678",
        canonical_digest=_post_digest(),
    )
    version = CreativeVersion(
        creative_family_id="family:linkedin-fixture",
        manifest=ServingCreativeManifest(
            provider_format="LINKEDIN_ADS_EXISTING_DARK_IMAGE_POST_ENGAGEMENT",
            locale="en-US",
            destination_ref="destination:NOT_APPLICABLE",
            rendition_refs=(
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            ),
            components=(
                CreativeComponent(
                    component_id="commentary",
                    component_role="PRIMARY_TEXT",
                    ordinal=0,
                    exact_text=_COMMENTARY,
                    normalized_text=_COMMENTARY.casefold(),
                    language="en",
                    provider_slot="post.commentary",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
            ),
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return LinkedInAdsCampaignBrief(
        account_ref="linkedin_ads.account.fixture",
        account_urn="urn:li:sponsoredAccount:45678",
        account_timezone="America/Chicago",
        campaign_group_handle="engagement-group",
        campaign_handle="dark-post-engagement",
        creative_handle="dark-post-creative",
        campaign_group_name="Fixture engagement group",
        campaign_name="Fixture dark-post campaign",
        group_total_budget=Money(minor_units=25_000, currency="USD"),
        daily_budget=Money(minor_units=1_000, currency="USD"),
        unit_cost=Money(minor_units=500, currency="USD"),
        start_time=datetime(2026, 10, 1, tzinfo=UTC),
        stop_time=datetime(2026, 10, 31, tzinfo=UTC),
        creative=LinkedInCreativeAssociation(
            creative_version=version,
            asset=asset,
            rendition=rendition,
            existing_post=post,
        ),
    )


def test_compiles_draft_existing_post_graph_with_group_total_spend_cap() -> None:
    spec = compile_linkedin_ads_campaign(_brief())
    assert spec.campaign_group_request.model_dump() == {
        "account": "urn:li:sponsoredAccount:45678",
        "name": "Fixture engagement group",
        "objectiveType": "ENGAGEMENT",
        "runSchedule": {"start": 1_790_812_800_000, "end": 1_793_404_800_000},
        "totalBudget": {"amount": "250.00", "currencyCode": "USD"},
        "status": "DRAFT",
    }
    assert spec.campaign_template.parent_campaign_group_handle == "engagement-group"
    assert spec.campaign_template.required_native_parent_field == "campaignGroup"
    assert spec.campaign_template.required_native_parent_urn_type == "urn:li:sponsoredCampaignGroup"
    assert spec.campaign_template.resolution_status == "REQUIRES_CREATE_READBACK_AND_REVALIDATION"
    assert spec.campaign_template.request.model_dump() == {
        "account": "urn:li:sponsoredAccount:45678",
        "associatedEntity": "urn:li:organization:12345",
        "audienceExpansionEnabled": False,
        "connectedTelevisionOnly": False,
        "costType": "CPC",
        "creativeSelection": "OPTIMIZED",
        "dailyBudget": {"amount": "10.00", "currencyCode": "USD"},
        "locale": {"country": "US", "language": "en"},
        "name": "Fixture dark-post campaign",
        "offsiteDeliveryEnabled": False,
        "optimizationTargetType": "NONE",
        "politicalIntent": "NOT_POLITICAL",
        "runSchedule": {"start": 1_790_812_800_000, "end": 1_793_404_800_000},
        "targetingCriteria": {
            "include": {
                "and": (
                    {"or": {"urn:li:adTargetingFacet:locations": ("urn:li:geo:103644278",)}},
                    {"or": {"urn:li:adTargetingFacet:interfaceLocales": ("urn:li:locale:en_US",)}},
                )
            }
        },
        "type": "SPONSORED_UPDATES",
        "unitCost": {"amount": "5.00", "currencyCode": "USD"},
        "status": "DRAFT",
    }
    assert spec.creative_template.parent_campaign_handle == "dark-post-engagement"
    assert spec.creative_template.required_native_parent_field == "campaign"
    assert spec.creative_template.required_native_parent_urn_type == "urn:li:sponsoredCampaign"
    assert spec.creative_template.resolution_status == "REQUIRES_CREATE_READBACK_AND_REVALIDATION"
    assert spec.creative_template.request.model_dump() == {
        "content": {"reference": "urn:li:ugcPost:123456789"},
        "intendedStatus": "DRAFT",
    }
    assert spec.maximum_spend == Money(minor_units=25_000, currency="USD")
    assert spec.planning_boundary.status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert spec.planning_boundary.requires_parent_urn_create_readback_and_revalidation
    assert spec.redacted_review.cta_status == "NOT_APPLICABLE"
    assert spec.redacted_review.landing_page_status == "NOT_APPLICABLE"


def test_candidate_is_deterministic_and_review_never_leaks_post_or_native_refs() -> None:
    first, second = compile_linkedin_ads_campaign(_brief()), compile_linkedin_ads_campaign(_brief())
    assert first.input_digest == second.input_digest
    assert first.spec_digest == second.spec_digest
    rendered = json.dumps(first.redacted_review.model_dump(mode="json"), sort_keys=True)
    for value in (
        _COMMENTARY,
        "urn:li:ugcPost:123456789",
        "urn:li:image:fixture_image_123",
        "urn:li:sponsoredAccount:45678",
        "urn:li:organization:12345",
        "Customer supplied image title",
    ):
        assert value not in rendered


def test_forged_model_copy_post_creative_schedule_and_budget_fail_closed() -> None:
    brief = _brief()
    with pytest.raises(ValidationError, match="canonical_digest"):
        compile_linkedin_ads_campaign(
            brief.model_copy(
                update={
                    "creative": brief.creative.model_copy(
                        update={
                            "existing_post": brief.creative.existing_post.model_copy(
                                update={"commentary": "forged post content"}
                            )
                        }
                    )
                }
            )
        )
    with pytest.raises(ValidationError, match="exactly bind"):
        compile_linkedin_ads_campaign(
            brief.model_copy(
                update={
                    "creative": brief.creative.model_copy(
                        update={
                            "creative_version": brief.creative.creative_version.model_copy(
                                update={
                                    "manifest": brief.creative.creative_version.manifest.model_copy(
                                        update={"destination_ref": "destination:sha256:" + "a" * 64}
                                    )
                                }
                            )
                        }
                    )
                }
            )
        )
    with pytest.raises(ValidationError, match="increasing"):
        compile_linkedin_ads_campaign(brief.model_copy(update={"stop_time": brief.start_time}))
    with pytest.raises(ValidationError, match="daily budget"):
        compile_linkedin_ads_campaign(
            brief.model_copy(update={"daily_budget": Money(minor_units=25_001, currency="USD")})
        )
    with pytest.raises(ValidationError, match="dsc_ad_account_urn"):
        compile_linkedin_ads_campaign(
            brief.model_copy(
                update={
                    "creative": brief.creative.model_copy(
                        update={
                            "existing_post": brief.creative.existing_post.model_copy(
                                update={
                                    "dsc_ad_account_urn": "urn:li:sponsoredAccount:999",
                                    "canonical_digest": _post_digest(
                                        dsc_ad_account_urn="urn:li:sponsoredAccount:999"
                                    ),
                                }
                            )
                        }
                    )
                }
            )
        )


def test_closed_input_no_cta_or_destination_and_strict_native_payloads() -> None:
    brief_data = _brief().model_dump(mode="python")
    with pytest.raises(ValidationError, match="Extra inputs"):
        LinkedInAdsCampaignBrief.model_validate({**brief_data, "cta": "no"})
    with pytest.raises(ValidationError):
        LinkedInAdsCampaignBrief.model_validate({**brief_data, "group_total_budget": 25_000})
    with pytest.raises(ValidationError, match="Extra inputs"):
        LinkedInAdsCampaignBrief.model_validate(
            {**brief_data, "group_total_budget_minor_units": 25_000}
        )
    with pytest.raises(ValidationError, match="literal"):
        LinkedInAdsCampaignBrief.model_validate(
            {**_brief().model_dump(mode="python"), "political_intent": "NOT_DECLARED"}
        )
    native_campaign = {
        "account": "urn:li:sponsoredAccount:45678",
        "campaignGroup": "urn:li:sponsoredCampaignGroup:98765",
        "associatedEntity": "urn:li:organization:12345",
        "dailyBudget": {"amount": "10.00", "currencyCode": "USD"},
        "name": "x",
        "runSchedule": {"start": 1, "end": 2},
        "targetingCriteria": {
            "include": {
                "and": (
                    {"or": {"urn:li:adTargetingFacet:locations": ("urn:li:geo:103644278",)}},
                    {"or": {"urn:li:adTargetingFacet:interfaceLocales": ("urn:li:locale:en_US",)}},
                )
            }
        },
        "unitCost": {"amount": "5.00", "currencyCode": "USD"},
    }
    parsed_campaign = LinkedInNativeCampaignPayload.model_validate(native_campaign)
    assert parsed_campaign.model_dump() == native_campaign | {
        "audienceExpansionEnabled": False,
        "connectedTelevisionOnly": False,
        "costType": "CPC",
        "creativeSelection": "OPTIMIZED",
        "locale": {"country": "US", "language": "en"},
        "offsiteDeliveryEnabled": False,
        "optimizationTargetType": "NONE",
        "politicalIntent": "NOT_POLITICAL",
        "type": "SPONSORED_UPDATES",
        "status": "DRAFT",
    }
    assert (
        LinkedInNativeCampaignPayload.model_validate(parsed_campaign.model_dump())
        == parsed_campaign
    )
    with pytest.raises(ValidationError, match="Extra inputs"):
        LinkedInNativeCampaignPayload.model_validate(
            {
                "campaignGroupLocalHandle": "group",
                **native_campaign,
                "format": "SINGLE_IMAGE",
            }
        )
    with pytest.raises(ValidationError, match="Field required"):
        LinkedInNativeCampaignPayload.model_validate(
            {key: value for key, value in native_campaign.items() if key != "campaignGroup"}
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        LinkedInNativeCreativePayload.model_validate(
            {
                "content": {"reference": "urn:li:ugcPost:123456789"},
                "campaign": "urn:li:sponsoredCampaign:98765",
                "campaignLocalHandle": "dark-post-engagement",
            }
        )


def test_serialized_result_revalidates_graph_budget_and_content_digest() -> None:
    spec = compile_linkedin_ads_campaign(_brief())
    assert type(spec).model_validate(spec.model_dump(mode="python")) == spec
    forged = spec.model_copy(
        update={
            "maximum_spend": Money(minor_units=1, currency="USD"),
            "creative_template": spec.creative_template.model_copy(
                update={"parent_campaign_handle": "other-campaign"}
            ),
        }
    )
    with pytest.raises(ValidationError, match="group spend cap"):
        type(spec).model_validate(forged.model_dump(mode="python"))

    forged_post_reference = spec.model_copy(
        update={
            "creative_template": spec.creative_template.model_copy(
                update={
                    "request": spec.creative_template.request.model_copy(
                        update={
                            "content": LinkedInNativeCreativeContent(
                                reference="urn:li:share:987654321"
                            )
                        }
                    )
                }
            )
        }
    )
    with pytest.raises(ValidationError, match="creative binding"):
        type(spec).model_validate(forged_post_reference.model_dump(mode="python"))

    forged_daily_budget = spec.model_copy(
        update={
            "campaign_template": spec.campaign_template.model_copy(
                update={
                    "request": spec.campaign_template.request.model_copy(
                        update={"dailyBudget": LinkedInNativeMoney(amount="250.01")}
                    )
                }
            )
        }
    )
    with pytest.raises(ValidationError, match="group spend cap"):
        type(spec).model_validate(forged_daily_budget.model_dump(mode="python"))
