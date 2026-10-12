"""Adversarial regressions for the offline OpenAI Ads compiler.

These probes use only local typed objects.  They make no provider request.
"""

from __future__ import annotations

import hashlib
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

ACCOUNT = "openai.account.local"
TITLE = "Plan work faster"
BODY = "Bring tasks and documents together."
FORMAT = "OPENAI_ADS_CHAT_CARD"
EVIDENCE = "evidence:sha256:" + "e" * 64


def _association(
    title: str = TITLE,
    body: str = BODY,
    destination_url: str = "https://example.test/demo?utm_source=ads",
) -> tuple[OpenAIAdsCreativeAssociation, str]:
    content_digest = hashlib.sha256(b"review-local-image").hexdigest()
    asset = CreativeAssetVersion(
        asset_version_id="asset:sha256:"
        + digest(
            {
                "content_digest": content_digest,
                "mime_type": "image/png",
                "width_px": 1,
                "height_px": 1,
            }
        ),
        content_digest=content_digest,
        media_type="IMAGE",
        mime_type="image/png",
        byte_size=1,
        width_px=1,
        height_px=1,
        aspect_ratio="1:1",
    )
    rendition = CreativeRendition.from_asset(asset, provider_format=FORMAT, locale="en-US")
    version = CreativeVersion(
        creative_family_id="family:chat-card",
        manifest=ServingCreativeManifest(
            provider_format=FORMAT,
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
    return OpenAIAdsCreativeAssociation(
        creative_version=version, asset=asset, rendition=rendition
    ), content_digest


def _brief(
    *,
    account_ref: str = ACCOUNT,
    file_account_ref: str | None = None,
    file_id: str = "file_opaque_1",
) -> OpenAIAdsCampaignBrief:
    destination_url = "https://example.test/demo?utm_source=ads"
    association, asset_content_digest = _association(destination_url=destination_url)
    return OpenAIAdsCampaignBrief(
        account_ref=account_ref,
        account_currency="USD",
        currency_minor_unit_exponent=2,
        account_timezone="America/Chicago",
        campaign_handle="review",
        name="Review campaign",
        start_time=1_788_264_800,
        end_time=1_790_856_000,
        lifetime_budget=Money(minor_units=2_500, currency="USD"),
        bidding_type="clicks",
        location_ids=("2000043",),
        destination_policy=OpenAIAdsDestinationPolicy(
            allowed_hosts=("example.test",), allowed_paths=("/demo",)
        ),
        ad_groups=(
            OpenAIAdsAdGroupBrief(
                ad_group_handle="us",
                name="US group",
                max_bid_micros=60_000,
                ads=(
                    OpenAIAdsAdBrief(
                        ad_handle="card",
                        name="Review card",
                        creative=OpenAIAdsChatCardCreative(
                            title=TITLE,
                            body=BODY,
                            destination_url=destination_url,
                            association=association,
                            file_reference=OpenAIAdsFileReferenceEvidence(
                                account_ref=file_account_ref or account_ref,
                                file_id=file_id,
                                asset_content_digest=asset_content_digest,
                                evidence_ref=EVIDENCE,
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )


def test_campaign_targeting_emits_the_documented_locations_include_object() -> None:
    body = compile_openai_ads_campaign(_brief()).campaign_request.model_dump(mode="json")

    assert body["targeting"] == {"locations": {"include": [{"id": "2000043"}]}}


@pytest.mark.parametrize("exponent", [0, 1, 3, 6])
def test_revalidation_rejects_usd_minor_unit_under_or_overstatement(exponent: int) -> None:
    bypassed = _brief().model_copy(update={"currency_minor_unit_exponent": exponent})

    with pytest.raises(ValidationError, match="minor-unit exponent"):
        compile_openai_ads_campaign(bypassed)


def test_revalidation_rejects_an_unreviewed_currency_and_non_integer_minor_units() -> None:
    unreviewed_currency = _brief().model_copy(
        update={"account_currency": "EUR", "currency_minor_unit_exponent": 2}
    )
    non_integer_minor_units = _brief().model_copy(
        update={"lifetime_budget": Money.model_construct(minor_units=2_500.0, currency="USD")}
    )

    with pytest.raises(ValidationError, match="no reviewed minor-unit exponent"):
        compile_openai_ads_campaign(unreviewed_currency)
    with pytest.raises(ValidationError, match="valid integer"):
        compile_openai_ads_campaign(non_integer_minor_units)


def test_largest_lossless_native_micros_cap_keeps_an_exact_minor_unit_value() -> None:
    maximum_lossless_micros = 9_007_199_254_740_000
    spec = compile_openai_ads_campaign(
        _brief().model_copy(
            update={
                "lifetime_budget": Money(
                    minor_units=maximum_lossless_micros // 10_000, currency="USD"
                )
            }
        )
    )

    assert spec.activation_boundary.maximum_spend_micros == maximum_lossless_micros
    assert spec.activation_boundary.maximum_spend.minor_units == 900_719_925_474


def test_revalidation_rejects_a_schedule_forged_with_model_copy() -> None:
    original = _brief()
    bypassed = original.model_copy(update={"end_time": original.start_time})

    with pytest.raises(ValidationError, match="end_time must follow"):
        compile_openai_ads_campaign(bypassed)


def test_file_reference_needs_evidence_and_cannot_self_attest() -> None:
    association, asset_content_digest = _association()
    with pytest.raises(ValidationError, match="evidence_ref"):
        OpenAIAdsFileReferenceEvidence.model_validate(
            {
                "account_ref": ACCOUNT,
                "file_id": "file_opaque_1",
                "asset_content_digest": asset_content_digest,
                "verification_state": "VERIFIED",
                "verification_digest": "a" * 64,
            }
        )

    with pytest.raises(ValidationError, match="file-reference evidence"):
        OpenAIAdsChatCardCreative(
            title=TITLE,
            body=BODY,
            destination_url="https://example.test/demo?utm_source=ads",
            association=association,
            file_reference=OpenAIAdsFileReferenceEvidence(
                account_ref=ACCOUNT,
                file_id="file_opaque_1",
                asset_content_digest="a" * 64,
                evidence_ref=EVIDENCE,
            ),
        )


def test_file_reference_must_bind_the_same_account_and_an_opaque_id_is_preserved() -> None:
    with pytest.raises(ValidationError, match="bound to the compiled account"):
        _brief(file_account_ref="openai.account.other")

    opaque_id = "provider-file:v2.alpha/opaque"
    spec = compile_openai_ads_campaign(_brief(file_id=opaque_id))
    assert spec.ad_groups[0].ads[0].request.creative.file_id == opaque_id
    assert opaque_id not in spec.redacted_review.model_dump_json()


def test_creative_version_asset_and_rendition_are_bound_to_the_candidate() -> None:
    spec = compile_openai_ads_campaign(_brief())
    ad = spec.ad_groups[0].ads[0]

    assert ad.creative_version_id.startswith("creative:sha256:")
    assert ad.asset_version_id.startswith("asset:sha256:")
    assert ad.rendition_id.startswith("rendition:sha256:")
    assert ad.file_reference_evidence_ref == EVIDENCE
    assert spec.planning_boundary.status == "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    assert spec.planning_boundary.requires_creative_ledger_check
    assert spec.planning_boundary.requires_account_bound_file_readback


def test_creative_content_must_match_the_declared_canonical_version() -> None:
    original = _brief()
    bad_creative = original.ad_groups[0].ads[0].creative.model_copy(update={"title": "Other title"})
    bypassed = original.model_copy(
        update={
            "ad_groups": (
                original.ad_groups[0].model_copy(
                    update={
                        "ads": (
                            original.ad_groups[0]
                            .ads[0]
                            .model_copy(update={"creative": bad_creative}),
                        )
                    }
                ),
            )
        }
    )

    with pytest.raises(ValidationError, match="chat-card title and body"):
        compile_openai_ads_campaign(bypassed)


def test_destination_cannot_change_without_a_new_canonical_creative_version() -> None:
    original = _brief()
    changed_url = "https://example.test/demo?utm_source=other"
    bad_creative = (
        original.ad_groups[0].ads[0].creative.model_copy(update={"destination_url": changed_url})
    )
    bypassed = original.model_copy(
        update={
            "ad_groups": (
                original.ad_groups[0].model_copy(
                    update={
                        "ads": (
                            original.ad_groups[0]
                            .ads[0]
                            .model_copy(update={"creative": bad_creative}),
                        )
                    }
                ),
            )
        }
    )

    with pytest.raises(ValidationError, match="destination does not match"):
        compile_openai_ads_campaign(bypassed)
