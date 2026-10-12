"""Private journaled child-egress adapter for the paused Meta Ads hierarchy CREATE.

This is the second :class:`adhelm.safety.ChildEgressAdapter` in the repository and
it mirrors ``providers/openai_create_adapter.py`` deliberately.  One approved
operation is five ordered provider requests, because Meta documents no atomic
hierarchy mutation and no way to create an ad creative without an ``image_hash``
that only an upload returns:

```text
upload-image     -> POST /v26.0/act_{id}/adimages     -> {"images": {"<file>": {"hash": ...}}}
create-campaign  -> POST /v26.0/act_{id}/campaigns    -> {"id": "..."}
create-ad-set    -> POST /v26.0/act_{id}/adsets       -> {"id": "..."}
create-creative  -> POST /v26.0/act_{id}/adcreatives  -> {"id": "..."}
create-ad        -> POST /v26.0/act_{id}/ads          -> {"id": "..."}
```

Each child begins with the kernel's durable ``CHILD_APPLY_STARTED``, claims its
one-use permit immediately before its POST, and reports one closed
acknowledgement subset.  Nothing else is retained from a response.

**A failed child after a successful one is not repaired.**  The route stops,
reads back whatever now exists, retains that partial hierarchy, and reports the
subplan ambiguous.  Rule 11 forbids deleting a resource that is not proven
unreferenced, every pausable entity this route creates is ``PAUSED``, and Meta
documents no idempotency key or settlement resource that could make a retry
safe.  The recovery disposition is therefore
``RETAIN_REFS_LEAVE_PAUSED_NO_DELETE``, and this adapter has no delete, remove,
or archive path at all.

**Every credential stays inside the Meta EDIT session.**  This adapter holds no
token, URL, HTTP client, or transport.  It holds one factory-issued
``_FixedMetaAdsHierarchyCreateEgress`` capability whose account is fixed, whose
five edges are the ones above, and whose form-field set is a closed allowlist per
edge, plus the session's own bounded readers.  The EDIT session's two credential
slots -- the inspected subject token and the inspector token -- are rechecked
before every provider request and before the durable readback append.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Never

import httpx

from adhelm import meta_ads_token_edit_session as edit_session
from adhelm.assets import read_image
from adhelm.canonical import digest, parse_json
from adhelm.creative import decode_image
from adhelm.credentials import export_key
from adhelm.egress_permit import EgressPermit
from adhelm.egress_permit import _required as _permit_required
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.meta_create_planning_session import MetaAdsCreateHierarchyObservation
from adhelm.meta_create_readback_records import (
    META_CREATE_READBACK_RECORD_KIND,
    MetaAdsSingleImageHierarchyCreateReadbackObservation,
    meta_create_readback_record_kind_installed,
    pseudonymized_resource_ref,
)
from adhelm.privacy import validate_data
from adhelm.providers.meta_ads import MetaAdsIdentity
from adhelm.providers.meta_compiler import MetaAdsCompiledCampaignSpec
from adhelm.providers.meta_create_contract import (
    META_CREATE_HIERARCHY_MEMBER_COUNT,
    META_CREATE_MAX_UPLOAD_BYTES,
    MetaAdsCreateChild,
    MetaAdsCreativeVaultBinding,
    MetaAdsSingleImageHierarchyCreatePlannedAction,
    checked_image_hash,
    checked_native_identifier,
    create_child_plan,
    fingerprint_key_marker,
    form_value_expressible,
    predicted_states,
    revalidate_meta_ads_create_planned_action,
    semantic_state_from_members,
)
from adhelm.safety import (
    AccountIdentity,
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    EffectStatus,
    KernelExecution,
    OperationKind,
    Reconciliation,
    RemoteIdentity,
    RemoteState,
    StateRef,
    SubPlan,
    Verification,
)
from adhelm.transport import SafeHttpClient

_ADAPTER_ISSUER: Final = object()
_PROOF_ISSUER: Final = object()
_EVIDENCE_PREFIX: Final = "evidence:sha256:"
_MAX_ACK_BYTES: Final = 65_536

#: The neutral multipart filename this route sends.  A customer file name, or a
#: name derived from the local content digest, would publish a local fact the
#: upload does not need.  Meta's ``images`` acknowledgement envelope is keyed by
#: the uploaded part's file name, so this value is also the exact key the
#: acknowledgement parser requires.
_UPLOAD_FILENAMES: Final = {"image/png": "creative.png", "image/jpeg": "creative.jpg"}

#: The closed acknowledgement allowlists.  A response field outside these fails
#: the acknowledgement, which -- after the permit is consumed -- is ambiguous.
_UPLOAD_ACK_FIELDS: Final = frozenset({"images"})
_UPLOAD_IMAGE_FIELDS: Final = frozenset(
    {
        "hash",
        "url",
        "url_128",
        "url_256",
        "url_256_height",
        "url_256_width",
        "height",
        "width",
        "name",
        "id",
    }
)
#: The Marketing API reference's return-type struct for these edges names both
#: ``id`` and ``success``.  This build accepts the single-member ``{"id": ...}``
#: object only; a ``success``-bearing acknowledgement is refused as ambiguous
#: rather than assumed equivalent.  The tension is recorded in
#: ``docs/implementation/meta-create-execution.md``.
_CREATED_ACK_FIELDS: Final = frozenset({"id"})

_RESOURCE_KIND_FOR_CHILD: Final = {
    "UPLOAD_IMAGE": "image",
    "CREATE_CAMPAIGN": "campaign",
    "CREATE_AD_SET": "ad_set",
    "CREATE_CREATIVE": "creative",
    "CREATE_AD": "ad",
}


def _now() -> datetime:
    """Private UTC clock seam used only by synthetic adapter tests."""

    return datetime.now(UTC)


def meta_create_upload_transport_installed() -> bool:
    """Report whether this build installs the bounded multipart upload seam.

    P04-OA added ``SafeHttpClient.upload_image``; this route reuses it exactly as
    shipped.  The check exists because a build that lacks it -- an older
    transport, or a vendored one -- must refuse the whole execution at admission
    rather than reach a credential and then find it cannot upload.
    """

    return callable(getattr(SafeHttpClient, "upload_image", None))


# -- the exact wire bodies ---------------------------------------------------


def _json_field(value: object) -> str:
    """Render one nested Graph form value as bounded printable-ASCII JSON.

    ``ensure_ascii`` matters: the shared transport's fixed mutation form accepts
    printable ASCII only, so a non-ASCII character in compiled copy must be
    escaped rather than refused at egress.
    """

    return json.dumps(value, allow_nan=False, ensure_ascii=True, separators=(",", ":"))


def campaign_request_form(spec: MetaAdsCompiledCampaignSpec) -> dict[str, str]:
    """Return the exact ``POST /act_{id}/campaigns`` form body this route sends."""

    campaign = spec.campaign_request
    if campaign.status != "PAUSED":
        raise ValueError("compiled campaign is not paused")
    return {
        "buying_type": campaign.buying_type,
        "lifetime_budget": str(campaign.lifetime_budget),
        "name": campaign.name,
        "objective": campaign.objective,
        "special_ad_categories": _json_field(list(campaign.special_ad_categories)),
        "start_time": campaign.start_time.astimezone(UTC).isoformat(),
        "status": campaign.status,
        "stop_time": campaign.stop_time.astimezone(UTC).isoformat(),
    }


def adset_request_form(spec: MetaAdsCompiledCampaignSpec, *, campaign_id: str) -> dict[str, str]:
    """Return the exact ``POST /act_{id}/adsets`` form body this route sends."""

    adset = spec.adset_request
    if adset.status != "PAUSED":
        raise ValueError("compiled ad set is not paused")
    targeting = adset.targeting
    return {
        "bid_strategy": adset.bid_strategy,
        "billing_event": adset.billing_event,
        "campaign_id": checked_native_identifier(campaign_id),
        "name": adset.name,
        "optimization_goal": adset.optimization_goal,
        "status": adset.status,
        "targeting": _json_field(
            {
                "facebook_positions": list(targeting.facebook_positions),
                "geo_locations": {"countries": list(targeting.geo_locations.countries)},
                "publisher_platforms": list(targeting.publisher_platforms),
            }
        ),
    }


def creative_request_form(spec: MetaAdsCompiledCampaignSpec, *, image_hash: str) -> dict[str, str]:
    """Return the exact ``POST /act_{id}/adcreatives`` form body this route sends.

    The compiled candidate's own ``image_hash`` is customer-supplied local
    evidence.  It is never sent: the creative must reference the image this
    attempt's own upload child minted, which is the only association a later
    readback can prove.
    """

    creative = spec.creative_request
    story = creative.object_story_spec
    link = story.link_data
    return {
        "name": creative.name,
        "object_story_spec": _json_field(
            {
                "link_data": {
                    "call_to_action": {"type": link.call_to_action.type},
                    "image_hash": checked_image_hash(image_hash),
                    "link": link.link,
                    "message": link.message,
                    "name": link.name,
                },
                "page_id": story.page_id,
            }
        ),
    }


def ad_request_form(
    spec: MetaAdsCompiledCampaignSpec, *, adset_id: str, creative_id: str
) -> dict[str, str]:
    """Return the exact ``POST /act_{id}/ads`` form body this route sends."""

    ad = spec.ad_request
    if ad.status != "PAUSED":
        raise ValueError("compiled ad is not paused")
    return {
        "adset_id": checked_native_identifier(adset_id),
        "creative": _json_field({"creative_id": checked_native_identifier(creative_id)}),
        "name": ad.name,
        "status": ad.status,
    }


def upload_request_parts(creative: MetaAdsCreativeVaultBinding) -> dict[str, Any]:
    """Return the exact non-byte multipart parts of the upload child request."""

    if type(creative) is not MetaAdsCreativeVaultBinding:
        raise ValueError("creative vault binding")
    filename = _UPLOAD_FILENAMES.get(creative.media_type)
    if filename is None or filename != creative.upload_part_filename:
        raise ValueError("creative media type")
    return {
        "field_name": creative.upload_field_name,
        "filename": filename,
        "media_type": creative.media_type,
        "byte_length": creative.byte_length,
    }


# -- the acknowledgement subsets ---------------------------------------------


def upload_acknowledgement(response: object, *, filename: str) -> str:
    """Accept only ``{"images": {"<filename>": {..., "hash": "..."}}}``.

    Meta's Ad Image return type is a two-level map keyed by the uploaded part's
    file name, whose struct carries ``hash`` plus temporary URLs and intrinsic
    dimensions.  Only the ``hash`` is kept, and the envelope must name exactly
    the one neutral file name this route sent.
    """

    if type(response) is not httpx.Response:
        raise ValueError("response type")
    if type(response.status_code) is not int or response.status_code not in (200, 201):
        raise ValueError("status")
    payload = parse_json(response.content, maximum_bytes=_MAX_ACK_BYTES)
    if type(payload) is not dict or set(payload) != _UPLOAD_ACK_FIELDS:
        raise ValueError("acknowledgement shape")
    images = payload["images"]
    if type(images) is not dict or set(images) != {filename}:
        raise ValueError("acknowledgement shape")
    entry = images[filename]
    if type(entry) is not dict or not set(entry) <= _UPLOAD_IMAGE_FIELDS or "hash" not in entry:
        raise ValueError("acknowledgement shape")
    validate_data({"hash": entry["hash"]})
    return checked_image_hash(entry["hash"])


def created_acknowledgement(response: object) -> str:
    """Accept only ``{"id": "<native decimal ID>"}`` and keep the identifier."""

    if type(response) is not httpx.Response:
        raise ValueError("response type")
    if type(response.status_code) is not int or response.status_code not in (200, 201):
        raise ValueError("status")
    payload = parse_json(response.content, maximum_bytes=_MAX_ACK_BYTES)
    if type(payload) is not dict or set(payload) != _CREATED_ACK_FIELDS:
        raise ValueError("acknowledgement shape")
    validate_data(payload)
    return checked_native_identifier(payload["id"])


# -- the adapter -------------------------------------------------------------


class MetaAdsSingleImageHierarchyCreateAdapter:
    """Factory-issued fixed-route child-egress adapter, used only by ``SafetyKernel``."""

    provider: str = "meta_ads"

    __slots__ = (
        "_action",
        "_clock",
        "_created",
        "_egress",
        "_issuer",
        "_ledger",
        "_proof",
        "_proof_issuer",
        "_session",
        "_uploaded_image_hash",
        "_used",
    )

    def __init__(
        self,
        *,
        action: MetaAdsSingleImageHierarchyCreatePlannedAction,
        session: edit_session.MetaAdsTokenEditSession,
        egress: edit_session._FixedMetaAdsHierarchyCreateEgress,
        ledger: Ledger,
        issuer: object,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._action = action
        self._session = session
        self._egress = egress
        self._ledger = ledger
        self._issuer = issuer
        self._clock = clock
        self._proof: tuple[str, ...] | None = None
        self._proof_issuer: object | None = None
        self._uploaded_image_hash = ""
        self._created: dict[str, str] = {}
        self._used = False

    def __repr__(self) -> str:
        return "<MetaAdsSingleImageHierarchyCreateAdapter private fixed route>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta Ads create adapters cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta Ads create adapters cannot be serialized")

    @property
    def action(self) -> MetaAdsSingleImageHierarchyCreatePlannedAction:
        """Return the reconstructed closed action for private composition only."""

        return self._checked_members()[0]

    # -- kernel surface -----------------------------------------------------

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        action = self._check_plan(plan)
        subplan = action.plan.subplans[0]
        return AdapterCapabilities(
            provider_api_version=subplan.provider_api_version,
            capability_version=subplan.capability_version,
            supported_operations=(OperationKind.CREATE,),
            # A factory-scoped guarded route exercised with fake HTTP.  It adds
            # no installed-default or live-conformance claim.
            writes_enabled=True,
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        action, _session, _egress, _ledger = self._checked_members()
        account = action.plan.subplans[0].account
        if type(account) is not AccountIdentity or account_ref != account.account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Meta ad account differs from the approved create plan."
            )
        identity = self._current_identity(action)
        return RemoteIdentity(
            stable_account_id=identity.account_client_ref,
            auth_profile_ref=action.execution_binding.profile_ref,
            principal_ref=action.execution_binding.principal_ref,
            client_application_ref=action.execution_binding.client_application_ref,
            scopes=(action.execution_binding.required_scope,),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        action = self._check_plan(plan)
        state, _observation = self._current_state(action)
        return RemoteState(fingerprint=state.fingerprint, revision=state.revision)

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError(
            "PERMIT_REQUIRED",
            "Meta Ads hierarchy create egress requires kernel-issued child permits.",
        )

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        del plan, idempotency_key, permit
        raise AdhelmError(
            "ADAPTER_CHILD_EGRESS_REQUIRED",
            "This approved Meta Ads operation is several journaled child requests.",
        )

    def apply_children(
        self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
    ) -> ApplyResponse:
        """Issue the approved children in order, and never repair a partial run."""

        action = self._check_plan(plan)
        if self._verified_proof() is not None or self._used:
            raise AdhelmError(
                "META_ADS_CREATE_ADAPTER_USED",
                "The fixed Meta Ads create adapter already handled this action.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Reconcile retained history and compile a new exact plan before any retry."
                ),
            )
        if not self._kernel_started(action, plan):
            raise AdhelmError(
                "KERNEL_START_REQUIRED",
                "Meta Ads create egress requires the kernel's durable start.",
            )
        if (
            execution.expected_child_egress_count != plan.expected_child_egress_count
            or execution.attempt_ref != idempotency_key
            or execution.subplan_digest != plan.subplan_digest
            or execution.plan_digest != action.plan.plan_digest
        ):
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "The kernel execution does not describe the approved Meta Ads subplan.",
            )
        self._used = True

        # Every precondition that can be checked without egress runs here, and
        # each one refuses before the first child's durable start exists.
        before, _observed = self._current_state(action)
        if before != plan.before:
            raise AdhelmError(
                "REMOTE_DRIFT",
                "Meta Ads already holds a campaign with this planned name.",
            )
        content = self._vault_bytes(action)
        children = create_child_plan(action.candidate_binding.specification)
        if len(children) != plan.expected_child_egress_count:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "The compiled hierarchy no longer needs the approved number of requests.",
            )
        self._check_plan_expiry(action)

        started_at = self._checked_now()
        identifiers: dict[str, str] = {}
        acknowledged: list[str] = []
        complete = False
        try:
            for child in children:
                value = self._run_child(
                    action,
                    child,
                    execution=execution,
                    idempotency_key=idempotency_key,
                    content=content,
                    identifiers=identifiers,
                )
                acknowledged.append(child.label)
                identifiers[child.kind] = value
                self._created[child.kind] = value
                if child.kind == "UPLOAD_IMAGE":
                    self._uploaded_image_hash = value
            complete = True
        except edit_session._FixedMetaCreateDispatchUnverified:
            complete = False
        except Exception:
            # A refusal before a later child's permit (an expired plan, broken
            # token continuity, an inexpressible form) sends nothing for that
            # child, but the earlier children already created resources.  Those
            # must still be read back and retained.  A refusal before the first
            # child created nothing and keeps its own precise code.
            if not acknowledged:
                raise
            complete = False

        readback = self._readback(action)
        observation = readback.observation
        matched = (
            complete and observation is not None and self._matches_prediction(action, observation)
        )
        record_digest = self._retain_readback_observation(
            action,
            readback,
            matched=matched,
            attempt_ref=idempotency_key,
            acknowledged_child_labels=tuple(acknowledged),
            complete=complete,
            observation_interval_start=started_at,
            observation_interval_end=self._checked_now(),
        )
        if not matched:
            raise _post_egress_ambiguous()
        proof = self._proof_for(action, tuple(acknowledged), record_digest)
        self._proof = proof
        self._proof_issuer = _PROOF_ISSUER
        return ApplyResponse(
            outcome=ApplyOutcome.CONFIRMED,
            opaque_resource_refs=self._opaque_refs(observation),
            evidence_refs=proof,
        )

    def verify(self, plan: SubPlan) -> Verification:
        """Report the fresh-readback proof this adapter actually established."""

        self._check_plan(plan)
        proof = self._verified_proof()
        return Verification(verified=proof is not None, evidence_refs=proof or ())

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        action = self._check_plan(plan)
        try:
            state, _observation = self._current_state(action)
            current = RemoteState(fingerprint=state.fingerprint, revision=state.revision)
        except Exception:
            current = RemoteState(fingerprint="unavailable")
        # A later matching read cannot attribute a missing or uncertain child
        # acknowledgement to this attempt.  Meta documents no idempotency key,
        # request identifier, or settlement resource, and a second create would
        # duplicate the hierarchy rather than converge.
        return Reconciliation(
            state=current, status=EffectStatus.AMBIGUOUS_REMOTE_STATE, settled=False
        )

    # -- one child ----------------------------------------------------------

    def _run_child(
        self,
        action: MetaAdsSingleImageHierarchyCreatePlannedAction,
        child: MetaAdsCreateChild,
        *,
        execution: KernelExecution,
        idempotency_key: str,
        content: bytes,
        identifiers: dict[str, str],
    ) -> str:
        """Journal, authorize, send and acknowledge exactly one child request.

        The body is built *before* the permit is claimed, so a body-construction
        failure can never become an ambiguous post-egress outcome, and the
        factory-issued capability consumes the permit on the statement
        immediately before its own POST.
        """

        _action, _session, egress, _ledger = self._checked_members()
        creative = action.observation_binding.creative
        parts: dict[str, Any] | None = None
        form: dict[str, str] | None = None
        spec = action.candidate_binding.specification
        if child.kind == "UPLOAD_IMAGE":
            parts = upload_request_parts(creative)
        elif child.kind == "CREATE_CAMPAIGN":
            form = campaign_request_form(spec)
        elif child.kind == "CREATE_AD_SET":
            form = adset_request_form(spec, campaign_id=identifiers["CREATE_CAMPAIGN"])
        elif child.kind == "CREATE_CREATIVE":
            form = creative_request_form(spec, image_hash=identifiers["UPLOAD_IMAGE"])
        else:
            form = ad_request_form(
                spec,
                adset_id=identifiers["CREATE_AD_SET"],
                creative_id=identifiers["CREATE_CREATIVE"],
            )
        # Every built value is checked against the route's form bound *before*
        # the child's durable start exists.  The image hash and the identifiers
        # only exist after earlier children, so planning cannot see them; a
        # residual failure here is a clean refusal between children, with no
        # unresolved ``CHILD_APPLY_STARTED`` for a request that was never sent.
        if form is not None and not all(form_value_expressible(value) for value in form.values()):
            raise AdhelmError(
                "META_ADS_CREATE_REQUEST_NOT_EXPRESSIBLE",
                "A Meta Ads request value cannot be expressed in this build's fixed form body.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Nothing was sent for this request; earlier requests of this attempt are "
                    "read back and retained paused. Reconcile before any new plan."
                ),
            )
        self._check_plan_expiry(action)
        # This local-only continuity check intentionally runs before the child
        # permit exists.  It holds neither configuration nor credential locks
        # across egress.
        egress._preflight()
        self._check_plan_expiry(action)
        with execution.begin_child(child.index, child.label) as permit:
            try:
                if type(permit) is not EgressPermit:
                    raise _permit_required()
                if parts is not None:
                    response = egress._upload_image(
                        permit=permit,
                        attempt_ref=idempotency_key,
                        child_index=child.index,
                        filename=str(parts["filename"]),
                        media_type=str(parts["media_type"]),
                        content=content,
                    )
                    identifier = upload_acknowledgement(response, filename=str(parts["filename"]))
                else:
                    assert form is not None
                    response = egress._create(
                        permit=permit,
                        attempt_ref=idempotency_key,
                        child_index=child.index,
                        edge=child.edge,
                        form=form,
                    )
                    identifier = created_acknowledgement(response)
                execution.report_child(
                    child.index,
                    ApplyResponse(
                        outcome=ApplyOutcome.CONFIRMED,
                        evidence_refs=(self._child_evidence(action, child, identifier),),
                    ),
                )
                return identifier
            except edit_session._FixedMetaCreateDispatchUnverified:
                raise
            except Exception:
                # The permit is consumed by the fixed capability at its own
                # handoff.  No transport, response, or parser failure after that
                # can prove the request never reached the provider.
                raise edit_session._FixedMetaCreateDispatchUnverified() from None

    # -- private members ----------------------------------------------------

    def _checked_members(
        self,
    ) -> tuple[
        MetaAdsSingleImageHierarchyCreatePlannedAction,
        edit_session.MetaAdsTokenEditSession,
        edit_session._FixedMetaAdsHierarchyCreateEgress,
        Ledger,
    ]:
        if type(self) is not MetaAdsSingleImageHierarchyCreateAdapter:
            raise _adapter_invalid()
        try:
            issuer = self._issuer
            action = self._action
            session = self._session
            egress = self._egress
            ledger = self._ledger
        except Exception:
            raise _adapter_invalid() from None
        if (
            issuer is not _ADAPTER_ISSUER
            or type(action) is not MetaAdsSingleImageHierarchyCreatePlannedAction
            or type(session) is not edit_session.MetaAdsTokenEditSession
            or type(ledger) is not Ledger
        ):
            raise _adapter_invalid()
        checked_egress = edit_session._checked_fixed_meta_ads_hierarchy_create_egress(egress)
        (
            egress_core,
            egress_account,
            egress_plan_digest,
            egress_subplan_digest,
        ) = checked_egress._checked_members()
        try:
            session_core = session._checked_core()
        except AdhelmError:
            raise _adapter_invalid() from None
        if (
            egress_core is not session_core
            or egress_account != action.account_binding.account_client_ref
            or egress_plan_digest != action.plan.plan_digest
            or egress_subplan_digest != action.plan.subplans[0].subplan_digest
        ):
            raise _adapter_invalid()
        return action, session, checked_egress, ledger

    def _check_plan(self, plan: object) -> MetaAdsSingleImageHierarchyCreatePlannedAction:
        action, _session, _egress, _ledger = self._checked_members()
        if type(plan) is not SubPlan or plan != action.plan.subplans[0]:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "Adapter refuses a different Meta Ads single-image create plan.",
            )
        return action

    def _current_identity(
        self, action: MetaAdsSingleImageHierarchyCreatePlannedAction
    ) -> MetaAdsIdentity:
        """Prove both EDIT token slots and the account baseline before any write."""

        _action, session, _egress, _ledger = self._checked_members()
        binding = edit_session._checked_binding(session.binding)
        try:
            identity = session._refresh_identity_for_fixed_create()
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None
        if type(identity) is not MetaAdsIdentity:
            raise _adapter_invalid()
        if not _binding_matches_action(action, binding):
            raise _binding_drift()
        if not _identity_matches_action(action, identity):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Current Meta ad-account facts differ from the approved create action.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured Meta ad account and compile a new exact plan.",
            )
        return identity

    def _fingerprint_key(self, action: MetaAdsSingleImageHierarchyCreatePlannedAction) -> bytes:
        return _verified_fingerprint_key(action)

    def _checked_states(
        self, action: MetaAdsSingleImageHierarchyCreatePlannedAction
    ) -> tuple[bytes, StateRef, StateRef]:
        return _verified_local_states(action)

    def _current_state(
        self,
        action: MetaAdsSingleImageHierarchyCreatePlannedAction,
    ) -> tuple[StateRef, MetaAdsCreateHierarchyObservation | None]:
        """Return the semantic state this workspace currently observes.

        Before the first child, the bounded name probe *is* the observation: no
        campaign with this name -- byte-exact or near-variant -- is the approved
        ``before``.  After a child acknowledged, the same probe must find exactly
        the campaign this attempt created, and the readback is then keyed by the
        identifiers this attempt itself received.  A campaign with this name that
        this attempt did not create is drift either way.
        """

        state, observation, conflict = self._inspect(action)
        if conflict is not None:
            raise AdhelmError("REMOTE_DRIFT", conflict.message)
        return state, observation

    def _inspect(
        self,
        action: MetaAdsSingleImageHierarchyCreatePlannedAction,
    ) -> tuple[StateRef, MetaAdsCreateHierarchyObservation | None, _NameConflict | None]:
        """Probe the planned name, then read back what this attempt acknowledged.

        The third element names what the probe showed when it did not find
        exactly the campaign this attempt created, and is ``None`` otherwise.
        Its ``kind`` keeps two different observations apart: the probe *listed*
        a campaign carrying the planned name (or a near variant) that this
        attempt cannot attribute to itself, or the probe listed *no* campaign
        with the planned name although this attempt acknowledged one.  It is
        reported rather than raised so the post-egress readback can record the
        observation that was actually made; the pre-egress callers raise it as
        drift.
        """

        _action, session, _egress, _ledger = self._checked_members()
        key, before, _after = self._checked_states(action)
        self._current_identity(action)
        name = action.observation_binding.campaign_name
        created = self._created_identifiers()
        campaign_client_ref = created.get("CREATE_CAMPAIGN", "")
        probe = session._probe_fixed_create_campaign_name(name)
        if probe.near_matches:
            return (
                before,
                None,
                _NameConflict(
                    "UNATTRIBUTED_NAME",
                    "Meta Ads holds a campaign whose name near-matches the planned name.",
                ),
            )
        if not campaign_client_ref:
            if probe.exact_matches:
                return (
                    before,
                    None,
                    _NameConflict(
                        "UNATTRIBUTED_NAME",
                        "Meta Ads already holds a campaign with this planned name.",
                    ),
                )
            return before, None, None
        if not probe.exact_matches:
            # The campaign identifier was acknowledged, yet the probe lists no
            # campaign with the planned name at all.  A list that lags the create
            # (an UNVERIFIED wire fact) looks exactly like this, so the record
            # states only that the acknowledged campaign was not listed.
            return (
                before,
                None,
                _NameConflict(
                    "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED",
                    "Meta Ads does not list the campaign this attempt created.",
                ),
            )
        if probe.exact_client_refs != (campaign_client_ref,):
            return (
                before,
                None,
                _NameConflict(
                    "UNATTRIBUTED_NAME",
                    "Meta Ads does not hold exactly the campaign this attempt created.",
                ),
            )
        observation = session._observe_fixed_create_hierarchy(
            campaign_client_ref=campaign_client_ref,
            adset_client_ref=created.get("CREATE_AD_SET", ""),
            creative_client_ref=created.get("CREATE_CREATIVE", ""),
            ad_client_ref=created.get("CREATE_AD", ""),
            uploaded_image_hash=created.get("UPLOAD_IMAGE", ""),
            creative_content_digest=action.observation_binding.creative.content_digest,
        )
        state = semantic_state_from_members(
            observation.members,
            account_client_ref=action.account_binding.account_client_ref,
            campaign_name=name,
            fingerprint_key=key,
        )
        return state, observation, None

    def _created_identifiers(self) -> dict[str, str]:
        """Return only the identifiers this attempt's own children acknowledged."""

        value = self._created
        if type(value) is not dict or any(
            type(key) is not str or type(item) is not str for key, item in value.items()
        ):
            raise _adapter_invalid()
        return dict(value)

    def _vault_bytes(self, action: MetaAdsSingleImageHierarchyCreatePlannedAction) -> bytes:
        """Read the declared creative from the private vault and re-verify it."""

        return _verified_vault_bytes(self._ledger.root, action)

    def _matches_prediction(
        self,
        action: MetaAdsSingleImageHierarchyCreatePlannedAction,
        observation: MetaAdsCreateHierarchyObservation,
    ) -> bool:
        key, _before, after = self._checked_states(action)
        observed = semantic_state_from_members(
            observation.members,
            account_client_ref=action.account_binding.account_client_ref,
            campaign_name=action.observation_binding.campaign_name,
            fingerprint_key=key,
        )
        return (
            observation.projection is not None
            and observed == after
            and observation.image_association_proven
            and observation.creative_association_proven
        )

    def _readback(
        self,
        action: MetaAdsSingleImageHierarchyCreatePlannedAction,
    ) -> _Readback:
        """Say what the post-egress readback did, never more than it did.

        With no acknowledged identifier there is nothing to read by identifier
        and no read is made.  Otherwise the planned-name probe runs, and then the
        acknowledged nodes are read.  A probe that listed a campaign this attempt
        cannot attribute, a probe that did not list the campaign this attempt
        acknowledged, and a read that raised, are different statements about a
        resource a child may have created, and none is "nothing exists".
        """

        if not self._created_identifiers():
            return _Readback(None, "NOTHING_ACKNOWLEDGED")
        try:
            _state, observation, conflict = self._inspect(action)
        except Exception:
            return _Readback(None, "FAILED")
        if conflict is not None:
            return _Readback(None, conflict.kind)
        if observation is None:
            return _Readback(None, "NO_CAMPAIGN_LISTED")
        return _Readback(observation, "OBSERVED")

    def _kernel_started(
        self, action: MetaAdsSingleImageHierarchyCreatePlannedAction, plan: SubPlan
    ) -> bool:
        try:
            return any(
                row.get("payload", {}).get("event_type") == "APPLY_STARTED"
                and row.get("payload", {}).get("plan_digest") == action.plan.plan_digest
                and row.get("payload", {}).get("subplan_digest") == plan.subplan_digest
                for row in self._ledger.records("changes")
                if type(row) is dict and type(row.get("payload")) is dict
            )
        except Exception:
            return False

    def _checked_now(self) -> datetime:
        try:
            current = self._clock()
        except Exception:
            raise AdhelmError("CLOCK_INVALID", "Meta Ads create clock is unavailable.") from None
        if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
            raise AdhelmError("CLOCK_INVALID", "Meta Ads create clock is invalid.")
        return current.astimezone(UTC)

    def _check_plan_expiry(self, action: MetaAdsSingleImageHierarchyCreatePlannedAction) -> None:
        if action.plan.expires_at <= self._checked_now():
            raise AdhelmError(
                "PLAN_EXPIRED",
                "Meta Ads create plan expired before provider egress.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Compile and approve a new exact Meta Ads create plan.",
            )

    @staticmethod
    def _child_evidence(
        action: MetaAdsSingleImageHierarchyCreatePlannedAction,
        child: MetaAdsCreateChild,
        identifier: str,
    ) -> str:
        """Digest one child acknowledgement without publishing its identifier."""

        return _EVIDENCE_PREFIX + digest(
            {
                "kind": "meta_ads_v26_single_image_create_child_acknowledgement",
                "action_id": action.action_id,
                "plan_digest": action.plan.plan_digest,
                "child_index": child.index,
                "child_label": child.label,
                "child_kind": child.kind,
                "resource_ref": pseudonymized_resource_ref(
                    _RESOURCE_KIND_FOR_CHILD[child.kind], identifier
                ),
            }
        )

    @staticmethod
    def _proof_for(
        action: MetaAdsSingleImageHierarchyCreatePlannedAction,
        acknowledged: tuple[str, ...],
        readback_record_digest: str,
    ) -> tuple[str, str]:
        sequence = _EVIDENCE_PREFIX + digest(
            {
                "kind": "meta_ads_v26_single_image_create_child_sequence",
                "action_id": action.action_id,
                "plan_digest": action.plan.plan_digest,
                "child_labels": list(acknowledged),
            }
        )
        if type(readback_record_digest) is not str or len(readback_record_digest) != 64:
            raise ValueError("readback record")
        return sequence, _EVIDENCE_PREFIX + readback_record_digest

    @staticmethod
    def _opaque_refs(
        observation: MetaAdsCreateHierarchyObservation | None,
    ) -> tuple[str, ...]:
        """Name every created resource as the kernel's opaque reference shape.

        The material is the same keyless pseudonym the readback record retains,
        re-prefixed as ``opaque:sha256:`` because that is the only reference
        form ``ApplyResponse`` accepts.  No native identifier is published.
        """

        if observation is None:
            return ()
        pseudonyms = [
            pseudonymized_resource_ref(kind, facts.client_ref)
            for kind, facts in (
                ("campaign", observation.campaign),
                ("ad_set", observation.adset),
                ("creative", observation.creative),
                ("ad", observation.ad),
            )
            if facts is not None
        ]
        return tuple("opaque:sha256:" + item.rsplit(":", 1)[1] for item in pseudonyms)

    def _retain_readback_observation(
        self,
        action: MetaAdsSingleImageHierarchyCreatePlannedAction,
        readback: _Readback,
        *,
        matched: bool,
        attempt_ref: str,
        acknowledged_child_labels: tuple[str, ...],
        complete: bool,
        observation_interval_start: datetime,
        observation_interval_end: datetime,
    ) -> str:
        """Append one dedicated post-acknowledgement observation, partial or not.

        The network reads have already completed before entering
        ``retention_recheck``.  Its callback only rechecks the currently-held
        configuration and credential transaction immediately before the durable
        append; it performs no network or ledger I/O itself.
        """

        _action, session, _egress, ledger = self._checked_members()
        subplan = action.plan.subplans[0]
        creative = action.observation_binding.creative
        observation = readback.observation
        found = observation is not None and observation.campaign is not None
        relation = _hierarchy_relation(readback, matched=matched)
        image_proven = bool(observation is not None and observation.image_association_proven)
        creative_proven = bool(observation is not None and observation.creative_association_proven)
        payload = MetaAdsSingleImageHierarchyCreateReadbackObservation(
            action_id=action.action_id,
            plan_digest=action.plan.plan_digest,
            subplan_digest=subplan.subplan_digest,
            attempt_ref=attempt_ref,
            account_ref=action.account_binding.account_ref,
            observation_interval_start=observation_interval_start,
            observation_interval_end=observation_interval_end,
            acknowledged_child_count=len(acknowledged_child_labels),
            acknowledged_child_labels=acknowledged_child_labels,
            attempt_completion="CHILDREN_COMPLETE" if complete else "CHILDREN_INCOMPLETE",
            observed_hierarchy_relation=relation,
            observed_member_count=0 if not found else _observed_reference_count(observation),
            every_pausable_entity_configured_status=_pausable_status(observation),
            campaign_resource_ref=_ref("campaign", observation, "campaign"),
            ad_set_resource_ref=_ref("ad_set", observation, "adset"),
            creative_resource_ref=_ref("creative", observation, "creative"),
            ad_resource_ref=_ref("ad", observation, "ad"),
            observed_campaign_effective_status=(
                None
                if not found or observation is None or observation.campaign is None
                else observation.campaign.effective_status
            ),
            creative_content_digest=creative.content_digest,
            creative_byte_length=creative.byte_length,
            creative_media_type=creative.media_type,
            image_resource_ref=(
                pseudonymized_resource_ref("image", self._uploaded_image_hash)
                if self._uploaded_image_hash and image_proven
                else None
            ),
            image_association=(
                "PROVEN_ACCOUNT_IMAGE_AND_CREATIVE_ASSOCIATION" if image_proven else "NOT_PROVEN"
            ),
            creative_association=(
                "PROVEN_SAME_ATTEMPT_CREATIVE" if creative_proven else "NOT_PROVEN"
            ),
        ).model_dump(mode="json", warnings="error")
        with session.retention_recheck() as recheck:
            record = ledger.append(
                "changes",
                META_CREATE_READBACK_RECORD_KIND,
                payload,
                provider="meta_ads",
                account_ref=action.account_binding.account_ref,
                run_id=action.plan.plan_id,
                observed_at=payload["observation_interval_end"],
                occurred_at=payload["observation_interval_end"],
                _before_durable_append=recheck,
            )
        record_digest = record.get("record_digest")
        if type(record_digest) is not str or len(record_digest) != 64:
            raise ValueError("readback record")
        return record_digest

    def _verified_proof(self) -> tuple[str, ...] | None:
        try:
            proof = self._proof
            issuer = self._proof_issuer
        except Exception:
            raise _adapter_invalid() from None
        if proof is None and issuer is None:
            return None
        if (
            issuer is not _PROOF_ISSUER
            or type(proof) is not tuple
            or len(proof) != 2
            or any(
                type(item) is not str
                or not item.startswith(_EVIDENCE_PREFIX)
                or len(item) != len(_EVIDENCE_PREFIX) + 64
                for item in proof
            )
        ):
            raise _adapter_invalid()
        return proof


