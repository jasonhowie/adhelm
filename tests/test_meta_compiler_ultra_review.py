"""Adversarial checks for the offline Meta single-image campaign compiler."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Literal

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
from adhelm.providers.meta_compiler import (
    MetaAdsCampaignBrief,
    MetaAdsCreativeAssociation,
    MetaAdsDestinationPolicy,
    MetaAdsImageReferenceEvidence,
    MetaAdsNativeCampaignPayload,
    MetaAdsNativeCreativePayload,
    MetaAdsNativeGeoLocations,
    MetaAdsNativeTargeting,
    MetaAdsSingleImageLinkCreative,
    compile_meta_ads_campaign,
)

_DESTINATION = "https://example.test/demo?utm_source=meta"
_DESTINATION_REF = "destination:sha256:" + digest({"target_url": _DESTINATION})


def _component(
    component_id: str,
    role: Literal["PRIMARY_TEXT", "HEADLINE", "CTA_BUTTON"],
    ordinal: int,
    text: str,
    slot: str,
    normalized_cta_intent: Literal[
        "SIGN_UP",
        "BOOK_DEMO",
        "CONTACT_SALES",
        "LEARN_MORE",
        "DOWNLOAD",
        "INSTALL",
        "BUY",
        "OTHER",
    ]
    | None = None,
    cta_destination_ref: str | None = None,
) -> CreativeComponent:
    return CreativeComponent(
        component_id=component_id,
        component_role=role,
        ordinal=ordinal,
        exact_text=text,
        normalized_text=text.casefold(),
        language="en",
        provider_slot=slot,
        normalized_cta_intent=normalized_cta_intent,
        cta_destination_ref=cta_destination_ref,
        provenance="DECLARED",
        confidence_basis_points=10_000,
        extraction_status="DECLARED",
    )


def _brief() -> MetaAdsCampaignBrief:
    content_digest = hashlib.sha256(b"fixture-image").hexdigest()
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
        byte_size=13,
        width_px=1200,
        height_px=628,
        aspect_ratio="300:157",
    )
    rendition = CreativeRendition.from_asset(
        asset,
        provider_format="META_ADS_SINGLE_IMAGE_LINK",
        locale="en-US",
    )
    manifest = ServingCreativeManifest(
        provider_format="META_ADS_SINGLE_IMAGE_LINK",
        locale="en-US",
        destination_ref=_DESTINATION_REF,
        rendition_refs=(
            ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
        ),
        components=(
            _component("body", "PRIMARY_TEXT", 0, "Fixture primary text", "message"),
            _component("headline", "HEADLINE", 1, "Fixture headline", "name"),
            _component(
                "cta",
                "CTA_BUTTON",
                2,
                "Learn More",
                "call_to_action.type",
                normalized_cta_intent="LEARN_MORE",
                cta_destination_ref=_DESTINATION_REF,
            ),
        ),
    )
    version = CreativeVersion(
        creative_family_id="family:meta-fixture",
        manifest=manifest,
        provenance="CUSTOMER_LOCAL",
        created_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    return MetaAdsCampaignBrief(
        account_ref="meta_ads.account.act_123",
        account_timezone="America/Chicago",
        campaign_handle="launch",
        adset_handle="us-feed",
        ad_handle="image-link",
        campaign_name="Fixture campaign",
        adset_name="Fixture ad set",
        ad_name="Fixture ad",
        lifetime_budget=Money(minor_units=25_000, currency="USD"),
        start_time=datetime(2026, 10, 1, tzinfo=UTC),
        stop_time=datetime(2026, 10, 31, tzinfo=UTC),
        destination_policy=MetaAdsDestinationPolicy(
            allowed_hosts=("example.test",),
            allowed_paths=("/demo",),
        ),
        creative=MetaAdsSingleImageLinkCreative(
            page_id="12345",
            primary_text="Fixture primary text",
            headline="Fixture headline",
            destination_url=_DESTINATION,
            association=MetaAdsCreativeAssociation(
                creative_version=version,
                asset=asset,
                rendition=rendition,
            ),
            image_reference=MetaAdsImageReferenceEvidence(
                account_ref="meta_ads.account.act_123",
                image_hash="opaque-image-hash",
                asset_content_digest=content_digest,
                evidence_ref="evidence:sha256:" + "a" * 64,
            ),
        ),
    )


def _replace_manifest(
    brief: MetaAdsCampaignBrief, manifest: ServingCreativeManifest
) -> MetaAdsCampaignBrief:
    association = brief.creative.association.model_copy(
        update={
            "creative_version": brief.creative.association.creative_version.model_copy(
                update={"manifest": manifest}
            )
        }
    )
    return brief.model_copy(
        update={"creative": brief.creative.model_copy(update={"association": association})}
    )


@pytest.mark.parametrize(
    ("case", "diagnostic"),
    [
        ("wrong_dimensions", "identity rendition"),
        ("forged_rendition_id", "Rendition identifier"),
        ("non_hero_role", "identity rendition"),
    ],
)
def test_identity_rendition_cannot_be_resized_reidentified_or_repurposed(
    case: str, diagnostic: str
) -> None:
    brief = _brief()
    association = brief.creative.association
    if case == "wrong_dimensions":
        rendition = association.rendition.model_copy(update={"width_px": 600})
        manifest = association.creative_version.manifest
    elif case == "forged_rendition_id":
        rendition = association.rendition.model_copy(
            update={"rendition_id": "rendition:sha256:" + "b" * 64}
        )
        manifest = association.creative_version.manifest.model_copy(
            update={
                "rendition_refs": (
                    ServingRendition(rendition_id=rendition.rendition_id, ordinal=0, role="HERO"),
                )
            }
        )
    else:
        rendition = association.rendition
        manifest = association.creative_version.manifest.model_copy(
            update={
                "rendition_refs": (
                    ServingRendition(
                        rendition_id=rendition.rendition_id,
                        ordinal=0,
                        role="THUMBNAIL",
                    ),
                )
            }
        )
    changed_association = association.model_copy(
        update={
            "rendition": rendition,
            "creative_version": association.creative_version.model_copy(
                update={"manifest": manifest}
            ),
        }
    )
    forged = brief.model_copy(
        update={"creative": brief.creative.model_copy(update={"association": changed_association})}
    )

    with pytest.raises(ValidationError, match=diagnostic):
        compile_meta_ads_campaign(forged)


@pytest.mark.parametrize("case", ["missing_cta", "extra_primary_text", "wrong_cta_slot"])
def test_manifest_must_exactly_match_the_fixed_copy_and_cta_surface(case: str) -> None:
    brief = _brief()
    manifest = brief.creative.association.creative_version.manifest
    if case == "missing_cta":
        changed = manifest.model_copy(update={"components": manifest.components[:2]})
    elif case == "extra_primary_text":
        changed = manifest.model_copy(
            update={
                "components": (
                    *manifest.components,
                    _component("extra", "PRIMARY_TEXT", 3, "Unsent alternate body", "message"),
                )
            }
        )
    else:
        changed = manifest.model_copy(
            update={
                "components": (
                    *manifest.components[:2],
                    manifest.components[2].model_copy(update={"provider_slot": "cta_type"}),
                )
            }
        )

    with pytest.raises(ValidationError, match="exactly match"):
        compile_meta_ads_campaign(_replace_manifest(brief, changed))


def test_role_handles_must_be_globally_unambiguous_and_timezone_must_be_iana() -> None:
    with pytest.raises(ValidationError, match="unique"):
        compile_meta_ads_campaign(
            _brief().model_copy(update={"campaign_handle": "creative_image-link"})
        )
    with pytest.raises(ValidationError, match="IANA"):
        compile_meta_ads_campaign(_brief().model_copy(update={"account_timezone": "not/a-zone"}))
    with pytest.raises(ValidationError, match="IANA"):
        compile_meta_ads_campaign(_brief().model_copy(update={"account_timezone": "/etc/passwd"}))


@pytest.mark.parametrize("value", ["bad\x7fhash", "badéhash"])
def test_image_reference_rejects_non_printable_or_non_ascii_provider_values(value: str) -> None:
    with pytest.raises(ValidationError, match="printable opaque"):
        MetaAdsImageReferenceEvidence(
            account_ref="meta_ads.account.act_123",
            image_hash=value,
            asset_content_digest="a" * 64,
            evidence_ref="evidence:sha256:" + "b" * 64,
        )


def test_native_requests_are_closed_paused_and_keep_schedule_and_budget_on_campaign() -> None:
    compiled = compile_meta_ads_campaign(_brief())
    assert set(compiled.campaign_request.model_dump()) == {
        "name",
        "objective",
        "buying_type",
        "special_ad_categories",
        "lifetime_budget",
        "start_time",
        "stop_time",
        "status",
    }
    assert set(compiled.adset_request.model_dump()) == {
        "name",
        "status",
        "billing_event",
        "optimization_goal",
        "bid_strategy",
        "targeting",
    }
    assert set(compiled.creative_request.model_dump()) == {"name", "object_story_spec"}
    assert compiled.campaign_request.status == compiled.adset_request.status == "PAUSED"
    assert compiled.ad_request.status == "PAUSED"
    assert compiled.campaign_request.lifetime_budget == compiled.maximum_spend.minor_units

    with pytest.raises(ValidationError, match="Extra inputs"):
        MetaAdsNativeCampaignPayload.model_validate(
            {**compiled.campaign_request.model_dump(mode="python"), "daily_budget": 1}
        )
    with pytest.raises(ValidationError, match="Extra inputs"):
        MetaAdsNativeCreativePayload.model_validate(
            {
                **compiled.creative_request.model_dump(mode="python"),
                "degrees_of_freedom_spec": {"enabled": True},
            }
        )
    for geo_locations in (
        MetaAdsNativeGeoLocations.model_construct(countries=()),
        MetaAdsNativeGeoLocations.model_construct(countries=("US", "US")),
    ):
        with pytest.raises(ValidationError, match="at most 1|at least 1"):
            MetaAdsNativeGeoLocations.model_validate(geo_locations.model_dump(mode="python"))
    for native_targeting in (
        MetaAdsNativeTargeting.model_construct(
            geo_locations=MetaAdsNativeGeoLocations(), publisher_platforms=()
        ),
        MetaAdsNativeTargeting.model_construct(
            geo_locations=MetaAdsNativeGeoLocations(), facebook_positions=("feed", "feed")
        ),
    ):
        with pytest.raises(ValidationError, match="at most 1|at least 1"):
            MetaAdsNativeTargeting.model_validate(native_targeting.model_dump(mode="python"))


def test_compiled_spec_revalidates_local_relationships_budget_and_unverified_statuses() -> None:
    compiled = compile_meta_ads_campaign(_brief())
    assert compiled.planning_boundary.requires_placement_eligibility_readback is True
    assert compiled.planning_boundary.requires_creative_transformation_readback is True
    assert compiled.redacted_review.account_identity_status == "CUSTOMER_DECLARED_REQUIRES_READBACK"
    assert compiled.redacted_review.placement_eligibility_status == "REQUIRES_API_READBACK"
    changed_link_data = compiled.creative_request.object_story_spec.link_data.model_copy(
        update={"link": "https://example.test/other?utm_source=meta"}
    )
    changed_story_spec = compiled.creative_request.object_story_spec.model_copy(
        update={"link_data": changed_link_data}
    )
    changed_creative = compiled.creative_request.model_copy(
        update={"object_story_spec": changed_story_spec}
    )
    zero_budget = compiled.model_copy(
        update={
            "campaign_request": compiled.campaign_request.model_copy(update={"lifetime_budget": 0}),
            "maximum_spend": Money(minor_units=0, currency="USD"),
        }
    )
    blank_names = compiled.model_copy(
        update={
            "campaign_request": compiled.campaign_request.model_copy(update={"name": ""}),
            "adset_request": compiled.adset_request.model_copy(update={"name": ""}),
            "creative_request": compiled.creative_request.model_copy(update={"name": ""}),
            "ad_request": compiled.ad_request.model_copy(update={"name": ""}),
        }
    )
    naive_schedule = compiled.model_copy(
        update={
            "campaign_request": compiled.campaign_request.model_copy(
                update={"start_time": compiled.campaign_request.start_time.replace(tzinfo=None)}
            )
        }
    )

    for forged in (
        compiled.model_copy(update={"adset_parent_campaign_handle": "other"}),
        compiled.model_copy(update={"maximum_spend": Money(minor_units=1, currency="USD")}),
        compiled.model_copy(update={"creative_request": changed_creative}),
        zero_budget,
        blank_names,
        naive_schedule,
        compiled.model_copy(update={"account_timezone": "not/a-zone"}),
    ):
        with pytest.raises(ValidationError):
            type(compiled).model_validate(forged.model_dump(mode="python"))
