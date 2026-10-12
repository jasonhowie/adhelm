"""Closed local compilation evidence, without native payload or execution authority."""

import json
import re
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, TypeAdapter, field_validator, model_validator

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.models import Money, SafeRef, StrictModel
from adhelm.providers.google_ads_compiler import GoogleAdsPlanningBoundary, GoogleAdsRedactedReview
from adhelm.providers.linkedin_compiler import (
    LinkedInAdsPlanningBoundary,
    LinkedInAdsRedactedReview,
)
from adhelm.providers.meta_compiler import MetaAdsPlanningBoundary, MetaAdsRedactedReview
from adhelm.providers.reddit_compiler import RedditAdsPlanningBoundary, RedditAdsRedactedReview
from adhelm.providers.tiktok_compiler import TikTokAdsPlanningBoundary, TikTokAdsRedactedReview

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CandidateCreativeReference(StrictModel):
    creative_version_id: str = Field(pattern=r"^creative:sha256:[0-9a-f]{64}$")
    creative_content_digest: Digest
    reference_status: Literal["DECLARED_NOT_VERIFIED_IN_LEDGER"] = "DECLARED_NOT_VERIFIED_IN_LEDGER"


class CandidateCompilation(StrictModel):
    """A reviewable observation of compilation, never an executable plan.

    The native specification and original brief are deliberately absent. Their
    digests identify the compiler inputs/output; this projection cannot recreate
    either. A new compilation and all unresolved checks are required for planning.
    """

    schema_version: Literal[1] = 1
    provider: Literal["openai_ads"] = "openai_ads"
    provider_api_version: Literal["v1"] = "v1"
    capability_version: Literal["openai-ads-write-contract-2026-09-04"] = (
        "openai-ads-write-contract-2026-09-04"
    )
    account_ref: SafeRef
    campaign_ref: SafeRef
    input_digest: Digest
    spec_digest: Digest
    initial_state: Literal["PAUSED"] = "PAUSED"
    location_count: int = Field(ge=1)
    ad_group_count: int = Field(ge=1)
    ad_count: int = Field(ge=1)
    creative_references: tuple[CandidateCreativeReference, ...] = Field(min_length=1)
    maximum_spend_micros: int = Field(ge=1_000_000, le=9_007_199_254_740_991)
    maximum_spend: Money
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = (
        "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    )
    requires_verified_account_identity: Literal[True] = True
    requires_creative_ledger_check: Literal[True] = True
    requires_account_bound_file_readback: Literal[True] = True
    requires_new_compilation_for_planning: Literal[True] = True
    activation_requires_new_digest_and_authorization: Literal[True] = True
    remote_side_effects: Literal[False] = False

    @model_validator(mode="after")
    def internally_consistent_review(self) -> "CandidateCompilation":
        if self.ad_count != len(self.creative_references) or self.ad_group_count > self.ad_count:
            raise ValueError("Candidate counts must describe every compiled ad")
        if (
            self.maximum_spend.currency != "USD"
            or self.maximum_spend_micros != self.maximum_spend.minor_units * 10_000
        ):
            raise ValueError("Candidate spend must preserve exact reviewed USD micros")
        return self

    @property
    def candidate_digest(self) -> str:
        return digest(self)


class _SingleCreativeCompilation(StrictModel):
    """Normalized review of one compilation; no replayable native request."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    campaign_ref: SafeRef
    input_digest: Digest
    spec_digest: Digest
    creative_references: tuple[CandidateCreativeReference, ...] = Field(min_length=1, max_length=1)
    maximum_spend: Money
    execution_status: Literal["OFFLINE_CANDIDATE_NOT_EXECUTABLE"] = (
        "OFFLINE_CANDIDATE_NOT_EXECUTABLE"
    )
    compiler_evidence: Literal["FIXTURE_VERIFIED_LIVE_UNVERIFIED"] = (
        "FIXTURE_VERIFIED_LIVE_UNVERIFIED"
    )
    requires_new_compilation_for_planning: Literal[True] = True
    activation_requires_new_digest_and_authorization: Literal[True] = True
    remote_side_effects: Literal[False] = False

    @property
    def candidate_digest(self) -> str:
        return digest(self)

    def _validate_review(
        self,
        review: (
            MetaAdsRedactedReview
            | GoogleAdsRedactedReview
            | LinkedInAdsRedactedReview
            | RedditAdsRedactedReview
            | TikTokAdsRedactedReview
        ),
        spend: Money,
    ) -> None:
        reference = self.creative_references[0]
        try:
            ZoneInfo(review.account_timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("Candidate timezone must preserve its IANA declaration") from exc
        if (
            self.account_ref != review.account_ref
            or self.campaign_ref != review.campaign_handle
            or self.spec_digest != review.spec_digest
            or self.maximum_spend != spend
            or self.maximum_spend.currency != "USD"
            or self.maximum_spend.minor_units <= 0
            or reference.creative_version_id != review.creative_version_id
            or reference.creative_content_digest != review.creative_content_digest
        ):
            raise ValueError("Candidate identity, spend and creative review must agree")


class MetaCandidateCompilation(_SingleCreativeCompilation):
    provider: Literal["meta_ads"] = "meta_ads"
    provider_api_version: Literal["v26.0"] = "v26.0"
    capability_version: Literal["meta-ads-sdk-26.0.0-write-contract-2026-09-04"] = (
        "meta-ads-sdk-26.0.0-write-contract-2026-09-04"
    )
    review: MetaAdsRedactedReview
    planning_boundary: MetaAdsPlanningBoundary
    native_lifetime_budget_minor_units: int = Field(ge=1, le=9_007_199_254_740_991)

    @model_validator(mode="after")
    def consistent_meta_review(self) -> "MetaCandidateCompilation":
        self._validate_review(self.review, self.review.maximum_spend)
        handles = (self.review.campaign_handle, self.review.adset_handle, self.review.ad_handle)
        if (
            any(re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value) is None for value in handles)
            or self.review.creative_handle != "creative_" + self.review.ad_handle
            or len({*handles, self.review.creative_handle}) != 4
        ):
            raise ValueError("Meta candidate role references must preserve distinct valid handles")
        if self.native_lifetime_budget_minor_units != self.maximum_spend.minor_units:
            raise ValueError("Meta lifetime budget must preserve exact USD minor units")
        return self


class GoogleCandidateCompilation(_SingleCreativeCompilation):
    provider: Literal["google_ads"] = "google_ads"
    provider_api_version: Literal["v25"] = "v25"
    capability_version: Literal["google-ads-v25-search-rsa-contract-2026-09-04"] = (
        "google-ads-v25-search-rsa-contract-2026-09-04"
    )
    review: GoogleAdsRedactedReview
    planning_boundary: GoogleAdsPlanningBoundary
    native_total_budget_micros: int = Field(ge=10_000, le=9_007_199_254_740_991)

    @model_validator(mode="after")
    def consistent_google_review(self) -> "GoogleCandidateCompilation":
        self._validate_review(self.review, self.review.campaign_total_budget)
        handles = (
            self.review.budget_handle,
            self.review.campaign_handle,
            self.review.ad_group_handle,
            self.review.ad_handle,
        )
        if (
            any(re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", value) is None for value in handles)
            or len(set(handles)) != 4
        ):
            raise ValueError(
                "Google candidate role references must preserve distinct valid handles"
            )
        if self.native_total_budget_micros != self.maximum_spend.minor_units * 10_000:
            raise ValueError("Google total budget must preserve exact USD micros")
        return self


def _validate_handles(handles: tuple[str, ...]) -> None:
    if len(set(handles)) != len(handles) or any(
        re.fullmatch(r"[a-z][a-z0-9_-]{0,79}", handle) is None for handle in handles
    ):
        raise ValueError("Candidate hierarchy handles must be distinct valid local references")


def _exact_usd_amount(spend: Money) -> str:
    if not 1 <= spend.minor_units <= 9_007_199_254_740_991 or spend.currency != "USD":
        raise ValueError("Candidate native budget must preserve bounded USD minor units")
    return f"{spend.minor_units // 100}.{spend.minor_units % 100:02d}"


class LinkedInCandidateCompilation(_SingleCreativeCompilation):
    provider: Literal["linkedin_ads"] = "linkedin_ads"
    provider_api_version: Literal["202608"] = "202608"
    capability_version: Literal["linkedin-marketing-202608-existing-dark-image-post-2026-09-04"] = (
        "linkedin-marketing-202608-existing-dark-image-post-2026-09-04"
    )
    review: LinkedInAdsRedactedReview
    planning_boundary: LinkedInAdsPlanningBoundary
    native_group_total_budget_amount: str = Field(pattern=r"^(0|[1-9][0-9]{0,13})\.[0-9]{2}$")

    @model_validator(mode="after")
    def consistent_linkedin_review(self) -> "LinkedInCandidateCompilation":
        self._validate_review(self.review, self.review.maximum_spend)
        _validate_handles(
            (
                self.review.campaign_group_handle,
                self.review.campaign_handle,
                self.review.creative_handle,
            )
        )
        if self.native_group_total_budget_amount != _exact_usd_amount(self.maximum_spend):
            raise ValueError("LinkedIn group total budget must preserve the exact reviewed USD cap")
        return self


class RedditCandidateCompilation(_SingleCreativeCompilation):
    provider: Literal["reddit_ads"] = "reddit_ads"
    provider_api_version: Literal["v3"] = "v3"
    capability_version: Literal["reddit-ads-api-v3-cbo-traffic-post-contract-2026-09-04"] = (
        "reddit-ads-api-v3-cbo-traffic-post-contract-2026-09-04"
    )
    review: RedditAdsRedactedReview
    planning_boundary: RedditAdsPlanningBoundary
    native_lifetime_spend_micros: int = Field(ge=10_000, le=9_007_199_254_740_991)

    @model_validator(mode="after")
    def consistent_reddit_review(self) -> "RedditCandidateCompilation":
        self._validate_review(self.review, self.review.maximum_spend)
        _validate_handles(
            (self.review.campaign_handle, self.review.ad_group_handle, self.review.ad_handle)
        )
        if (
            self.native_lifetime_spend_micros != self.maximum_spend.minor_units * 10_000
            or self.review.maximum_spend_micros != self.native_lifetime_spend_micros
        ):
            raise ValueError(
                "Reddit native lifetime budget must preserve the exact reviewed USD micros"
            )
        return self


class TikTokCandidateCompilation(_SingleCreativeCompilation):
    provider: Literal["tiktok_ads"] = "tiktok_ads"
    provider_api_version: Literal["v1.3"] = "v1.3"
    capability_version: Literal["tiktok-marketing-api-v1.3-traffic-video-contract-2026-09-04"] = (
        "tiktok-marketing-api-v1.3-traffic-video-contract-2026-09-04"
    )
    review: TikTokAdsRedactedReview
    planning_boundary: TikTokAdsPlanningBoundary
    native_adgroup_total_budget_amount: str = Field(pattern=r"^(0|[1-9][0-9]{0,13})\.[0-9]{2}$")

    @model_validator(mode="after")
    def consistent_tiktok_review(self) -> "TikTokCandidateCompilation":
        self._validate_review(self.review, self.review.maximum_spend)
        _validate_handles(
            (self.review.campaign_handle, self.review.adgroup_handle, self.review.ad_handle)
        )
        if self.native_adgroup_total_budget_amount != _exact_usd_amount(self.maximum_spend):
            raise ValueError(
                "TikTok ad-group total budget must preserve the exact reviewed USD cap"
            )
        return self


CompiledCandidate = Annotated[
    CandidateCompilation
    | MetaCandidateCompilation
    | GoogleCandidateCompilation
    | LinkedInCandidateCompilation
    | RedditCandidateCompilation
    | TikTokCandidateCompilation,
    Field(discriminator="provider"),
]
_COMPILED_CANDIDATE_ADAPTER: TypeAdapter[CompiledCandidate] = TypeAdapter(CompiledCandidate)


def _preserve_supplied_scalar_types(raw: object, checked: object) -> None:
    """Literal booleans/versions must not merge with integer or float inputs."""
    if isinstance(raw, dict) and isinstance(checked, dict):
        for key, value in raw.items():
            if key in checked:
                _preserve_supplied_scalar_types(value, checked[key])
    elif isinstance(raw, list) and isinstance(checked, list):
        for value, validated in zip(raw, checked, strict=True):
            _preserve_supplied_scalar_types(value, validated)
    elif type(raw) is not type(checked):
        raise ValueError("Candidate input must preserve exact JSON scalar types")


class CandidateCompilationRecord(StrictModel):
    schema_version: Literal[1] = 1
    event_type: Literal["CANDIDATE_COMPILED"] = "CANDIDATE_COMPILED"
    compilation: CompiledCandidate
    candidate_digest: Digest

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("candidate record schema_version must be the integer 1")
        return value

    @field_validator("compilation", mode="before")
    @classmethod
    def reparse_model_instance(cls, value: object) -> object:
        """Validate every variant through JSON, including already-built models.

        Strict tuple fields deliberately accept their JSON array representation
        at the serialized ledger boundary.  Reusing the JSON adapter preserves
        that behavior for raw payload objects while avoiding Pydantic's nested
        model-instance fast path for ``model_copy``/``model_construct`` input.
        """
        try:
            if isinstance(value, BaseModel):
                raw = value.model_dump_json(warnings="error")
            elif isinstance(value, dict):
                # Check representability, but never feed JCS-normalized floats
                # back to strict validation as integers.
                canonical_bytes(value)
                raw = json.dumps(value, allow_nan=False)
            else:
                return value
            parsed = parse_json(raw)
            canonical_bytes(parsed)
        except Exception:
            raise ValueError("Candidate compilation violates its closed JSON contract") from None
        checked = _COMPILED_CANDIDATE_ADAPTER.validate_json(raw)
        _preserve_supplied_scalar_types(parsed, checked.model_dump(mode="json", warnings="error"))
        return checked

    @model_validator(mode="after")
    def exact_review_digest(self) -> "CandidateCompilationRecord":
        if self.candidate_digest != self.compilation.candidate_digest:
            raise ValueError("Candidate digest does not match the retained review")
        return self
