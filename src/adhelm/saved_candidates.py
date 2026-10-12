"""Retained compilation reviews selected only through verified canonical history."""

import re
from typing import Any, overload

from adhelm.candidate_records import (
    CandidateCompilation,
    CandidateCompilationRecord,
    CandidateCreativeReference,
    CompiledCandidate,
    GoogleCandidateCompilation,
    LinkedInCandidateCompilation,
    MetaCandidateCompilation,
    RedditCandidateCompilation,
    TikTokCandidateCompilation,
)
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.providers.google_ads_compiler import (
    GoogleAdsSearchCampaignBrief,
    compile_google_ads_search_campaign,
)
from adhelm.providers.linkedin_compiler import (
    LinkedInAdsCampaignBrief,
    LinkedInAdsCompiledCampaignSpec,
    compile_linkedin_ads_campaign,
)
from adhelm.providers.meta_compiler import MetaAdsCampaignBrief, compile_meta_ads_campaign
from adhelm.providers.openai_compiler import OpenAIAdsCampaignBrief, compile_openai_ads_campaign
from adhelm.providers.reddit_compiler import (
    RedditAdsCampaignBrief,
    RedditAdsCompiledCampaignSpec,
    compile_reddit_ads_campaign,
)
from adhelm.providers.tiktok_compiler import (
    TikTokAdsCampaignBrief,
    TikTokAdsCompiledCampaignSpec,
    compile_tiktok_ads_campaign,
)

CampaignBrief = (
    OpenAIAdsCampaignBrief
    | MetaAdsCampaignBrief
    | GoogleAdsSearchCampaignBrief
    | LinkedInAdsCampaignBrief
    | RedditAdsCampaignBrief
    | TikTokAdsCampaignBrief
)


@overload
def compile_candidate(brief: OpenAIAdsCampaignBrief) -> CandidateCompilation: ...


@overload
def compile_candidate(brief: MetaAdsCampaignBrief) -> MetaCandidateCompilation: ...


@overload
def compile_candidate(brief: GoogleAdsSearchCampaignBrief) -> GoogleCandidateCompilation: ...


@overload
def compile_candidate(brief: LinkedInAdsCampaignBrief) -> LinkedInCandidateCompilation: ...


@overload
def compile_candidate(brief: RedditAdsCampaignBrief) -> RedditCandidateCompilation: ...


@overload
def compile_candidate(brief: TikTokAdsCampaignBrief) -> TikTokCandidateCompilation: ...


def compile_candidate(brief: CampaignBrief) -> CompiledCandidate:
    """Recompile untrusted input; never accept an asserted native spec/digest."""
    if isinstance(brief, LinkedInAdsCampaignBrief):
        linkedin = compile_linkedin_ads_campaign(brief)
        return LinkedInCandidateCompilation(
            **_additional_review_fields(linkedin),
            native_group_total_budget_amount=linkedin.campaign_group_request.totalBudget.amount,
        )
    if isinstance(brief, RedditAdsCampaignBrief):
        reddit = compile_reddit_ads_campaign(brief)
        return RedditCandidateCompilation(
            **_additional_review_fields(reddit),
            native_lifetime_spend_micros=reddit.campaign_request.goal_value,
        )
    if isinstance(brief, TikTokAdsCampaignBrief):
        tiktok = compile_tiktok_ads_campaign(brief)
        return TikTokCandidateCompilation(
            **_additional_review_fields(tiktok),
            native_adgroup_total_budget_amount=format(tiktok.adgroup_request.budget, ".2f"),
        )
    if isinstance(brief, MetaAdsCampaignBrief):
        meta = compile_meta_ads_campaign(brief)
        return MetaCandidateCompilation(
            account_ref=meta.account_ref,
            campaign_ref=meta.campaign_handle,
            input_digest=meta.input_digest,
            spec_digest=meta.spec_digest,
            creative_references=(
                CandidateCreativeReference(
                    creative_version_id=meta.creative_version_id,
                    creative_content_digest=meta.creative_content_digest,
                ),
            ),
            maximum_spend=meta.maximum_spend,
            review=meta.redacted_review,
            planning_boundary=meta.planning_boundary,
            native_lifetime_budget_minor_units=meta.campaign_request.lifetime_budget,
        )
    if isinstance(brief, GoogleAdsSearchCampaignBrief):
        google = compile_google_ads_search_campaign(brief)
        return GoogleCandidateCompilation(
            account_ref=google.account_ref,
            campaign_ref=google.campaign_handle,
            input_digest=google.input_digest,
            spec_digest=google.spec_digest,
            creative_references=(
                CandidateCreativeReference(
                    creative_version_id=google.responsive_search_ad.creative_version_id,
                    creative_content_digest=google.responsive_search_ad.creative_content_digest,
                ),
            ),
            maximum_spend=google.maximum_spend,
            review=google.redacted_review,
            planning_boundary=google.planning_boundary,
            native_total_budget_micros=google.budget.request.total_amount_micros,
        )
    if not isinstance(brief, OpenAIAdsCampaignBrief):
        raise AdhelmError(
            "UNSUPPORTED_CANDIDATE", "No installed compiler accepts this brief.", exit_code=2
        )
    spec = compile_openai_ads_campaign(brief)
    review = spec.redacted_review
    return CandidateCompilation(
        provider=spec.provider,
        provider_api_version=spec.provider_api_version,
        capability_version=spec.capability_version,
        account_ref=spec.account_ref,
        campaign_ref=spec.campaign_handle,
        input_digest=spec.input_digest,
        spec_digest=spec.spec_digest,
        location_count=review.location_count,
        ad_group_count=review.ad_group_count,
        ad_count=review.ad_count,
        creative_references=tuple(
            CandidateCreativeReference(
                creative_version_id=ad.creative_version_id,
                creative_content_digest=ad.creative_content_digest,
            )
            for group in spec.ad_groups
            for ad in group.ads
        ),
        maximum_spend_micros=review.maximum_spend_micros,
        maximum_spend=review.maximum_spend,
    )


