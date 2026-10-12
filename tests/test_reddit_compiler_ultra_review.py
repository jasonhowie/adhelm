"""Adversarial integrity checks for the offline Reddit v3 compiler."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from test_reddit_compiler import _brief

from adhelm.canonical import digest
from adhelm.providers.reddit_compiler import (
    RedditAdsCampaignBrief,
    RedditAdsFundingInstrumentReferenceEvidence,
    RedditAdsPixelReferenceEvidence,
    compile_reddit_ads_campaign,
)


def _content_digest_with(
    serialized: dict[str, object], *, destination_url: str | None = None
) -> str:
    ad = serialized["ad_request"]
    assert isinstance(ad, dict)
    return digest(
        {
            "type": "standard_post",
            "creative_version_id": serialized["creative_version_id"],
            "asset_content_digest": serialized["asset_content_digest"],
            "post_id": ad["post_id"],
            "post_content_digest": serialized["post_content_digest"],
            "destination_url": destination_url or ad["click_url"],
            "call_to_action": "LEARN_MORE",
        }
    )


def test_serialized_candidate_rejects_divergent_schedule_and_recomputed_destination() -> None:
    spec = compile_reddit_ads_campaign(_brief())

    divergent_schedule = spec.model_dump(mode="json")
    divergent_schedule["ad_group_request"]["start_time"] = (
        datetime(2026, 10, 2, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    )
    divergent_schedule["ad_group_request"]["end_time"] = (
        datetime(2026, 10, 30, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    )
    with pytest.raises(ValidationError, match="schedule"):
        type(spec).model_validate_json(json.dumps(divergent_schedule))

    forged_destination = spec.model_dump(mode="json")
    forged_destination["ad_request"]["click_url"] = "https://example.test/demo?utm_source=changed"
    forged_destination["creative_content_digest"] = _content_digest_with(
        forged_destination, destination_url="https://example.test/demo?utm_source=changed"
    )
    with pytest.raises(ValidationError, match="destination"):
        type(spec).model_validate_json(json.dumps(forged_destination))

    forged_spend = spec.model_dump(mode="json")
    forged_spend["campaign_request"]["goal_value"] += 1
    with pytest.raises(ValidationError, match="exposure limit"):
        type(spec).model_validate_json(json.dumps(forged_spend))


def test_account_bound_evidence_revalidates_copy_and_serialized_graph() -> None:
    brief = _brief()
    foreign_account = "reddit_ads.account.a2_other"
    foreign_pixel = brief.pixel_reference.model_copy(update={"account_ref": foreign_account})
    with pytest.raises(ValidationError, match="bound to the compiled account"):
        compile_reddit_ads_campaign(brief.model_copy(update={"pixel_reference": foreign_pixel}))

    foreign_funding = brief.funding_instrument_reference.model_copy(
        update={"account_ref": foreign_account}
    )
    bypassed = RedditAdsCampaignBrief.model_construct(
        **{**brief.__dict__, "funding_instrument_reference": foreign_funding}
    )
    with pytest.raises(ValidationError, match="bound to the compiled account"):
        compile_reddit_ads_campaign(bypassed)

    spec = compile_reddit_ads_campaign(brief)
    serialized = spec.model_dump(mode="json")
    serialized["funding_instrument_reference"]["account_ref"] = foreign_account
    with pytest.raises(ValidationError, match="account evidence bindings"):
        type(spec).model_validate_json(json.dumps(serialized))


@pytest.mark.parametrize("name_field", ("campaign_name", "ad_group_name", "ad_name"))
def test_revalidating_model_copy_enforces_current_reddit_name_limit(name_field: str) -> None:
    with pytest.raises(ValidationError, match="at most 500 characters"):
        compile_reddit_ads_campaign(_brief().model_copy(update={name_field: "x" * 501}))


@pytest.mark.parametrize("native_id", ("has space", "non-ascii-π", "bad\x7f"))
def test_opaque_pixel_and_funding_ids_are_bounded_printable_ascii(native_id: str) -> None:
    brief = _brief()
    with pytest.raises(ValidationError, match="printable ASCII"):
        RedditAdsPixelReferenceEvidence(
            account_ref=brief.account_ref,
            pixel_id=native_id,
            evidence_ref="evidence:sha256:" + "d" * 64,
        )
    with pytest.raises(ValidationError, match="printable ASCII"):
        RedditAdsFundingInstrumentReferenceEvidence(
            account_ref=brief.account_ref,
            funding_instrument_id=native_id,
            evidence_ref="evidence:sha256:" + "e" * 64,
        )


def test_native_graph_requires_funding_utc_targeting_and_keeps_review_private() -> None:
    brief = _brief()
    with pytest.raises(ValidationError):
        RedditAdsCampaignBrief.model_validate(
            {
                key: value
                for key, value in brief.model_dump(mode="python").items()
                if key != "funding_instrument_reference"
            }
        )

    opaque_references = brief.model_copy(
        update={
            "pixel_reference": RedditAdsPixelReferenceEvidence(
                account_ref=brief.account_ref,
                pixel_id="provider:pixel/opaque-1",
                evidence_ref="evidence:sha256:" + "d" * 64,
            ),
            "funding_instrument_reference": RedditAdsFundingInstrumentReferenceEvidence(
                account_ref=brief.account_ref,
                funding_instrument_id="provider:funding/opaque-1",
                evidence_ref="evidence:sha256:" + "e" * 64,
            ),
        }
    )
    spec = compile_reddit_ads_campaign(opaque_references)
    assert spec.campaign_request.conversion_pixel_id == "provider:pixel/opaque-1"
    assert spec.campaign_request.funding_instrument_id == "provider:funding/opaque-1"

    missing_funding = spec.model_dump(mode="json")
    del missing_funding["campaign_request"]["funding_instrument_id"]
    with pytest.raises(ValidationError):
        type(spec).model_validate_json(json.dumps(missing_funding))

    non_utc = spec.model_dump(mode="json")
    non_utc["campaign_request"]["start_time"] = "2026-09-30T19:00:00-05:00"
    non_utc["ad_group_request"]["start_time"] = "2026-09-30T19:00:00-05:00"
    with pytest.raises(ValidationError, match="UTC offset"):
        type(spec).model_validate_json(json.dumps(non_utc))

    unsupported_target = spec.model_dump(mode="json")
    unsupported_target["ad_group_request"]["targeting"]["custom_audience_ids"] = ["blocked"]
    with pytest.raises(ValidationError, match="Extra inputs"):
        type(spec).model_validate_json(json.dumps(unsupported_target))

    review = json.dumps(spec.redacted_review.model_dump(mode="json"), sort_keys=True)
    for private in (
        spec.pixel_reference.pixel_id,
        spec.pixel_reference.evidence_ref,
        spec.funding_instrument_reference.funding_instrument_id,
        spec.funding_instrument_reference.evidence_ref,
        spec.creative.post_reference.post_id,
        spec.creative.post_reference.evidence_ref,
        spec.creative.destination_url,
        spec.creative.association.asset.content_digest,
        spec.creative.association.creative_version.manifest.components[0].exact_text,
    ):
        assert private not in review
