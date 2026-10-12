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
from adhelm.providers.openai_compiler import (
    OpenAIAdsAdBrief,
    OpenAIAdsAdGroupBrief,
    OpenAIAdsCampaignBrief,
    OpenAIAdsChatCardCreative,
    OpenAIAdsCreativeAssociation,
    OpenAIAdsDestinationPolicy,
    OpenAIAdsFileReferenceEvidence,
    compile_openai_ads_campaign,
)


def _association(
    title: str, body: str, destination_url: str
) -> tuple[OpenAIAdsCreativeAssociation, str]:
    content_digest = hashlib.sha256(b"openai-chat-card-image").hexdigest()
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
        byte_size=1234,
        width_px=1200,
        height_px=628,
        aspect_ratio="300:157",
    )
    rendition = CreativeRendition.from_asset(
        asset, provider_format="OPENAI_ADS_CHAT_CARD", locale="en-US"
    )
    creative_version = CreativeVersion(
        creative_family_id="family:openai-launch",
        manifest=ServingCreativeManifest(
            provider_format="OPENAI_ADS_CHAT_CARD",
            locale="en-US",
            destination_ref="destination:sha256:" + digest({"target_url": destination_url}),
            rendition_refs=(
                ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
            ),
            components=(
                CreativeComponent(
                    component_id="title",
                    component_role="HEADLINE",
                    ordinal=0,
                    exact_text=title,
                    normalized_text=title.lower(),
                    language="en",
                    provider_slot="title",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
                CreativeComponent(
                    component_id="body",
                    component_role="PRIMARY_TEXT",
                    ordinal=1,
                    exact_text=body,
                    normalized_text=body.lower(),
                    language="en",
                    provider_slot="body",
                    provenance="DECLARED",
                    confidence_basis_points=10_000,
                    extraction_status="DECLARED",
                ),
            ),
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return (
        OpenAIAdsCreativeAssociation(
            creative_version=creative_version,
            asset=asset,
            rendition=rendition,
        ),
        content_digest,
    )


def _brief(
    *,
    destination_url: str = "https://example.test/demo?utm_source=ads",
    title: str = "Plan work faster",
    body: str = "Bring tasks and documents together.",
) -> OpenAIAdsCampaignBrief:
    association, asset_content_digest = _association(title, body, destination_url)
    return OpenAIAdsCampaignBrief(
        account_ref="profile.openai.account",
        account_currency="USD",
        currency_minor_unit_exponent=2,
        account_timezone="America/Chicago",
        campaign_handle="launch",
        name="September launch",
        start_time=1_788_264_800,
        end_time=1_790_856_000,
        lifetime_budget=Money(minor_units=2_500, currency="USD"),
        bidding_type="clicks",
        location_ids=("2000043", "3000194"),
        destination_policy=OpenAIAdsDestinationPolicy(
            allowed_hosts=("example.test",), allowed_paths=("/demo",)
        ),
        ad_groups=(
            OpenAIAdsAdGroupBrief(
                ad_group_handle="us",
                name="US campaign group",
                max_bid_micros=60_000,
                ads=(
                    OpenAIAdsAdBrief(
                        ad_handle="card",
                        name="Planner card",
                        creative=OpenAIAdsChatCardCreative(
                            title=title,
                            body=body,
                            destination_url=destination_url,
                            association=association,
                            file_reference=OpenAIAdsFileReferenceEvidence(
                                account_ref="profile.openai.account",
                                file_id="file_customer_asset_01",
                                asset_content_digest=asset_content_digest,
                                evidence_ref="evidence:sha256:" + "a" * 64,
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


def test_compiles_complete_paused_native_hierarchy_with_only_local_parent_handles() -> None:
    spec = compile_openai_ads_campaign(_brief())

    assert spec.provider == "openai_ads"
    assert spec.provider_api_version == "v1"
    assert spec.campaign_request.status == "paused"
    assert spec.campaign_request.bidding_type == "clicks"
    assert spec.campaign_request.budget.lifetime_spend_limit_micros == 25_000_000
    assert tuple(item.id for item in spec.campaign_request.targeting.locations.include) == (
        "2000043",
        "3000194",
    )
    assert spec.ad_groups[0].parent_campaign_handle == "launch"
    assert spec.ad_groups[0].request.status == "paused"
    assert spec.ad_groups[0].request.bidding_config.billing_event_type == "click"
    assert spec.ad_groups[0].ads[0].parent_ad_group_handle == "us"
    assert spec.ad_groups[0].ads[0].request.status == "paused"
    assert spec.ad_groups[0].ads[0].request.creative.type == "chat_card"
    assert spec.activation_boundary.initial_state == "PAUSED"
    assert spec.activation_boundary.activation_compiled is False
    assert spec.activation_boundary.maximum_spend_micros == 25_000_000
    assert spec.activation_boundary.maximum_spend.minor_units == 2_500
    assert spec.planning_boundary.status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert spec.ad_groups[0].ads[0].creative_version_id.startswith("creative:sha256:")
    assert spec.lossy_mappings == ()
    assert spec.unsupported_mappings == ()
    assert "cmpn_" not in spec.model_dump_json()
    assert "adgrp_" not in spec.model_dump_json()


def test_compilation_is_deterministic_and_copy_changes_are_content_addressed() -> None:
    first = compile_openai_ads_campaign(_brief())
    second = compile_openai_ads_campaign(_brief())
    changed = _brief(title="Plan faster now")
    changed_spec = compile_openai_ads_campaign(changed)

    assert first.spec_digest == second.spec_digest
    assert first.input_digest == second.input_digest
    assert (
        first.ad_groups[0].ads[0].creative_content_digest
        != changed_spec.ad_groups[0].ads[0].creative_content_digest
    )
    assert first.spec_digest != changed_spec.spec_digest


def test_default_review_redacts_exact_copy_file_and_destination() -> None:
    spec = compile_openai_ads_campaign(_brief())
    rendered = json.dumps(spec.redacted_review.model_dump(mode="json"), sort_keys=True)

    assert "Plan work faster" not in rendered
    assert "Bring tasks" not in rendered
    assert "file_customer_asset_01" not in rendered
    assert "example.test" not in rendered
    assert spec.ad_groups[0].ads[0].creative_content_digest in rendered


def test_unsafe_destination_fails_before_a_spec_is_created() -> None:
    with pytest.raises(AdhelmError, match="Only declared controlled UTMs") as raised:
        compile_openai_ads_campaign(_brief(destination_url="https://example.test/demo?coupon=yes"))

    assert raised.value.code == "SENSITIVE_DATA_RISK"


def test_conversion_bidding_and_unreviewed_audience_surface_fail_closed() -> None:
    with pytest.raises(AdhelmError, match="Conversion bidding") as raised:
        compile_openai_ads_campaign(_brief().model_copy(update={"bidding_type": "conversions"}))
    assert raised.value.code == "UNSUPPORTED_CONVERSION_OPTIMIZATION"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        OpenAIAdsCampaignBrief.model_validate(
            {**_brief().model_dump(mode="python"), "custom_audience_ids": ["audience_1"]}
        )


def test_bare_native_micros_cannot_replace_the_typed_lifetime_budget() -> None:
    data = _brief().model_dump(mode="python")
    data.pop("lifetime_budget")
    data["lifetime_spend_limit_micros"] = 1_000_001
    with pytest.raises(ValidationError):
        OpenAIAdsCampaignBrief.model_validate(data)
