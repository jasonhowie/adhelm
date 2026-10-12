"""Closed factual Meta Ads single-image CREATE readbacks, separate from plans.

One record describes exactly one post-acknowledgement observation of the ordered
paused single-image hierarchy create: the bounded observation interval, the
ordered child steps this attempt acknowledged, the hierarchy the readback
actually found, and the two association facts Meta's own surface can supply --
that the ad account holds the exact image this attempt uploaded, and that the
created ad creative references it.

No request body, response body, access token, ad copy, destination URL, Facebook
Page identifier, provider image ``hash``, or **image bytes** are retained, and
the record asserts no acknowledgement, conditional-write, idempotency, delivery,
or spend semantics.

**The native identifiers are deliberately pseudonymized.**  A create mints
identifiers this workspace never chose: the campaign, ad-set, creative and ad
IDs, and the opaque ``hash`` the upload returned.  The plan already carries the
customer-configured names, which are the handles a bounded recovery uses.  Every
reference here is therefore a keyless SHA-256 reference over the exact native
value, so two records can be compared for identity without the record publishing
a Meta Ads resource identifier or an ad-image hash.

**A partial hierarchy is recorded, not repaired.**  When a child fails after an
earlier child succeeded, the attempt is ambiguous and rule 11 forbids deleting
whatever now exists.  The record then carries ``PARTIAL_HIERARCHY_OBSERVED`` and
the disposition ``RETAIN_REFS_LEAVE_PAUSED_NO_DELETE``.  It is the honest
description of a half-created hierarchy, never an instruction to retry or clean
up.

Registering the kind is one ``_KINDS`` entry plus the scan-time history
validator below.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Literal

from pydantic import Field, field_validator, model_validator

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import StrictModel
from adhelm.providers.meta_create_contract import (
    META_CREATE_API_VERSION,
    META_CREATE_CAPABILITY_VERSION,
    META_CREATE_CHILD_EGRESS_COUNT,
    META_CREATE_CHILD_LABELS,
    META_CREATE_HIERARCHY_MEMBER_COUNT,
    META_CREATE_MAX_UPLOAD_BYTES,
    META_CREATE_PLAN_RECORD_KIND,
)

Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]

#: The one additive ledger record kind this module describes, in ``changes``.
META_CREATE_READBACK_RECORD_KIND: Final = (
    "META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_READBACK_OBSERVATION"
)

#: Domain separation for the pseudonymized native references.
_RESOURCE_DOMAIN: Final = "adhelm.meta-ads.single-image-hierarchy-create.resource.v1"

#: The kinds of native reference this record pseudonymizes.
_RESOURCE_KINDS: Final = frozenset({"campaign", "ad_set", "creative", "ad", "image"})

#: The bound the guarded adapter's post-acknowledgement readback must fit in.
_MAX_READBACK_INTERVAL: Final = timedelta(minutes=10)

#: The exact ordered child labels this route issues.
META_CREATE_CHILD_LABEL_SET: Final = frozenset(META_CREATE_CHILD_LABELS)

#: The label of the child whose acknowledgement carries the campaign identifier.
_CAMPAIGN_CHILD_LABEL: Final = "create-campaign"

#: The relations whose record carries no observed resource.
_UNOBSERVED_RELATIONS: Final = frozenset(
    {
        "NO_HIERARCHY_OBSERVED",
        "READBACK_NOT_ATTEMPTED",
        "UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED",
        "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED",
        "READBACK_UNAVAILABLE",
    }
)


def pseudonymized_resource_ref(kind: str, native_identifier: str) -> str:
    """Return the stable keyless reference this record retains for one resource."""

    if kind not in _RESOURCE_KINDS:
        raise ValueError(
            "only a campaign, ad set, creative, ad or image reference is pseudonymized here"
        )
    if type(native_identifier) is not str or not 1 <= len(native_identifier) <= 512:
        raise ValueError("a bounded native identifier is required")
    return f"meta_ads.{kind}:sha256:" + digest(
        {"domain": _RESOURCE_DOMAIN, "kind": kind, "native_identifier": native_identifier}
    )


class MetaAdsSingleImageHierarchyCreateReadbackObservation(StrictModel):
    """One dedicated post-acknowledgement Meta Ads v26 hierarchy observation."""

    schema_version: Literal[1] = 1
    action_id: Digest
    plan_digest: Digest
    subplan_digest: Digest
    attempt_ref: str = Field(min_length=1, max_length=256, pattern=r"^[A-Za-z0-9_.:/-]+$")
    account_ref: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:/-]+$")
    provider_api_version: Literal["v26.0"] = META_CREATE_API_VERSION
    capability_version: Literal[
        "meta-graph-v26-single-image-hierarchy-create-contract-2026-09-09"
    ] = META_CREATE_CAPABILITY_VERSION
    observation_interval_start: datetime
    observation_interval_end: datetime

    # -- the ordered child journal, restated as facts ------------------------
    #: How many of the approved children reached a closed acknowledgement, and
    #: which step each of those was.  The kernel's own CHILD_APPLY_STARTED and
    #: CHILD_APPLY_RESULT records remain the authority; this restates the shape
    #: so one retained observation describes the whole attempt.
    approved_child_count: Literal[5] = META_CREATE_CHILD_EGRESS_COUNT
    acknowledged_child_count: int = Field(ge=0, le=META_CREATE_CHILD_EGRESS_COUNT)
    acknowledged_child_labels: tuple[str, ...] = Field(max_length=META_CREATE_CHILD_EGRESS_COUNT)
    attempt_completion: Literal["CHILDREN_COMPLETE", "CHILDREN_INCOMPLETE"]

    # -- what the readback actually found ------------------------------------
    before_observed_hierarchy: Literal["NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"] = (
        "NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"
    )
    #: Eight relations, each a different statement and none interchangeable:
    #:
    #: * ``READBACK_NOT_ATTEMPTED``: no child had acknowledged an identifier, so
    #:   there was nothing to read by identifier and no read was made.
    #: * ``NO_HIERARCHY_OBSERVED``: the planned-name probe completed and listed no
    #:   campaign with the planned name or a near variant, and no campaign
    #:   identifier had been acknowledged (so no node was read).
    #: * ``UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED``: the probe listed a campaign with
    #:   the planned name (or a near variant) that this attempt cannot attribute
    #:   to itself.  Without an acknowledged campaign identifier it is most
    #:   likely this attempt's own campaign whose response was lost; with one it
    #:   is a second campaign carrying the name.  Nothing in this record proves
    #:   which, and it may be a rival writer either way.
    #: * ``ACKNOWLEDGED_CAMPAIGN_NOT_LISTED``: the campaign identifier was
    #:   acknowledged and the probe listed no campaign with the planned name or a
    #:   near variant.  A list that lags the create looks exactly like this.  It
    #:   says nothing about whether the acknowledged campaign exists.
    #: * ``READBACK_UNAVAILABLE``: a child acknowledged a resource and the
    #:   readback could not be completed or parsed (an allowlist miss, a
    #:   transport failure).  It is not "nothing exists".
    #: * ``PARTIAL_`` / ``DIVERGENT_`` / ``MATCHES_``: acknowledged nodes were read.
    observed_hierarchy_relation: Literal[
        "MATCHES_PREDICTED_SEMANTICS",
        "DIVERGENT_HIERARCHY_OBSERVED",
        "PARTIAL_HIERARCHY_OBSERVED",
        "NO_HIERARCHY_OBSERVED",
        "READBACK_NOT_ATTEMPTED",
        "UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED",
        "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED",
        "READBACK_UNAVAILABLE",
    ]
    observed_member_count: int = Field(ge=0, le=META_CREATE_HIERARCHY_MEMBER_COUNT)
    #: Derived from the observed ``configured_status`` of the campaign, ad set and
    #: ad.  The creative is not a pausable Meta entity and is never counted.
    every_pausable_entity_configured_status: Literal["PAUSED", "NOT_ALL_PAUSED", "NOT_OBSERVED"]
    campaign_resource_ref: str | None = Field(
        default=None, pattern=r"^meta_ads\.campaign:sha256:[0-9a-f]{64}$"
    )
    ad_set_resource_ref: str | None = Field(
        default=None, pattern=r"^meta_ads\.ad_set:sha256:[0-9a-f]{64}$"
    )
    creative_resource_ref: str | None = Field(
        default=None, pattern=r"^meta_ads\.creative:sha256:[0-9a-f]{64}$"
    )
    ad_resource_ref: str | None = Field(default=None, pattern=r"^meta_ads\.ad:sha256:[0-9a-f]{64}$")
    #: The observed effective statuses, retained as observations only.  A paused
    #: configuration predicts neither effective delivery nor zero spend.
    observed_campaign_effective_status: (
        Literal["ACTIVE", "ARCHIVED", "DELETED", "IN_PROCESS", "PAUSED", "WITH_ISSUES"] | None
    ) = None

    # -- the creative, by content only ---------------------------------------
    #: The only three facts the upload child retains about the bytes.  The bytes
    #: themselves are never retained, and the provider hash is pseudonymized.
    creative_content_digest: Digest
    creative_byte_length: int = Field(ge=1, le=META_CREATE_MAX_UPLOAD_BYTES)
    creative_media_type: Literal["image/png", "image/jpeg"]
    image_resource_ref: str | None = Field(
        default=None, pattern=r"^meta_ads\.image:sha256:[0-9a-f]{64}$"
    )
    image_association: Literal["PROVEN_ACCOUNT_IMAGE_AND_CREATIVE_ASSOCIATION", "NOT_PROVEN"]
    creative_association: Literal["PROVEN_SAME_ATTEMPT_CREATIVE", "NOT_PROVEN"]
    uploaded_image_readback: Literal["AVAILABLE_ACCOUNT_ADIMAGES_HASHES_READ"] = (
        "AVAILABLE_ACCOUNT_ADIMAGES_HASHES_READ"
    )
    provider_image_bytes: Literal["PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ"] = (
        "PROVIDER_BYTES_UNKNOWN_NO_DOCUMENTED_CONTENT_READ"
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
        if any(item not in META_CREATE_CHILD_LABEL_SET for item in value):
            raise ValueError("a child acknowledgement carries only the planned step labels")
        if value != META_CREATE_CHILD_LABELS[: len(value)]:
            raise ValueError("acknowledged children are the ordered prefix of the child plan")
        return value

    @model_validator(mode="after")
    def coherent_observation(self) -> MetaAdsSingleImageHierarchyCreateReadbackObservation:
        if (
            not timedelta(0)
            <= self.observation_interval_end - self.observation_interval_start
            <= _MAX_READBACK_INTERVAL
        ):
            raise ValueError("readback interval is reversed or too long")
        if self.acknowledged_child_count != len(self.acknowledged_child_labels):
            raise ValueError("the acknowledgement count does not describe its own child labels")
        complete = self.attempt_completion == "CHILDREN_COMPLETE"
        if complete != (self.acknowledged_child_count == self.approved_child_count):
            raise ValueError("the completion statement does not describe its own child count")
        observed = (
            self.campaign_resource_ref,
            self.ad_set_resource_ref,
            self.creative_resource_ref,
            self.ad_resource_ref,
        )
        if self.observed_hierarchy_relation in _UNOBSERVED_RELATIONS:
            if (
                self.observed_member_count
                or any(item is not None for item in observed)
                or self.observed_campaign_effective_status is not None
                or self.every_pausable_entity_configured_status != "NOT_OBSERVED"
            ):
                raise ValueError("an unobserved hierarchy cannot carry observed resources")
        else:
            if (
                self.campaign_resource_ref is None
                or self.every_pausable_entity_configured_status == "NOT_OBSERVED"
                or self.observed_campaign_effective_status is None
            ):
                raise ValueError("an observed hierarchy names its campaign and pausable statuses")
            if self.observed_member_count != sum(item is not None for item in observed):
                raise ValueError("the observed member count does not describe its own references")
        # Each unobserved relation is a statement about the acknowledged prefix,
        # so the model refuses one that contradicts its own child journal.
        if self.observed_hierarchy_relation == "READBACK_NOT_ATTEMPTED" and (
            self.acknowledged_child_count != 0
        ):
            raise ValueError("a readback is not attempted only when nothing was acknowledged")
        if (
            self.observed_hierarchy_relation
            in {
                "NO_HIERARCHY_OBSERVED",
                "UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED",
                "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED",
                "READBACK_UNAVAILABLE",
            }
            and self.acknowledged_child_count < 1
        ):
            raise ValueError("a completed or failed readback follows an acknowledged child")
        if self.observed_hierarchy_relation == "NO_HIERARCHY_OBSERVED" and (
            _CAMPAIGN_CHILD_LABEL in self.acknowledged_child_labels
        ):
            raise ValueError(
                "no hierarchy is observed only while no campaign identifier was acknowledged"
            )
        if self.observed_hierarchy_relation == "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED" and (
            _CAMPAIGN_CHILD_LABEL not in self.acknowledged_child_labels
        ):
            raise ValueError(
                "an acknowledged campaign is absent from the listing only after the campaign "
                "identifier was acknowledged"
            )
        complete_hierarchy = self.observed_member_count == META_CREATE_HIERARCHY_MEMBER_COUNT
        if self.observed_hierarchy_relation == "PARTIAL_HIERARCHY_OBSERVED" and complete_hierarchy:
            raise ValueError("a partial hierarchy cannot observe every predicted member")
        if self.observed_hierarchy_relation == "DIVERGENT_HIERARCHY_OBSERVED" and (
            not complete or not complete_hierarchy
        ):
            raise ValueError(
                "a divergent hierarchy is a complete attempt that observed every member"
            )
        if self.observed_hierarchy_relation == "MATCHES_PREDICTED_SEMANTICS":
            if not complete:
                raise ValueError("an incomplete attempt cannot match the predicted hierarchy")
            if not complete_hierarchy:
                raise ValueError("a matching hierarchy observes every predicted member")
            if self.every_pausable_entity_configured_status != "PAUSED":
                raise ValueError("a matching hierarchy has every pausable entity paused")
            if self.image_association != "PROVEN_ACCOUNT_IMAGE_AND_CREATIVE_ASSOCIATION" or (
                self.creative_association != "PROVEN_SAME_ATTEMPT_CREATIVE"
            ):
                raise ValueError("a matching hierarchy requires both proven associations")
        if self.image_association == "PROVEN_ACCOUNT_IMAGE_AND_CREATIVE_ASSOCIATION" and (
            self.image_resource_ref is None or self.creative_resource_ref is None
        ):
            raise ValueError(
                "the image association is proven only by an observed account image and creative"
            )
        if self.creative_association == "PROVEN_SAME_ATTEMPT_CREATIVE" and (
            self.ad_resource_ref is None or self.creative_resource_ref is None
        ):
            raise ValueError("the creative association is proven only by an observed ad")
        return self


def validate_meta_create_readback_history(records: list[dict[str, Any]]) -> None:
    """Use already-scanned records only; never acquire a lock or read a ledger.

    Every retained readback must name one retained
    ``META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_PLAN`` action, restate that action's
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
        if kind == META_CREATE_PLAN_RECORD_KIND:
            action = payload["action"]
            actions[action["action_id"]] = action
        elif kind == "CHANGE_EVENT" and payload.get("event_type") == "APPLY_STARTED":
            starts.append(record)
        elif kind == META_CREATE_READBACK_RECORD_KIND:
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
                    or record.get("provider") != "meta_ads"
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
                ):
                    raise ValueError("readback hierarchy")
                matching = [
                    start
                    for start in starts
                    if start.get("provider") == "meta_ads"
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
                    "INVALID_META_ADS_CREATE_READBACK_HISTORY",
                    "A Meta Ads CREATE readback requires its exact retained action and start.",
                    exit_code=2,
                ) from None


def _instant(value: object) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def meta_create_readback_record_kind_installed() -> bool:
    """Report whether this build's ledger registers the additive readback kind.

    The adapter checks this at factory admission -- before any credential,
    header, or provider request -- so an ordinary build refuses the whole
    execution rather than creating a hierarchy and then failing to retain the
    observation that proves what it created.  This is a build fact, not a
    fallback: nothing here registers the kind, and no path continues without it.
    """

    from adhelm import ledger as ledger_module

    try:
        registered = ledger_module._KINDS["changes"].get(META_CREATE_READBACK_RECORD_KIND)
    except Exception:  # pragma: no cover - a ledger without its own registry
        return False
    return registered is MetaAdsSingleImageHierarchyCreateReadbackObservation


__all__ = [
    "META_CREATE_CHILD_LABEL_SET",
    "META_CREATE_READBACK_RECORD_KIND",
    "MetaAdsSingleImageHierarchyCreateReadbackObservation",
    "meta_create_readback_record_kind_installed",
    "pseudonymized_resource_ref",
    "validate_meta_create_readback_history",
]
