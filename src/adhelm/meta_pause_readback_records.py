"""Closed factual Meta pause readbacks, separate from inventory and execution claims."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import StrictModel

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class MetaCampaignPauseReadbackObservation(StrictModel):
    schema_version: Literal[1] = 1
    action_id: Digest
    plan_digest: Digest
    subplan_digest: Digest
    attempt_ref: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    account_ref: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:/-]+$")
    campaign_ref: str = Field(pattern=r"^meta_ads\.campaign\.[1-9][0-9]{0,39}$")
    provider_api_version: Literal["v26.0"] = "v26.0"
    capability_version: Literal["meta-graph-v26-campaign-pause-contract-2026-09-06"] = (
        "meta-graph-v26-campaign-pause-contract-2026-09-06"
    )
    observation_interval_start: datetime
    observation_interval_end: datetime
    configured_status: Literal["PAUSED"] = "PAUSED"
    effective_status: Literal[
        "ACTIVE", "ARCHIVED", "DELETED", "IN_PROCESS", "PAUSED", "WITH_ISSUES"
    ]
    acknowledgement_semantics: Literal["LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"] = (
        "LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"
    )

    @field_validator("observation_interval_start", "observation_interval_end")
    @classmethod
    def aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("readback interval requires an aware clock")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_interval(self) -> "MetaCampaignPauseReadbackObservation":
        if (
            not timedelta(0)
            <= self.observation_interval_end - self.observation_interval_start
            <= timedelta(minutes=5)
        ):
            raise ValueError("readback interval is reversed or too long")
        return self


def validate_meta_pause_readback_history(records: list[dict[str, Any]]) -> None:
    """Use already-scanned records only; never acquire a lock or read a ledger."""
    actions: dict[str, dict[str, Any]] = {}
    starts: list[dict[str, Any]] = []
    observed: set[str] = set()
    for record in records:
        kind = record.get("record_kind")
        payload = record.get("payload", {})
        if kind == "META_CAMPAIGN_PAUSE_PLAN":
            action = payload["action"]
            actions[action["action_id"]] = action
        elif kind == "CHANGE_EVENT" and payload.get("event_type") == "APPLY_STARTED":
            starts.append(record)
        elif kind == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION":
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
                    or record.get("provider") != "meta_ads"
                    or record.get("account_ref") != account_ref
                    or record.get("profile_ref") != plan["profile_ref"]
                    or record.get("run_id") != plan["plan_id"]
                    or record.get("source") != "LOCAL_CLI"
                    or record.get("occurred_at") is not None
                    or datetime.fromisoformat(record["observed_at"].replace("Z", "+00:00"))
                    != datetime.fromisoformat(
                        payload["observation_interval_end"].replace("Z", "+00:00")
                    )
                    or payload["attempt_ref"] in observed
                ):
                    raise ValueError("readback binding")
                matching = [
                    start
                    for start in starts
                    if start.get("provider") == "meta_ads"
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
                    "INVALID_META_PAUSE_READBACK_HISTORY",
                    "Meta pause readback requires its exact retained action and durable start.",
                    exit_code=2,
                ) from None