def _binding_drift() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_EDIT_BINDING_DRIFT",
        "Current Meta EDIT token facts differ from the approved create action.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Inspect local Meta credentials and compile a new exact plan.",
    )


def _binding_matches_action(
    action: MetaAdsSingleImageHierarchyCreatePlannedAction,
    binding: edit_session.MetaAdsTokenEditBinding,
) -> bool:
    """Report whether the EDIT token binding still reproduces the plan's binding.

    The single predicate factory admission and every in-apply identity read use,
    so the two can never disagree about what counts as the approved credential
    context.  Timestamps are compared as bounds, never as immutable values.
    """

    execution = action.execution_binding
    return not (
        binding.profile_ref != execution.profile_ref
        or binding.client_application_ref != execution.client_application_ref
        or binding.principal_ref != execution.principal_ref
        or binding.required_scope != execution.required_scope
        or binding.credential_binding_ref != execution.credential_binding_ref
        or binding.configuration_digest != execution.configuration_digest
        or binding.profile_configuration_digest != execution.profile_configuration_digest
        or action.plan.expires_at > binding.expires_at
        or action.plan.expires_at > binding.data_access_expires_at
    )


def _identity_matches_action(
    action: MetaAdsSingleImageHierarchyCreatePlannedAction, identity: MetaAdsIdentity
) -> bool:
    """Report whether the live ad-account facts still reproduce the plan's account."""

    account = action.account_binding
    return not (
        identity.account_ref != account.account_ref
        or identity.account_client_ref != account.account_client_ref
        or identity.timezone != account.timezone
        or identity.currency != account.currency
        or identity.status != account.account_status
        or identity.provider_api_version != account.provider_api_version
    )


