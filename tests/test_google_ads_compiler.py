from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from adhelm.canonical import digest
from adhelm.creative import (
    CreativeComponent,
    CreativeVersion,
    ServingCreativeManifest,
    ServingRendition,
)
from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.providers.google_ads_compiler import (
    GoogleAdsAdSchedule,
    GoogleAdsDestinationPolicy,
    GoogleAdsKeyword,
    GoogleAdsLanguageTarget,
    GoogleAdsLocationTarget,
    GoogleAdsNativeCampaignPayload,
    GoogleAdsRsaCreative,
    GoogleAdsSearchCampaignBrief,
    compile_google_ads_search_campaign,
)


def _creative(
    *, destination_url: str = "https://example.test/demo?utm_source=google"
) -> GoogleAdsRsaCreative:
    headlines = ("Customer headline", "Customer proof", "Learn more")
    descriptions = ("Customer supplied description", "A second approved description")
    components = (
        tuple(
            CreativeComponent(
                component_id=f"headline-{index}",
                component_role="HEADLINE",
                ordinal=index,
                exact_text=text,
                normalized_text=text.casefold(),
                language="en",
                provider_slot="responsive_search_ad.headlines",
                provenance="DECLARED",
                confidence_basis_points=10_000,
                extraction_status="DECLARED",
            )
            for index, text in enumerate(headlines)
        )
        + tuple(
            CreativeComponent(
                component_id=f"description-{index}",
                component_role="DESCRIPTION",
                ordinal=index + len(headlines),
                exact_text=text,
                normalized_text=text.casefold(),
                language="en",
                provider_slot="responsive_search_ad.descriptions",
                provenance="DECLARED",
                confidence_basis_points=10_000,
                extraction_status="DECLARED",
            )
            for index, text in enumerate(descriptions)
        )
        + (
            CreativeComponent(
                component_id="cta",
                component_role="CTA_BUTTON",
                ordinal=len(headlines) + len(descriptions),
                exact_text="Learn more",
                normalized_text="learn more",
                language="en",
                provider_slot="responsive_search_ad.headlines",
                normalized_cta_intent="LEARN_MORE",
                cta_destination_ref="destination:sha256:" + digest({"target_url": destination_url}),
                provenance="DECLARED",
                confidence_basis_points=10_000,
                extraction_status="DECLARED",
            ),
        )
    )
    version = CreativeVersion(
        creative_family_id="family:google-search",
        manifest=ServingCreativeManifest(
            provider_format="GOOGLE_ADS_RESPONSIVE_SEARCH_AD",
            locale="en-US",
            destination_ref="destination:sha256:" + digest({"target_url": destination_url}),
            surface_kind="TEXT_ONLY",
            rendition_refs=(),
            components=components,
        ),
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return GoogleAdsRsaCreative(
        creative_version=version,
        headlines=headlines,
        descriptions=descriptions,
        call_to_action_headline="Learn more",
        destination_url=destination_url,
    )


def _brief(
    *, destination_url: str = "https://example.test/demo?utm_source=google"
) -> GoogleAdsSearchCampaignBrief:
    return GoogleAdsSearchCampaignBrief(
        account_ref="google_ads.account.1234567890",
        account_timezone="America/Chicago",
        budget_handle="budget",
        campaign_handle="campaign",
        ad_group_handle="adgroup",
        ad_handle="rsa",
        campaign_name="Customer Search campaign",
        ad_group_name="Customer Search ad group",
        ad_name="Customer RSA",
        total_budget=Money(minor_units=25_000, currency="USD"),
        start_date=date(2026, 10, 1),
        end_date=date(2026, 10, 31),
        locations=(
            GoogleAdsLocationTarget(handle="us", geo_target_constant="geoTargetConstants/2840"),
        ),
        languages=(
            GoogleAdsLanguageTarget(handle="english", language_constant="languageConstants/1000"),
        ),
        ad_schedules=(
            GoogleAdsAdSchedule(
                handle="weekday-hours",
                day_of_week="MONDAY",
                start_hour=9,
                start_minute=0,
                end_hour=17,
                end_minute=0,
            ),
        ),
        keywords=(
            GoogleAdsKeyword(handle="keyword-one", text="customer software", match_type="PHRASE"),
        ),
        destination_policy=GoogleAdsDestinationPolicy(
            allowed_hosts=("example.test",), allowed_paths=("/demo",)
        ),
        creative=_creative(destination_url=destination_url),
    )


def test_compiles_closed_paused_search_rsa_hierarchy_with_total_budget() -> None:
    spec = compile_google_ads_search_campaign(_brief())
    assert spec.budget.request.model_dump() == {
        "explicitly_shared": False,
        "period": "CUSTOM_PERIOD",
        "delivery_method": "STANDARD",
        "total_amount_micros": 250_000_000,
    }
    assert spec.campaign_request.model_dump() == {
        "name": "Customer Search campaign",
        "advertising_channel_type": "SEARCH",
        "status": "PAUSED",
        "start_date_time": "2026-10-01 00:00:00",
        "end_date_time": "2026-10-31 23:59:59",
        "network_settings": {
            "target_google_search": True,
            "target_search_network": False,
            "target_content_network": False,
            "target_partner_search_network": False,
            "target_youtube": False,
            "target_google_tv_network": False,
        },
        "target_spend": {},
    }
    assert spec.campaign_budget_handle == "budget"
    assert spec.ad_group.parent_campaign_handle == "campaign"
    assert spec.keywords[0].parent_ad_group_handle == "adgroup"
    assert spec.responsive_search_ad.parent_ad_group_handle == "adgroup"
    assert spec.responsive_search_ad.request.status == "PAUSED"
    assert all(item.request.status == "PAUSED" for item in spec.campaign_criteria)
    assert spec.maximum_spend.minor_units == 25_000
    assert spec.planning_boundary.requires_campaign_total_budget_eligibility_readback is True


def test_redacted_review_preserves_rsa_combination_and_attribution_limit() -> None:
    first, second = (
        compile_google_ads_search_campaign(_brief()),
        compile_google_ads_search_campaign(_brief()),
    )
    assert first.spec_digest == second.spec_digest
    assert first.redacted_review.rsa_component_composition == "DYNAMIC_COMBINATION_UNKNOWN"
    assert first.redacted_review.component_level_attribution == "NOT_AVAILABLE_FROM_PROVIDER"
    rendered = json.dumps(first.redacted_review.model_dump(mode="json"), sort_keys=True)
    assert "Customer headline" not in rendered
    assert "customer software" not in rendered
    assert "example.test" not in rendered
    assert "2840" not in rendered


def test_revalidates_forged_models_and_rejects_extra_serving_components() -> None:
    brief = _brief()
    extra = CreativeComponent(
        component_id="extra",
        component_role="PRIMARY_TEXT",
        ordinal=6,
        exact_text="Unmatched copy",
        normalized_text="unmatched copy",
        language="en",
        provider_slot="unreviewed",
        provenance="DECLARED",
        confidence_basis_points=10_000,
        extraction_status="DECLARED",
    )
    forged_manifest = brief.creative.creative_version.manifest.model_copy(
        update={"components": (*brief.creative.creative_version.manifest.components, extra)}
    )
    forged = brief.model_copy(
        update={
            "creative": brief.creative.model_copy(
                update={
                    "creative_version": brief.creative.creative_version.model_copy(
                        update={"manifest": forged_manifest}
                    )
                }
            )
        }
    )
    with pytest.raises(ValidationError, match="exactly the ordered RSA"):
        compile_google_ads_search_campaign(forged)

    rendition_manifest = brief.creative.creative_version.manifest.model_copy(
        update={
            "rendition_refs": (
                ServingRendition(rendition_id="unexpected", ordinal=0, role="asset"),
            )
        }
    )
    with pytest.raises(ValidationError, match="(?i)text-only"):
        compile_google_ads_search_campaign(
            brief.model_copy(
                update={
                    "creative": brief.creative.model_copy(
                        update={
                            "creative_version": brief.creative.creative_version.model_copy(
                                update={"manifest": rendition_manifest}
                            )
                        }
                    )
                }
            )
        )

    pinned_components = (
        brief.creative.creative_version.manifest.components[0].model_copy(
            update={"pinned_position": "HEADLINE_1"}
        ),
        *brief.creative.creative_version.manifest.components[1:],
    )
    with pytest.raises(ValidationError, match="exactly the ordered RSA"):
        compile_google_ads_search_campaign(
            brief.model_copy(
                update={
                    "creative": brief.creative.model_copy(
                        update={
                            "creative_version": brief.creative.creative_version.model_copy(
                                update={
                                    "manifest": brief.creative.creative_version.manifest.model_copy(
                                        update={"components": pinned_components}
                                    )
                                }
                            )
                        }
                    )
                }
            )
        )


def test_rejects_unsafe_destination_unsupported_mapping_and_handle_collision() -> None:
    with pytest.raises(AdhelmError) as raised:
        compile_google_ads_search_campaign(
            _brief(destination_url="https://example.test/demo?email=x")
        )
    assert raised.value.code == "SENSITIVE_DATA_RISK"

    with pytest.raises(ValidationError, match="literal"):
        GoogleAdsKeyword(handle="broad", text="customer software", match_type="BROAD")
    with pytest.raises(ValidationError, match="unique"):
        compile_google_ads_search_campaign(
            _brief().model_copy(
                update={
                    "keywords": (
                        GoogleAdsKeyword(handle="campaign", text="other term", match_type="EXACT"),
                    )
                }
            )
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        GoogleAdsNativeCampaignPayload.model_validate(
            {
                "name": "x",
                "start_date_time": "2026-10-01 00:00:00",
                "end_date_time": "2026-10-31 23:59:59",
                "network_settings": {},
                "target_spend": {},
                "user_list": "forbidden",
            }
        )


def test_rejects_invalid_timezone_dates_and_unbound_cta() -> None:
    with pytest.raises(ValidationError, match="IANA"):
        compile_google_ads_search_campaign(
            _brief().model_copy(update={"account_timezone": "not/a-timezone"})
        )
    with pytest.raises(ValidationError, match="366"):
        compile_google_ads_search_campaign(
            _brief().model_copy(update={"end_date": date(2027, 10, 2)})
        )
    with pytest.raises(ValidationError, match="CTA must be one"):
        GoogleAdsRsaCreative(
            creative_version=_creative().creative_version,
            headlines=("Customer headline", "Customer proof", "Other CTA"),
            descriptions=("Customer supplied description", "A second approved description"),
            call_to_action_headline="Learn more",
            destination_url="https://example.test/demo?utm_source=google",
        )
