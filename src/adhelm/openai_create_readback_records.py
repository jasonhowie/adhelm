"""Closed factual OpenAI Ads chat-card CREATE readbacks, separate from plans.

One record describes exactly one post-acknowledgement observation of the ordered
paused chat-card hierarchy create: the bounded observation interval, the ordered
child steps this attempt acknowledged, the hierarchy the readback actually
found, and the one fact the provider's own file surface can never supply -- that
the created paused ad references the exact file this attempt uploaded.

No request body, response body, bearer, account key, ad copy, destination URL,
image URL, or **file bytes** are retained, and the record asserts no
acknowledgement, conditional-write, idempotency, delivery, or spend semantics.

**The native identifiers are deliberately pseudonymized.**  A create mints
identifiers this workspace never chose: the campaign, ad-group and ad IDs, and
the ``file_id`` the upload returned.  The plan already carries the
customer-configured campaign, ad-group and ad names, which are the handles a
bounded recovery uses.  Every reference here is therefore a keyless SHA-256
reference over the exact native identifier, so two records can be compared for
identity without the record publishing an OpenAI Ads resource or file ID.

**A partial hierarchy is recorded, not repaired.**  When a child fails after an
earlier child succeeded, the attempt is ambiguous and rule 11 forbids deleting
whatever now exists.  The record then carries
``PARTIAL_HIERARCHY_OBSERVED`` and the disposition
``RETAIN_REFS_LEAVE_PAUSED_NO_DELETE``.  It is the honest description of a
half-created hierarchy, never an instruction to retry or clean up.

Registering the kind is one ``_KINDS`` entry plus the scan-time history
validator below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import MAX_CHILD_EGRESS_COUNT, StrictModel
from adhelm.providers.openai_create_contract import (
    OPENAI_CREATE_API_VERSION,
    OPENAI_CREATE_CAPABILITY_VERSION,
    OPENAI_CREATE_CHILD_LABELS,
    OPENAI_CREATE_MAX_HIERARCHY_MEMBERS,
    OPENAI_CREATE_MAX_UPLOAD_BYTES,
    OPENAI_CREATE_PLAN_RECORD_KIND,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

#: The one additive ledger record kind this module describes, in ``changes``.
OPENAI_CREATE_READBACK_RECORD_KIND: Final = (
    "OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_READBACK_OBSERVATION"
)

#: Domain separation for the pseudonymized native references.
_RESOURCE_DOMAIN: Final = "adhelm.openai-ads.chat-card-hierarchy-create.resource.v1"

#: The kinds of native identifier this record pseudonymizes.
_RESOURCE_KINDS: Final = frozenset({"campaign", "ad_group", "ad", "file"})

#: The bound the guarded adapter's post-acknowledgement readback must fit in.
_MAX_READBACK_INTERVAL: Final = timedelta(minutes=10)

#: The exact ordered child labels this route issues.
OPENAI_CREATE_CHILD_LABEL_SET: Final = frozenset(OPENAI_CREATE_CHILD_LABELS)


def pseudonymized_resource_ref(kind: str, native_identifier: str) -> str:
    """Return the stable keyless reference this record retains for one resource."""

    if kind not in _RESOURCE_KINDS:
        raise ValueError("only a campaign, ad group, ad or file identifier is pseudonymized here")
    if type(native_identifier) is not str or not 1 <= len(native_identifier) <= 256:
        raise ValueError("a bounded native identifier is required")
    return f"openai_ads.{kind}:sha256:" + digest(
        {"domain": _RESOURCE_DOMAIN, "kind": kind, "native_identifier": native_identifier}
    )


class OpenAIAdsChatCardHierarchyCreateReadbackObservation(StrictModel):
    """One dedicated post-acknowledgement OpenAI Ads v1 hierarchy observation."""

    schema_version: Literal[1] = 1
    action_id: Digest
    plan_digest: Digest
    subplan_digest: Digest
    attempt_ref: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    account_ref: str = Field(min_length=1, max_length=160)
    provider_api_version: Literal["v1"] = OPENAI_CREATE_API_VERSION
    capability_version: Literal["openai-ads-v1-chat-card-hierarchy-create-contract-2026-09-09"] = (
        OPENAI_CREATE_CAPABILITY_VERSION
    )
    observation_interval_start: datetime
    observation_interval_end: datetime

    # -- the ordered child journal, restated as facts ------------------------
    #: How many of the approved children reached a closed acknowledgement, and
    #: which step each of those was.  The kernel's own CHILD_APPLY_STARTED and
    #: CHILD_APPLY_RESULT records remain the authority; this restates the shape
    #: so one retained observation describes the whole attempt.
    approved_child_count: int = Field(ge=4, le=MAX_CHILD_EGRESS_COUNT)
    acknowledged_child_count: int = Field(ge=0, le=MAX_CHILD_EGRESS_COUNT)
    acknowledged_child_labels: tuple[str, ...] = Field(max_length=MAX_CHILD_EGRESS_COUNT)
    attempt_completion: Literal["CHILDREN_COMPLETE", "CHILDREN_INCOMPLETE"]

    # -- what the readback actually found ------------------------------------
    before_observed_hierarchy: Literal["NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"] = (
        "NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"
    )
    observed_hierarchy_relation: Literal[
        "MATCHES_PREDICTED_SEMANTICS",
        "PARTIAL_HIERARCHY_OBSERVED",
        "NO_HIERARCHY_OBSERVED",
    ]
    observed_ad_group_count: int = Field(ge=0, le=OPENAI_CREATE_MAX_HIERARCHY_MEMBERS)
    observed_ad_count: int = Field(ge=0, le=OPENAI_CREATE_MAX_HIERARCHY_MEMBERS)
    observed_member_count: int = Field(ge=0, le=OPENAI_CREATE_MAX_HIERARCHY_MEMBERS)
    every_entity_configured_status: Literal["paused", "NOT_OBSERVED"] = "paused"
    campaign_resource_ref: str | None = Field(
        default=None, pattern=r"^openai_ads\.campaign:sha256:[0-9a-f]{64}$"
    )
    ad_group_resource_refs: tuple[str, ...] = Field(
        default=(), max_length=OPENAI_CREATE_MAX_HIERARCHY_MEMBERS
    )
    ad_resource_refs: tuple[str, ...] = Field(
        default=(), max_length=OPENAI_CREATE_MAX_HIERARCHY_MEMBERS
    )

    # -- the creative, by content only ---------------------------------------
    #: The only three facts the upload child retains.  The bytes are never
    #: retained, and the returned file identifier is pseudonymized.
    creative_content_digest: Digest
    creative_byte_length: int = Field(ge=1, le=OPENAI_CREATE_MAX_UPLOAD_BYTES)
    creative_media_type: Literal["image/png", "image/jpeg"]
    creative_file_ref: str | None = Field(
        default=None, pattern=r"^openai_ads\.file:sha256:[0-9a-f]{64}$"
    )
    file_association: Literal["PROVEN_UPLOAD_ASSOCIATION", "NOT_PROVEN"]
    uploaded_file_readback: Literal["NOT_AVAILABLE_NO_DOCUMENTED_ADS_FILE_GET"] = (
        "NOT_AVAILABLE_NO_DOCUMENTED_ADS_FILE_GET"
    )
    provider_file_bytes: Literal["PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ"] = (
        "PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_HASH_READ"
    )

    # -- fixed semantics -----------------------------------------------------
    acknowledgement_semantics: Literal["LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"] = (
        "LOCAL_ACKNOWLEDGEMENT_SUBSET_NOT_PROVIDER_CONTRACT"
    )
    atomicity_semantics: Literal["ORDERED_NON_ATOMIC_CHILD_REQUESTS"] = (
        "ORDERED_NON_ATOMIC_CHILD_REQUESTS"
    )
    recovery_disposition: Literal["RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"] = (
        "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"
    )
    effective_delivery: Literal["NOT_PREDICTED"] = "NOT_PREDICTED"
    activation: Literal["SEPARATE_SPEND_EXPLICIT_AUTHORIZATION_REQUIRED"] = (
        "SEPARATE_SPEND_EXPLICIT_AUTHORIZATION_REQUIRED"
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

    @field_validator("acknowledged_child_labels")
    @classmethod
    def closed_child_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(item not in OPENAI_CREATE_CHILD_LABEL_SET for item in value):
            raise ValueError("a child acknowledgement carries only the planned step labels")
        return value

    @field_validator("ad_group_resource_refs", "ad_resource_refs")
    @classmethod
    def pseudonymized_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            type(item) is not str
            or not item.startswith(("openai_ads.ad_group:sha256:", "openai_ads.ad:sha256:"))
            or len(item.rsplit(":", 1)[1]) != 64
            for item in value
        ):
            raise ValueError("native references are retained only as keyless pseudonyms")
        if len(set(value)) != len(value):
            raise ValueError("one created resource is recorded once")
        return value

    @model_validator(mode="after")
    def coherent_observation(self) -> OpenAIAdsChatCardHierarchyCreateReadbackObservation:
        if (
            not timedelta(0)
            <= self.observation_interval_end - self.observation_interval_start
            <= _MAX_READBACK_INTERVAL
        ):
            raise ValueError("readback interval is reversed or too long")
        if self.acknowledged_child_count != len(self.acknowledged_child_labels):
            raise ValueError("the acknowledgement count does not describe its own child labels")
        if self.acknowledged_child_count > self.approved_child_count:
            raise ValueError("more children were acknowledged than the plan approved")
        complete = self.attempt_completion == "CHILDREN_COMPLETE"
        if complete != (self.acknowledged_child_count == self.approved_child_count):
            raise ValueError("the completion statement does not describe its own child count")
        if self.acknowledged_child_labels[:1] not in ((), ("upload-creative",)):
            raise ValueError("the first journaled child of this route is the creative upload")
        if self.observed_hierarchy_relation == "NO_HIERARCHY_OBSERVED":
            if (
                self.observed_member_count
                or self.observed_ad_group_count
                or self.observed_ad_count
                or self.campaign_resource_ref is not None
                or self.ad_group_resource_refs
                or self.ad_resource_refs
                or self.every_entity_configured_status != "NOT_OBSERVED"
            ):
                raise ValueError("an unobserved hierarchy cannot carry observed resources")
        else:
            if self.campaign_resource_ref is None or self.every_entity_configured_status != (
                "paused"
            ):
                raise ValueError("an observed hierarchy names its paused campaign")
            if self.observed_member_count != (
                1 + self.observed_ad_group_count + self.observed_ad_count
            ):
                raise ValueError("the observed member count does not describe its own hierarchy")
            if (
                len(self.ad_group_resource_refs) != self.observed_ad_group_count
                or len(self.ad_resource_refs) != self.observed_ad_count
            ):
                raise ValueError("the observed references do not describe the observed counts")
        if self.observed_hierarchy_relation == "MATCHES_PREDICTED_SEMANTICS" and not complete:
            raise ValueError("an incomplete attempt cannot match the predicted hierarchy")
        if self.file_association == "PROVEN_UPLOAD_ASSOCIATION":
            if self.creative_file_ref is None or not self.observed_ad_count:
                raise ValueError("upload association is proven only by an observed paused ad")
        if self.observed_hierarchy_relation == "MATCHES_PREDICTED_SEMANTICS" and (
            self.file_association != "PROVEN_UPLOAD_ASSOCIATION"
        ):
            raise ValueError("a matching hierarchy requires the proven upload association")
        return self


def validate_openai_create_readback_history(records: list[dict[str, Any]]) -> None:
    """Use already-scanned records only; never acquire a lock or read a ledger.

    Every retained readback must name one retained
    ``OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN`` action, restate that action's
    own plan and subplan digests, approved child count and creative binding, and
    follow exactly one durable ``APPLY_STARTED`` for the same attempt.  A second
    readback for one attempt is refused: this route performs one ordered child
    sequence and one readback.
    """

    actions: dict[str, dict[str, Any]] = {}
    starts: list[dict[str, Any]] = []
    observed: set[str] = set()
    for record in records:
        kind = record.get("record_kind")
        payload = record.get("payload", {})
        if kind == OPENAI_CREATE_PLAN_RECORD_KIND:
            action = payload["action"]
            actions[action["action_id"]] = action
        elif kind == "CHANGE_EVENT" and payload.get("event_type") == "APPLY_STARTED":
            starts.append(record)
        elif kind == OPENAI_CREATE_READBACK_RECORD_KIND:
            try:
                action = actions[payload["action_id"]]
                plan = action["plan"]
                subplan = plan["subplans"][0]
                account_ref = subplan["account"]["account_ref"]
                postcondition = action["postcondition"]
                creative = action["observation_binding"]["creative"]
                if (
                    payload["plan_digest"] != digest(plan)
                    or payload["subplan_digest"] != digest(subplan)
                    or payload["account_ref"] != account_ref
                    or payload["provider_api_version"] != subplan["provider_api_version"]
                    or payload["capability_version"] != subplan["capability_version"]
                    or payload["approved_child_count"] != postcondition["child_egress_count"]
                    or payload["creative_content_digest"] != creative["content_digest"]
                    or payload["creative_byte_length"] != creative["byte_length"]
                    or payload["creative_media_type"] != creative["media_type"]
                    or record.get("provider") != "openai_ads"
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
                if payload["observed_hierarchy_relation"] == "MATCHES_PREDICTED_SEMANTICS" and (
                    payload["observed_member_count"] != postcondition["member_count"]
                    or payload["observed_ad_group_count"] != postcondition["ad_group_count"]
                    or payload["observed_ad_count"] != postcondition["ad_count"]
                ):
                    raise ValueError("readback hierarchy")
                matching = [
                    start
                    for start in starts
                    if start.get("provider") == "openai_ads"
                    and start.get("account_ref") == account_ref
                    and start.get("run_id") == plan["plan_id"]
                    and start["payload"].get("plan_digest") == payload["plan_digest"]
                    and start["payload"].get("subplan_digest") == payload["subplan_digest"]
                    and start["payload"].get("idempotency_ref") == payload["attempt_ref"]
                    and start["payload"].get("operation") == "CREATE"
                    and start["payload"].get("resource_ref") == subplan["subplan_id"]
                    and start["payload"].get("before_digest") == subplan["before"]["fingerprint"]
                    and start["payload"].get("after_digest") == subplan["after"]["fingerprint"]
                ]
                if len(matching) != 1:
                    raise ValueError("readback start")
                observed.add(payload["attempt_ref"])
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                raise AdhelmError(
                    "INVALID_OPENAI_ADS_CREATE_READBACK_HISTORY",
                    "An OpenAI Ads CREATE readback requires its exact retained action and start.",
                    exit_code=2,
                ) from None


def _instant(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def openai_create_readback_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive readback kind.

    The adapter checks this at factory admission -- before any credential,
    header, or provider request -- so an ordinary build refuses the whole
    execution rather than creating a hierarchy and then failing to retain the
    observation that proves what it created.  This is a build fact, not a
    fallback: nothing here registers the kind, and no path continues without it.
    """

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(OPENAI_CREATE_READBACK_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is OpenAIAdsChatCardHierarchyCreateReadbackObservation


__all__ = [
    "OPENAI_CREATE_CHILD_LABEL_SET",
    "OPENAI_CREATE_READBACK_RECORD_KIND",
    "OpenAIAdsChatCardHierarchyCreateReadbackObservation",
    "openai_create_readback_record_kind_installed",
    "pseudonymized_resource_ref",
    "validate_openai_create_readback_history",
]
