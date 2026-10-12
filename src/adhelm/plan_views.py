"""Deterministic, redacted review facts derived from a reconstructed pause plan."""

import re
from datetime import UTC, datetime
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import Money, StrictModel
from adhelm.privacy import opaque_ref
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
OpaqueRef = Annotated[str, Field(pattern=r"^opaque:[0-9a-f]{64}$")]
_LOCAL_REF = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")
_NATIVE_REF = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_BIDDING_TYPES = frozenset({"impressions", "clicks", "conversions"})


class PauseCampaignReviewState(StrictModel):
    status: Literal["active", "paused"]
    bidding_type: Literal["impressions", "clicks", "conversions"]
    lifetime_spend_limit_micros: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    start_at: datetime
    end_at: datetime | None


class OpenAIPauseReview(StrictModel):
    schema_version: Literal[1] = 1
    provider: Literal["openai_ads"] = "openai_ads"
    operation: Literal["PAUSE"] = "PAUSE"
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = (
        "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
    )
    plan_digest: Digest
    subplan_digests: tuple[Digest, ...] = Field(min_length=1, max_length=1)
    account_ref: OpaqueRef
    campaign_ref: OpaqueRef
    profile_ref: OpaqueRef
    workspace_profile_ref: OpaqueRef
    account_timezone: str = Field(min_length=1, max_length=80)
    account_currency: str = Field(pattern=r"^[A-Z]{3}$")
    before: PauseCampaignReviewState
    after: PauseCampaignReviewState
    maximum_incremental_spend: Money
    expires_at: datetime
    reviewed_at: datetime
    expired_at_review: bool
    egress_hosts: tuple[Literal["api.ads.openai.com"], ...] = ("api.ads.openai.com",)
    review_authorizes_execution: Literal[False] = False
    provider_conditional_write: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    provider_idempotency: Literal["NOT_DOCUMENTED"] = "NOT_DOCUMENTED"
    ambiguous_attempt: Literal["UNRESOLVED_NO_BLIND_RETRY"] = "UNRESOLVED_NO_BLIND_RETRY"
    residual_delivery_or_billing: Literal["MAY_CONTINUE_AFTER_PAUSE"] = "MAY_CONTINUE_AFTER_PAUSE"

    @property
    def review_digest(self) -> str:
        return digest(self)


def _validated_action(action: OpenAIPausePlannedAction) -> OpenAIPausePlannedAction:
    """Detach a possibly constructed value without emitting its representation."""
    if type(action) is not OpenAIPausePlannedAction:
        raise AdhelmError(
            "INVALID_PLAN", "Plan review requires the installed closed action.", exit_code=2
        )
    try:
        # ``model_copy`` and ``model_construct`` can bypass Pydantic validation.
        # Serialize with warnings promoted to errors so malformed nested values
        # cannot put their ``repr`` into process output before this fails closed.
        checked = OpenAIPausePlannedAction.model_validate(
            action.model_dump(mode="python", warnings="error")
        )
    except Exception:
        raise AdhelmError(
            "INVALID_PLAN", "Plan failed closed-action validation.", exit_code=2
        ) from None

    binding, campaign = checked.binding, checked.binding.campaign
    if (
        _LOCAL_REF.fullmatch(binding.account_ref) is None
        or _LOCAL_REF.fullmatch(campaign.campaign_ref) is None
        or _LOCAL_REF.fullmatch(binding.profile_ref) is None
        or _NATIVE_REF.fullmatch(binding.account_client_ref) is None
        or _NATIVE_REF.fullmatch(campaign.campaign_client_ref) is None
        or campaign.bidding_type not in _BIDDING_TYPES
    ):
        raise AdhelmError("INVALID_PLAN", "Plan failed closed-source validation.", exit_code=2)
    try:
        ZoneInfo(binding.account_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise AdhelmError(
            "INVALID_PLAN", "Plan failed closed-source validation.", exit_code=2
        ) from None
    return checked


def preview_openai_pause(
    action: OpenAIPausePlannedAction, *, opaque_key: bytes, now: datetime
) -> OpenAIPauseReview:
    """Review an expired or current plan without reading credentials or remote state.

    The review preserves exact plan digests and native budget/schedule values.
    It does not become fresh provider evidence, a grant, or a write instruction.
    """
    if not isinstance(opaque_key, bytes) or len(opaque_key) < 32:
        raise AdhelmError(
            "EXPORT_KEY_REQUIRED", "Plan review requires a private local sharing key."
        )
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise AdhelmError(
            "CLOCK_INVALID", "Plan review time must be an aware instant.", exit_code=2
        )
    checked = _validated_action(action)
    binding, plan = checked.binding, checked.plan
    campaign = binding.campaign
    unchanged = {
        "bidding_type": campaign.bidding_type,
        "lifetime_spend_limit_micros": campaign.lifetime_spend_limit_micros,
        "start_at": campaign.start_at,
        "end_at": campaign.end_at,
    }
    return OpenAIPauseReview(
        plan_digest=plan.plan_digest,
        subplan_digests=plan.subplan_digests,
        account_ref=opaque_ref(opaque_key, binding.account_ref),
        campaign_ref=opaque_ref(opaque_key, campaign.campaign_ref),
        profile_ref=opaque_ref(opaque_key, binding.profile_ref),
        workspace_profile_ref=opaque_ref(opaque_key, plan.profile_ref),
        account_timezone=binding.account_timezone,
        account_currency=binding.account_currency,
        before=PauseCampaignReviewState.model_validate({**unchanged, "status": "active"}),
        after=PauseCampaignReviewState.model_validate({**unchanged, "status": "paused"}),
        maximum_incremental_spend=Money.model_validate(plan.subplans[0].max_spend.model_dump()),
        expires_at=plan.expires_at,
        reviewed_at=now.astimezone(UTC),
        expired_at_review=plan.expires_at <= now,
    )