def _verified_fingerprint_key(action: MetaAdsSingleImageHierarchyCreatePlannedAction) -> bytes:
    key = export_key()
    if fingerprint_key_marker(key) != action.observation_binding.fingerprint_key_marker:
        raise AdhelmError(
            "META_ADS_CREATE_FINGERPRINT_KEY_CHANGED",
            "The local fingerprint key differs from the one this plan was projected under.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Compile a new exact plan from a fresh bounded read.",
        )
    return key


def _verified_local_states(
    action: MetaAdsSingleImageHierarchyCreatePlannedAction,
) -> tuple[bytes, StateRef, StateRef]:
    """Re-project the retained plan's semantic states under the current local key."""

    key = _verified_fingerprint_key(action)
    subplan = action.plan.subplans[0]
    before, after = predicted_states(action, fingerprint_key=key)
    if before != subplan.before or after != subplan.after:
        raise AdhelmError(
            "META_ADS_CREATE_STATE_UNREPRODUCIBLE",
            "The retained plan's semantic states are not reproducible locally.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Compile a new exact plan from a fresh bounded read.",
        )
    return key, before, after


def _verified_vault_bytes(
    root: Path, action: MetaAdsSingleImageHierarchyCreatePlannedAction
) -> bytes:
    """Read the declared creative from the private vault and re-verify it.

    The vault read already rehashes the object.  This additionally re-decodes
    the bytes, so a plan compiled against one image cannot upload another with
    the same declared media type, size or dimensions.
    """

    creative = action.observation_binding.creative
    data = read_image(root, creative.content_digest)
    decoded = decode_image(data)
    if (
        len(data) != creative.byte_length
        or len(data) > META_CREATE_MAX_UPLOAD_BYTES
        or decoded.mime_type != creative.media_type
        or decoded.width_px != creative.width_px
        or decoded.height_px != creative.height_px
    ):
        raise AdhelmError(
            "META_ADS_CREATE_CREATIVE_DRIFT",
            "The private vault object differs from the creative this plan bound.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Register the creative again and compile a new exact plan; nothing was uploaded."
            ),
        )
    return data


