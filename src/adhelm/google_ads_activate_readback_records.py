"""Closed factual Google Ads Search ACTIVATE readbacks, separate from plans.

One record describes exactly one post-acknowledgement observation of the fixed
atomic Search hierarchy activation: the bounded observation interval, the
observed customer status, the member count the readback projected, the exact
spend exposure the activation authorized, the precomputed recovery pause
proposal digest, and the ordered result kinds the single ``googleAds:mutate``
acknowledgement carried.

No request body, response body, bearer, developer token, or provider diagnostic
is retained, and the record asserts no acknowledgement, conditional-write,
idempotency, delivery, or spend *outcome* semantics.  It says what was
configured and what the bounded readback observed, and nothing about serving.

**The native resource names are deliberately pseudonymized**, exactly as the
P04 CREATE readback pseudonymizes them: the plan already carries the
customer-configured campaign name, which is the handle a bounded recovery uses.

This record mirrors ``GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_READBACK_OBSERVATION``
and carries ``occurred_at`` equal to its own ``observation_interval_end``,
because the thing that occurred *is* the local observation and its instant is
known exactly.  Registering the kind is one ``_KINDS`` entry plus the scan-time
history validator below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import StrictModel
from adhelm.providers.google_ads_activate_contract import (
    GOOGLE_ADS_ACTIVATE_API_VERSION,
    GOOGLE_ADS_ACTIVATE_CAPABILITY_VERSION,
    GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

#: The one additive ledger record kind this module describes, in ``changes``.
GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND: Final = (
    "GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_READBACK_OBSERVATION"
)

#: Domain separation for the pseudonymized native resource references.
_RESOURCE_DOMAIN: Final = "adhelm.google-ads.search-campaign-activate.resource.v1"

#: The bound the guarded adapter's post-acknowledgement readback must fit in.
_MAX_READBACK_INTERVAL: Final = timedelta(minutes=5)

#: The exact ordered acknowledgement result kinds this route accepts.
GOOGLE_ADS_ACTIVATE_RESULT_KINDS: Final = frozenset(
    {
        "campaignResult",
        "campaignCriterionResult",
        "adGroupResult",
        "adGroupCriterionResult",
        "adGroupAdResult",
    }
)

GoogleAdsCustomerStatus = Literal["CANCELED", "CLOSED", "ENABLED", "SUSPENDED"]


def pseudonymized_activation_ref(kind: str, resource_name: str) -> str:
    """Return the stable keyless reference this record retains for one resource."""

    if kind not in {"campaign", "ad_group"}:
        raise ValueError("only a campaign or ad group resource is pseudonymized here")
    if type(resource_name) is not str or not 1 <= len(resource_name) <= 256:
        raise ValueError("a bounded provider resource path is required")
    return f"google_ads.{kind}:sha256:" + digest(
        {"domain": _RESOURCE_DOMAIN, "kind": kind, "resource_name": resource_name}
    )


class GoogleAdsSearchCampaignActivateReadbackObservation(StrictModel):
    """One dedicated post-acknowledgement Google Ads v25 activation observation."""

    schema_version: Literal[1] = 1
    action_id: Digest
    plan_digest: Digest
    subplan_digest: Digest
    attempt_ref: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    account_ref: str = Field(pattern=r"^google_ads\.customer\.[0-9]{10}$")
    campaign_resource_ref: str = Field(pattern=r"^google_ads\.campaign:sha256:[0-9a-f]{64}$")
    ad_group_resource_ref: str = Field(pattern=r"^google_ads\.ad_group:sha256:[0-9a-f]{64}$")
    provider_api_version: Literal["v25"] = GOOGLE_ADS_ACTIVATE_API_VERSION
    capability_version: Literal["google-ads-v25-search-activate-contract-2026-09-09"] = (
        GOOGLE_ADS_ACTIVATE_CAPABILITY_VERSION
    )
    observation_interval_start: datetime
    observation_interval_end: datetime
    #: The approved transition, restated as the two semantic hierarchy states
    #: this workspace actually observed on either side of the one mutation.
    before_observed_hierarchy: Literal["PAUSED_SEARCH_HIERARCHY_MATCHES_PLAN_BEFORE"] = (
        "PAUSED_SEARCH_HIERARCHY_MATCHES_PLAN_BEFORE"
    )
    after_observed_hierarchy: Literal["ENABLED_SEARCH_HIERARCHY_MATCHES_PREDICTED_SEMANTICS"] = (
        "ENABLED_SEARCH_HIERARCHY_MATCHES_PREDICTED_SEMANTICS"
    )
    observed_member_count: int = Field(ge=6, le=140)
    every_entity_configured_status: Literal["ENABLED"] = "ENABLED"
    #: Retained as an observed provider value only.
    observed_account_status: GoogleAdsCustomerStatus
    #: The spend-explicit fact this authorization turned on, restated from the
    #: plan's own per-currency bound.  It is an authorized ceiling, never an
    #: observation of spend: nothing local can observe provider delivery.
    authorized_maximum_spend_minor_units: int = Field(ge=1, le=900_719_925_474)
    authorized_currency: str = Field(pattern=r"^[A-Z]{3}$")
    spend_exposure: Literal["CAMPAIGN_LIFETIME_CUSTOM_PERIOD_TOTAL"] = (
        "CAMPAIGN_LIFETIME_CUSTOM_PERIOD_TOTAL"
    )
    observed_spend: Literal["NOT_OBSERVABLE_FROM_THIS_ROUTE"] = "NOT_OBSERVABLE_FROM_THIS_ROUTE"
    effective_delivery: Literal["BEGINS_ON_ACTIVATION_VOLUME_NOT_PREDICTED"] = (
        "BEGINS_ON_ACTIVATION_VOLUME_NOT_PREDICTED"
    )
    #: The exact policy fact the plan disclosed, carried forward verbatim.
    policy_approval_status: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    #: The sole acknowledgement facts: how many ordered operation responses the
    #: one mutate returned and which result field each carried, in order.
    acknowledged_operation_count: int = Field(ge=5, le=140)
    acknowledged_result_kinds: tuple[str, ...] = Field(min_length=5, max_length=140)
    acknowledgement_semantics: Literal["LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"] = (
        "LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"
    )
    atomicity_semantics: Literal["SINGLE_MUTATE_UPDATE_MASK_STATUS_PARTIAL_FAILURE_DISABLED"] = (
        "SINGLE_MUTATE_UPDATE_MASK_STATUS_PARTIAL_FAILURE_DISABLED"
    )
    recovery_disposition: Literal["RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL"] = (
        "RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL"
    )
    recovery_pause_plan_digest: Digest
    recovery_pause_plan_authorization: Literal["NOT_AUTHORIZED_PROPOSAL_ONLY"] = (
        "NOT_AUTHORIZED_PROPOSAL_ONLY"
    )
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

    @field_validator("acknowledged_result_kinds")
    @classmethod
    def closed_result_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(item not in GOOGLE_ADS_ACTIVATE_RESULT_KINDS for item in value):
            raise ValueError("a mutate acknowledgement carries only the planned result kinds")
        return value

    @model_validator(mode="after")
    def bounded_interval_and_exact_counts(
        self,
    ) -> GoogleAdsSearchCampaignActivateReadbackObservation:
        if (
            not timedelta(0)
            <= self.observation_interval_end - self.observation_interval_start
            <= _MAX_READBACK_INTERVAL
        ):
            raise ValueError("readback interval is reversed or too long")
        if self.acknowledged_operation_count != len(self.acknowledged_result_kinds):
            raise ValueError("the acknowledgement count does not describe its own result kinds")
        # The budget is the one observed member no activation addresses: its
        # status is output-only in v25 and tracks the campaign.
        if self.acknowledged_operation_count != self.observed_member_count - 1:
            raise ValueError("the acknowledgement does not describe the activated hierarchy")
        if self.campaign_resource_ref == self.ad_group_resource_ref:
            raise ValueError("a campaign and its ad group are distinct resources")
        return self


def validate_google_ads_activate_readback_history(records: list[dict[str, Any]]) -> None:
    """Use already-scanned records only; never acquire a lock or read a ledger.

    Every retained readback must name one retained
    ``GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN`` action, restate that action's
    own plan and subplan digests, its predicted member count, its authorized
    per-currency bound and its recovery pause proposal digest, and follow
    exactly one durable ``APPLY_STARTED`` for the same attempt.  A second
    readback for one attempt is refused: the fixed route performs one mutation
    and one readback.
    """

    actions: dict[str, dict[str, Any]] = {}
    starts: list[dict[str, Any]] = []
    observed: set[str] = set()
    for record in records:
        kind = record.get("record_kind")
        payload = record.get("payload", {})
        if kind == GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND:
            action = payload["action"]
            actions[action["action_id"]] = action
        elif kind == "CHANGE_EVENT" and payload.get("event_type") == "APPLY_STARTED":
            starts.append(record)
        elif kind == GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND:
            try:
                action = actions[payload["action_id"]]
                plan = action["plan"]
                subplan = plan["subplans"][0]
                account_ref = subplan["account"]["account_ref"]
                if (
                    payload["plan_digest"] != digest(plan)
                    or payload["subplan_digest"] != digest(subplan)
                    or payload["account_ref"] != account_ref
                    or payload["provider_api_version"] != subplan["provider_api_version"]
                    or payload["capability_version"] != subplan["capability_version"]
                    or payload["observed_member_count"] != action["postcondition"]["member_count"]
                    or payload["observed_account_status"]
                    != action["account_binding"]["account_status"]
                    or payload["authorized_maximum_spend_minor_units"]
                    != subplan["max_spend"]["minor_units"]
                    or payload["authorized_currency"] != subplan["max_spend"]["currency"]
                    or payload["recovery_pause_plan_digest"]
                    != action["recovery_disposition"]["recovery_pause_plan_digest"]
                    or payload["policy_approval_status"]
                    != action["observation_binding"]["preconditions"]["policy_approval_status"]
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
                    and start["payload"].get("operation") == "ACTIVATE"
                    and start["payload"].get("resource_ref") == subplan["subplan_id"]
                    and start["payload"].get("before_digest") == subplan["before"]["fingerprint"]
                    and start["payload"].get("after_digest") == subplan["after"]["fingerprint"]
                ]
                if len(matching) != 1:
                    raise ValueError("readback start")
                observed.add(payload["attempt_ref"])
            except (AttributeError, KeyError, TypeError, ValueError, IndexError):
                raise AdhelmError(
                    "INVALID_GOOGLE_ADS_ACTIVATE_READBACK_HISTORY",
                    "A Google Ads ACTIVATE readback requires its exact retained action and start.",
                    exit_code=2,
                ) from None


def _instant(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def google_ads_activate_readback_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive readback kind.

    The adapter checks this at factory admission -- before any credential,
    header, or provider request -- so an ordinary build refuses the whole
    execution rather than activating a hierarchy and then failing to retain the
    observation that proves what it activated.  This is a build fact, not a
    fallback: nothing here registers the kind, and no path continues without it.
    """

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is GoogleAdsSearchCampaignActivateReadbackObservation


__all__ = [
    "GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND",
    "GOOGLE_ADS_ACTIVATE_RESULT_KINDS",
    "GoogleAdsSearchCampaignActivateReadbackObservation",
    "google_ads_activate_readback_record_kind_installed",
    "pseudonymized_activation_ref",
    "validate_google_ads_activate_readback_history",
]
