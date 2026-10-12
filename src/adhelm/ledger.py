"""Append-only, tamper-evident local JSONL history.

This module performs no provider requests. Callers hand it already validated,
normalized records; the narrow record-kind validators below prevent
the ledger from becoming a place to stash raw provider payloads.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

import portalocker
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from adhelm.approval_records import ApprovalReceiptClaimRecord
from adhelm.campaign_joins import CampaignJoinMap
from adhelm.candidate_records import CandidateCompilationRecord
from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.conversion_contracts import ConversionContract
from adhelm.creative import (
    AssetAnalysisRecord,
    CreativeAssetVersion,
    CreativeDeployment,
    CreativeFamilyRecord,
    CreativeRendition,
    CreativeVersionRecord,
    ImageFingerprintRecord,
    is_legacy_creative_payload,
    revalidate_creative_payload,
    validate_creative_history_graph,
)
from adhelm.creative_registration_closure import (
    CreativeRegistrationClosure,
    CreativeTextRegistrationClosure,
    closure_records,
    revalidate_creative_registration_closure,
)
from adhelm.errors import AdhelmError
from adhelm.execution_plan_history import (
    validate_execution_plan_envelope,
    validate_execution_plan_history,
)
from adhelm.execution_plan_records import (
    ExecutionPlanRecord,
    revalidate_execution_plan_record,
)
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_create_plan_history import (
    validate_ga4_create_plan_envelope,
    validate_ga4_create_plan_history,
    validate_ga4_create_start_history,
)
from adhelm.ga4_create_plan_records import (
    Ga4KeyEventCreatePlanRecord,
    revalidate_ga4_key_event_create_plan_record,
)
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateRecord,
    revalidate_ga4_key_event_candidate_record,
    validate_ga4_key_event_candidate_binding,
)
from adhelm.google_ads_activate_readback_records import (
    GoogleAdsSearchCampaignActivateReadbackObservation,
    validate_google_ads_activate_readback_history,
)
from adhelm.google_ads_create_readback_records import (
    GoogleAdsSearchCampaignCreateReadbackObservation,
    validate_google_ads_create_readback_history,
)
from adhelm.google_ads_maintain_readback_records import (
    GoogleAdsSearchCampaignMaintainReadbackObservation,
    validate_google_ads_maintain_readback_history,
)
from adhelm.google_ads_pause_plan_history import validate_google_ads_pause_plan_envelope
from adhelm.google_ads_pause_plan_records import (
    GoogleAdsCampaignPausePlanRecord,
    revalidate_google_ads_campaign_pause_plan_record,
)
from adhelm.google_ads_pause_readback_records import (
    GoogleAdsCampaignPauseReadbackObservation,
    validate_google_ads_pause_readback_history,
)
from adhelm.measurement_chunks import (
    CHUNK_KIND,
    CHUNKED_KIND,
    ChunkedMeasurementObservation,
    MeasurementObservationChunk,
    validate_chunked_measurement_history,
)
from adhelm.measurement_records import MeasurementObservation
from adhelm.meta_create_readback_records import (
    MetaAdsSingleImageHierarchyCreateReadbackObservation,
    validate_meta_create_readback_history,
)
from adhelm.meta_pause_plan_history import validate_meta_pause_plan_envelope
from adhelm.meta_pause_plan_records import (
    MetaCampaignPausePlanRecord,
    revalidate_meta_campaign_pause_plan_record,
)
from adhelm.meta_pause_readback_records import (
    MetaCampaignPauseReadbackObservation,
    validate_meta_pause_readback_history,
)
from adhelm.models import MAX_CHILD_EGRESS_COUNT
from adhelm.openai_create_readback_records import (
    OpenAIAdsChatCardHierarchyCreateReadbackObservation,
    validate_openai_create_readback_history,
)
from adhelm.plan_records import OpenAIPauseExecutionPlanRecord
from adhelm.privacy import validate_data
from adhelm.private_directories import ensure_private_directory
from adhelm.providers.google_ads_activate_contract import (
    GoogleAdsSearchCampaignActivatePlanRecord,
    revalidate_google_ads_search_campaign_activate_plan_record,
)
from adhelm.providers.google_ads_create_contract import (
    GoogleAdsSearchCampaignCreatePlanRecord,
    revalidate_google_ads_search_campaign_create_plan_record,
)
from adhelm.providers.google_ads_maintain_contract import (
    GoogleAdsSearchCampaignMaintainPlanRecord,
    revalidate_google_ads_search_campaign_maintain_plan_record,
)
from adhelm.providers.meta_create_contract import (
    MetaAdsSingleImageHierarchyCreatePlanRecord,
    revalidate_meta_ads_create_plan_record,
)
from adhelm.providers.openai_create_contract import (
    OpenAIAdsChatCardHierarchyCreatePlanRecord,
    revalidate_openai_ads_create_plan_record,
)
from adhelm.reporting import NormalizedReport, PerformanceObservationPayload
from adhelm.strategy_records import StrategyCandidateCompilationRecord
from adhelm.strict_json import validate_json_value

SCHEMA_URI = "https://aptsonic.com/adhelm/schema/ledger-record"
SCHEMA_VERSION = 1
CANONICALIZATION_VERSION = "RFC8785-1"
MAX_RECORD_BYTES = 1_048_576
# What a record's envelope may add around its payload: the digests, timestamps,
# identifiers and kind take about a kilobyte, so a writer that must decide
# before appending whether a payload fits one record reserves a safe margin.
RECORD_ENVELOPE_RESERVE_BYTES = 4_096
MAX_PAYLOAD_BYTES = MAX_RECORD_BYTES - RECORD_ENVELOPE_RESERVE_BYTES
DEFAULT_SEGMENT_BYTES = 4 * 1024 * 1024
MAX_SEGMENT_BYTES = 16 * 1024 * 1024
STREAMS = frozenset({"changes", "performance", "creative"})
SOURCES = frozenset({"LOCAL_CLI", "PROVIDER_UI", "PROVIDER_AUTOMATION", "IMPORT", "UNKNOWN"})

# The generic resource-snapshot schema intentionally does not encode each
# provider hierarchy.  OpenAI native micros are meaningful only inside this
# narrow, fixture-reviewed hierarchy, so retain enough namespace and parent
# information here to validate them both at append time and while rereading a
# ledger.
_OPENAI_RESOURCE_PREFIXES = {
    "ACCOUNT": "openai_ads.account.",
    "CAMPAIGN": "openai_ads.campaign.",
    "AD_GROUP": "openai_ads.ad_group.",
    "AD": "openai_ads.ad.",
}
_OPENAI_PARENT_TYPES = {
    "ACCOUNT": (),
    "CAMPAIGN": ("ACCOUNT",),
    "AD_GROUP": ("CAMPAIGN",),
    "AD": ("CAMPAIGN", "AD_GROUP"),
}
_OPENAI_REF_SUFFIX = re.compile(r"^[A-Za-z0-9_-]{1,120}$")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(value: str | None, *, nullable: bool = False) -> str | None:
    if value is None:
        if nullable:
            return None
        return _utc_now()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdhelmError(
            "INVALID_TIMESTAMP", "Timestamps must be ISO 8601 instants.", exit_code=2
        ) from exc
    if parsed.tzinfo is None:
        raise AdhelmError("INVALID_TIMESTAMP", "Timestamps must include a UTC offset.", exit_code=2)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _adhelm_validation(exc: ValidationError) -> AdhelmError:
    return AdhelmError(
        "INVALID_LEDGER_PAYLOAD", "Record payload does not match its declared schema.", exit_code=2
    )


def _safe_value(value: Any, *, depth: int = 0) -> Any:
    """Permit normalized JSON only, while rejecting common raw/secret payload shapes."""
    if depth > 16:
        raise AdhelmError(
            "INVALID_LEDGER_PAYLOAD", "Nested record data exceeds the allowed depth.", exit_code=2
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise AdhelmError(
            "INVALID_LEDGER_PAYLOAD",
            "Floating point values are not allowed in canonical history.",
            exit_code=2,
        )
    if isinstance(value, list):
        return [_safe_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise AdhelmError(
                    "INVALID_LEDGER_PAYLOAD",
                    "Record field names must be bounded strings.",
                    exit_code=2,
                )
            lowered = key.lower()
            if any(
                term in lowered
                for term in ("secret", "token", "password", "authorization", "cookie", "header")
            ):
                raise AdhelmError(
                    "PROHIBITED_LEDGER_DATA",
                    "Secrets and raw transport data cannot enter history.",
                    exit_code=2,
                )
            if lowered in {
                "raw",
                "raw_response",
                "request_body",
                "response_body",
                "html",
                "dom",
                "form_value",
            }:
                raise AdhelmError(
                    "PROHIBITED_LEDGER_DATA",
                    "Raw provider or page data cannot enter history.",
                    exit_code=2,
                )
            result[key] = _safe_value(item, depth=depth + 1)
        return result
    raise AdhelmError(
        "INVALID_LEDGER_PAYLOAD", "Record values must be canonical JSON primitives.", exit_code=2
    )


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FieldDelta(_StrictModel):
    path: str
    before_status: str
    before_value: Any | None = None
    after_status: str
    after_value: Any | None = None
    value_classification: str

    @field_validator("before_value", "after_value")
    @classmethod
    def safe_value(cls, value: Any) -> Any:
        return _safe_value(value)


class PolicyReservation(_StrictModel):
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    minor_units: int = Field(ge=0)


class RunParticipant(_StrictModel):
    provider: str
    account_ref: str
    resource_ref: str
    subplan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class CompositeRunRecord(_StrictModel):
    """Durable barrier spanning every participant, including unstarted providers."""

    schema_version: Literal[1] = 1
    event_type: Literal["RUN_STARTED", "RUN_VERIFIED", "PROVIDER_RECONCILED"]
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_ref: str
    authorization_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    participants: list[RunParticipant] = Field(min_length=1)
    reconciled_subplan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    effect_status: Literal["APPLIED_VERIFIED", "FAILED_NO_EFFECT", "PARTIAL_EFFECT"] | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_barrier(self) -> CompositeRunRecord:
        identities = [p.subplan_digest for p in self.participants]
        if len(identities) != len(set(identities)):
            raise ValueError("Composite participants must be unique")
        if self.event_type == "PROVIDER_RECONCILED":
            if self.reconciled_subplan_digest not in identities or self.effect_status is None:
                raise ValueError("Reconciliation must identify a selected participant")
            if not self.evidence_refs or any(
                re.fullmatch(r"evidence:sha256:[0-9a-f]{64}", ref) is None
                for ref in self.evidence_refs
            ):
                raise ValueError("Reconciliation requires bounded evidence references")
        elif self.reconciled_subplan_digest or self.effect_status or self.evidence_refs:
            raise ValueError("Only participant reconciliation carries effect evidence")
        return self


# Every hyphen-separated word is at least two characters, starts with a letter,
# and carries at most four consecutive digits.  That is a shape constraint, not
# proof that a label is meaningful: it rejects the identifier shapes a provider
# assigns (bare digit runs, ``campaign-88213``, ``act120210000000000``,
# ``g-abc123def``) while still admitting a bounded ordinal such as ``step2``.
# ``adhelm.safety`` applies the same pattern plus the privacy screen before it
# appends.  The pattern avoids look-around, which pydantic's regex engine
# rejects.
_CHILD_LABEL_WORD = r"[a-z](?:(?:[0-9]{0,4}[a-z])+[0-9]{0,4}|[0-9]{1,4})"
_CHILD_LABEL = f"^{_CHILD_LABEL_WORD}(?:-{_CHILD_LABEL_WORD})*$"


class ChildEgressIntent(_StrictModel):
    """Bounded redacted description of one journaled child request.

    It restates only facts the approved subplan already contains: the sorted
    allowlisted egress hosts, a digest of the full declared egress allowlist,
    and a deterministic digest of this child's position inside that subplan.
    No request body, provider response, or provider-assigned identifier is
    recorded here, so the record stays useful for reconciliation without
    becoming a second copy of the mutation payload.

    The one adapter-supplied value in a child record is the sibling
    ``child_label``.  It is not free-form: ``_CHILD_LABEL`` requires every
    hyphen-separated word to be at least two characters, to start with a letter
    and to carry at most four consecutive digits, which admits a step name such
    as ``create-ad-set`` and
    rejects the identifier shapes a provider assigns.  It constrains shape, not
    meaning; a determined caller can still choose an unhelpful step name.
    """

    egress_hosts: list[str] = Field(min_length=1, max_length=16)
    egress_allowlist_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    intent_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("egress_hosts")
    @classmethod
    def sorted_unique_hosts(cls, value: list[str]) -> list[str]:
        if sorted(set(value)) != value or any(
            re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host) is None for host in value
        ):
            raise ValueError("Child egress hosts must be unique, sorted and bounded")
        return value


class _ChildApplyRecord(_StrictModel):
    """Shared identity for a journaled child request inside one subplan attempt."""

    schema_version: Literal[1] = 1
    operation: Literal[
        "CREATE",
        "UPDATE",
        "ASSOCIATE",
        "REPLACE",
        "ACTIVATE",
        "PAUSE",
        "ENABLE",
        "ARCHIVE",
        "DELETE",
        "PUBLISH",
        "RESTORE",
    ]
    resource_ref: str = Field(min_length=1, max_length=160)
    plan_ref: str = Field(min_length=1, max_length=160)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    subplan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_ref: str = Field(min_length=1, max_length=256)
    idempotency_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    child_index: int = Field(ge=1, le=MAX_CHILD_EGRESS_COUNT)
    expected_child_egress_count: int = Field(ge=1, le=MAX_CHILD_EGRESS_COUNT)
    child_label: str = Field(pattern=_CHILD_LABEL, max_length=64)

    @model_validator(mode="after")
    def bounded_child_position(self) -> _ChildApplyRecord:
        if self.child_index > self.expected_child_egress_count:
            raise ValueError("A child index cannot exceed its approved child egress count")
        return self


class ChildApplyStarted(_ChildApplyRecord):
    """Durable pre-egress marker for one child request of a multi-step subplan.

    It is appended and fsynced before the kernel issues that child's one-use
    permit.  A retained start without its matching result is an unresolved
    child: the subplan cannot be retried until reconciliation resolves it.
    """

    effect_status: Literal["NO_REMOTE_WRITE"] = "NO_REMOTE_WRITE"
    intent: ChildEgressIntent


class ChildApplyResult(_ChildApplyRecord):
    """Terminal marker for one journaled child request.

    Only an evidenced ``APPLIED_VERIFIED`` resolves the matching start.  A child
    whose permit was issued may already have reached the provider, so no
    adapter-declared no-effect claim can clear its own pre-egress marker; the
    only other way out is a subplan-level reconciliation of the same attempt.
    """

    effect_status: Literal["APPLIED_VERIFIED", "AMBIGUOUS_REMOTE_STATE"]
    evidence_refs: list[str] = Field(default_factory=list)

    @field_validator("evidence_refs")
    @classmethod
    def bounded_evidence(cls, value: list[str]) -> list[str]:
        if len(value) > 32 or len(set(value)) != len(value):
            raise ValueError("Child evidence references must be unique and bounded")
        if any(re.fullmatch(r"evidence:sha256:[0-9a-f]{64}", ref) is None for ref in value):
            raise ValueError("Child evidence must be opaque SHA-256 evidence references")
        return value

    @model_validator(mode="after")
    def resolved_child_has_evidence(self) -> ChildApplyResult:
        if self.effect_status == "APPLIED_VERIFIED" and not self.evidence_refs:
            raise ValueError("A resolving child result requires bounded evidence")
        return self


class AttemptRecoveryObservation(_StrictModel):
    """Rule 24 recording of the current state after an unattributable attempt.

    A mutating attempt that recorded ``APPLY_STARTED`` and never reached an
    evidenced ``APPLIED_VERIFIED`` result can never be attributed by a later
    read.  Without an exit, one lost acknowledgement would refuse every later
    plan on every provider forever, so this record provides one -- and provides
    exactly one thing.

    It records what was observed, when the observation window opened and
    closed, how the observed state relates to the approved before/after pair,
    and under which authorization the observation was taken.  ``actor_ref`` is
    fixed to ``UNKNOWN`` and ``attempt_effect_status`` is fixed to
    ``AMBIGUOUS_REMOTE_STATE`` by the schema, so this record can never claim who
    caused the state or upgrade the attempt's outcome.  It releases the write
    barrier; it settles nothing about the original request.
    """

    schema_version: Literal[1] = 1
    operation: Literal[
        "CREATE",
        "UPDATE",
        "ASSOCIATE",
        "REPLACE",
        "ACTIVATE",
        "PAUSE",
        "ENABLE",
        "ARCHIVE",
        "DELETE",
        "PUBLISH",
        "RESTORE",
    ]
    resource_ref: str = Field(min_length=1, max_length=160)
    plan_ref: str = Field(min_length=1, max_length=160)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    subplan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_ref: str = Field(min_length=1, max_length=256)
    idempotency_ref: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    recovery_source: Literal["PROVIDER_SETTLEMENT_PROOF", "OPERATOR_ATTESTED_OBSERVATION"]
    recovery_approval_ref: str | None = Field(default=None, max_length=256)
    recovery_authorization_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    recovery_subject_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    observed_state_fingerprint: str = Field(min_length=16, max_length=256)
    observed_state_revision: str | None = Field(default=None, max_length=256)
    observed_relation: Literal["AT_BASELINE", "AT_TARGET", "OTHER"]
    disposition: Literal["RECOVERED_AT_BASELINE", "RECOVERED_AT_TARGET", "RECOVERED_DIVERGED"]
    actor_ref: Literal["UNKNOWN"] = "UNKNOWN"
    attempt_effect_status: Literal["AMBIGUOUS_REMOTE_STATE"] = "AMBIGUOUS_REMOTE_STATE"
    #: When the attempt's durable ``APPLY_STARTED`` was recorded.  The
    #: observation interval closes at this record's own ledger ``recorded_at``:
    #: taking the closing instant from the ledger rather than from the caller
    #: keeps both ends on one clock and stops a supplied end from preceding its
    #: own start.
    observation_interval_start: str
    evidence_refs: list[str] = Field(min_length=1, max_length=32)

    @field_validator("observation_interval_start")
    @classmethod
    def recovery_timestamp(cls, value: str) -> str:
        checked = _timestamp(value, nullable=False)
        if checked is None:
            raise ValueError("A recovery observation needs a bounded timestamp")
        return checked

    @field_validator("evidence_refs")
    @classmethod
    def bounded_evidence(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            re.fullmatch(r"evidence:sha256:[0-9a-f]{64}", ref) is None for ref in value
        ):
            raise ValueError("Recovery evidence must be unique opaque SHA-256 references")
        return value

    @model_validator(mode="after")
    def coherent_recovery(self) -> AttemptRecoveryObservation:
        expected = {
            "AT_BASELINE": "RECOVERED_AT_BASELINE",
            "AT_TARGET": "RECOVERED_AT_TARGET",
            "OTHER": "RECOVERED_DIVERGED",
        }
        if self.disposition != expected[self.observed_relation]:
            raise ValueError("A recovery disposition must restate its observed relation")
        operator = self.recovery_source == "OPERATOR_ATTESTED_OBSERVATION"
        bound = (
            self.recovery_approval_ref is not None
            and self.recovery_authorization_digest is not None
            and self.recovery_subject_digest is not None
        )
        if operator != bound:
            raise ValueError(
                "An operator-attested recovery names its grant, digest and bound subject; "
                "a provider settlement proof names none of them"
            )
        return self


class ChangeEvent(_StrictModel):
    event_type: Literal[
        "PLAN_CREATED",
        "APPROVAL_GRANTED",
        "APPLY_STARTED",
        "APPLY_RESULT",
        "VERIFICATION_RESULT",
        "REMOTE_CHANGE_OBSERVED",
        "RECONCILIATION_RESULT",
        "RECOVERY_STARTED",
        "RECOVERY_RESULT",
        "RECORD_SUPERSEDED",
    ]
    operation: Literal[
        "CREATE",
        "UPDATE",
        "ASSOCIATE",
        "REPLACE",
        "ACTIVATE",
        "PAUSE",
        "ENABLE",
        "ARCHIVE",
        "DELETE",
        "PUBLISH",
        "RESTORE",
        "OBSERVE",
    ]
    origin: Literal["LOCAL_CLI", "PROVIDER_UI", "PROVIDER_AUTOMATION", "IMPORT", "UNKNOWN"]
    resource_type: str
    resource_ref: str
    effect_status: Literal[
        "NO_REMOTE_WRITE",
        "APPLIED_VERIFIED",
        "FAILED_NO_EFFECT",
        "PARTIAL_EFFECT",
        "AMBIGUOUS_REMOTE_STATE",
        "EXTERNAL_CHANGE_DETECTED",
    ]
    parent_resource_refs: list[str] = Field(default_factory=list)
    recommendation_ref: str | None = None
    plan_ref: str | None = None
    plan_digest: str | None = None
    subplan_digest: str | None = None
    approval_ref: str | None = None
    authorization_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    idempotency_ref: str | None = None
    actor_ref: str | None = None
    before_snapshot_ref: str | None = None
    before_digest: str | None = None
    after_snapshot_ref: str | None = None
    after_digest: str | None = None
    field_deltas: list[FieldDelta] = Field(default_factory=list)
    spend_impact_minor: int | None = None
    currency: str | None = None
    provider_api_version: str | None = None
    connector_version: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    observation_interval_start: str | None = None
    observation_interval_end: str | None = None
    superseded_record_ref: str | None = None
    replacement_record_ref: str | None = None
    supersession_reason: str | None = None
    policy_id: str | None = None
    policy_reservation: list[PolicyReservation] = Field(default_factory=list)

    @field_validator("observation_interval_start", "observation_interval_end")
    @classmethod
    def interval_timestamp(cls, value: str | None) -> str | None:
        return _timestamp(value, nullable=True)

    @model_validator(mode="after")
    def policy_claim_is_coherent(self) -> ChangeEvent:
        if self.policy_reservation and self.policy_id is None:
            raise ValueError("A policy reservation requires its policy identifier")
        if self.event_type != "APPLY_STARTED" and (
            self.policy_reservation or self.policy_id is not None
        ):
            raise ValueError("Policy claim fields are only valid at APPLY_STARTED")
        return self


class NormalizedResourceState(_StrictModel):
    """Closed shared scalar state; unsupported provider extensions fail explicitly."""

    status: str | None = None
    name: str | None = None
    objective: str | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    timezone: str | None = None
    budget_minor: int | None = Field(default=None, ge=0, le=9_007_199_254_740_991)
    daily_budget_minor: int | None = Field(default=None, ge=0, le=9_007_199_254_740_991)
    lifetime_budget_minor: int | None = Field(default=None, ge=0, le=9_007_199_254_740_991)
    spend_cap_minor: int | None = Field(default=None, ge=0, le=9_007_199_254_740_991)
    bid_amount_minor: int | None = Field(default=None, ge=0, le=9_007_199_254_740_991)
    bid_strategy: str | None = None
    budget_type: Literal["DAILY", "LIFETIME"] | None = None
    tiktok_ads_budget_mode: (
        Literal["BUDGET_MODE_DAY", "BUDGET_MODE_TOTAL", "BUDGET_MODE_INFINITE"] | None
    ) = None
    openai_ads_lifetime_spend_limit_micros: str | None = Field(
        default=None, pattern=r"^[1-9][0-9]{0,18}$"
    )
    openai_ads_max_bid_micros: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,18}$")
    schedule_start: str | None = None
    schedule_end: str | None = None
    destination_ref: str | None = None
    creative_version_ref: str | None = None
    conversion_ref: str | None = None

    @model_validator(mode="after")
    def minor_amounts_have_currency(self) -> NormalizedResourceState:
        """Keep persisted minor units interpretable after the provider response is gone."""
        if self.currency is None and any(
            value is not None
            for value in (
                self.budget_minor,
                self.daily_budget_minor,
                self.lifetime_budget_minor,
                self.spend_cap_minor,
                self.bid_amount_minor,
                self.openai_ads_lifetime_spend_limit_micros,
                self.openai_ads_max_bid_micros,
            )
        ):
            raise ValueError("Minor-unit amounts require the state currency")
        for value in (
            self.openai_ads_lifetime_spend_limit_micros,
            self.openai_ads_max_bid_micros,
        ):
            if value is not None and int(value) > 9_223_372_036_854_775_807:
                raise ValueError("Native micros exceed the supported local signed-int64 bound")
        for native, projections in (
            (
                self.openai_ads_lifetime_spend_limit_micros,
                (self.budget_minor, self.lifetime_budget_minor),
            ),
            (self.openai_ads_max_bid_micros, (self.bid_amount_minor,)),
        ):
            projected = [amount for amount in projections if amount is not None]
            if native is not None and projected:
                if (
                    self.currency != "USD"
                    or int(native) % 10_000
                    or any(amount != int(native) // 10_000 for amount in projected)
                ):
                    raise ValueError(
                        "OpenAI minor projection must exactly match reviewed native units"
                    )
        return self


class ResourceSnapshot(_StrictModel):
    snapshot_id: str
    provider_api_version: str
    connector_version: str
    resource_type: str
    resource_ref: str
    parent_resource_refs: list[str] = Field(default_factory=list)
    remote_fingerprint: str
    normalized_state: dict[str, Any]
    normalized_state_digest: str
    effective_status: str
    source_updated_at: str | None = None
    data_quality_statuses: list[str] = Field(default_factory=list)

    @field_validator("normalized_state")
    @classmethod
    def normalized_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        NormalizedResourceState.model_validate(value)
        return cast(dict[str, Any], _safe_value(value))

    @model_validator(mode="after")
    def correct_state_digest(self) -> ResourceSnapshot:
        if self.normalized_state_digest != digest(self.normalized_state):
            raise ValueError("Normalized snapshot digest does not match its state")
        return self

    @field_validator("source_updated_at")
    @classmethod
    def snapshot_timestamp(cls, value: str | None) -> str | None:
        return _timestamp(value, nullable=True)


class InventoryReadCompleted(_StrictModel):
    """The completion boundary of one bounded inventory read.

    `run_inventory` normalizes a whole hierarchy before it appends anything,
    but it then appends one `RESOURCE_SNAPSHOT` at a time.  A process that
    stops between two appends leaves a durable prefix that is indistinguishable
    from a finished read by shape alone, so a later reader that assumes the
    prefix is current invents out-of-band deletions for every resource the run
    never reached.

    This marker is appended after the last snapshot of a read and names exactly
    the snapshot records that read produced.  Its absence is meaningful: a
    snapshot set with no marker is a prefix or a legacy read, never a proven
    complete read.  It records no provider payload and authorizes nothing.
    """

    schema_version: Literal[1] = 1
    provider: str = Field(min_length=1, max_length=80)
    account_ref: str = Field(min_length=1, max_length=200)
    snapshot_record_digests: list[str] = Field(min_length=1, max_length=5_000)
    resource_counts: dict[str, int] = Field(min_length=1, max_length=16)
    read_started_at: str
    read_finished_at: str

    @field_validator("snapshot_record_digests")
    @classmethod
    def unique_record_digests(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value) or any(
            re.fullmatch(r"[0-9a-f]{64}", item) is None for item in value
        ):
            raise ValueError("A read completion names unique retained record digests")
        return value

    @field_validator("resource_counts")
    @classmethod
    def bounded_counts(cls, value: dict[str, int]) -> dict[str, int]:
        if any(
            not isinstance(count, int) or isinstance(count, bool) or count < 0
            for count in value.values()
        ):
            raise ValueError("Resource counts are nonnegative integers")
        return value

    @field_validator("read_started_at", "read_finished_at")
    @classmethod
    def read_timestamp(cls, value: str) -> str:
        checked = _timestamp(value)
        if checked is None:
            raise ValueError("A read completion needs bounded timestamps")
        return checked

    @model_validator(mode="after")
    def counts_match_the_named_records(self) -> InventoryReadCompleted:
        if sum(self.resource_counts.values()) != len(self.snapshot_record_digests):
            raise ValueError("Resource counts must total the named snapshot records")
        if self.read_finished_at < self.read_started_at:
            raise ValueError("A read cannot finish before it starts")
        return self


class CreativePayload(_StrictModel):
    schema_version: int = Field(ge=1)
    # A closed, intentionally small superset keeps creative records structured
    # without treating analyzer output or provider media as an opaque blob.
    creative_family_id: str | None = None
    creative_version_id: str | None = None
    asset_version_id: str | None = None
    rendition_id: str | None = None
    deployment_id: str | None = None
    fingerprint_id: str | None = None
    provider_format: str | None = None
    locale: str | None = None
    content_digest: str | None = None
    source_asset_version_ref: str | None = None
    creative_version_ref: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    observed_from: str | None = None
    observed_to: str | None = None
    association_status: str | None = None
    components: list[dict[str, Any]] = Field(default_factory=list)
    palette: list[dict[str, Any]] = Field(default_factory=list)
    provenance: str | None = None
    availability_status: str | None = None
    analyzer_name: str | None = None
    analyzer_version: str | None = None

    @field_validator("components", "palette")
    @classmethod
    def creative_safe(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [_safe_value(row) for row in value]

    @field_validator("effective_from", "effective_to", "observed_from", "observed_to")
    @classmethod
    def creative_timestamp(cls, value: str | None) -> str | None:
        return _timestamp(value, nullable=True)


class SegmentStarted(_StrictModel):
    previous_segment_head_digest: str | None = None
    previous_segment_name: str | None = None


_KINDS: dict[str, dict[str, type[BaseModel]]] = {
    "changes": {
        "APPROVAL_RECEIPT_CLAIMED": ApprovalReceiptClaimRecord,
        "CAMPAIGN_JOIN_MAP_RETAINED": CampaignJoinMap,
        "CONVERSION_CONTRACT": ConversionContract,
        "MEASUREMENT_OBSERVATION": MeasurementObservation,
        CHUNK_KIND: MeasurementObservationChunk,
        CHUNKED_KIND: ChunkedMeasurementObservation,
        "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION": Ga4AdminEditInventoryObservation,
        "GA4_KEY_EVENT_CANDIDATE_COMPILED": Ga4KeyEventCandidateRecord,
        "GA4_KEY_EVENT_CREATE_PLAN": Ga4KeyEventCreatePlanRecord,
        "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN": GoogleAdsCampaignPausePlanRecord,
        "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN": GoogleAdsSearchCampaignCreatePlanRecord,
        "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_READBACK_OBSERVATION": (
            GoogleAdsSearchCampaignCreateReadbackObservation
        ),
        "GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN": GoogleAdsSearchCampaignActivatePlanRecord,
        "GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_READBACK_OBSERVATION": (
            GoogleAdsSearchCampaignActivateReadbackObservation
        ),
        "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_PLAN": GoogleAdsSearchCampaignMaintainPlanRecord,
        "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_READBACK_OBSERVATION": (
            GoogleAdsSearchCampaignMaintainReadbackObservation
        ),
        "GOOGLE_ADS_CAMPAIGN_PAUSE_READBACK_OBSERVATION": GoogleAdsCampaignPauseReadbackObservation,
        "OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN": OpenAIAdsChatCardHierarchyCreatePlanRecord,
        "OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_READBACK_OBSERVATION": (
            OpenAIAdsChatCardHierarchyCreateReadbackObservation
        ),
        "META_CAMPAIGN_PAUSE_PLAN": MetaCampaignPausePlanRecord,
        "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION": MetaCampaignPauseReadbackObservation,
        "META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_PLAN": (
            MetaAdsSingleImageHierarchyCreatePlanRecord
        ),
        "META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_READBACK_OBSERVATION": (
            MetaAdsSingleImageHierarchyCreateReadbackObservation
        ),
        "CANDIDATE_COMPILED": CandidateCompilationRecord,
        "STRATEGY_CANDIDATE_COMPILED": StrategyCandidateCompilationRecord,
        "EXECUTION_PLAN": OpenAIPauseExecutionPlanRecord,
        "EXECUTION_PLAN_RETAINED": ExecutionPlanRecord,
        "COMPOSITE_RUN": CompositeRunRecord,
        "CHILD_APPLY_STARTED": ChildApplyStarted,
        "CHILD_APPLY_RESULT": ChildApplyResult,
        "ATTEMPT_RECOVERY_OBSERVED": AttemptRecoveryObservation,
        "CHANGE_EVENT": ChangeEvent,
        "RESOURCE_SNAPSHOT": ResourceSnapshot,
        "INVENTORY_READ_COMPLETED": InventoryReadCompleted,
        "SEGMENT_STARTED": SegmentStarted,
    },
    "performance": {
        "PERFORMANCE_OBSERVATION": PerformanceObservationPayload,
        "SEGMENT_STARTED": SegmentStarted,
    },
    "creative": {
        "CREATIVE_FAMILY": CreativeFamilyRecord,
        "CREATIVE_ASSET_VERSION": CreativeAssetVersion,
        "CREATIVE_ASSET_ANALYSIS": AssetAnalysisRecord,
        "CREATIVE_RENDITION": CreativeRendition,
        "CREATIVE_VERSION": CreativeVersionRecord,
        "CREATIVE_FINGERPRINT": ImageFingerprintRecord,
        "CREATIVE_DEPLOYMENT": CreativeDeployment,
        "SEGMENT_STARTED": SegmentStarted,
    },
}


class Ledger:
    """Canonical local ledger for one profile.

    ``root`` is the profile data directory.  Index data is disposable and is
    always derived from these stream files.
    """

    def __init__(
        self,
        root: Path,
        profile_ref: str = "default",
        *,
        segment_bytes: int = DEFAULT_SEGMENT_BYTES,
    ) -> None:
        if not profile_ref or "/" in profile_ref or "\\" in profile_ref:
            raise AdhelmError(
                "INVALID_PROFILE_REF",
                "Profile reference is not a safe local identifier.",
                exit_code=2,
            )
        self.root = Path(root)
        self.profile_ref = profile_ref
        self.segment_bytes = self._validated_segment_bytes(segment_bytes)
        self.ledger_root = self.root / "ledger"

    def append(
        self,
        stream: str,
        record_kind: str,
        payload: dict[str, Any],
        *,
        provider: str | None = None,
        account_ref: str | None = None,
        run_id: str | None = None,
        source: str = "LOCAL_CLI",
        occurred_at: str | None = None,
        observed_at: str | None = None,
        _before_durable_append: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        self._validate_stream(stream)
        if source not in SOURCES:
            raise AdhelmError(
                "INVALID_LEDGER_SOURCE", "Record source is not allowlisted.", exit_code=2
            )
        validated_payload = self._validate_payload(stream, record_kind, payload, provider=provider)
        self._validate_candidate_binding(
            record_kind, validated_payload, provider, account_ref, source, run_id
        )
        self._validate_strategy_compilation_binding(
            record_kind, provider, account_ref, source, run_id, observed_at, occurred_at
        )
        self._validate_approval_receipt_binding(
            record_kind,
            validated_payload,
            self.profile_ref,
            provider,
            account_ref,
            source,
            run_id,
            observed_at,
            occurred_at,
        )
        self._validate_conversion_binding(record_kind, provider, account_ref, source, run_id)
        self._validate_campaign_join_binding(
            record_kind,
            validated_payload,
            provider,
            account_ref,
            source,
            run_id,
            observed_at,
            occurred_at,
        )
        self._validate_ga4_key_event_candidate_envelope(
            record_kind,
            validated_payload,
            provider,
            account_ref,
            source,
            run_id,
            observed_at,
            occurred_at,
        )
        self._validate_measurement_binding(
            record_kind,
            validated_payload,
            provider,
            account_ref,
            source,
            run_id,
            observed_at,
            occurred_at,
        )
        self._validate_execution_plan_binding(
            record_kind,
            validated_payload,
            self.profile_ref,
            provider,
            account_ref,
            source,
            run_id,
            observed_at,
            occurred_at,
        )
        validate_ga4_create_plan_envelope(
            record_kind,
            validated_payload,
            self.profile_ref,
            provider,
            account_ref,
            source,
            run_id,
            observed_at,
            occurred_at,
        )
        validate_meta_pause_plan_envelope(
            record_kind,
            validated_payload,
            self.profile_ref,
            provider,
            account_ref,
            source,
            run_id,
            observed_at,
            occurred_at,
        )
        validate_google_ads_pause_plan_envelope(
            record_kind,
            validated_payload,
            self.profile_ref,
            provider,
            account_ref,
            source,
            run_id,
            observed_at,
            occurred_at,
        )
        validate_execution_plan_envelope(
            record_kind,
            validated_payload,
            self.profile_ref,
            provider,
            account_ref,
            source,
            run_id,
            observed_at,
            occurred_at,
        )
        self._ensure_dirs(stream)
        with self._append_state(stream, record_kind, validated_payload) as state:
            if stream == "changes":
                if record_kind == "GOOGLE_ADS_CAMPAIGN_PAUSE_READBACK_OBSERVATION":
                    validate_google_ads_pause_readback_history(
                        [
                            *state["record_list"],
                            {
                                "record_kind": record_kind,
                                "payload": validated_payload,
                                "provider": provider,
                                "account_ref": account_ref,
                                "profile_ref": self.profile_ref,
                                "run_id": run_id,
                                "source": source,
                                "observed_at": observed_at,
                                "occurred_at": occurred_at,
                            },
                        ]
                    )
                if record_kind == "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_READBACK_OBSERVATION":
                    validate_google_ads_create_readback_history(
                        [
                            *state["record_list"],
                            {
                                "record_kind": record_kind,
                                "payload": validated_payload,
                                "provider": provider,
                                "account_ref": account_ref,
                                "profile_ref": self.profile_ref,
                                "run_id": run_id,
                                "source": source,
                                "observed_at": observed_at,
                                "occurred_at": occurred_at,
                            },
                        ]
                    )
                if record_kind == "OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_READBACK_OBSERVATION":
                    validate_openai_create_readback_history(
                        [
                            *state["record_list"],
                            {
                                "record_kind": record_kind,
                                "payload": validated_payload,
                                "provider": provider,
                                "account_ref": account_ref,
                                "profile_ref": self.profile_ref,
                                "run_id": run_id,
                                "source": source,
                                "observed_at": observed_at,
                                "occurred_at": occurred_at,
                            },
                        ]
                    )
                if record_kind == "GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_READBACK_OBSERVATION":
                    validate_google_ads_activate_readback_history(
                        [
                            *state["record_list"],
                            {
                                "record_kind": record_kind,
                                "payload": validated_payload,
                                "provider": provider,
                                "account_ref": account_ref,
                                "profile_ref": self.profile_ref,
                                "run_id": run_id,
                                "source": source,
                                "observed_at": observed_at,
                                "occurred_at": occurred_at,
                            },
                        ]
                    )
                if record_kind == "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_READBACK_OBSERVATION":
                    validate_google_ads_maintain_readback_history(
                        [
                            *state["record_list"],
                            {
                                "record_kind": record_kind,
                                "payload": validated_payload,
                                "provider": provider,
                                "account_ref": account_ref,
                                "profile_ref": self.profile_ref,
                                "run_id": run_id,
                                "source": source,
                                "observed_at": observed_at,
                                "occurred_at": occurred_at,
                            },
                        ]
                    )
                if record_kind == "META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_READBACK_OBSERVATION":
                    validate_meta_create_readback_history(
                        [
                            *state["record_list"],
                            {
                                "record_kind": record_kind,
                                "payload": validated_payload,
                                "provider": provider,
                                "account_ref": account_ref,
                                "profile_ref": self.profile_ref,
                                "run_id": run_id,
                                "source": source,
                                "observed_at": observed_at,
                                "occurred_at": occurred_at,
                            },
                        ]
                    )
                if record_kind == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION":
                    validate_meta_pause_readback_history(
                        [
                            *state["record_list"],
                            {
                                "record_kind": record_kind,
                                "payload": validated_payload,
                                "provider": provider,
                                "account_ref": account_ref,
                                "profile_ref": self.profile_ref,
                                "run_id": run_id,
                                "source": source,
                                "observed_at": observed_at,
                                "occurred_at": occurred_at,
                            },
                        ]
                    )
                if record_kind == CHUNKED_KIND:
                    # The scan that built `state` already verified the earlier
                    # history, so only the new commit is resolved.
                    validate_chunked_measurement_history(
                        [
                            *state["record_list"],
                            {
                                "record_kind": record_kind,
                                "payload": validated_payload,
                                "provider": provider,
                                "account_ref": account_ref,
                                "observed_at": observed_at,
                            },
                        ],
                        verified=len(state["record_list"]),
                    )
                validate_ga4_create_start_history(
                    [
                        *state["record_list"],
                        {
                            "record_kind": record_kind,
                            "payload": validated_payload,
                            "provider": provider,
                            "account_ref": account_ref,
                            "run_id": run_id,
                            "source": source,
                        },
                    ]
                )
                self._validate_approval_receipt_history(
                    [
                        *state["record_list"],
                        {"record_kind": record_kind, "payload": validated_payload},
                    ]
                )
                self._validate_conversion_history(
                    [
                        *state["record_list"],
                        {"record_kind": record_kind, "payload": validated_payload},
                    ]
                )
                self._validate_campaign_join_history(
                    [
                        *state["record_list"],
                        {"record_kind": record_kind, "payload": validated_payload},
                    ]
                )
                self._validate_ga4_key_event_candidate_history(
                    [
                        *state["record_list"],
                        {"record_kind": record_kind, "payload": validated_payload},
                    ]
                )
                validate_ga4_create_plan_history(
                    [
                        *state["record_list"],
                        {"record_kind": record_kind, "payload": validated_payload},
                    ]
                )
                validate_execution_plan_history(
                    [
                        *state["record_list"],
                        {"record_kind": record_kind, "payload": validated_payload},
                    ]
                )
                self._validate_openai_snapshot_history(
                    [
                        *state["record_list"],
                        {
                            "record_kind": record_kind,
                            "provider": provider,
                            "account_ref": account_ref,
                            "payload": validated_payload,
                        },
                    ]
                )
            if stream == "creative":
                validate_creative_history_graph(
                    [
                        *state["record_list"],
                        {"record_kind": record_kind, "payload": validated_payload},
                    ]
                )
            if record_kind == "PERFORMANCE_OBSERVATION":
                prior = [
                    item
                    for item in state["record_list"]
                    if item["record_kind"] == record_kind
                    and item["payload"]["observation_key"] == validated_payload["observation_key"]
                ]
                previous = prior[-1] if prior else None
                revision = previous["payload"]["observation_revision"] + 1 if previous else 1
                supersedes = previous["record_id"] if previous else None
                if (
                    validated_payload["observation_revision"] != revision
                    or validated_payload.get("supersedes_record_id") != supersedes
                ):
                    raise AdhelmError(
                        "OBSERVATION_REVISION_CONFLICT",
                        "Concurrent or invalid report revision; reload the source checkpoint.",
                    )
            self._require_current_receipt_claim(record_kind, validated_payload)
            segment = self._writable_segment(stream, state)
            if segment is None:
                segment = self._new_segment(stream, state)
                state = self._scan_stream(stream)
            record = self._make_record(
                stream,
                record_kind,
                validated_payload,
                sequence=state["last_sequence"] + 1,
                previous_digest=state["head_digest"],
                provider=provider,
                account_ref=account_ref,
                run_id=run_id,
                source=source,
                occurred_at=occurred_at,
                observed_at=observed_at,
            )
            encoded = canonical_bytes(record) + b"\n"
            if len(encoded) > MAX_RECORD_BYTES:
                raise AdhelmError(
                    "LEDGER_RECORD_TOO_LARGE",
                    "Record exceeds the bounded ledger line size.",
                    exit_code=2,
                )
            # Rotate before writing the record, except for an empty segment.
            if (
                segment.stat().st_size
                and segment.stat().st_size + len(encoded) > self.segment_bytes
            ):
                segment = self._new_segment(stream, state)
                state = self._scan_stream(stream)
                record = self._make_record(
                    stream,
                    record_kind,
                    validated_payload,
                    sequence=state["last_sequence"] + 1,
                    previous_digest=state["head_digest"],
                    provider=provider,
                    account_ref=account_ref,
                    run_id=run_id,
                    source=source,
                    occurred_at=occurred_at,
                    observed_at=observed_at,
                )
                encoded = canonical_bytes(record) + b"\n"
            self._require_current_receipt_claim(record_kind, validated_payload)
            # Internal authorization handoff only. The callback must be bounded
            # and must not acquire/read this ledger while its stream lock is held.
            if _before_durable_append is not None:
                _before_durable_append()
            self._append_bytes(segment, encoded)
            return record

    def append_creative_registration(
        self,
        closure: CreativeRegistrationClosure | CreativeTextRegistrationClosure | dict[str, Any],
    ) -> dict[str, Any]:
        """Append one prevalidated local creative identity graph under one lock.

        This is deliberately the only multi-record creative append surface.  It
        accepts a source asset and analysis that are already durable, validates
        their exact record digests, then appends only missing dependency records
        in a fixed order.  An interrupted prior registration can therefore
        recover exact staging records without permitting a general batch append.
        """
        checked = revalidate_creative_registration_closure(closure)
        durable_write_started = False
        try:
            self._ensure_dirs("creative")
            with self._stream_lock("creative"):
                state = self._scan_stream("creative")
                records = state["record_list"]
                if isinstance(checked, CreativeRegistrationClosure):
                    self._require_creative_registration_source(
                        records,
                        record_digest=checked.source_asset_record_digest,
                        record_kind="CREATIVE_ASSET_VERSION",
                        payload=checked.asset.model_dump(mode="json", warnings="error"),
                    )
                    self._require_creative_registration_source(
                        records,
                        record_digest=checked.source_analysis_record_digest,
                        record_kind="CREATIVE_ASSET_ANALYSIS",
                        payload=checked.asset_analysis.model_dump(mode="json", warnings="error"),
                    )
                proposed = closure_records(checked)
                pending: list[tuple[str, dict[str, Any]]] = []
                existing_digests: dict[str, str] = {}
                for kind, model in proposed:
                    payload = model.model_dump(mode="json", warnings="error")
                    identity = self._creative_identity(kind, payload)
                    matching = [
                        item
                        for item in records
                        if item["record_kind"] == kind
                        and self._creative_identity(kind, item["payload"]) == identity
                    ]
                    if matching:
                        expected = canonical_bytes(payload)
                        if any(canonical_bytes(item["payload"]) != expected for item in matching):
                            raise AdhelmError(
                                "CREATIVE_REGISTRATION_CONFLICT",
                                "A retained creative identity conflicts with the "
                                "registration graph.",
                                exit_code=3,
                            )
                        existing_digests[kind] = matching[-1]["record_digest"]
                    else:
                        pending.append((kind, payload))
                validate_creative_history_graph(
                    [
                        *records,
                        *({"record_kind": kind, "payload": payload} for kind, payload in pending),
                    ]
                )
                appended_digests: dict[str, str] = {}
                for kind, payload in pending:
                    # The append/fsync boundary has no durable rollback.  Set
                    # this before the first child write so an error from the
                    # write itself, a later child, lock release, or checkpoint
                    # cannot be misreported as a retryable preflight failure.
                    durable_write_started = True
                    record, state = self._append_local_creative_record_locked(kind, payload, state)
                    appended_digests[kind] = record["record_digest"]

            checkpoint = self.checkpoint()
            all_digests = {**existing_digests, **appended_digests}
            if not pending:
                status = "ALREADY_REGISTERED"
            elif any(kind != "CREATIVE_FAMILY" for kind in existing_digests):
                status = "RECOVERED_STAGING"
            else:
                status = "REGISTERED"
            result: dict[str, Any] = {
                "status": status,
                "checkpoint_digest": checkpoint["checkpoint_digest"],
                "creative_family_id": checked.family.creative_family_id,
                "creative_version_id": checked.version.creative_version_id,
                "record_digests": {kind: all_digests[kind] for kind, _ in closure_records(checked)},
            }
            if isinstance(checked, CreativeRegistrationClosure):
                result.update(
                    {
                        "source_asset_record_digest": checked.source_asset_record_digest,
                        "source_analysis_record_digest": checked.source_analysis_record_digest,
                        "asset_version_id": checked.asset.asset_version_id,
                        "asset_analysis_id": checked.asset_analysis.asset_analysis_id,
                        "rendition_id": checked.rendition.rendition_id,
                        "fingerprint_id": checked.fingerprint.fingerprint_id,
                    }
                )
            return result
        except Exception:
            if durable_write_started:
                raise AdhelmError(
                    "CREATIVE_REGISTRATION_UNVERIFIED",
                    "Local creative registration could not be verified after retention.",
                    exit_code=6,
                    next_action="Inspect creative history before any deliberate retry.",
                ) from None
            raise

    @staticmethod
    def _creative_identity(kind: str, payload: dict[str, Any]) -> str:
        fields = {
            "CREATIVE_FAMILY": "creative_family_id",
            "CREATIVE_ASSET_VERSION": "asset_version_id",
            "CREATIVE_ASSET_ANALYSIS": "asset_analysis_id",
            "CREATIVE_RENDITION": "rendition_id",
            "CREATIVE_VERSION": "creative_version_id",
            "CREATIVE_FINGERPRINT": "fingerprint_id",
            "CREATIVE_DEPLOYMENT": "deployment_id",
        }
        field = fields.get(kind)
        identity = payload.get(field) if field is not None else None
        if type(identity) is not str:
            raise AdhelmError(
                "INVALID_CREATIVE_HISTORY", "Creative record identity is invalid.", exit_code=2
            )
        return identity

    @staticmethod
    def _require_creative_registration_source(
        records: list[dict[str, Any]],
        *,
        record_digest: str,
        record_kind: str,
        payload: dict[str, Any],
    ) -> None:
        matches = [
            item
            for item in records
            if item["record_digest"] == record_digest and item["record_kind"] == record_kind
        ]
        if len(matches) != 1 or canonical_bytes(matches[0]["payload"]) != canonical_bytes(payload):
            raise AdhelmError(
                "CREATIVE_REGISTRATION_SOURCE_CHANGED",
                "The selected local creative analysis or asset is unavailable or changed.",
                exit_code=3,
            )

    def _append_local_creative_record_locked(
        self, kind: str, payload: dict[str, Any], state: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Append one closed local creative record while its stream lock is held."""
        validated = self._validate_payload("creative", kind, payload, provider=None)
        segment = self._writable_segment("creative", state)
        if segment is None:
            segment = self._new_segment("creative", state)
            state = self._scan_stream("creative")
        record = self._make_record(
            "creative",
            kind,
            validated,
            sequence=state["last_sequence"] + 1,
            previous_digest=state["head_digest"],
            provider=None,
            account_ref=None,
            run_id=None,
            source="LOCAL_CLI",
            occurred_at=None,
            observed_at=None,
        )
        encoded = canonical_bytes(record) + b"\n"
        if len(encoded) > MAX_RECORD_BYTES:
            raise AdhelmError(
                "LEDGER_RECORD_TOO_LARGE",
                "Record exceeds the bounded ledger line size.",
                exit_code=2,
            )
        if segment.stat().st_size and segment.stat().st_size + len(encoded) > self.segment_bytes:
            segment = self._new_segment("creative", state)
            state = self._scan_stream("creative")
            record = self._make_record(
                "creative",
                kind,
                validated,
                sequence=state["last_sequence"] + 1,
                previous_digest=state["head_digest"],
                provider=None,
                account_ref=None,
                run_id=None,
                source="LOCAL_CLI",
                occurred_at=None,
                observed_at=None,
            )
            encoded = canonical_bytes(record) + b"\n"
        self._append_bytes(segment, encoded)
        return record, self._scan_stream("creative")

    @contextmanager
    def _append_state(
        self, stream: str, record_kind: str, payload: dict[str, Any]
    ) -> Iterator[dict[str, Any]]:
        """Bind history-sensitive retention to its source state under ordered locks.

        A pre-append caller checkpoint cannot exclude a concurrent append. GA4
        CREATE plans and performance observations therefore hold the same
        complete lock set as checkpoint() while their source references are
        checked; ordinary appends retain their stream lock. This check runs
        before any automatic segment rotation and never calls checkpoint()
        recursively or performs I/O in the final callback.
        """
        guarded = record_kind in {"GA4_KEY_EVENT_CREATE_PLAN", "PERFORMANCE_OBSERVATION"}
        selected_streams = sorted(STREAMS) if guarded else [stream]
        for selected in selected_streams:
            self._ensure_dirs(selected)
        with ExitStack() as locks:
            for selected in selected_streams:
                lock = self._stream_lock(selected)
                lock.acquire()
                locks.callback(lock.release)
            states = {selected: self._scan_stream(selected) for selected in selected_streams}
            if guarded:
                self._validate_cross_stream_references(states)
            if record_kind == "PERFORMANCE_OBSERVATION":
                prospective = dict(states)
                prospective["performance"] = {
                    **states["performance"],
                    "record_list": [
                        *states["performance"]["record_list"],
                        {
                            "_prospective": True,
                            "record_kind": record_kind,
                            "payload": payload,
                        },
                    ],
                }
                self._validate_cross_stream_references(prospective)
            if record_kind == "GA4_KEY_EVENT_CREATE_PLAN":
                vector = {
                    selected: {
                        "stream_id": self._stream_id(selected),
                        "records": states[selected]["records"],
                        "high_watermark": states[selected]["last_sequence"],
                        "head_digest": states[selected]["head_digest"],
                        "segments": states[selected]["segments"],
                    }
                    for selected in selected_streams
                }
                if digest(vector) != payload["action"]["selection_binding"]["checkpoint_digest"]:
                    raise AdhelmError(
                        "GA4_CREATE_PLAN_HISTORY_CHANGED",
                        "Local history changed before the CREATE plan could be retained.",
                        exit_code=3,
                    )
            yield states[stream]

    def verify(self, stream: str | None = None) -> dict[str, Any]:
        streams = [stream] if stream is not None else sorted(STREAMS)
        report: dict[str, Any] = {"status": "VERIFIED", "streams": {}}
        global_ids: set[str] = set()
        for item in streams:
            self._validate_stream(item)
            state = self._scan_stream(item)
            if stream is None:
                for record in state["record_list"]:
                    if record["record_id"] in global_ids:
                        raise AdhelmError(
                            "LEDGER_DUPLICATE_RECORD",
                            "Ledger record identifier is duplicated across streams.",
                        )
                    global_ids.add(record["record_id"])
            report["streams"][item] = {
                "stream_id": self._stream_id(item),
                "records": state["records"],
                "high_watermark": state["last_sequence"],
                "head_digest": state["head_digest"],
                "segments": state["segments"],
            }
        return report

    def records(self, stream: str, *, as_of: int | None = None) -> list[dict[str, Any]]:
        self._validate_stream(stream)
        state = self._scan_stream(stream)
        if as_of is not None and (as_of < 0 or as_of > state["last_sequence"]):
            raise AdhelmError(
                "INVALID_HIGH_WATERMARK", "Requested high-watermark is unavailable.", exit_code=2
            )
        return [
            record
            for record in state["record_list"]
            if as_of is None or record["stream_sequence"] <= as_of
        ]

    def unresolved_starts(self) -> list[dict[str, Any]]:
        """Return provider mutations whose durable start has no resolving record.

        A start is resolved by an evidenced ``APPLIED_VERIFIED`` terminal event,
        by a reconciliation that proved a known effect, or by an
        ``ATTEMPT_RECOVERY_OBSERVED`` record for the exact attempt.  The last of
        those releases the write barrier without settling or attributing the
        attempt; see :class:`AttemptRecoveryObservation`.
        """

        unresolved: dict[str, dict[str, Any]] = {}
        terminal = {"VERIFICATION_RESULT", "RECONCILIATION_RESULT", "RECOVERY_RESULT"}
        for record in self.records("changes"):
            if record["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED":
                self._resolve_recovered_start(unresolved, record)
                continue
            if record["record_kind"] != "CHANGE_EVENT":
                continue
            payload = record["payload"]
            key = (
                payload.get("idempotency_ref")
                or payload.get("subplan_digest")
                or payload.get("plan_digest")
            )
            if not key:
                continue
            if payload["event_type"] == "APPLY_STARTED":
                unresolved[key] = record
            elif payload["event_type"] in terminal:
                start = unresolved.get(key)
                if start is None:
                    continue
                original = start["payload"]
                matching_identity = all(
                    record.get(field) == start.get(field)
                    for field in ("provider", "account_ref", "run_id")
                ) and all(
                    payload.get(field) == original.get(field)
                    for field in (
                        "operation",
                        "resource_ref",
                        "approval_ref",
                        "plan_digest",
                        "subplan_digest",
                        "idempotency_ref",
                    )
                )
                if not matching_identity or not payload.get("evidence_refs"):
                    continue
                proven = payload.get("effect_status") == "APPLIED_VERIFIED"
                reconciled_known_effect = (
                    payload["event_type"] == "RECONCILIATION_RESULT"
                    and payload.get("effect_status") in {"FAILED_NO_EFFECT", "PARTIAL_EFFECT"}
                    and bool(payload.get("evidence_refs"))
                )
                if proven or reconciled_known_effect:
                    unresolved.pop(key, None)
        return list(unresolved.values())

    @staticmethod
    def _resolve_recovered_start(
        unresolved: dict[str, dict[str, Any]], record: dict[str, Any]
    ) -> None:
        payload = record["payload"]
        start = unresolved.get(payload["idempotency_ref"])
        if start is None:
            return
        original = start["payload"]
        if all(
            record.get(field) == start.get(field) for field in ("provider", "account_ref", "run_id")
        ) and all(
            payload.get(field) == original.get(field)
            for field in ("operation", "resource_ref", "plan_digest", "subplan_digest")
        ):
            unresolved.pop(payload["idempotency_ref"], None)

    def unresolved_child_starts(self) -> list[dict[str, Any]]:
        """Return journaled child egress attempts with no resolving terminal record.

        This mirrors ``unresolved_starts`` one level down.  A multi-step subplan
        appends ``CHILD_APPLY_STARTED`` and fsyncs it before each child request,
        so a crash between two children leaves a durable start with no result.
        Only a matching ``CHILD_APPLY_RESULT`` that proves the child applied
        resolves it; a later subplan-level ``RECONCILIATION_RESULT`` or
        ``ATTEMPT_RECOVERY_OBSERVED`` record for the same attempt resolves every
        child of that attempt, because releasing the parent attempt is the only
        other documented way out.
        """

        identity = (
            "operation",
            "resource_ref",
            "plan_ref",
            "plan_digest",
            "subplan_digest",
            "approval_ref",
            "idempotency_ref",
            "expected_child_egress_count",
            "child_label",
        )
        unresolved: dict[tuple[str, str, int], dict[str, Any]] = {}
        for record in self.records("changes"):
            kind = record["record_kind"]
            payload = record["payload"]
            if kind == "CHILD_APPLY_STARTED":
                key = (
                    payload["idempotency_ref"],
                    payload["subplan_digest"],
                    payload["child_index"],
                )
                unresolved[key] = record
            elif kind == "CHILD_APPLY_RESULT":
                key = (
                    payload["idempotency_ref"],
                    payload["subplan_digest"],
                    payload["child_index"],
                )
                start = unresolved.get(key)
                if start is None:
                    continue
                original = start["payload"]
                if not all(
                    record.get(field) == start.get(field)
                    for field in ("provider", "account_ref", "run_id")
                ) or not all(payload.get(field) == original.get(field) for field in identity):
                    continue
                if payload["effect_status"] == "APPLIED_VERIFIED" and payload["evidence_refs"]:
                    unresolved.pop(key, None)
            elif (
                kind == "ATTEMPT_RECOVERY_OBSERVED"
                or kind == "CHANGE_EVENT"
                and payload.get("event_type") == "RECONCILIATION_RESULT"
                and payload.get("evidence_refs")
            ):
                for key, start in list(unresolved.items()):
                    original = start["payload"]
                    if (
                        payload.get("idempotency_ref") == original["idempotency_ref"]
                        and payload.get("subplan_digest") == original["subplan_digest"]
                        and payload.get("plan_digest") == original["plan_digest"]
                        and payload.get("approval_ref") == original["approval_ref"]
                    ):
                        unresolved.pop(key, None)
        return list(unresolved.values())

    def unresolved_runs(self) -> list[dict[str, Any]]:
        """A partially executed composite remains blocked until every participant is read."""
        records = self.records("changes")
        opened: dict[str, dict[str, Any]] = {}
        reconciled: dict[str, set[str]] = {}
        for record in records:
            if record["record_kind"] != "COMPOSITE_RUN":
                continue
            payload = record["payload"]
            key = payload["plan_digest"]
            if payload["event_type"] == "RUN_STARTED":
                opened[key] = record
                reconciled[key] = set()
                continue
            original = opened.get(key)
            if original is None:
                continue
            expected = original["payload"]
            if record["run_id"] != original["run_id"] or any(
                payload[field] != expected[field]
                for field in ("approval_ref", "authorization_digest", "participants")
            ):
                continue
            proofs = [
                r
                for r in records
                if original["stream_sequence"] < r["stream_sequence"] < record["stream_sequence"]
                and r["record_kind"] == "CHANGE_EVENT"
                and r["run_id"] == original["run_id"]
                and r["payload"].get("plan_digest") == key
                and r["payload"].get("approval_ref") == payload["approval_ref"]
                and r["payload"].get("evidence_refs")
            ]
            if payload["event_type"] == "PROVIDER_RECONCILED":
                participant = next(
                    p
                    for p in payload["participants"]
                    if p["subplan_digest"] == payload["reconciled_subplan_digest"]
                )
                if not any(
                    r["payload"]["event_type"] == "RECONCILIATION_RESULT"
                    and r["payload"]["effect_status"] == payload["effect_status"]
                    and r["payload"].get("subplan_digest") == participant["subplan_digest"]
                    and r["payload"].get("resource_ref") == participant["resource_ref"]
                    and r["payload"].get("evidence_refs") == payload["evidence_refs"]
                    and r["provider"] == participant["provider"]
                    and r["account_ref"] == participant["account_ref"]
                    for r in proofs
                ):
                    continue
                reconciled[key].add(payload["reconciled_subplan_digest"])
                if reconciled[key] == {p["subplan_digest"] for p in payload["participants"]}:
                    opened.pop(key)
            elif payload["event_type"] == "RUN_VERIFIED":
                # A closing assertion cannot replace every provider's durable verified result.
                complete = all(
                    any(
                        r["payload"]["event_type"] == "VERIFICATION_RESULT"
                        and r["payload"]["effect_status"] == "APPLIED_VERIFIED"
                        and r["payload"].get("subplan_digest") == p["subplan_digest"]
                        and r["payload"].get("resource_ref") == p["resource_ref"]
                        and r["provider"] == p["provider"]
                        and r["account_ref"] == p["account_ref"]
                        for r in proofs
                    )
                    for p in payload["participants"]
                )
                if complete:
                    opened.pop(key)
        for key, record in list(opened.items()):
            # A participant whose unattributable attempt was recorded and
            # released is read for barrier purposes, exactly like one that
            # reconciled. Its effect status is still not settled.
            payload = record["payload"]
            covered = reconciled.get(key, set()) | {
                item["payload"]["subplan_digest"]
                for item in records
                if item["record_kind"] == "ATTEMPT_RECOVERY_OBSERVED"
                and item["run_id"] == record["run_id"]
                and item["payload"]["plan_digest"] == key
                and item["payload"]["approval_ref"] == payload["approval_ref"]
                and any(
                    participant["subplan_digest"] == item["payload"]["subplan_digest"]
                    and participant["provider"] == item["provider"]
                    and participant["account_ref"] == item["account_ref"]
                    and participant["resource_ref"] == item["payload"]["resource_ref"]
                    for participant in payload["participants"]
                )
            }
            if covered >= {p["subplan_digest"] for p in payload["participants"]}:
                opened.pop(key)
        return list(opened.values())

    def checkpoint(self) -> dict[str, Any]:
        """Select one cross-stream, verified ledger vector under ordered locks.

        The returned records are canonical inputs for reports, indexes, and
        projections.  They must not be mixed with later ``records()`` reads.
        """
        for stream in sorted(STREAMS):
            self._ensure_dirs(stream)
        with ExitStack() as locks:
            for stream in sorted(STREAMS):
                lock = self._stream_lock(stream)
                lock.acquire()
                locks.callback(lock.release)
            states = {stream: self._scan_stream(stream) for stream in sorted(STREAMS)}
            self._validate_cross_stream_references(states)
            streams = {
                stream: {
                    "stream_id": self._stream_id(stream),
                    "records": states[stream]["records"],
                    "high_watermark": states[stream]["last_sequence"],
                    "head_digest": states[stream]["head_digest"],
                    "segments": states[stream]["segments"],
                }
                for stream in sorted(STREAMS)
            }
            vector = {stream: streams[stream] for stream in sorted(STREAMS)}
            return {
                "status": "VERIFIED_CHECKPOINT",
                "checkpoint_digest": digest(vector),
                "streams": streams,
                "records_by_stream": {
                    stream: list(states[stream]["record_list"]) for stream in sorted(STREAMS)
                },
            }

    def rebuild_index(self) -> dict[str, Any]:
        """Recreate the disposable index from exactly one verified checkpoint."""
        for stream in sorted(STREAMS):
            self._ensure_dirs(stream)
        with self._index_lock():
            checkpoint = self.checkpoint()
            self._reject_symlink(self.ledger_root / "index.sqlite3")
            database = self.ledger_root / "index.sqlite3"
            temp = self.ledger_root / f".index-{uuid.uuid4().hex}.rebuild"
            connection = sqlite3.connect(temp)
            try:
                connection.executescript(
                    """
                    PRAGMA journal_mode=DELETE;
                    CREATE TABLE metadata (
                      key TEXT PRIMARY KEY, value TEXT NOT NULL
                    );
                    CREATE TABLE records (
                      stream TEXT NOT NULL, sequence INTEGER NOT NULL,
                      record_id TEXT NOT NULL UNIQUE, record_kind TEXT NOT NULL,
                      record_digest TEXT NOT NULL, payload_json TEXT NOT NULL,
                      recorded_at TEXT NOT NULL, PRIMARY KEY(stream, sequence)
                    );
                    CREATE TABLE watermarks (
                      stream TEXT PRIMARY KEY, high_watermark INTEGER NOT NULL,
                      head_digest TEXT
                    );
                    """
                )
                with connection:
                    connection.executemany(
                        "INSERT INTO metadata VALUES (?, ?)",
                        [
                            ("schema_version", "1"),
                            ("checkpoint_digest", checkpoint["checkpoint_digest"]),
                        ],
                    )
                    for stream in sorted(STREAMS):
                        records = checkpoint["records_by_stream"][stream]
                        connection.executemany(
                            "INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?)",
                            [
                                (
                                    stream,
                                    record["stream_sequence"],
                                    record["record_id"],
                                    record["record_kind"],
                                    record["record_digest"],
                                    canonical_bytes(record["payload"]).decode("utf-8"),
                                    record["recorded_at"],
                                )
                                for record in records
                            ],
                        )
                        watermark = checkpoint["streams"][stream]
                        connection.execute(
                            "INSERT INTO watermarks VALUES (?, ?, ?)",
                            (stream, watermark["high_watermark"], watermark["head_digest"]),
                        )
                connection.close()
                self._fsync_file(temp)
                os.replace(temp, database)
                self._fsync_directory(self.ledger_root)
            finally:
                connection.close()
                if temp.exists():
                    temp.unlink()
        return {
            "status": "REBUILT",
            "path": str(database),
            "checkpoint_digest": checkpoint["checkpoint_digest"],
            "watermarks": checkpoint["streams"],
        }

    def snapshot_diff(
        self,
        snapshot: dict[str, Any],
        *,
        provider: str | None = None,
        account_ref: str | None = None,
    ) -> dict[str, Any]:
        """Compare a normalized snapshot to the newest matching local snapshot.

        The caller appends the returned change event only after a provider read;
        this helper never invents an exact external change time or actor.
        """
        validated = self._validate_payload(
            "changes", "RESOURCE_SNAPSHOT", snapshot, provider=provider
        )
        prior: dict[str, Any] | None = None
        for record in reversed(self.records("changes")):
            if record["record_kind"] != "RESOURCE_SNAPSHOT":
                continue
            if provider is not None and record["provider"] != provider:
                continue
            if account_ref is not None and record["account_ref"] != account_ref:
                continue
            candidate = record["payload"]
            if (
                candidate["provider_api_version"] == validated["provider_api_version"]
                and candidate["resource_ref"] == validated["resource_ref"]
            ):
                prior = record
                break
        if (
            prior is None
            or prior["payload"]["normalized_state_digest"] == validated["normalized_state_digest"]
        ):
            return {
                "changed": False,
                "previous_record_id": None if prior is None else prior["record_id"],
            }
        return {
            "changed": True,
            "previous_record_id": prior["record_id"],
            "change_payload": {
                "event_type": "REMOTE_CHANGE_OBSERVED",
                "operation": "OBSERVE",
                "origin": "UNKNOWN",
                "resource_type": validated["resource_type"],
                "resource_ref": validated["resource_ref"],
                "effect_status": "EXTERNAL_CHANGE_DETECTED",
                "before_snapshot_ref": prior["record_id"],
                "before_digest": prior["payload"]["normalized_state_digest"],
                "after_digest": validated["normalized_state_digest"],
                "observation_interval_start": prior["observed_at"],
                "observation_interval_end": _utc_now(),
            },
        }

    def _validate_stream(self, stream: str) -> None:
        if stream not in STREAMS:
            raise AdhelmError("INVALID_LEDGER_STREAM", "Unknown ledger stream.", exit_code=2)

    @staticmethod
    def _require_reference(reference: str | None, available: set[str]) -> None:
        if reference is not None and reference not in available:
            raise AdhelmError(
                "CHECKPOINT_MISSING_REFERENCE",
                "Cross-stream reference is unavailable at the selected ledger checkpoint.",
            )

    def _validate_cross_stream_references(self, states: dict[str, dict[str, Any]]) -> None:
        """Reject reports that point at absent creative history instead of guessing a join."""
        creative = states["creative"]["record_list"]
        families: set[str] = set()
        assets: set[str] = set()
        renditions: set[str] = set()
        versions: set[str] = set()
        deployments: set[str] = set()
        for record in creative:
            payload = record["payload"]
            kind = record["record_kind"]
            if kind == "CREATIVE_FAMILY":
                families.add(payload["creative_family_id"])
            elif kind == "CREATIVE_ASSET_VERSION":
                assets.add(payload["asset_version_id"])
            elif kind == "CREATIVE_ASSET_ANALYSIS":
                self._require_reference(payload["asset_version_id"], assets)
            elif kind == "CREATIVE_RENDITION":
                if payload["source_relation_status"] == "PROVEN_SOURCE_ASSET":
                    self._require_reference(payload["source_asset_version_ref"], assets)
                renditions.add(payload["rendition_id"])
            elif kind == "CREATIVE_VERSION":
                self._require_reference(payload["creative_family_id"], families)
                for rendition in payload["rendition_refs"]:
                    self._require_reference(rendition["rendition_id"], renditions)
                versions.add(payload["creative_version_id"])
            elif kind == "CREATIVE_FINGERPRINT":
                self._require_reference(payload["asset_version_id"], assets)
                self._require_reference(payload["creative_version_id"], versions)
                self._require_reference(payload["rendition_id"], renditions)
            elif kind == "CREATIVE_DEPLOYMENT":
                self._require_reference(payload["creative_family_id"], families)
                self._require_reference(payload["creative_version_id"], versions)
                for rendition_ref in payload["rendition_refs"]:
                    self._require_reference(rendition_ref, renditions)
                deployments.add(payload["deployment_id"])
        for record in states["changes"]["record_list"]:
            if record["record_kind"] != "RESOURCE_SNAPSHOT":
                continue
            self._require_reference(
                record["payload"]["normalized_state"].get("creative_version_ref"), versions
            )
        for record in states["performance"]["record_list"]:
            if record["record_kind"] != "PERFORMANCE_OBSERVATION":
                continue
            source_watermarks = self._validate_report_source_checkpoints(record, states)
            creative_prefix = [
                item
                for item in creative
                if item["stream_sequence"] <= source_watermarks["creative"]
            ]
            validate_creative_history_graph(creative_prefix)
            source_families = {
                item["payload"]["creative_family_id"]
                for item in creative_prefix
                if item["record_kind"] == "CREATIVE_FAMILY"
            }
            source_versions = {
                item["payload"]["creative_version_id"]: item["payload"]
                for item in creative_prefix
                if item["record_kind"] == "CREATIVE_VERSION"
            }
            source_deployments = {
                item["payload"]["deployment_id"]: item["payload"]
                for item in creative_prefix
                if item["record_kind"] == "CREATIVE_DEPLOYMENT"
            }
            source_renditions = {
                item["payload"]["rendition_id"]
                for item in creative_prefix
                if item["record_kind"] == "CREATIVE_RENDITION"
            }
            envelope = record["payload"]["report_envelope"]
            for row in record["payload"]["performance_rows"]:
                self._require_reference(row.get("creative_family_id"), source_families)
                self._require_reference(row.get("creative_version_id"), set(source_versions))
                self._require_reference(row.get("creative_deployment_id"), set(source_deployments))
                self._require_reference(row.get("served_rendition_id"), source_renditions)
                self._validate_performance_creative_association(
                    row,
                    envelope,
                    source_versions,
                    source_deployments,
                )

    @staticmethod
    def _validate_performance_creative_association(
        row: dict[str, Any],
        envelope: dict[str, Any],
        versions: dict[str, dict[str, Any]],
        deployments: dict[str, dict[str, Any]],
    ) -> None:
        """Keep a reported creative tuple bound to one retained association.

        Absence remains an explicit unmapped fact.  When the report supplies a
        version, rendition, family, or deployment reference, the supplied facts
        must agree rather than independently pointing at unrelated history.
        """
        version_ref = row.get("creative_version_id")
        deployment_ref = row.get("creative_deployment_id")
        family_ref = row.get("creative_family_id")
        rendition_ref = row.get("served_rendition_id")
        version = versions.get(version_ref) if type(version_ref) is str else None
        deployment = deployments.get(deployment_ref) if type(deployment_ref) is str else None
        if version is not None:
            if family_ref is not None and family_ref != version["creative_family_id"]:
                raise AdhelmError(
                    "PERFORMANCE_CREATIVE_ASSOCIATION_INVALID",
                    "Performance creative references do not match one retained version.",
                    exit_code=2,
                )
            if rendition_ref is not None and rendition_ref not in {
                item["rendition_id"] for item in version["rendition_refs"]
            }:
                raise AdhelmError(
                    "PERFORMANCE_CREATIVE_ASSOCIATION_INVALID",
                    "Performance rendition does not belong to the retained creative version.",
                    exit_code=2,
                )
        if deployment is None:
            return
        if (
            deployment["provider"] != envelope["provider"]
            or deployment["account_ref"] != envelope["source_account_ref"]
            or deployment["ad_ref"] != row["resource_ref"]
            or (version_ref is not None and version_ref != deployment["creative_version_id"])
            or (family_ref is not None and family_ref != deployment["creative_family_id"])
            or (rendition_ref is not None and rendition_ref not in deployment["rendition_refs"])
        ):
            raise AdhelmError(
                "PERFORMANCE_CREATIVE_ASSOCIATION_INVALID",
                "Performance deployment does not match its retained provider, account, "
                "ad, or creative facts.",
                exit_code=2,
            )

    def _validate_report_source_checkpoints(
        self, record: dict[str, Any], states: dict[str, dict[str, Any]]
    ) -> dict[str, int]:
        checkpoints = record["payload"]["report_envelope"].get("source_ledger_checkpoints", [])
        stream_by_id = {self._stream_id(stream): stream for stream in STREAMS}
        expected_ids = set(stream_by_id)
        if len(checkpoints) != len(STREAMS):
            raise AdhelmError(
                "CHECKPOINT_INVALID_REPORT_SOURCE",
                "Performance observation lacks the complete source checkpoint vector.",
            )
        supplied_ids = {item["stream_id"] for item in checkpoints}
        if supplied_ids != expected_ids or len(supplied_ids) != len(checkpoints):
            raise AdhelmError(
                "CHECKPOINT_INVALID_REPORT_SOURCE",
                "Performance observation has an invalid or duplicate source stream checkpoint.",
            )
        prefixes: dict[str, dict[int, str | None]] = {}
        for stream in STREAMS:
            prefixes[stream] = {0: None}
            for item in states[stream]["record_list"]:
                if item.get("_prospective") is True:
                    continue
                prefixes[stream][item["stream_sequence"]] = item["record_digest"]
        for item in checkpoints:
            stream = stream_by_id[item["stream_id"]]
            sequence = item["high_watermark_sequence"]
            if prefixes[stream].get(sequence, object()) != item["head_digest"]:
                raise AdhelmError(
                    "CHECKPOINT_INVALID_REPORT_SOURCE",
                    "Performance observation source checkpoint is not a verified retained prefix.",
                )
        return {
            stream_by_id[item["stream_id"]]: item["high_watermark_sequence"] for item in checkpoints
        }

    @staticmethod
    def _openai_resource_ref_matches(resource_type: str, resource_ref: Any) -> bool:
        prefix = _OPENAI_RESOURCE_PREFIXES.get(resource_type)
        if (
            prefix is None
            or not isinstance(resource_ref, str)
            or not resource_ref.startswith(prefix)
        ):
            return False
        return _OPENAI_REF_SUFFIX.fullmatch(resource_ref.removeprefix(prefix)) is not None

    @staticmethod
    def _invalid_openai_snapshot(message: str) -> NoReturn:
        raise AdhelmError("INVALID_LEDGER_PAYLOAD", message, exit_code=2)

    def _validate_openai_snapshot_shape(self, payload: dict[str, Any]) -> None:
        """Validate the provider namespace before any snapshot enters history."""
        resource_type = payload["resource_type"]
        resource_ref = payload["resource_ref"]
        if not isinstance(resource_type, str) or resource_type not in _OPENAI_RESOURCE_PREFIXES:
            self._invalid_openai_snapshot(
                "OpenAI Ads snapshots require a reviewed native resource type."
            )
        if not self._openai_resource_ref_matches(resource_type, resource_ref):
            self._invalid_openai_snapshot(
                "OpenAI Ads snapshot resource reference does not match its native namespace."
            )
        parents = payload["parent_resource_refs"]
        expected_parent_types = _OPENAI_PARENT_TYPES[resource_type]
        if not isinstance(parents, list) or len(parents) != len(expected_parent_types):
            self._invalid_openai_snapshot(
                "OpenAI Ads snapshot parent references do not match its native hierarchy."
            )
        if any(
            not self._openai_resource_ref_matches(parent_type, parent_ref)
            for parent_type, parent_ref in zip(expected_parent_types, parents, strict=True)
        ):
            self._invalid_openai_snapshot(
                "OpenAI Ads snapshot parent reference does not match its native namespace."
            )
        state = payload["normalized_state"]
        if not isinstance(state.get("currency"), str):
            self._invalid_openai_snapshot(
                "OpenAI Ads snapshots require the discovered account currency."
            )
        lifetime_micros = state.get("openai_ads_lifetime_spend_limit_micros")
        bid_micros = state.get("openai_ads_max_bid_micros")
        if lifetime_micros is not None and resource_type != "CAMPAIGN":
            self._invalid_openai_snapshot(
                "OpenAI lifetime budget micros are valid only on campaign snapshots."
            )
        if bid_micros is not None and resource_type != "AD_GROUP":
            self._invalid_openai_snapshot(
                "OpenAI maximum bid micros are valid only on ad-group snapshots."
            )
        if lifetime_micros is not None and (
            state.get("budget_type") != "LIFETIME" or state.get("daily_budget_minor") is not None
        ):
            self._invalid_openai_snapshot(
                "OpenAI lifetime budget state must remain a lifetime amount without a daily "
                "projection."
            )

    def _validate_openai_snapshot_history(self, records: list[dict[str, Any]]) -> None:
        """Require retained OpenAI snapshots to remain one currency-consistent tree.

        Snapshot records are appended parent-first by inventory collection.  Requiring
        the parent to exist before a child prevents a stale or forged child from
        acquiring a parent only later in a rewritten retained stream.
        """
        current: dict[str, dict[str, Any]] = {}
        for record in records:
            if (
                record.get("record_kind") != "RESOURCE_SNAPSHOT"
                or record.get("provider") != "openai_ads"
            ):
                continue
            payload = record["payload"]
            self._validate_openai_snapshot_shape(payload)
            parents = payload["parent_resource_refs"]
            expected_parent_types = _OPENAI_PARENT_TYPES[payload["resource_type"]]
            for parent_type, parent_ref in zip(expected_parent_types, parents, strict=True):
                parent = current.get(parent_ref)
                if parent is None or parent["payload"]["resource_type"] != parent_type:
                    self._invalid_openai_snapshot(
                        "OpenAI Ads snapshot parent is absent from the retained hierarchy."
                    )
                if (
                    payload["normalized_state"]["currency"]
                    != parent["payload"]["normalized_state"]["currency"]
                ):
                    self._invalid_openai_snapshot(
                        "OpenAI Ads snapshot currency disagrees with its retained parent."
                    )
            if payload["resource_type"] == "AD":
                campaign_ref, ad_group_ref = parents
                ad_group = current[ad_group_ref]
                if ad_group["payload"]["parent_resource_refs"] != [campaign_ref]:
                    self._invalid_openai_snapshot(
                        "OpenAI Ads ad campaign and ad-group parents disagree."
                    )
            current[payload["resource_ref"]] = record

        for record in current.values():
            payload = record["payload"]
            resource_type = payload["resource_type"]
            parents = payload["parent_resource_refs"]
            expected_parent_types = _OPENAI_PARENT_TYPES[resource_type]
            root = record
            for parent_type, parent_ref in zip(expected_parent_types, parents, strict=True):
                parent = current.get(parent_ref)
                if parent is None or parent["payload"]["resource_type"] != parent_type:
                    self._invalid_openai_snapshot(
                        "OpenAI Ads retained snapshot hierarchy is incomplete."
                    )
                if (
                    payload["normalized_state"]["currency"]
                    != parent["payload"]["normalized_state"]["currency"]
                ):
                    self._invalid_openai_snapshot(
                        "OpenAI Ads retained snapshot currency disagrees with its parent."
                    )
                root = parent
            if resource_type == "AD":
                campaign_ref, ad_group_ref = parents
                ad_group = current[ad_group_ref]
                if ad_group["payload"]["parent_resource_refs"] != [campaign_ref]:
                    self._invalid_openai_snapshot(
                        "OpenAI Ads retained ad campaign and ad-group parents disagree."
                    )
            while root["payload"]["resource_type"] != "ACCOUNT":
                root = current[root["payload"]["parent_resource_refs"][0]]
            account_ref = record.get("account_ref")
            if account_ref is not None:
                root_ref = root["payload"]["resource_ref"]
                root_client_ref = root_ref.removeprefix(_OPENAI_RESOURCE_PREFIXES["ACCOUNT"])
                if account_ref != root_client_ref:
                    self._invalid_openai_snapshot(
                        "OpenAI Ads snapshot account binding disagrees with its retained account."
                    )

    def _validate_payload(
        self,
        stream: str,
        kind: str,
        payload: dict[str, Any],
        *,
        provider: str | None = None,
        allow_legacy_creative: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AdhelmError(
                "INVALID_LEDGER_PAYLOAD", "Record payload must be an object.", exit_code=2
            )
        model = _KINDS[stream].get(kind)
        if model is None:
            raise AdhelmError(
                "INVALID_RECORD_KIND", "Record kind is not allowed in this stream.", exit_code=2
            )
        try:
            validated_model = (
                revalidate_ga4_key_event_candidate_record(payload).model_dump(mode="json")
                if kind == "GA4_KEY_EVENT_CANDIDATE_COMPILED"
                else revalidate_ga4_key_event_create_plan_record(payload).model_dump(mode="json")
                if kind == "GA4_KEY_EVENT_CREATE_PLAN"
                else revalidate_meta_campaign_pause_plan_record(payload).model_dump(mode="json")
                if kind == "META_CAMPAIGN_PAUSE_PLAN"
                else revalidate_google_ads_campaign_pause_plan_record(payload).model_dump(
                    mode="json"
                )
                if kind == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
                else revalidate_google_ads_search_campaign_create_plan_record(payload).model_dump(
                    mode="json"
                )
                if kind == "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN"
                else revalidate_google_ads_search_campaign_activate_plan_record(payload).model_dump(
                    mode="json"
                )
                if kind == "GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN"
                else revalidate_google_ads_search_campaign_maintain_plan_record(payload).model_dump(
                    mode="json"
                )
                if kind == "GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_PLAN"
                else revalidate_openai_ads_create_plan_record(payload).model_dump(mode="json")
                if kind == "OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN"
                else revalidate_meta_ads_create_plan_record(payload).model_dump(mode="json")
                if kind == "META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_PLAN"
                else revalidate_execution_plan_record(payload).model_dump(mode="json")
                if kind == "EXECUTION_PLAN_RETAINED"
                else revalidate_creative_payload(kind, payload)
                if stream == "creative" and kind != "SEGMENT_STARTED"
                else validate_json_value(model, payload)
            )
            if isinstance(validated_model, BaseModel):
                if (
                    stream == "creative"
                    and kind != "SEGMENT_STARTED"
                    and is_legacy_creative_payload(validated_model)
                    and not allow_legacy_creative
                ):
                    raise AdhelmError(
                        "CREATIVE_HISTORY_MIGRATION_REQUIRED",
                        "New creative writes require the current payload schema.",
                        exit_code=3,
                        next_action=(
                            "Inspect retained history; migration for new use is not installed."
                        ),
                    )
                validated = validated_model.model_dump(mode="json", warnings="error")
            else:
                validated = validated_model
            validate_data(validated)
            if (
                stream == "changes"
                and kind == "RESOURCE_SNAPSHOT"
                and validated["normalized_state"].get("tiktok_ads_budget_mode") is not None
                and provider != "tiktok_ads"
            ):
                raise AdhelmError(
                    "INVALID_LEDGER_PAYLOAD",
                    "TikTok budget mode is only valid for TikTok Ads snapshots.",
                    exit_code=2,
                )
            if stream == "changes" and kind == "RESOURCE_SNAPSHOT" and provider != "openai_ads":
                references = [
                    validated["resource_ref"],
                    *validated["parent_resource_refs"],
                ]
                if any(reference.startswith("openai_ads.") for reference in references):
                    raise AdhelmError(
                        "INVALID_LEDGER_PAYLOAD",
                        "OpenAI Ads resource namespaces require the OpenAI Ads provider.",
                        exit_code=2,
                    )
                if any(
                    validated["normalized_state"].get(field) is not None
                    for field in (
                        "openai_ads_lifetime_spend_limit_micros",
                        "openai_ads_max_bid_micros",
                    )
                ):
                    raise AdhelmError(
                        "INVALID_LEDGER_PAYLOAD",
                        "OpenAI native micros are only valid for OpenAI Ads snapshots.",
                        exit_code=2,
                    )
            if stream == "changes" and kind == "RESOURCE_SNAPSHOT" and provider == "openai_ads":
                self._validate_openai_snapshot_shape(validated)
            if stream == "performance" and kind == "PERFORMANCE_OBSERVATION":
                report = NormalizedReport.model_validate_json(
                    canonical_bytes(
                        {
                            "envelope": validated["report_envelope"],
                            "performance_rows": validated["performance_rows"],
                            "conversion_rows": validated["conversion_rows"],
                        }
                    )
                )
                if validated["source_report_digest"] != digest(report):
                    raise AdhelmError(
                        "INVALID_REPORT_PROVENANCE",
                        "Performance observation source digest does not match its normalized "
                        "report.",
                        exit_code=2,
                    )
            return validated
        except AdhelmError as exc:
            if stream == "creative":
                if exc.code == "CREATIVE_HISTORY_MIGRATION_REQUIRED":
                    raise
                raise AdhelmError(
                    "INVALID_LEDGER_PAYLOAD",
                    "Creative record does not match its closed history schema.",
                    exit_code=2,
                ) from None
            raise
        except ValidationError as exc:
            raise _adhelm_validation(exc) from exc

    @staticmethod
    def _validate_conversion_binding(
        kind: str,
        provider: str | None,
        account_ref: str | None,
        source: str,
        run_id: str | None,
    ) -> None:
        if kind == "CONVERSION_CONTRACT" and (
            provider is not None
            or account_ref is not None
            or source != "LOCAL_CLI"
            or run_id is not None
        ):
            raise AdhelmError(
                "INVALID_CONVERSION_PROVENANCE",
                "A conversion definition records local declarations, not provider "
                "observations or execution authority.",
                exit_code=2,
            )

    @staticmethod
    def _validate_conversion_history(records: list[dict[str, Any]]) -> None:
        latest: dict[str, dict[str, Any]] = {}
        for record in records:
            if record["record_kind"] != "CONVERSION_CONTRACT":
                continue
            contract = record["payload"]
            definition = contract["definition"]
            prior = latest.get(definition["contract_id"])
            if prior is None:
                valid = definition["version"] == 1 and definition["supersedes_ref"] is None
            else:
                previous = prior["definition"]
                valid = (
                    definition["version"] == previous["version"] + 1
                    and definition["supersedes_ref"] == prior["content_digest"]
                    and datetime.fromisoformat(definition["effective_from"])
                    > datetime.fromisoformat(previous["effective_from"])
                )
            if not valid:
                raise AdhelmError(
                    "CONVERSION_VERSION_CONFLICT",
                    "Conversion definitions must extend the latest retained version with "
                    "its exact digest and a later effective time.",
                    next_action="Inspect conversion catalog/show and compile a new version "
                    "against the latest retained definition.",
                )
            latest[definition["contract_id"]] = contract

    @staticmethod
    def _validate_campaign_join_binding(
        kind: str,
        payload: dict[str, Any],
        provider: str | None,
        account_ref: str | None,
        source: str,
        run_id: str | None,
        observed_at: str | None,
        occurred_at: str | None,
    ) -> None:
        """Pin a retained join map to the GA4 property its contract declares.

        A join map is a local customer declaration about one GA4 property.  It
        is not a provider observation and carries no execution authority, so an
        envelope naming another provider, another account, an execution run, or
        a remote observation time is refused rather than retained.
        """
        if kind != "CAMPAIGN_JOIN_MAP_RETAINED":
            return
        declared = payload["contract"]["ga4_property"]["property_ref"]
        if (
            provider != "ga4"
            or account_ref != declared
            or source != "LOCAL_CLI"
            or run_id is not None
            or observed_at is not None
            or occurred_at is not None
        ):
            raise AdhelmError(
                "INVALID_CAMPAIGN_JOIN_PROVENANCE",
                "A retained campaign join map must bind the GA4 property it declares and a "
                "local declaration; it cannot claim a remote observation or execution run.",
                exit_code=2,
            )

    @staticmethod
    def _validate_campaign_join_history(records: list[dict[str, Any]]) -> None:
        """Refuse a forked join-contract chain, at append and over history.

        This scan runs under the changes-stream lock at append and over every
        verified historical prefix, so one contract reference has exactly one
        version-one declaration and every later version extends the latest
        retained one with its exact content digest and a later effective time.
        A competing registration is refused instead of shadowing a retained
        declaration that no chain selector could then reach.
        """
        latest: dict[str, dict[str, Any]] = {}
        for record in records:
            if record["record_kind"] != "CAMPAIGN_JOIN_MAP_RETAINED":
                continue
            join_map = record["payload"]
            contract = join_map["contract"]
            prior = latest.get(contract["contract_id"])
            if prior is None:
                valid = contract["version"] == 1 and contract["supersedes_ref"] is None
            else:
                previous = prior["contract"]
                valid = (
                    contract["version"] == previous["version"] + 1
                    and contract["supersedes_ref"] == prior["content_digest"]
                    and datetime.fromisoformat(contract["effective_from"])
                    > datetime.fromisoformat(previous["effective_from"])
                    and contract["ga4_property"] == previous["ga4_property"]
                    and sorted(contract["provider_accounts"], key=canonical_bytes)
                    == sorted(previous["provider_accounts"], key=canonical_bytes)
                )
            if not valid:
                raise AdhelmError(
                    "JOIN_MAP_CHAIN_INVALID",
                    (
                        "A join map for this contract reference is already retained; a "
                        "further version must extend the latest retained one with its exact "
                        "content digest, a later effective time, and the same GA4 property "
                        "and advertising accounts."
                        if prior is not None
                        else "The first join map for a contract reference must be version "
                        "one and supersede nothing."
                    ),
                    next_action="Inspect measurement join catalog and supersede the latest "
                    "retained declaration, or declare a new contract reference.",
                )
            latest[contract["contract_id"]] = join_map

    @staticmethod
    def _validate_ga4_key_event_candidate_envelope(
        kind: str,
        payload: dict[str, Any],
        provider: str | None,
        account_ref: str | None,
        source: str,
        run_id: str | None,
        observed_at: str | None,
        occurred_at: str | None,
    ) -> None:
        if kind != "GA4_KEY_EVENT_CANDIDATE_COMPILED":
            return
        record = revalidate_ga4_key_event_candidate_record(payload)
        if (
            provider != "ga4_admin"
            or account_ref != record.compilation.compiler_candidate.identity.property_ref
            or source != "LOCAL_CLI"
            or run_id is not None
            or observed_at is not None
            or occurred_at is not None
        ):
            raise AdhelmError(
                "INVALID_GA4_KEY_EVENT_CANDIDATE_PROVENANCE",
                "A GA4 candidate must bind its declared property and local compilation; "
                "it cannot claim a remote observation or execution run.",
                exit_code=2,
            )

    @staticmethod
    def _validate_ga4_key_event_candidate_history(records: list[dict[str, Any]]) -> None:
        # This scan runs under the changes-stream lock at append, and over
        # every verified historical prefix. A newer contract blocks a stale
        # new candidate without invalidating an earlier historical candidate.
        latest: dict[str, dict[str, Any]] = {}
        for record in records:
            if record["record_kind"] == "CONVERSION_CONTRACT":
                latest[record["payload"]["definition"]["contract_id"]] = record
            elif record["record_kind"] == "GA4_KEY_EVENT_CANDIDATE_COMPILED":
                candidate = revalidate_ga4_key_event_candidate_record(record["payload"])
                binding = candidate.compilation.conversion_binding
                source = latest.get(binding.contract_id)
                if source is None or source["record_digest"] != (
                    binding.conversion_contract_record_digest
                ):
                    raise AdhelmError(
                        "GA4_KEY_EVENT_CANDIDATE_CONTRACT_CONFLICT",
                        "Candidate compilation requires the exact latest preceding "
                        "retained conversion definition.",
                        exit_code=2,
                        next_action="Inspect conversion catalog/show and compile against "
                        "the latest retained definition.",
                    )
                contract = ConversionContract.model_validate_json(
                    canonical_bytes(source["payload"])
                )
                validate_ga4_key_event_candidate_binding(
                    candidate, contract, source["record_digest"]
                )

    @staticmethod
    def _validate_measurement_binding(
        kind: str,
        payload: dict[str, Any],
        provider: str | None,
        account_ref: str | None,
        source: str,
        run_id: str | None,
        observed_at: str | None,
        occurred_at: str | None,
    ) -> None:
        if kind == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION":
            edit_observation = validate_json_value(Ga4AdminEditInventoryObservation, payload)
            expected_provider = "ga4_admin"
            expected_account = edit_observation.inventory.target.property_ref
            finished = edit_observation.read_finished_at
        elif kind == "MEASUREMENT_OBSERVATION":
            observation = validate_json_value(MeasurementObservation, payload)
            expected_provider = observation.snapshot.provider
            expected_account = observation.snapshot.account_ref
            finished = observation.read_finished_at
        elif kind == CHUNKED_KIND:
            commit = validate_json_value(ChunkedMeasurementObservation, payload)
            expected_provider = commit.provider
            expected_account = commit.account_ref
            finished = commit.read_finished_at
        elif kind == CHUNK_KIND:
            # The payload is already schema-validated. A chunk has no account
            # of its own: the commit that names it binds the account, and
            # history validation requires the two envelopes to agree.
            if (
                provider != payload["provider"]
                or account_ref is None
                or source != "UNKNOWN"
                or run_id is not None
                or occurred_at is not None
                or observed_at is None
            ):
                raise AdhelmError(
                    "INVALID_MEASUREMENT_PROVENANCE",
                    "Measurement chunks must bind their provider and account; external "
                    "change time, actor and execution authority are not established.",
                    exit_code=2,
                )
            return
        else:
            return
        if (
            provider != expected_provider
            or account_ref != expected_account
            or source != "UNKNOWN"
            or run_id is not None
            or occurred_at is not None
            or observed_at is None
            or _timestamp(observed_at) != _timestamp(finished.isoformat())
        ):
            raise AdhelmError(
                "INVALID_MEASUREMENT_PROVENANCE",
                "Measurement history must bind its provider, account and read interval; "
                "external change time, actor and execution authority are not established.",
                exit_code=2,
            )

    @staticmethod
    def _validate_candidate_binding(
        kind: str,
        payload: dict[str, Any],
        provider: str | None,
        account_ref: str | None,
        source: str,
        run_id: str | None,
    ) -> None:
        if kind == "CANDIDATE_COMPILED" and (
            provider != payload["compilation"]["provider"]
            or account_ref != payload["compilation"]["account_ref"]
            or source != "LOCAL_CLI"
            or run_id is not None
        ):
            raise AdhelmError(
                "INVALID_CANDIDATE_PROVENANCE",
                "Candidate history must match its local provider and account declaration and "
                "cannot join an execution run.",
                exit_code=2,
            )

    @staticmethod
    def _require_current_receipt_claim(kind: str, payload: dict[str, Any]) -> None:
        """Only new claims use current time; historical verification does not."""
        if kind != "APPROVAL_RECEIPT_CLAIMED":
            return
        now = datetime.fromisoformat(_utc_now().replace("Z", "+00:00"))
        accepted = datetime.fromisoformat(payload["accepted_at"].replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(payload["receipt"]["expires_at"].replace("Z", "+00:00"))
        if accepted > now or now >= expiry:
            raise AdhelmError(
                "APPROVAL_RECEIPT_CLAIM_EXPIRED",
                "Receipt consumption requires a current receipt and a non-future local "
                "acceptance time at the durable append handoff.",
                exit_code=4,
            )

    @staticmethod
    def _validate_approval_receipt_binding(
        kind: str,
        payload: dict[str, Any],
        profile_ref: str,
        provider: str | None,
        account_ref: str | None,
        source: str,
        run_id: str | None,
        observed_at: str | None,
        occurred_at: str | None,
    ) -> None:
        if kind == "APPROVAL_RECEIPT_CLAIMED" and (
            payload["workspace_profile_ref"] != profile_ref
            or provider is not None
            or account_ref is not None
            or source != "LOCAL_CLI"
            or run_id is not None
            or observed_at is not None
            or occurred_at is not None
        ):
            raise AdhelmError(
                "INVALID_APPROVAL_RECEIPT_PROVENANCE",
                "Approval receipt consumption belongs to one local workspace and cannot "
                "claim a provider event or execution run.",
                exit_code=2,
            )

    @staticmethod
    def _validate_approval_receipt_history(records: list[dict[str, Any]]) -> None:
        """Consume receipt/nonces atomically in the canonical changes stream.

        Called with the prospective append under that stream's append lock and
        during history verification. It never uses the rebuildable index.
        """
        plans: dict[str, dict[str, Any]] = {}
        ga4_create_plans: dict[str, dict[str, Any]] = {}
        meta_pause_plans: dict[str, dict[str, Any]] = {}
        consumed_nonces: set[str] = set()
        consumed_receipts: set[str] = set()
        consumed_identifiers: set[str] = set()
        for record in records:
            kind = record["record_kind"]
            payload = record["payload"]
            if kind == "EXECUTION_PLAN" and "record_digest" in record:
                plans[record["record_digest"]] = payload
            if kind == "GA4_KEY_EVENT_CREATE_PLAN" and "record_digest" in record:
                ga4_create_plans[record["record_digest"]] = payload
            if kind == "META_CAMPAIGN_PAUSE_PLAN" and "record_digest" in record:
                meta_pause_plans[record["record_digest"]] = payload
            if kind != "APPROVAL_RECEIPT_CLAIMED":
                continue
            nonce = payload["challenge_nonce_digest"]
            receipt = payload["receipt_digest"]
            identifier = payload["receipt_id"]
            if (
                nonce in consumed_nonces
                or receipt in consumed_receipts
                or identifier in consumed_identifiers
            ):
                raise AdhelmError(
                    "APPROVAL_RECEIPT_REPLAY",
                    "This workspace has already consumed the receipt or its challenge nonce.",
                    exit_code=4,
                )
            if payload["receipt_kind"] == "OPENAI_PAUSE_PLAN":
                source = plans.get(payload["source_plan_record_digest"])
                if source is None or canonical_bytes(source["action"]) != canonical_bytes(
                    payload["challenge"]["action"]
                ):
                    raise AdhelmError(
                        "APPROVAL_SOURCE_PLAN_MISMATCH",
                        "The receipt must bind an earlier exact retained pause action "
                        "in this workspace.",
                        exit_code=4,
                    )
            elif payload["receipt_kind"] == "GA4_KEY_EVENT_CREATE_PLAN":
                source = ga4_create_plans.get(payload["source_plan_record_digest"])
                if source is None or canonical_bytes(source["action"]) != canonical_bytes(
                    payload["challenge"]["action"]
                ):
                    raise AdhelmError(
                        "APPROVAL_SOURCE_PLAN_MISMATCH",
                        "The receipt must bind an earlier exact retained GA4 CREATE action "
                        "in this workspace.",
                        exit_code=4,
                    )
            elif payload["receipt_kind"] == "META_CAMPAIGN_PAUSE_PLAN":
                source = meta_pause_plans.get(payload["source_plan_record_digest"])
                if source is None or canonical_bytes(source["action"]) != canonical_bytes(
                    payload["challenge"]["action"]
                ):
                    raise AdhelmError(
                        "APPROVAL_SOURCE_PLAN_MISMATCH",
                        "The receipt must bind an earlier exact retained Meta PAUSE action "
                        "in this workspace.",
                        exit_code=4,
                    )
            consumed_nonces.add(nonce)
            consumed_receipts.add(receipt)
            consumed_identifiers.add(identifier)

    @staticmethod
    def _validate_strategy_compilation_binding(
        kind: str,
        provider: str | None,
        account_ref: str | None,
        source: str,
        run_id: str | None,
        observed_at: str | None,
        occurred_at: str | None,
    ) -> None:
        if kind == "STRATEGY_CANDIDATE_COMPILED" and (
            provider is not None
            or account_ref is not None
            or source != "LOCAL_CLI"
            or run_id is not None
            or observed_at is not None
            or occurred_at is not None
        ):
            raise AdhelmError(
                "INVALID_STRATEGY_CANDIDATE_PROVENANCE",
                "A retained strategy is one local compilation observation; it cannot claim "
                "a provider, account, execution run, or remote event time.",
                exit_code=2,
            )

    @staticmethod
    def _validate_execution_plan_binding(
        kind: str,
        payload: dict[str, Any],
        profile_ref: str,
        provider: str | None,
        account_ref: str | None,
        source: str,
        run_id: str | None,
        observed_at: str | None,
        occurred_at: str | None,
    ) -> None:
        """Bind the sole retained execution-plan shape to its local envelope.

        Retention is a local record, so ``recorded_at`` is the only retention
        time.  A plan record must not manufacture a remote occurrence or
        observation time, an actor/run, or provider execution provenance.
        """
        if kind != "EXECUTION_PLAN":
            return
        record = OpenAIPauseExecutionPlanRecord.model_validate_json(canonical_bytes(payload))
        binding = record.action.binding
        subplan = record.action.plan.subplans[0]
        if (
            provider != "openai_ads"
            or account_ref != binding.account_ref
            or profile_ref != record.action.plan.profile_ref
            or subplan.account.auth_profile_ref != binding.profile_ref
            or source != "LOCAL_CLI"
            or run_id is not None
            or occurred_at is not None
            or observed_at is not None
        ):
            raise AdhelmError(
                "INVALID_EXECUTION_PLAN_PROVENANCE",
                "Retained OpenAI pause plans must match their local profile and account "
                "binding without remote event, observation, run, or execution provenance.",
                exit_code=2,
            )

    def _stream_id(self, stream: str) -> str:
        return f"{self.profile_ref}:{stream}"

    def _stream_dir(self, stream: str) -> Path:
        return self.ledger_root / stream

    def _ensure_dirs(self, stream: str) -> None:
        self._reject_symlink(self.ledger_root)
        ensure_private_directory(self.ledger_root)
        self._reject_symlink(self.ledger_root)
        stream_dir = self._stream_dir(stream)
        self._reject_symlink(stream_dir)
        stream_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
        self._reject_symlink(stream_dir)
        self._fsync_directory(stream_dir.parent)

    def _segment_paths(self, stream: str) -> list[Path]:
        directory = self._stream_dir(stream)
        self._reject_symlink(self.ledger_root)
        self._reject_symlink(directory)
        if not directory.exists():
            return []
        paths = sorted(directory.glob("*.jsonl"))
        for expected, path in enumerate(paths, start=1):
            self._reject_symlink(path)
            if path.name != f"{expected:020d}.jsonl":
                raise AdhelmError("LEDGER_SEGMENT_GAP", "Ledger segments are missing or misnamed.")
        return paths

    def _scan_stream(self, stream: str) -> dict[str, Any]:
        self._validated_segment_bytes(self.segment_bytes)
        records: list[dict[str, Any]] = []
        ids: set[str] = set()
        previous: str | None = None
        last_sequence = 0
        paths = self._segment_paths(stream)
        for path in paths:
            data = self._read_bytes(path)
            if not data:
                raise AdhelmError(
                    "LEDGER_EMPTY_SEGMENT",
                    "Ledger segment creation was interrupted; explicit repair is required.",
                )
            if data and not data.endswith(b"\n"):
                raise AdhelmError(
                    "LEDGER_TORN_TAIL", "Ledger has a torn final line; explicit repair is required."
                )
            if len(data) > 0:
                for line_number, raw in enumerate(data[:-1].split(b"\n"), start=1):
                    if not raw:
                        raise AdhelmError(
                            "LEDGER_CORRUPTION", "Ledger contains an empty physical record."
                        )
                    if b"\r" in raw:
                        raise AdhelmError(
                            "LEDGER_CORRUPTION", "Ledger records must use LF line endings."
                        )
                    if len(raw) + 1 > MAX_RECORD_BYTES:
                        raise AdhelmError(
                            "LEDGER_RECORD_TOO_LARGE", "Ledger contains an oversized record."
                        )
                    try:
                        item = parse_json(raw)
                    except AdhelmError as exc:
                        raise AdhelmError(
                            "LEDGER_CORRUPTION", "Ledger contains invalid canonical JSON."
                        ) from exc
                    self._verify_record(item, stream, last_sequence + 1, previous)
                    if item["record_id"] in ids:
                        raise AdhelmError(
                            "LEDGER_DUPLICATE_RECORD",
                            "Ledger contains a duplicate record identifier.",
                        )
                    if line_number == 1 and item["record_kind"] != "SEGMENT_STARTED":
                        raise AdhelmError(
                            "LEDGER_SEGMENT_HEADER",
                            "Every segment must begin with SEGMENT_STARTED.",
                        )
                    if line_number == 1:
                        header = item["payload"]
                        if header["previous_segment_head_digest"] != previous:
                            raise AdhelmError(
                                "LEDGER_SEGMENT_CHAIN",
                                "Segment link does not match the previous head.",
                            )
                    ids.add(item["record_id"])
                    previous = item["record_digest"]
                    last_sequence = item["stream_sequence"]
                    records.append(item)
        if stream == "changes":
            validate_google_ads_activate_readback_history(records)
            validate_google_ads_create_readback_history(records)
            validate_google_ads_maintain_readback_history(records)
            validate_openai_create_readback_history(records)
            validate_google_ads_pause_readback_history(records)
            validate_meta_pause_readback_history(records)
            validate_meta_create_readback_history(records)
            validate_chunked_measurement_history(records)
            self._validate_conversion_history(records)
            self._validate_campaign_join_history(records)
            self._validate_ga4_key_event_candidate_history(records)
            validate_ga4_create_plan_history(records)
            validate_ga4_create_start_history(records)
            validate_execution_plan_history(records)
            self._validate_approval_receipt_history(records)
            self._validate_openai_snapshot_history(records)
        elif stream == "creative":
            validate_creative_history_graph(records)
        return {
            "records": len(records),
            "record_list": records,
            "head_digest": previous,
            "last_sequence": last_sequence,
            "segments": len(paths),
        }

    def _verify_record(
        self, item: Any, stream: str, expected_sequence: int, previous: str | None
    ) -> None:
        if not isinstance(item, dict):
            raise AdhelmError("LEDGER_CORRUPTION", "Ledger record is not an object.")
        required = {
            "schema_uri",
            "schema_version",
            "canonicalization_version",
            "record_kind",
            "record_id",
            "stream_id",
            "stream_sequence",
            "occurred_at",
            "observed_at",
            "recorded_at",
            "profile_ref",
            "provider",
            "account_ref",
            "run_id",
            "source",
            "payload",
            "previous_record_digest",
            "record_digest",
        }
        if set(item) != required:
            raise AdhelmError(
                "LEDGER_SCHEMA_MISMATCH", "Ledger record has an unsupported envelope."
            )
        if (
            item["schema_uri"] != SCHEMA_URI
            or item["schema_version"] != SCHEMA_VERSION
            or item["canonicalization_version"] != CANONICALIZATION_VERSION
        ):
            raise AdhelmError(
                "LEDGER_SCHEMA_MISMATCH", "Ledger record uses an unsupported schema version."
            )
        if item["stream_id"] != self._stream_id(stream) or item["profile_ref"] != self.profile_ref:
            raise AdhelmError(
                "LEDGER_STREAM_MISMATCH", "Ledger record belongs to a different profile or stream."
            )
        if item["source"] not in SOURCES:
            raise AdhelmError("LEDGER_SCHEMA_MISMATCH", "Ledger record has an unsupported source.")
        if (
            item["stream_sequence"] != expected_sequence
            or item["previous_record_digest"] != previous
        ):
            raise AdhelmError("LEDGER_CHAIN_BROKEN", "Ledger sequence or digest chain is invalid.")
        validated_payload = self._validate_payload(
            stream,
            item["record_kind"],
            item["payload"],
            provider=item["provider"],
            allow_legacy_creative=True,
        )
        self._validate_candidate_binding(
            item["record_kind"],
            item["payload"],
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
        )
        self._validate_strategy_compilation_binding(
            item["record_kind"],
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
            item["observed_at"],
            item["occurred_at"],
        )
        self._validate_approval_receipt_binding(
            item["record_kind"],
            validated_payload,
            item["profile_ref"],
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
            item["observed_at"],
            item["occurred_at"],
        )
        self._validate_conversion_binding(
            item["record_kind"],
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
        )
        self._validate_measurement_binding(
            item["record_kind"],
            validated_payload,
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
            item["observed_at"],
            item["occurred_at"],
        )
        self._validate_ga4_key_event_candidate_envelope(
            item["record_kind"],
            validated_payload,
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
            item["observed_at"],
            item["occurred_at"],
        )
        self._validate_execution_plan_binding(
            item["record_kind"],
            validated_payload,
            item["profile_ref"],
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
            item["observed_at"],
            item["occurred_at"],
        )
        validate_ga4_create_plan_envelope(
            item["record_kind"],
            validated_payload,
            item["profile_ref"],
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
            item["observed_at"],
            item["occurred_at"],
        )
        validate_meta_pause_plan_envelope(
            item["record_kind"],
            validated_payload,
            item["profile_ref"],
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
            item["observed_at"],
            item["occurred_at"],
        )
        validate_google_ads_pause_plan_envelope(
            item["record_kind"],
            validated_payload,
            item["profile_ref"],
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
            item["observed_at"],
            item["occurred_at"],
        )
        validate_execution_plan_envelope(
            item["record_kind"],
            validated_payload,
            item["profile_ref"],
            item["provider"],
            item["account_ref"],
            item["source"],
            item["run_id"],
            item["observed_at"],
            item["occurred_at"],
        )
        hashed = dict(item)
        stored_digest = hashed.pop("record_digest")
        if digest(hashed) != stored_digest:
            raise AdhelmError(
                "LEDGER_DIGEST_MISMATCH",
                "Ledger record digest does not match its canonical content.",
            )

    def _writable_segment(self, stream: str, state: dict[str, Any]) -> Path | None:
        paths = self._segment_paths(stream)
        return paths[-1] if paths else None

    def _new_segment(self, stream: str, state: dict[str, Any]) -> Path:
        paths = self._segment_paths(stream)
        path = self._stream_dir(stream) / f"{len(paths) + 1:020d}.jsonl"
        self._reject_symlink(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as exc:
            raise AdhelmError("UNSAFE_LEDGER_PATH", "New ledger segment already exists.") from exc
        except OSError as exc:
            raise AdhelmError(
                "UNSAFE_LEDGER_PATH", "Ledger records cannot traverse symbolic links.", exit_code=2
            ) from exc
        else:
            os.close(descriptor)
        self._fsync_directory(path.parent)
        header = self._make_record(
            stream,
            "SEGMENT_STARTED",
            {
                "previous_segment_head_digest": state["head_digest"],
                "previous_segment_name": paths[-1].name if paths else None,
            },
            sequence=state["last_sequence"] + 1,
            previous_digest=state["head_digest"],
            provider=None,
            account_ref=None,
            run_id=None,
            source="LOCAL_CLI",
            occurred_at=None,
            observed_at=None,
        )
        self._append_bytes(path, canonical_bytes(header) + b"\n")
        return path

    def _make_record(
        self,
        stream: str,
        kind: str,
        payload: dict[str, Any],
        *,
        sequence: int,
        previous_digest: str | None,
        provider: str | None,
        account_ref: str | None,
        run_id: str | None,
        source: str,
        occurred_at: str | None,
        observed_at: str | None,
    ) -> dict[str, Any]:
        local_compilation = kind in {
            "EXECUTION_PLAN",
            "STRATEGY_CANDIDATE_COMPILED",
            "APPROVAL_RECEIPT_CLAIMED",
            "GA4_KEY_EVENT_CANDIDATE_COMPILED",
            "GA4_KEY_EVENT_CREATE_PLAN",
            "META_CAMPAIGN_PAUSE_PLAN",
            "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN",
            "EXECUTION_PLAN_RETAINED",
        }
        record = {
            "schema_uri": SCHEMA_URI,
            "schema_version": SCHEMA_VERSION,
            "canonicalization_version": CANONICALIZATION_VERSION,
            "record_kind": kind,
            "record_id": f"rec:{_utc_now()}:{uuid.uuid4().hex}",
            "stream_id": self._stream_id(stream),
            "stream_sequence": sequence,
            "occurred_at": _timestamp(
                occurred_at,
                nullable=source != "LOCAL_CLI"
                or local_compilation
                or kind == "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION",
            ),
            "observed_at": _timestamp(observed_at, nullable=local_compilation),
            "recorded_at": _utc_now(),
            "profile_ref": self.profile_ref,
            "provider": provider,
            "account_ref": account_ref,
            "run_id": run_id,
            "source": source,
            "payload": self._validate_payload(stream, kind, payload, provider=provider),
            "previous_record_digest": previous_digest,
        }
        record["record_digest"] = digest(record)
        return record

    def _stream_lock(self, stream: str) -> portalocker.Lock:
        self._reject_symlink(self._stream_dir(stream))
        lock_path = self._stream_dir(stream) / ".append.lock"
        self._reject_symlink(lock_path)
        return portalocker.Lock(str(lock_path), mode="a", timeout=5)

    def _index_lock(self) -> portalocker.Lock:
        self._reject_symlink(self.ledger_root)
        lock_path = self.ledger_root / ".index-rebuild.lock"
        self._reject_symlink(lock_path)
        return portalocker.Lock(str(lock_path), mode="a", timeout=5)

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise AdhelmError(
                "UNSAFE_LEDGER_PATH",
                "Ledger paths cannot traverse symbolic links.",
                exit_code=2,
            )

    @staticmethod
    def _append_bytes(path: Path, data: bytes) -> None:
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise AdhelmError(
                "UNSAFE_LEDGER_PATH", "Ledger records cannot traverse symbolic links.", exit_code=2
            ) from exc
        with os.fdopen(descriptor, "ab", buffering=0) as handle:
            written = 0
            while written < len(data):
                count = handle.write(data[written:])
                if type(count) is not int or not 1 <= count <= len(data) - written:
                    raise OSError("Ledger append made no write progress")
                written += count
            handle.flush()
            os.fsync(handle.fileno())

    def _read_bytes(self, path: Path) -> bytes:
        """Read one configured segment without confusing it with one JSONL record.

        A segment normally stays below ``segment_bytes``.  Rotation can leave a
        header plus one maximum-size record in a newly-created segment, so keep
        two record-size allowances while retaining a deterministic, bounded read.
        """
        segment_bytes = self._validated_segment_bytes(self.segment_bytes)
        maximum_segment_bytes = segment_bytes + (2 * MAX_RECORD_BYTES)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise AdhelmError(
                "UNSAFE_LEDGER_PATH", "Ledger records cannot traverse symbolic links.", exit_code=2
            ) from exc
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(maximum_segment_bytes + 1)
        if len(data) > maximum_segment_bytes:
            raise AdhelmError(
                "LEDGER_SEGMENT_TOO_LARGE",
                "Ledger segment exceeds the configured bounded read limit.",
            )
        return data

    @staticmethod
    def _validated_segment_bytes(segment_bytes: object) -> int:
        if type(segment_bytes) is not int or not 1024 <= segment_bytes <= MAX_SEGMENT_BYTES:
            raise AdhelmError(
                "INVALID_SEGMENT_SIZE",
                "Segment size must be a bounded whole number within the supported range.",
                exit_code=2,
            )
        return segment_bytes

    @staticmethod
    def _fsync_file(path: Path) -> None:
        # Windows flushes through FlushFileBuffers, which needs write access.
        flags = os.O_RDWR if sys.platform == "win32" else os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise AdhelmError(
                "UNSAFE_LEDGER_PATH", "Ledger records cannot traverse symbolic links.", exit_code=2
            ) from exc
        with os.fdopen(descriptor, "rb") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