@dataclass(frozen=True, slots=True)
class _Readback:
    """What the post-egress readback did, as one closed outcome plus its facts."""

    observation: MetaAdsCreateHierarchyObservation | None
    outcome: Literal[
        "NOTHING_ACKNOWLEDGED",
        "NO_CAMPAIGN_LISTED",
        "UNATTRIBUTED_NAME",
        "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED",
        "FAILED",
        "OBSERVED",
    ]


@dataclass(frozen=True, slots=True)
class _NameConflict:
    """What the planned-name probe showed when it did not find this attempt's campaign."""

    kind: Literal["UNATTRIBUTED_NAME", "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED"]
    message: str


def _hierarchy_relation(
    readback: _Readback, *, matched: bool
) -> Literal[
    "MATCHES_PREDICTED_SEMANTICS",
    "DIVERGENT_HIERARCHY_OBSERVED",
    "PARTIAL_HIERARCHY_OBSERVED",
    "NO_HIERARCHY_OBSERVED",
    "READBACK_NOT_ATTEMPTED",
    "UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED",
    "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED",
    "READBACK_UNAVAILABLE",
]:
    """Say what the readback found, never more than it observed."""

    if readback.outcome == "NOTHING_ACKNOWLEDGED":
        return "READBACK_NOT_ATTEMPTED"
    if readback.outcome == "FAILED":
        return "READBACK_UNAVAILABLE"
    if readback.outcome == "UNATTRIBUTED_NAME":
        return "UNATTRIBUTED_CAMPAIGN_NAME_OBSERVED"
    if readback.outcome == "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED":
        return "ACKNOWLEDGED_CAMPAIGN_NOT_LISTED"
    if readback.outcome == "NO_CAMPAIGN_LISTED":
        return "NO_HIERARCHY_OBSERVED"
    if matched:
        return "MATCHES_PREDICTED_SEMANTICS"
    if _observed_reference_count(readback.observation) == META_CREATE_HIERARCHY_MEMBER_COUNT:
        return "DIVERGENT_HIERARCHY_OBSERVED"
    return "PARTIAL_HIERARCHY_OBSERVED"


