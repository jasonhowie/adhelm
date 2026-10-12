"""Adversarial regression coverage for the fixed Google Ads v25 offline compiler."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from test_google_ads_compiler import _brief

from adhelm.errors import AdhelmError
from adhelm.models import Money
from adhelm.providers.google_ads_compiler import (
    GoogleAdsAdSchedule,
    GoogleAdsCompiledCampaignSpec,
    GoogleAdsNativeAdTextAsset,
    GoogleAdsNativeCampaignPayload,
    GoogleAdsNativeScheduleCriterionPayload,
    compile_google_ads_search_campaign,
)


def test_emits_v25_campaign_date_target_spend_and_schedule_minute_enums() -> None:
    spec = compile_google_ads_search_campaign(_brief())

    campaign = spec.campaign_request.model_dump(mode="json")
    assert campaign["start_date_time"] == "2026-10-01 00:00:00"
    assert campaign["end_date_time"] == "2026-10-31 23:59:59"
    assert campaign["target_spend"] == {}
    assert "start_date" not in campaign
    assert "end_date" not in campaign
    assert "maximize_clicks" not in campaign

    schedule = next(
        item.request.ad_schedule
        for item in spec.campaign_criteria
        if isinstance(item.request, GoogleAdsNativeScheduleCriterionPayload)
    )
    assert schedule.start_minute == "ZERO"
    assert schedule.end_minute == "ZERO"
    assert spec.redacted_review.campaign_duration_days == 31
    assert spec.planning_boundary.requires_asset_automation_and_ai_max_readback is True
    assert spec.planning_boundary.requires_geo_target_type_readback is True


@pytest.mark.parametrize(
    ("minute", "expected_native_minute"),
    ((0, "ZERO"), (15, "FIFTEEN"), (30, "THIRTY"), (45, "FORTY_FIVE")),
)
def test_maps_every_quarter_hour_input_to_the_v25_minute_enum(
    minute: int, expected_native_minute: str
) -> None:
    input_schedule = GoogleAdsAdSchedule(
        handle=f"quarter-hour-{minute}",
        day_of_week="TUESDAY",
        start_hour=9,
        start_minute=minute,
        end_hour=10,
        end_minute=minute,
    )
    spec = compile_google_ads_search_campaign(
        _brief().model_copy(update={"ad_schedules": (input_schedule,)})
    )
    native_schedule = next(
        item.request.ad_schedule
        for item in spec.campaign_criteria
        if isinstance(item.request, GoogleAdsNativeScheduleCriterionPayload)
    )
    assert native_schedule.start_minute == expected_native_minute
    assert native_schedule.end_minute == expected_native_minute


def test_output_revalidation_rejects_forged_hierarchy_creative_and_destination() -> None:
    spec = compile_google_ads_search_campaign(_brief())

    wrong_parent = spec.model_copy(
        update={
            "ad_group": spec.ad_group.model_copy(
                update={"parent_campaign_handle": "other-campaign"}
            )
        }
    )
    with pytest.raises(ValidationError, match="parent handle"):
        GoogleAdsCompiledCampaignSpec.model_validate(wrong_parent.model_dump(mode="python"))

    native_ad = spec.responsive_search_ad.request.ad
    changed_text = native_ad.responsive_search_ad.model_copy(
        update={
            "headlines": (
                GoogleAdsNativeAdTextAsset(text="Changed headline"),
                *native_ad.responsive_search_ad.headlines[1:],
            )
        }
    )
    wrong_creative = spec.model_copy(
        update={
            "responsive_search_ad": spec.responsive_search_ad.model_copy(
                update={
                    "request": spec.responsive_search_ad.request.model_copy(
                        update={
                            "ad": native_ad.model_copy(
                                update={"responsive_search_ad": changed_text}
                            )
                        }
                    )
                }
            )
        }
    )
    with pytest.raises(ValidationError, match="creative identity"):
        GoogleAdsCompiledCampaignSpec.model_validate(wrong_creative.model_dump(mode="python"))

    wrong_destination = spec.model_copy(
        update={
            "responsive_search_ad": spec.responsive_search_ad.model_copy(
                update={
                    "request": spec.responsive_search_ad.request.model_copy(
                        update={
                            "ad": native_ad.model_copy(
                                update={"final_urls": ("https://unapproved.test/demo",)}
                            )
                        }
                    )
                }
            )
        }
    )
    with pytest.raises(AdhelmError) as raised:
        GoogleAdsCompiledCampaignSpec.model_validate(wrong_destination.model_dump(mode="python"))
    assert raised.value.code == "UNAPPROVED_EGRESS"


def test_rejects_obsolete_or_extra_v25_campaign_bidding_and_date_fields() -> None:
    valid_campaign = {
        "name": "Customer Search campaign",
        "start_date_time": "2026-10-01 00:00:00",
        "end_date_time": "2026-10-31 23:59:59",
        "network_settings": {},
        "target_spend": {},
    }
    assert (
        GoogleAdsNativeCampaignPayload.model_validate(valid_campaign).target_spend.model_dump()
        == {}
    )

    with pytest.raises(ValidationError, match="start_date_time"):
        GoogleAdsNativeCampaignPayload.model_validate(
            {
                "name": "Customer Search campaign",
                "start_date": "20261001",
                "end_date": "20261031",
                "network_settings": {},
                "target_spend": {},
            }
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        GoogleAdsNativeCampaignPayload.model_validate(
            {
                **valid_campaign,
                "target_spend": {"target_spend_micros": 10_000},
            }
        )
    with pytest.raises(ValidationError, match="end_date_time"):
        GoogleAdsNativeCampaignPayload.model_validate(
            {**valid_campaign, "end_date_time": "2026-10-31 00:00:00"}
        )


def test_revalidates_copied_names_schedule_limits_and_native_schedule_symbol_types() -> None:
    seven_monday_schedules = tuple(
        GoogleAdsAdSchedule(
            handle=f"monday-{index}",
            day_of_week="MONDAY",
            start_hour=index,
            start_minute=0,
            end_hour=index + 1,
            end_minute=0,
        )
        for index in range(7)
    )
    with pytest.raises(ValidationError, match="six ad schedules per day"):
        compile_google_ads_search_campaign(
            _brief().model_copy(update={"ad_schedules": seven_monday_schedules})
        )

    with pytest.raises(ValidationError, match="NUL, newline, or carriage return"):
        compile_google_ads_search_campaign(
            _brief().model_copy(update={"campaign_name": "campaign\nname"})
        )

    with pytest.raises(ValidationError, match="start_minute"):
        GoogleAdsNativeScheduleCriterionPayload.model_validate(
            {
                "ad_schedule": {
                    "day_of_week": "MONDAY",
                    "start_hour": 9,
                    "start_minute": 0,
                    "end_hour": 10,
                    "end_minute": "ZERO",
                }
            }
        )


def test_output_revalidation_preserves_total_budget_and_local_ad_label() -> None:
    spec = compile_google_ads_search_campaign(_brief())
    assert spec.responsive_search_ad.local_name == "Customer RSA"
    assert "name" not in spec.responsive_search_ad.request.model_dump(mode="json")

    forged_budget = spec.model_copy(
        update={"maximum_spend": Money(minor_units=25_001, currency="USD")}
    )
    with pytest.raises(ValidationError, match="total budget"):
        GoogleAdsCompiledCampaignSpec.model_validate(forged_budget.model_dump(mode="python"))
