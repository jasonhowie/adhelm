"""Adversarial checks for the LinkedIn existing-dark-post offline compiler."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_linkedin_compiler import _brief, _post_digest

from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.providers.linkedin_compiler import (
    LinkedInAdsCampaignBrief,
    LinkedInCreativeAssociation,
    LinkedInExistingDarkImagePost,
    LinkedInNativeCampaignPayload,
    LinkedInNativeCreativePayload,
    compile_linkedin_ads_campaign,
)


def test_native_targeting_aliases_round_trip_and_local_handles_stay_out_of_wire_fields() -> None:
    spec = compile_linkedin_ads_campaign(_brief())
    serialized = spec.model_dump(mode="json")

    targeting = serialized["campaign_template"]["request"]["targetingCriteria"]["include"]
    assert set(targeting) == {"and"}
    assert targeting["and"][0] == {
        "or": {"urn:li:adTargetingFacet:locations": ["urn:li:geo:103644278"]}
    }
    assert targeting["and"][1] == {
        "or": {"urn:li:adTargetingFacet:interfaceLocales": ["urn:li:locale:en_US"]}
    }
    assert "campaignGroupLocalHandle" not in json.dumps(serialized, sort_keys=True)
    assert "campaignLocalHandle" not in json.dumps(serialized, sort_keys=True)
    assert "campaignGroup" not in serialized["campaign_template"]["request"]
    assert "campaign" not in serialized["creative_template"]["request"]
    assert type(spec).model_validate_json(json.dumps(serialized)) == spec


def test_complete_native_create_payloads_require_provider_urns_and_reject_local_substitutes() -> (
    None
):
    spec = compile_linkedin_ads_campaign(_brief())
    campaign = LinkedInNativeCampaignPayload.model_validate(
        {
            **spec.campaign_template.request.model_dump(mode="python"),
            "campaignGroup": "urn:li:sponsoredCampaignGroup:7654321",
        }
    )
    creative = LinkedInNativeCreativePayload.model_validate(
        {
            **spec.creative_template.request.model_dump(mode="python"),
            "campaign": "urn:li:sponsoredCampaign:7654321",
        }
    )
    assert campaign.campaignGroup == "urn:li:sponsoredCampaignGroup:7654321"
    assert creative.campaign == "urn:li:sponsoredCampaign:7654321"
    assert LinkedInNativeCampaignPayload.model_validate(campaign.model_dump()) == campaign
    assert LinkedInNativeCreativePayload.model_validate(creative.model_dump()) == creative

    internal_alias = campaign.model_dump(mode="python")
    targeting = internal_alias["targetingCriteria"]["include"]
    targeting["all_of"] = targeting.pop("and")
    with pytest.raises(ValidationError, match="all_of"):
        LinkedInNativeCampaignPayload.model_validate(internal_alias)

    with pytest.raises(ValidationError, match="campaignGroup"):
        LinkedInNativeCampaignPayload.model_validate(spec.campaign_template.request.model_dump())
    with pytest.raises(ValidationError, match="campaign"):
        LinkedInNativeCreativePayload.model_validate(spec.creative_template.request.model_dump())
    with pytest.raises(ValidationError, match="Extra inputs"):
        LinkedInNativeCreativePayload.model_validate(
            {
                **creative.model_dump(mode="python"),
                "campaignLocalHandle": spec.campaign_handle,
                "inlineContent": {"post": {}},
            }
        )


def test_model_construct_revalidates_dark_post_account_and_exact_epoch_boundary() -> None:
    brief = _brief()
    post = brief.creative.existing_post
    bypassed_post = LinkedInExistingDarkImagePost.model_construct(
        **{**post.__dict__, "is_dsc": False}
    )
    bypassed_association = LinkedInCreativeAssociation.model_construct(
        **{**brief.creative.__dict__, "existing_post": bypassed_post}
    )
    bypassed_brief = LinkedInAdsCampaignBrief.model_construct(
        **{**brief.__dict__, "creative": bypassed_association}
    )
    with pytest.raises(ValidationError, match="is_dsc"):
        compile_linkedin_ads_campaign(bypassed_brief)

    with pytest.raises(ValidationError, match="millisecond-aligned"):
        compile_linkedin_ads_campaign(
            brief.model_copy(update={"start_time": datetime(2026, 10, 1, 0, 0, 0, 1, tzinfo=UTC)})
        )

    with pytest.raises(ValidationError, match="declared account currency"):
        compile_linkedin_ads_campaign(
            brief.model_copy(
                update={"daily_budget": brief.daily_budget.model_copy(update={"currency": "EUR"})}
            )
        )

    bypassed_money_brief = LinkedInAdsCampaignBrief.model_construct(
        **{
            **brief.__dict__,
            "unit_cost": Money.model_construct(minor_units=0, currency="USD"),
        }
    )
    with pytest.raises(ValidationError, match="positive bounded Money"):
        compile_linkedin_ads_campaign(bypassed_money_brief)

    with pytest.raises(ValidationError, match="Extra inputs"):
        LinkedInExistingDarkImagePost.model_validate(
            {
                **post.model_dump(mode="python"),
                "contentLandingPage": "https://example.test/undeclared",
            }
        )


def test_serialized_graph_binds_page_account_and_post_reference_and_screens_prose() -> None:
    spec = compile_linkedin_ads_campaign(_brief())

    for path, replacement in (
        (("campaign_template", "request", "associatedEntity"), "urn:li:organization:999"),
        (("creative_template", "request", "content", "reference"), "urn:li:share:999"),
    ):
        serialized = spec.model_dump(mode="json")
        target: dict[str, object] = serialized
        for key in path[:-1]:
            child = target[key]
            assert isinstance(child, dict)
            target = child
        target[path[-1]] = replacement
        with pytest.raises(ValidationError, match="creative binding"):
            type(spec).model_validate_json(json.dumps(serialized))

    foreign_account = spec.model_dump(mode="json")
    foreign_account["campaign_group_request"]["account"] = "urn:li:sponsoredAccount:999"
    foreign_account["campaign_template"]["request"]["account"] = "urn:li:sponsoredAccount:999"
    with pytest.raises(ValidationError, match="creative binding"):
        type(spec).model_validate_json(json.dumps(foreign_account))

    divergent_schedule = spec.model_dump(mode="json")
    divergent_schedule["campaign_template"]["request"]["runSchedule"]["end"] += 1
    with pytest.raises(ValidationError, match="group spend cap"):
        type(spec).model_validate_json(json.dumps(divergent_schedule))

    post = _brief().creative.existing_post
    with pytest.raises(AdhelmError) as raised:
        LinkedInExistingDarkImagePost.model_validate(
            {
                **post.model_dump(mode="python"),
                "commentary": "contact person@example.test",
                "canonical_digest": _post_digest(commentary="contact person@example.test"),
            }
        )
    assert raised.value.code == "SENSITIVE_DATA_RISK"