def _pausable_status(
    observation: MetaAdsCreateHierarchyObservation | None,
) -> Literal["PAUSED", "NOT_ALL_PAUSED", "NOT_OBSERVED"]:
    """Report the observed configured status of the campaign, ad set and ad.

    The ad creative is not a pausable Meta entity and has no status to report.
    """

    if observation is None or observation.campaign is None:
        return "NOT_OBSERVED"
    pausable = (observation.campaign, observation.adset, observation.ad)
    if all(item.configured_status == "PAUSED" for item in pausable if item is not None):
        return "PAUSED"
    return "NOT_ALL_PAUSED"


def _observed_reference_count(observation: MetaAdsCreateHierarchyObservation | None) -> int:
    if observation is None:
        return 0
    return sum(
        item is not None
        for item in (
            observation.campaign,
            observation.adset,
            observation.creative,
            observation.ad,
        )
    )


def _ref(
    kind: str, observation: MetaAdsCreateHierarchyObservation | None, attribute: str
) -> str | None:
    if observation is None or observation.campaign is None:
        return None
    facts = getattr(observation, attribute)
    if facts is None:
        return None
    return pseudonymized_resource_ref(kind, facts.client_ref)


@contextmanager
def _configured_meta_ads_create_adapter(
    directory: Path,
    action: MetaAdsSingleImageHierarchyCreatePlannedAction,
    ledger: Ledger,
    *,
    clock: Callable[[], datetime] = _now,
) -> Iterator[MetaAdsSingleImageHierarchyCreateAdapter]:
    """Yield one configured fixed-route Meta Ads CREATE child-egress adapter.

    The composition uses only the action's closed EDIT profile and exact
    configuration digest.  It has no public token/client/URL input and does not
    mutate until a safety-kernel child permit reaches ``apply_children``.
    """

    if not isinstance(directory, Path) or type(ledger) is not Ledger:
        raise _adapter_invalid()
    checked_action = revalidate_meta_ads_create_planned_action(action)
    if ledger.root != directory or ledger.profile_ref != checked_action.plan.profile_ref:
        raise _adapter_invalid()
    if ledger.profile_ref != checked_action.workspace_binding.workspace_profile_ref:
        raise _adapter_invalid()
    # Two fail-closed build checks, before any credential, header, or provider
    # request.  Neither is a fallback: no path continues without them.
    if not meta_create_readback_record_kind_installed():
        raise AdhelmError(
            "META_ADS_CREATE_READBACK_RECORD_KIND_NOT_INSTALLED",
            "This build's ledger does not register the Meta Ads CREATE readback record.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a build whose ledger registers "
                f"{META_CREATE_READBACK_RECORD_KIND}; no credential was read and no provider "
                "request was made."
            ),
        )
    if not meta_create_upload_transport_installed():
        raise AdhelmError(
            "META_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED",
            "This build's transport installs no bounded multipart image upload.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a build whose SafeHttpClient exposes the reviewed bounded "
                "upload_image seam; no credential was read and no provider request was made."
            ),
        )
    # The purely local re-checks refuse here, under their own codes, before the
    # Meta EDIT credential session opens and before the kernel's
    # ``APPLY_STARTED``.  The fingerprint-key check reads the local OS-store
    # export key, exactly as planning already does; no Meta token is read.
    # Inside ``apply_children`` the kernel turns every adapter exception into an
    # ambiguous outcome with no diagnostic code and blocks the workspace, so a
    # refusal that needs no provider read must not wait for it.  The same checks
    # run again in ``apply_children`` as defense in depth.
    _verified_local_states(checked_action)
    _verified_vault_bytes(directory, checked_action)
    with edit_session.configured_meta_ads_token_edit_session(
        directory,
        checked_action.execution_binding.profile_ref,
        expected_configuration_digest=checked_action.execution_binding.configuration_digest,
    ) as session:
        # The initial session already performs one account GET.  It establishes
        # factory admission; execution performs fresh identity reads again.
        _assert_factory_facts(checked_action, session)
        egress = edit_session._issue_fixed_meta_ads_hierarchy_create_egress(
            session,
            account_client_ref=checked_action.account_binding.account_client_ref,
            plan_digest=checked_action.plan.plan_digest,
            subplan_digest=checked_action.plan.subplans[0].subplan_digest,
        )
        yield MetaAdsSingleImageHierarchyCreateAdapter(
            action=checked_action,
            session=session,
            egress=egress,
            ledger=ledger,
            issuer=_ADAPTER_ISSUER,
            clock=clock,
        )


