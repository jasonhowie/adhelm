"""Closed factual Google Ads Search maintenance readbacks, separate from plans.

One record describes exactly one post-acknowledgement observation of one of the
four maintenance operations: the bounded observation interval, the observed
customer status, what the value was before and what it is now, and the ordered
result kinds the single ``googleAds:mutate`` acknowledgement carried.

**The previous values are the recovery.**  This family reverts nothing
automatically.  A budget, bid, or schedule change is undone by compiling a new
plan with the recorded previous value and authorizing it; an archive is not
undone at all, and the record says so.

No request body, response body, bearer, developer token, or provider diagnostic
is retained, and the record asserts no acknowledgement, conditional-write,
idempotency, delivery, or spend semantics.  Native resource names are
pseudonymized exactly as they are in the P04 CREATE readback.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import StrictModel
from adhelm.providers.google_ads_maintain_contract import (
    GOOGLE_ADS_MAINTAIN_API_VERSION,
    GOOGLE_ADS_MAINTAIN_CAPABILITY_VERSION,
    GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND,
    MaintenanceKind,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

#: The one additive ledger record kind this module describes, in ``changes``.
GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND: Final = (
    "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_READBACK_OBSERVATION"
)

#: Domain separation for the pseudonymized native resource references.
_RESOURCE_DOMAIN: Final = "adhelm.google-ads.search-campaign-maintain.resource.v1"

#: The bound the guarded adapter's post-acknowledgement readback must fit in.
_MAX_READBACK_INTERVAL: Final = timedelta(minutes=5)

#: The exact ordered acknowledgement result kinds this route accepts.
GOOGLE_ADS_MAINTAIN_RESULT_KINDS: Final = frozenset(
    {"campaignBudgetResult", "campaignResult", "campaignCriterionResult", "adGroupResult"}
)

GoogleAdsCustomerStatus = Literal["CANCELED", "CLOSED", "ENABLED", "SUSPENDED"]


def pseudonymized_maintenance_ref(kind: str, resource_name: str) -> str:
    """Return the stable keyless reference this record retains for one resource."""

    if kind not in {"campaign", "ad_group", "campaign_budget"}:
        raise ValueError("only a campaign, budget, or ad group resource is pseudonymized here")
    if type(resource_name) is not str or not 1 <= len(resource_name) <= 256:
        raise ValueError("a bounded provider resource path is required")
    return f"google_ads.{kind}:sha256:" + digest(
        {"domain": _RESOURCE_DOMAIN, "kind": kind, "resource_name": resource_name}
    )


class GoogleAdsSearchCampaignMaintainReadbackObservation(StrictModel):
    """One dedicated post-acknowledgement Google Ads v25 maintenance observation."""

    schema_version: Literal[1] = 1
    action_id: Digest
    plan_digest: Digest
    subplan_digest: Digest
    attempt_ref: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    account_ref: str = Field(pattern=r"^google_ads\.customer\.[0-9]{10}$")
    campaign_resource_ref: str = Field(pattern=r"^google_ads\.campaign:sha256:[0-9a-f]{64}$")
    provider_api_version: Literal["v25"] = GOOGLE_ADS_MAINTAIN_API_VERSION
    capability_version: Literal["google-ads-v25-search-maintain-contract-2026-09-09"] = (
        GOOGLE_ADS_MAINTAIN_CAPABILITY_VERSION
    )
    maintenance_kind: MaintenanceKind
    operation: Literal["UPDATE", "ARCHIVE"]
    observation_interval_start: datetime
    observation_interval_end: datetime
    #: The two facts a later corrective plan needs, and the whole of what this
    #: build calls recovery for a maintenance operation.
    previous_value: str = Field(pattern=r"^[A-Z0-9_]{1,64}$|^[0-9]{1,20}$")
    new_value: str = Field(pattern=r"^[A-Z0-9_]{1,64}$|^[0-9]{1,20}$")
    observed_after: Literal["LIVE_HIERARCHY_MATCHES_PREDICTED_SEMANTICS", "CAMPAIGN_STATUS_REMOVED"]
    observed_member_count: int | None = Field(default=None, ge=6, le=140)
    observed_account_status: GoogleAdsCustomerStatus
    authorized_maximum_spend_minor_units: int = Field(ge=0, le=900_719_925_474)
    authorized_currency: str = Field(pattern=r"^[A-Z]{3}$")
    reversibility: Literal["REVERSIBLE_BY_A_NEW_PLAN", "IRREVERSIBLE_ARCHIVE"]
    recovery_disposition: Literal["RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT"] = (
        "RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT"
    )
    automatic_revert: Literal[False] = False
    acknowledged_operation_count: int = Field(ge=1, le=140)
    acknowledged_result_kinds: tuple[str, ...] = Field(min_length=1, max_length=140)
    acknowledgement_semantics: Literal["LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"] = (
        "LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"
    )
    atomicity_semantics: Literal["SINGLE_MUTATE_PARTIAL_FAILURE_DISABLED"] = (
        "SINGLE_MUTATE_PARTIAL_FAILURE_DISABLED"
    )
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"
    conditional_write_proof: Literal[False] = False
    idempotency_proof: Literal[False] = False

    @field_validator(
        "automatic_revert", "conditional_write_proof", "idempotency_proof", mode="before"
    )
    @classmethod
    def exact_false(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("a readback observation proves neither CAS, idempotency, nor a revert")
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
        if any(item not in GOOGLE_ADS_MAINTAIN_RESULT_KINDS for item in value):
            raise ValueError("a mutate acknowledgement carries only the planned result kinds")
        return value

    @model_validator(mode="after")
    def bounded_interval_and_exact_shape(
        self,
    ) -> GoogleAdsSearchCampaignMaintainReadbackObservation:
        if (
            not timedelta(0)
            <= self.observation_interval_end - self.observation_interval_start
            <= _MAX_READBACK_INTERVAL
        ):
            raise ValueError("readback interval is reversed or too long")
        if self.acknowledged_operation_count != len(self.acknowledged_result_kinds):
            raise ValueError("the acknowledgement count does not describe its own result kinds")
        if self.previous_value == self.new_value:
            raise ValueError("a maintenance operation must change the value it addresses")
        archive = self.maintenance_kind == "CAMPAIGN_ARCHIVE"
        if archive:
            if (
                self.operation != "ARCHIVE"
                or self.observed_after != "CAMPAIGN_STATUS_REMOVED"
                or self.observed_member_count is not None
                or self.reversibility != "IRREVERSIBLE_ARCHIVE"
                or self.authorized_maximum_spend_minor_units != 0
            ):
                raise ValueError("an archive readback is removed, irreversible, and zero cap")
        elif (
            self.operation != "UPDATE"
            or self.observed_after != "LIVE_HIERARCHY_MATCHES_PREDICTED_SEMANTICS"
            or self.observed_member_count is None
            or self.reversibility != "REVERSIBLE_BY_A_NEW_PLAN"
            or self.authorized_maximum_spend_minor_units < 1
        ):
            raise ValueError("a maintenance readback keeps a live hierarchy and a spend cap")
        return self


def validate_google_ads_maintain_readback_history(records: list[dict[str, Any]]) -> None:
    """Use already-scanned records only; never acquire a lock or read a ledger.

    Every retained readback must name one retained
    ``GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_PLAN`` action, restate that action's
    own plan and subplan digests, its maintenance kind, its authorized
    per-currency bound and its reversibility, and follow exactly one durable
    ``APPLY_STARTED`` for the same attempt.  A second readback for one attempt
    is refused: the fixed route performs one mutation and one readback.
    """

    actions: dict[str, dict[str, Any]] = {}
    starts: list[dict[str, Any]] = []
    observed: set[str] = set()
    for record in records:
        kind = record.get("record_kind")
        payload = record.get("payload", {})
        if kind == GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND:
            action = payload["action"]
            actions[action["action_id"]] = action
        elif kind == "CHANGE_EVENT" and payload.get("event_type") == "APPLY_STARTED":
            starts.append(record)
        elif kind == GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND:
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
                    or payload["maintenance_kind"] != action["intent"]["maintenance_kind"]
                    or payload["operation"] != subplan["operation"]
                    or payload["observed_member_count"] != action["postcondition"]["member_count"]
                    or payload["reversibility"] != action["postcondition"]["reversibility"]
                    or payload["observed_account_status"]
                    != action["account_binding"]["account_status"]
                    or payload["authorized_maximum_spend_minor_units"]
                    != subplan["max_spend"]["minor_units"]
                    or payload["authorized_currency"] != subplan["max_spend"]["currency"]
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
                    and start["payload"].get("operation") == payload["operation"]
                    and start["payload"].get("resource_ref") == subplan["subplan_id"]
                    and start["payload"].get("before_digest") == subplan["before"]["fingerprint"]
                    and start["payload"].get("after_digest") == subplan["after"]["fingerprint"]
                ]
                if len(matching) != 1:
                    raise ValueError("readback start")
                observed.add(payload["attempt_ref"])
            except (AttributeError, KeyError, TypeError, ValueError, IndexError):
                raise AdhelmError(
                    "INVALID_GOOGLE_ADS_MAINTAIN_READBACK_HISTORY",
                    "A Google Ads MAINTAIN readback requires its exact retained action and start.",
                    exit_code=2,
                ) from None


def _instant(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def google_ads_maintain_readback_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive readback kind."""

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is GoogleAdsSearchCampaignMaintainReadbackObservation


__all__ = [
    "GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND",
    "GOOGLE_ADS_MAINTAIN_RESULT_KINDS",
    "GoogleAdsSearchCampaignMaintainReadbackObservation",
    "google_ads_maintain_readback_record_kind_installed",
    "pseudonymized_maintenance_ref",
    "validate_google_ads_maintain_readback_history",
]