def _additional_review_fields(
    spec: LinkedInAdsCompiledCampaignSpec
    | RedditAdsCompiledCampaignSpec
    | TikTokAdsCompiledCampaignSpec,
) -> dict[str, Any]:
    """Select only the closed review; native requests and source briefs stay out of history."""
    return {
        "account_ref": spec.account_ref,
        "campaign_ref": spec.campaign_handle,
        "input_digest": spec.input_digest,
        "spec_digest": spec.spec_digest,
        "creative_references": (
            CandidateCreativeReference(
                creative_version_id=spec.creative_version_id,
                creative_content_digest=spec.creative_content_digest,
            ),
        ),
        "maximum_spend": spec.maximum_spend,
        "review": spec.redacted_review,
        "planning_boundary": spec.planning_boundary,
    }


def retain_candidate(ledger: Ledger, brief: CampaignBrief) -> dict[str, Any]:
    compilation = compile_candidate(brief)
    record = CandidateCompilationRecord(
        compilation=compilation, candidate_digest=compilation.candidate_digest
    )
    return ledger.append(
        "changes",
        "CANDIDATE_COMPILED",
        record.model_dump(mode="json"),
        provider=compilation.provider,
        account_ref=compilation.account_ref,
    )


def load_candidate(ledger: Ledger, record_digest: str) -> tuple[CompiledCandidate, dict[str, Any]]:
    if not isinstance(record_digest, str) or re.fullmatch(r"[0-9a-f]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_CANDIDATE_SELECTION", "A retained record digest is required.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in checkpoint["records_by_stream"]["changes"]
        if record["record_kind"] == "CANDIDATE_COMPILED"
        and record["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError(
            "CANDIDATE_NOT_RETAINED", "Selected candidate is absent from verified local history."
        )
    record = records[0]
    compilation = CandidateCompilationRecord.model_validate_json(
        canonical_bytes(record["payload"])
    ).compilation
    return compilation, {
        "record_digest": record_digest,
        "candidate_digest": compilation.candidate_digest,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": record["recorded_at"],
    }


def candidate_catalog(ledger: Ledger, *, cursor: int = 0, limit: int = 50) -> dict[str, Any]:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError(
            "INVALID_CANDIDATE_SELECTION", "Invalid candidate pagination.", exit_code=2
        )
    checkpoint = ledger.checkpoint()
    records = [
        record
        for record in reversed(checkpoint["records_by_stream"]["changes"])
        if record["record_kind"] == "CANDIDATE_COMPILED"
    ]
    if cursor > len(records):
        raise AdhelmError(
            "INVALID_CANDIDATE_SELECTION", "Cursor exceeds retained candidate count.", exit_code=2
        )
    return {
        "candidates": [
            {
                "record_digest": record["record_digest"],
                "candidate_digest": record["payload"]["candidate_digest"],
                "spec_digest": record["payload"]["compilation"]["spec_digest"],
                "provider": record["provider"],
                "recorded_at": record["recorded_at"],
                "execution_status": record["payload"]["compilation"]["execution_status"],
                "maximum_spend": record["payload"]["compilation"]["maximum_spend"],
            }
            for record in records[cursor : cursor + limit]
        ],
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
    }
