"""Adversarial review regressions for the TikTok v1.3 offline compiler."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_tiktok_compiler import _brief

from adhelm.models import Money
from adhelm.providers.tiktok_compiler import (
    TikTokAdsCreativeAssociation,
    TikTokAdsNativeAdGroupPayload,
    TikTokAdsNativeTargeting,
    compile_tiktok_ads_campaign,
)


def test_emits_v13_major_unit_total_budget_utc_times_and_video_field_fragment() -> None:
    spec = compile_tiktok_ads_campaign(_brief())
    adgroup = spec.adgroup_request.model_dump(mode="json")
    assert adgroup["budget"] == "250.00"
    assert "budget_minor_units" not in adgroup
    assert adgroup["schedule_start_time"] == "2026-10-01 00:00:00"
    assert adgroup["schedule_end_time"] == "2026-10-31 00:00:00"
    assert adgroup["budget_mode"] == "BUDGET_MODE_TOTAL"
    assert adgroup["schedule_type"] == "SCHEDULE_START_END"
    assert adgroup["operation_status"] == "DISABLE"
    assert spec.campaign_request.budget_optimize_on is False
    ad = spec.ad_request.model_dump(mode="json")
    assert ad["identity_type"] == "CUSTOMIZED_USER"
    assert ad["ad_format"] == "SINGLE_VIDEO"
    assert ad["call_to_action"] == "LEARN_MORE"
    assert ad["operation_status"] == "DISABLE"
    assert "advertiser_id" not in ad
    assert "adgroup_id" not in ad


def test_reparse_rejects_forged_native_destination_reference_and_video_graph() -> None:
    spec = compile_tiktok_ads_campaign(_brief())
    forged_native_destination = spec.model_copy(
        update={
            "ad_request": spec.ad_request.model_copy(
                update={"landing_page_url": "https://attacker.test/demo?utm_source=tiktok"}
            )
        }
    )
    with pytest.raises(ValidationError, match="creative binding, destination"):
        type(spec).model_validate(forged_native_destination.model_dump(mode="python"))
    forged_reference = spec.model_copy(update={"reference_binding_digest": "0" * 64})
    with pytest.raises(ValidationError, match="creative binding, destination"):
        type(spec).model_validate(forged_reference.model_dump(mode="python"))
    forged_video = spec.source_brief.creative.association.video_asset.model_copy(
        update={"declared_duration_ms": 31_000}
    )
    forged_source = spec.source_brief.model_copy(
        update={
            "creative": spec.source_brief.creative.model_copy(
                update={
                    "association": spec.source_brief.creative.association.model_copy(
                        update={"video_asset": forged_video}
                    )
                }
            )
        }
    )
    with pytest.raises(ValidationError, match="intrinsic metadata"):
        type(spec).model_validate(
            spec.model_copy(update={"source_brief": forged_source}).model_dump()
        )


def test_rejects_microseconds_documented_date_ceiling_names_and_non_native_budget_forms() -> None:
    brief = _brief()
    with pytest.raises(ValidationError, match="whole UTC seconds"):
        compile_tiktok_ads_campaign(
            brief.model_copy(update={"start_time": datetime(2026, 10, 1, 0, 0, 0, 1, UTC)})
        )
    with pytest.raises(ValidationError, match="documented TikTok"):
        compile_tiktok_ads_campaign(
            brief.model_copy(
                update={
                    "start_time": datetime(2028, 1, 1, 0, 0, 1, tzinfo=UTC),
                    "stop_time": datetime(2028, 1, 2, tzinfo=UTC),
                }
            )
        )
    with pytest.raises(ValidationError, match="emoji or symbol"):
        compile_tiktok_ads_campaign(brief.model_copy(update={"ad_name": "campaign 🚀"}))
    payload = {
        "adgroup_name": "native group",
        "targeting": TikTokAdsNativeTargeting(location_ids=("6252001",)),
        "budget": Decimal("250.001"),
        "schedule_start_time": "2026-10-01 00:00:00",
        "schedule_end_time": "2026-10-02 00:00:00",
    }
    with pytest.raises(ValidationError, match="two USD decimal places"):
        TikTokAdsNativeAdGroupPayload.model_validate(payload)
    with pytest.raises(ValidationError, match="two USD decimal places"):
        TikTokAdsNativeAdGroupPayload.model_validate({**payload, "budget": Decimal("250.0")})
    with pytest.raises(ValidationError, match="instance of Decimal"):
        TikTokAdsNativeAdGroupPayload.model_validate({**payload, "budget": 250.0})
    with pytest.raises(ValidationError, match="String should match pattern"):
        TikTokAdsNativeAdGroupPayload.model_validate(
            {**payload, "budget": Decimal("250.00"), "schedule_start_time": "2026-10-01T00:00:00Z"}
        )


def test_closed_association_cannot_accept_an_image_placeholder_or_extra_native_fields() -> None:
    association = _brief().creative.association
    with pytest.raises(ValidationError, match="Extra inputs"):
        TikTokAdsCreativeAssociation.model_validate(
            {**association.model_dump(mode="python"), "asset": {"media_type": "IMAGE"}}
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        TikTokAdsNativeAdGroupPayload.model_validate(
            {
                "adgroup_name": "native group",
                "targeting": TikTokAdsNativeTargeting(location_ids=("6252001",)),
                "budget": Decimal("250.00"),
                "schedule_start_time": "2026-10-01 00:00:00",
                "schedule_end_time": "2026-10-02 00:00:00",
                "daily_budget": Decimal("25.00"),
            }
        )


def test_review_and_plan_keep_video_and_live_boundary_explicit() -> None:
    spec = compile_tiktok_ads_campaign(_brief())
    review = spec.redacted_review
    assert review.video_asset_status == "DECLARED_UNVERIFIED_NO_LOCAL_DECODE"
    assert review.video_execution_eligibility == "REQUIRES_VERIFIED_CANONICAL_VIDEO_ASSET"
    assert spec.planning_boundary.requires_verified_canonical_video_asset_history
    assert spec.planning_boundary.requires_schedule_eligibility_readback
    assert spec.planning_boundary.requires_provider_id_binding_before_request
    assert spec.planning_boundary.requires_remote_drift_check_and_authorization


def test_total_budget_is_typed_money_and_nested_revalidation_fails_closed() -> None:
    brief = _brief()
    brief_payload = brief.model_dump(mode="python")
    for invalid_scalar in (True, 25_000.0):
        with pytest.raises(ValidationError):
            type(brief).model_validate({**brief_payload, "total_budget": invalid_scalar})
    for constructed_bypass in (Money.model_construct(minor_units=True, currency="USD"),):
        with pytest.raises(ValidationError):
            type(brief).model_validate({**brief_payload, "total_budget": constructed_bypass})
    with pytest.warns(UserWarning), pytest.raises(ValidationError):
        type(brief).model_validate(
            {
                **brief_payload,
                "total_budget": Money.model_construct(minor_units=25_000.0, currency="USD"),
            }
        )
    for forged_money, message in (
        (Money.model_construct(minor_units=0, currency="USD"), "positive Money"),
        (Money.model_construct(minor_units=25_000, currency="EUR"), "account currency"),
        (
            Money.model_construct(minor_units=9_007_199_254_740_992, currency="USD"),
            "less than or equal",
        ),
    ):
        with pytest.raises(ValidationError, match=message):
            compile_tiktok_ads_campaign(brief.model_copy(update={"total_budget": forged_money}))
    spec = compile_tiktok_ads_campaign(brief)
    forged_source = spec.source_brief.model_copy(
        update={"total_budget": Money.model_construct(minor_units=0, currency="USD")}
    )
    with pytest.raises(ValidationError, match="positive Money"):
        type(spec).model_validate(
            spec.model_copy(update={"source_brief": forged_source}).model_dump(mode="python")
        )
