"""Closed factual Google Ads PAUSE readbacks, separate from plans and claims.

One record describes exactly one post-acknowledgement observation of the fixed
``ENABLED -> PAUSED`` campaign transition: the factual before and after
configured statuses, the observed customer status, the bounded observation
interval, and the single fact the v25 ``campaigns:mutate`` acknowledgement
carried -- its ``results[].resourceName``.  No request body, response body,
bearer, developer token, or provider diagnostic is retained, and the record
asserts no acknowledgement, conditional-write, idempotency, delivery, or spend
semantics.

The record kind is additive in the ``changes`` stream and mirrors
``META_CAMPAIGN_PAUSE_READBACK_OBSERVATION``.  It differs in one deliberate
way: this record carries ``occurred_at`` equal to its own
``observation_interval_end``, because the thing that occurred *is* the local
observation and its instant is known exactly.  Meta's record leaves
``occurred_at`` null and needs a ledger exemption for that; this one does not,
so registering this kind is one ``_KINDS`` entry plus the optional history
validator below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import StrictModel
from adhelm.providers.google_ads_pause_contract import (
    GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION,
    GOOGLE_ADS_CAMPAIGN_PAUSE_CAPABILITY_VERSION,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

#: The one additive ledger record kind this module describes, in ``changes``.
GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND: Final = "GOOGLE_ADS_CAMPAIGN_PAUSE_READBACK_OBSERVATION"

#: The bound the fixed adapter's post-acknowledgement readback must fit in.
_MAX_READBACK_INTERVAL: Final = timedelta(minutes=5)

GoogleAdsCustomerStatus = Literal["CANCELED", "CLOSED", "ENABLED", "SUSPENDED"]


class GoogleAdsCampaignPauseReadbackObservation(StrictModel):
    """One dedicated post-acknowledgement Google Ads v25 campaign observation."""

    schema_version: Literal[1] = 1
    action_id: Digest
    plan_digest: Digest
    subplan_digest: Digest
    attempt_ref: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    account_ref: str = Field(pattern=r"^google_ads\.customer\.[0-9]{10}$")
    campaign_ref: str = Field(pattern=r"^google_ads\.campaign\.[0-9]{10}\.[1-9][0-9]{0,18}$")
    provider_api_version: Literal["v25"] = GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION
    capability_version: Literal["google-ads-v25-campaign-pause-contract-2026-09-06"] = (
        GOOGLE_ADS_CAMPAIGN_PAUSE_CAPABILITY_VERSION
    )
    observation_interval_start: datetime
    observation_interval_end: datetime
    #: The approved transition, restated as the two configured statuses this
    #: workspace actually observed on either side of the single mutation.
    before_configured_status: Literal["ENABLED"] = "ENABLED"
    after_configured_status: Literal["PAUSED"] = "PAUSED"
    #: Retained as an observed provider value only.  ``CLOSED`` is not an
    #: eligibility statement, exactly as in the M54/M55 observation contract.
    observed_account_status: GoogleAdsCustomerStatus
    #: The sole acknowledgement fact: the resource name the fixed
    #: ``campaigns:mutate`` response named.  It restates the campaign this plan
    #: already binds and carries no other response content.
    acknowledged_resource_name: str = Field(
        pattern=r"^customers/[0-9]{10}/campaigns/[1-9][0-9]{0,18}$"
    )
    acknowledgement_semantics: Literal["LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"] = (
        "LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"
    )
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"
    conditional_write_proof: Literal[False] = False
    idempotency_proof: Literal[False] = False

    @field_validator("conditional_write_proof", "idempotency_proof", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("a readback observation proves neither CAS nor idempotency")
        return value

    @field_validator("observation_interval_start", "observation_interval_end")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("readback interval requires an aware clock")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_interval_and_exact_binding(self) -> GoogleAdsCampaignPauseReadbackObservation:
        if (
            not timedelta(0)
            <= self.observation_interval_end - self.observation_interval_start
            <= _MAX_READBACK_INTERVAL
        ):
            raise ValueError("readback interval is reversed or too long")
        customer = self.account_ref.removeprefix("google_ads.customer.")
        campaign = self.campaign_ref.removeprefix(f"google_ads.campaign.{customer}.")
        if (
            not self.campaign_ref.startswith(f"google_ads.campaign.{customer}.")
            or self.acknowledged_resource_name != f"customers/{customer}/campaigns/{campaign}"
        ):
            raise ValueError("the acknowledgement does not name this plan's exact campaign")
        return self


def validate_google_ads_pause_readback_history(records: list[dict[str, Any]]) -> None:
    """Use already-scanned records only; never acquire a lock or read a ledger.

    Every retained readback must name one retained
    ``GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN`` action, restate that action's own plan
    and subplan digests, and follow exactly one durable ``APPLY_STARTED`` for
    the same attempt.  A second readback for one attempt is refused: the fixed
    route performs one mutation and one readback.
    """

    actions: dict[str, dict[str, Any]] = {}
    starts: list[dict[str, Any]] = []
    observed: set[str] = set()
    for record in records:
        kind = record.get("record_kind")
        payload = record.get("payload", {})
        if kind == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN":
            action = payload["action"]
            actions[action["action_id"]] = action
        elif kind == "CHANGE_EVENT" and payload.get("event_type") == "APPLY_STARTED":
            starts.append(record)
        elif kind == GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND:
            try:
                action = actions[payload["action_id"]]
                plan = action["plan"]
                subplan = plan["subplans"][0]
                campaign = action["observation_binding"]["campaign"]
                account_ref = subplan["account"]["account_ref"]
                if (
                    payload["plan_digest"] != digest(plan)
                    or payload["subplan_digest"] != digest(subplan)
                    or payload["account_ref"] != account_ref
                    or payload["campaign_ref"] != campaign["campaign_ref"]
                    or payload["provider_api_version"] != subplan["provider_api_version"]
                    or payload["capability_version"] != subplan["capability_version"]
                    or payload["observed_account_status"]
                    != action["account_binding"]["account_status"]
                    or record.get("provider") != "google_ads"
                    or record.get("account_ref") != account_ref
                    or record.get("profile_ref") != plan["profile_ref"]
                    or record.get("run_id") != plan["plan_id"]
                    or record.get("source") != "LOCAL_CLI"
                    or _instant(record["observed_at"])
                    != _instant(payload["observation_interval_end"])
                    or _instant(record["occurred_at"])
                    != _instant(payload["observation_interval_end"])
                    or payload["attempt_ref"] in observed
                ):
                    raise ValueError("readback binding")
                matching = [
                    start
                    for start in starts
                    if start.get("provider") == "google_ads"
                    and start.get("account_ref") == account_ref
                    and start.get("run_id") == plan["plan_id"]
                    and start["payload"].get("plan_digest") == payload["plan_digest"]
                    and start["payload"].get("subplan_digest") == payload["subplan_digest"]
                    and start["payload"].get("idempotency_ref") == payload["attempt_ref"]
                    and start["payload"].get("operation") == "PAUSE"
                    and start["payload"].get("resource_ref") == subplan["subplan_id"]
                    and start["payload"].get("before_digest") == subplan["before"]["fingerprint"]
                    and start["payload"].get("after_digest") == subplan["after"]["fingerprint"]
                ]
                if len(matching) != 1:
                    raise ValueError("readback start")
                observed.add(payload["attempt_ref"])
            except (AttributeError, KeyError, TypeError, ValueError, IndexError):
                raise AdhelmError(
                    "INVALID_GOOGLE_ADS_PAUSE_READBACK_HISTORY",
                    "A Google Ads PAUSE readback requires its exact retained action and start.",
                    exit_code=2,
                ) from None


def _instant(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def google_ads_pause_readback_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive readback kind.

    The adapter checks this at factory admission -- before any credential,
    header, or provider request -- so an ordinary build refuses the whole
    execution rather than pausing a campaign and then failing to retain the
    observation that proves it.  This is a build fact, not a fallback: nothing
    here registers the kind, and no path continues without it.
    """

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is GoogleAdsCampaignPauseReadbackObservation


__all__ = [
    "GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND",
    "GoogleAdsCampaignPauseReadbackObservation",
    "google_ads_pause_readback_record_kind_installed",
    "validate_google_ads_pause_readback_history",
]