def _assert_factory_facts(
    action: MetaAdsSingleImageHierarchyCreatePlannedAction,
    session: edit_session.MetaAdsTokenEditSession,
) -> None:
    """Validate the initial EDIT session without treating timestamps as immutable."""

    binding = edit_session._checked_binding(session.binding)
    if not _binding_matches_action(action, binding) or not _identity_matches_action(
        action, session.identity
    ):
        raise _binding_drift()


def _adapter_invalid() -> AdhelmError:
    return AdhelmError(
        "META_ADS_CREATE_ADAPTER_INVALID",
        "Meta Ads single-image create adapter state is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Start a new fixed Meta Ads create operation from local state.",
    )


def _post_egress_ambiguous() -> AdhelmError:
    return AdhelmError(
        "AMBIGUOUS_REMOTE_STATE",
        "Meta Ads hierarchy create outcome requires reconciliation before any new plan.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Reconcile remote state and compile a new exact plan before any retry; anything "
            "this attempt created is paused and must not be deleted."
        ),
    )


__all__ = [
    "MetaAdsSingleImageHierarchyCreateAdapter",
    "ad_request_form",
    "adset_request_form",
    "campaign_request_form",
    "created_acknowledgement",
    "creative_request_form",
    "meta_create_upload_transport_installed",
    "upload_acknowledgement",
    "upload_request_parts",
]
