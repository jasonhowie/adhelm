"""Private journaled child-egress adapter for the paused OpenAI Ads hierarchy CREATE.

This is the first adapter in the repository that implements
:class:`adhelm.safety.ChildEgressAdapter`.  One approved operation is four or more
ordered provider requests, because OpenAI Ads documents no atomic hierarchy
mutation and no way to create an ad without a ``file_id`` that only an upload
returns:

```text
upload-creative  -> POST /v1/upload      -> {"file_id": "file_..."}
create-campaign  -> POST /v1/campaigns   -> the campaign resource object
create-ad-group  -> POST /v1/ad_groups   -> the ad-group resource object   (xN)
create-ad        -> POST /v1/ads         -> the ad resource object         (xM)
```

Each child begins with the kernel's durable ``CHILD_APPLY_STARTED``, claims its
one-use permit immediately before its POST, and reports one closed
acknowledgement subset.  Nothing else is retained from a response.

**A failed child after a successful one is not repaired.**  The route stops,
reads back whatever now exists, retains that partial hierarchy, and reports the
subplan ambiguous.  Rule 11 forbids deleting a resource that is not proven
unreferenced, everything this route creates is paused, and OpenAI documents no
idempotency key or settlement resource that could make a retry safe
(re-verified 2026-09-09).  The recovery disposition is therefore
``RETAIN_REFS_LEAVE_PAUSED_NO_DELETE``, and this adapter has no delete, remove,
or archive path at all.

**The upload needs one bounded multipart seam.**
:class:`adhelm.transport.SafeHttpClient` previously supported JSON, query and
urlencoded form bodies only.  The documented binary upload is
``-F "file=@..."``, and the alternative documented form (``{"image_url": ...}``)
would make OpenAI fetch a customer asset from some other host instead of sending
the private-vault bytes -- a different trust boundary, and one that would not
read the creative from the vault at all.  This ticket therefore adds the single
additive ``SafeHttpClient.upload_image``: one part, a fixed field name, a neutral
generated filename, a declared image media type, an in-memory byte bound, no
caller-set content headers, and single-shot mutation semantics.  A build that
does not install it is refused at factory admission -- before any credential,
header, or provider request -- with
``OPENAI_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED``.  That is a fail-closed
build check, never a fallback that uploads some other way.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Never

import httpx

from adhelm.assets import read_image
from adhelm.canonical import digest, parse_json
from adhelm.creative import decode_image
from adhelm.credentials import export_key
from adhelm.egress_permit import EgressPermit
from adhelm.egress_permit import _required as _permit_required
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.openai_create_planning_session import (
    OpenAIAdsCreateHierarchyObservation,
    OpenAIAdsCreateReader,
)
from adhelm.openai_create_readback_records import (
    OPENAI_CREATE_READBACK_RECORD_KIND,
    OpenAIAdsChatCardHierarchyCreateReadbackObservation,
    openai_create_readback_record_kind_installed,
    pseudonymized_resource_ref,
)
from adhelm.privacy import validate_data
from adhelm.providers.openai_ads import OpenAIAdsIdentity
from adhelm.providers.openai_create_contract import (
    OPENAI_CREATE_API_VERSION,
    OPENAI_CREATE_HOST,
    OPENAI_CREATE_MAX_UPLOAD_BYTES,
    OPENAI_CREATE_UPLOAD_FIELD,
    OpenAIAdsChatCardHierarchyCreatePlannedAction,
    OpenAIAdsCreateChild,
    OpenAIAdsCreativeVaultBinding,
    checked_file_identifier,
    checked_native_identifier,
    create_child_plan,
    fingerprint_key_marker,
    predicted_states,
    semantic_state_from_members,
)
from adhelm.providers.openai_pause import OpenAIPauseCredentials, _credential_binding
from adhelm.safety import (
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    EffectStatus,
    KernelExecution,
    OpenAIAccountKeyIdentity,
    OperationKind,
    Reconciliation,
    RemoteOpenAIAccountKeyIdentity,
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
#: upload does not need.
_UPLOAD_FILENAMES: Final = {"image/png": "creative.png", "image/jpeg": "creative.jpg"}

#: The closed acknowledgement allowlists.  A response field outside these fails
#: the acknowledgement, which -- after the permit is consumed -- is ambiguous.
_UPLOAD_ACK_FIELDS: Final = frozenset({"file_id"})
_CAMPAIGN_ACK_FIELDS: Final = frozenset(
    {
        "id",
        "ad_account_id",
        "created_at",
        "updated_at",
        "name",
        "description",
        "status",
        "start_time",
        "end_time",
        "budget",
        "bidding_type",
        "targeting",
    }
)
_AD_GROUP_ACK_FIELDS: Final = frozenset(
    {"id", "created_at", "updated_at", "name", "description", "status", "bidding_config"}
)
_AD_ACK_FIELDS: Final = frozenset(
    {"id", "name", "created_at", "updated_at", "creative", "status", "review_status"}
)


class _FixedOpenAICreateDispatchUnverified(Exception):
    """Internal marker for a child failure after its kernel permit was consumed."""


def _now() -> datetime:
    """Private UTC clock seam used only by synthetic adapter tests."""

    return datetime.now(UTC)


def openai_create_upload_transport_installed() -> bool:
    """Report whether this build installs the bounded multipart upload seam.

    This ticket adds the seam, so an ordinary build of this branch answers
    ``True``.  The check exists because a build that lacks it -- an older
    transport, or a vendored one -- must refuse the whole execution at admission
    rather than reach a credential and then find it cannot upload.
    """

    return callable(getattr(SafeHttpClient, "upload_image", None))


# -- the exact wire bodies ---------------------------------------------------


def campaign_request_body(action: OpenAIAdsChatCardHierarchyCreatePlannedAction) -> dict[str, Any]:
    """Return the exact ``POST /v1/campaigns`` body this route sends."""

    body = action.candidate_binding.specification.campaign_request.model_dump(
        mode="json", warnings="error"
    )
    if body.get("status") != "paused":
        raise ValueError("compiled campaign is not paused")
    return body


def ad_group_request_body(
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
    ad_group_handle: str,
    *,
    campaign_id: str,
) -> dict[str, Any]:
    """Return the exact ``POST /v1/ad_groups`` body for one compiled group."""

    group = _compiled_group(action, ad_group_handle)
    body = group.request.model_dump(mode="json", warnings="error")
    if body.get("status") != "paused":
        raise ValueError("compiled ad group is not paused")
    return {"campaign_id": checked_native_identifier(campaign_id), **body}


def ad_request_body(
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
    ad_group_handle: str,
    ad_handle: str,
    *,
    ad_group_id: str,
    file_id: str,
) -> dict[str, Any]:
    """Return the exact ``POST /v1/ads`` body for one compiled chat-card ad.

    The compiled candidate's own ``creative.file_id`` is customer-supplied local
    evidence.  It is never sent: an ad must reference the file this attempt's
    own upload child minted, which is the only association a later readback can
    prove.
    """

    group = _compiled_group(action, ad_group_handle)
    matching = [item for item in group.ads if item.ad_handle == ad_handle]
    if len(matching) != 1:
        raise ValueError("compiled ad handle")
    body = matching[0].request.model_dump(mode="json", warnings="error")
    creative = body.get("creative")
    if body.get("status") != "paused" or type(creative) is not dict:
        raise ValueError("compiled ad is not a paused chat card")
    creative["file_id"] = checked_file_identifier(file_id)
    return {"ad_group_id": checked_native_identifier(ad_group_id), **body}


def _compiled_group(
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction, ad_group_handle: str
) -> Any:
    matching = [
        group
        for group in action.candidate_binding.specification.ad_groups
        if group.ad_group_handle == ad_group_handle
    ]
    if len(matching) != 1:
        raise ValueError("compiled ad group handle")
    return matching[0]


def upload_request_parts(creative: OpenAIAdsCreativeVaultBinding) -> dict[str, Any]:
    """Return the exact non-byte multipart parts of the upload child request."""

    if type(creative) is not OpenAIAdsCreativeVaultBinding:
        raise ValueError("creative vault binding")
    filename = _UPLOAD_FILENAMES.get(creative.media_type)
    if filename is None:
        raise ValueError("creative media type")
    return {
        "field_name": OPENAI_CREATE_UPLOAD_FIELD,
        "filename": filename,
        "media_type": creative.media_type,
        "byte_length": creative.byte_length,
    }


# -- the acknowledgement subsets ---------------------------------------------


def _ack_object(response: object, allowed: frozenset[str], *, require_paused: bool) -> str:
    """Accept one closed resource acknowledgement and keep only its identifier."""

    if type(response) is not httpx.Response:
        raise ValueError("response type")
    if type(response.status_code) is not int or response.status_code not in (200, 201):
        raise ValueError("status")
    payload = parse_json(response.content, maximum_bytes=_MAX_ACK_BYTES)
    if type(payload) is not dict or not set(payload) <= allowed or "id" not in payload:
        raise ValueError("acknowledgement shape")
    validate_data(payload)
    if require_paused and payload.get("status") != "paused":
        raise ValueError("acknowledged resource is not paused")
    return checked_native_identifier(payload["id"])


def upload_acknowledgement(response: object) -> str:
    """Accept only ``{"file_id": "file_..."}``.

    The Files reference's own response example is exactly that one member
    (re-verified 2026-09-09).  There is no documented file ``GET``, so this
    acknowledgement is the only provider fact this route ever learns about the
    uploaded object.
    """

    if type(response) is not httpx.Response:
        raise ValueError("response type")
    if type(response.status_code) is not int or response.status_code not in (200, 201):
        raise ValueError("status")
    payload = parse_json(response.content, maximum_bytes=_MAX_ACK_BYTES)
    if type(payload) is not dict or set(payload) != _UPLOAD_ACK_FIELDS:
        raise ValueError("acknowledgement shape")
    validate_data(payload)
    return checked_file_identifier(payload["file_id"])


def campaign_acknowledgement(response: object) -> str:
    return _ack_object(response, _CAMPAIGN_ACK_FIELDS, require_paused=True)


def ad_group_acknowledgement(response: object) -> str:
    return _ack_object(response, _AD_GROUP_ACK_FIELDS, require_paused=True)


def ad_acknowledgement(response: object) -> str:
    return _ack_object(response, _AD_ACK_FIELDS, require_paused=True)


# -- the adapter -------------------------------------------------------------


class OpenAIAdsChatCardHierarchyCreateAdapter:
    """Factory-issued fixed-route child-egress adapter, used only by ``SafetyKernel``."""

    provider: str = "openai_ads"

    __slots__ = (
        "_action",
        "_clock",
        "_credential_supplier",
        "_issuer",
        "_ledger",
        "_proof",
        "_proof_issuer",
        "_transport",
        "_uploaded_file_id",
        "_used",
    )

    def __init__(
        self,
        *,
        action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
        transport: SafeHttpClient,
        credential_supplier: Callable[[], OpenAIPauseCredentials],
        ledger: Ledger,
        issuer: object,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._action = action
        self._transport = transport
        self._credential_supplier = credential_supplier
        self._ledger = ledger
        self._issuer = issuer
        self._clock = clock
        self._proof: tuple[str, ...] | None = None
        self._proof_issuer: object | None = None
        self._uploaded_file_id = ""
        self._used = False

    def __repr__(self) -> str:
        return "<OpenAIAdsChatCardHierarchyCreateAdapter private fixed route>"

    def __reduce__(self) -> Never:
        raise TypeError("OpenAI Ads create adapters cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("OpenAI Ads create adapters cannot be serialized")

    @property
    def action(self) -> OpenAIAdsChatCardHierarchyCreatePlannedAction:
        """Return the reconstructed closed action for private composition only."""

        return self._checked_action()

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

    def read_identity(self, account_ref: str) -> RemoteOpenAIAccountKeyIdentity:
        action = self._checked_action()
        account = action.plan.subplans[0].account
        if type(account) is not OpenAIAccountKeyIdentity or account_ref != account.account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "OpenAI ad account differs from the approved create plan.",
            )
        credentials = self._credentials()
        self._current_identity(action, credentials)
        return RemoteOpenAIAccountKeyIdentity(
            stable_account_id=action.account_binding.account_client_ref,
            auth_profile_ref=credentials.profile_ref,
            credential_binding_ref=_credential_binding(credentials),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        action = self._check_plan(plan)
        state, _observation = self._current_state(action, self._credentials())
        return RemoteState(fingerprint=state.fingerprint, revision=state.revision)

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError(
            "PERMIT_REQUIRED",
            "OpenAI Ads hierarchy create egress requires kernel-issued child permits.",
        )

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        del plan, idempotency_key, permit
        raise AdhelmError(
            "ADAPTER_CHILD_EGRESS_REQUIRED",
            "This approved OpenAI Ads operation is several journaled child requests.",
        )

    def apply_children(
        self, plan: SubPlan, *, idempotency_key: str, execution: KernelExecution
    ) -> ApplyResponse:
        """Issue the approved children in order, and never repair a partial run."""

        action = self._check_plan(plan)
        if self._verified_proof() is not None or self._used:
            raise AdhelmError(
                "OPENAI_ADS_CREATE_ADAPTER_USED",
                "The fixed OpenAI Ads create adapter already handled this action.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Reconcile retained history and compile a new exact plan before any retry."
                ),
            )
        if not self._kernel_started(action, plan):
            raise AdhelmError(
                "KERNEL_START_REQUIRED",
                "OpenAI Ads create egress requires the kernel's durable start.",
            )
        if (
            execution.expected_child_egress_count != plan.expected_child_egress_count
            or execution.attempt_ref != idempotency_key
            or execution.subplan_digest != plan.subplan_digest
            or execution.plan_digest != action.plan.plan_digest
        ):
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "The kernel execution does not describe the approved OpenAI Ads subplan.",
            )
        self._used = True
        credentials = self._credentials()

        # Every precondition that can be checked without egress runs here, and
        # each one refuses before the first child's durable start exists.
        before, _observed = self._current_state(action, credentials)
        if before != plan.before:
            raise AdhelmError(
                "REMOTE_DRIFT",
                "OpenAI Ads already holds a campaign with this planned name.",
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
        file_id = ""
        campaign_id = ""
        group_ids: dict[str, str] = {}
        acknowledged: list[str] = []
        complete = False
        try:
            for child in children:
                identifier = self._run_child(
                    action,
                    child,
                    execution=execution,
                    idempotency_key=idempotency_key,
                    credentials=credentials,
                    content=content,
                    file_id=file_id,
                    campaign_id=campaign_id,
                    group_ids=group_ids,
                )
                acknowledged.append(child.label)
                if child.kind == "UPLOAD_CREATIVE":
                    file_id = identifier
                    self._uploaded_file_id = identifier
                elif child.kind == "CREATE_CAMPAIGN":
                    campaign_id = identifier
                elif child.kind == "CREATE_AD_GROUP":
                    group_ids[str(child.ad_group_handle)] = identifier
            complete = True
        except _FixedOpenAICreateDispatchUnverified:
            complete = False

        observation = self._readback_or_none(action, credentials, campaign_client_ref=campaign_id)
        matched = (
            complete and observation is not None and self._matches_prediction(action, observation)
        )
        record_digest = self._retain_readback_observation(
            action,
            observation,
            attempt_ref=idempotency_key,
            approved_child_count=len(children),
            acknowledged_child_labels=tuple(acknowledged),
            complete=complete,
            matched=matched,
            file_id=file_id,
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
            state, _observation = self._current_state(action, self._credentials())
            current = RemoteState(fingerprint=state.fingerprint, revision=state.revision)
        except Exception:
            current = RemoteState(fingerprint="unavailable")
        # A later matching read cannot attribute a missing or uncertain child
        # acknowledgement to this attempt.  OpenAI documents no idempotency key,
        # request identifier, or settlement resource, and a second create would
        # duplicate the hierarchy rather than converge.
        return Reconciliation(
            state=current, status=EffectStatus.AMBIGUOUS_REMOTE_STATE, settled=False
        )

    # -- one child ----------------------------------------------------------

    def _run_child(
        self,
        action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
        child: OpenAIAdsCreateChild,
        *,
        execution: KernelExecution,
        idempotency_key: str,
        credentials: OpenAIPauseCredentials,
        content: bytes,
        file_id: str,
        campaign_id: str,
        group_ids: dict[str, str],
    ) -> str:
        """Journal, authorize, send and acknowledge exactly one child request.

        The body is built *before* the permit is claimed, so a body-construction
        failure can never become an ambiguous post-egress outcome, and the permit
        is consumed on the statement immediately before the POST.
        """

        creative = action.observation_binding.creative
        url = f"https://{OPENAI_CREATE_HOST}/{OPENAI_CREATE_API_VERSION}{child.path}"
        body: dict[str, Any] | None = None
        if child.kind == "CREATE_CAMPAIGN":
            body = campaign_request_body(action)
        elif child.kind == "CREATE_AD_GROUP":
            body = ad_group_request_body(
                action, str(child.ad_group_handle), campaign_id=campaign_id
            )
        elif child.kind == "CREATE_AD":
            body = ad_request_body(
                action,
                str(child.ad_group_handle),
                str(child.ad_handle),
                ad_group_id=group_ids[str(child.ad_group_handle)],
                file_id=file_id,
            )
        parts = upload_request_parts(creative) if child.kind == "UPLOAD_CREATIVE" else None
        self._check_plan_expiry(action)
        with execution.begin_child(child.index, child.label) as permit:
            try:
                headers = {"authorization": f"Bearer {credentials.secret}"}
                if type(permit) is not EgressPermit:
                    raise _permit_required()
                permit.consume(
                    plan_digest=action.plan.plan_digest,
                    subplan_digest=action.plan.subplans[0].subplan_digest,
                    attempt_ref=idempotency_key,
                    child_index=child.index,
                )
                try:
                    if parts is not None:
                        response = self._transport.upload_image(
                            url,
                            headers=headers,
                            field_name=str(parts["field_name"]),
                            filename=str(parts["filename"]),
                            media_type=str(parts["media_type"]),
                            content=content,
                        )
                    else:
                        response = self._transport.request(
                            "POST", url, headers=headers, json=body, mutation=True
                        )
                except Exception:
                    # The permit is consumed.  No transport or response failure
                    # can prove the request never reached the provider.
                    raise _FixedOpenAICreateDispatchUnverified() from None
                identifier = _acknowledgement_for(child, response, file_id=file_id)
                execution.report_child(
                    child.index,
                    ApplyResponse(
                        outcome=ApplyOutcome.CONFIRMED,
                        evidence_refs=(self._child_evidence(action, child, identifier),),
                    ),
                )
                return identifier
            except _FixedOpenAICreateDispatchUnverified:
                raise
            except Exception:
                raise _FixedOpenAICreateDispatchUnverified() from None

    # -- private members ----------------------------------------------------

    def _checked_action(self) -> OpenAIAdsChatCardHierarchyCreatePlannedAction:
        if type(self) is not OpenAIAdsChatCardHierarchyCreateAdapter:
            raise _adapter_invalid()
        try:
            issuer = self._issuer
            action = self._action
            transport = self._transport
            ledger = self._ledger
            supplier = self._credential_supplier
        except Exception:
            raise _adapter_invalid() from None
        if (
            issuer is not _ADAPTER_ISSUER
            or type(action) is not OpenAIAdsChatCardHierarchyCreatePlannedAction
            or type(transport) is not SafeHttpClient
            or type(ledger) is not Ledger
            or not callable(supplier)
        ):
            raise _adapter_invalid()
        return action

    def _check_plan(self, plan: object) -> OpenAIAdsChatCardHierarchyCreatePlannedAction:
        action = self._checked_action()
        if type(plan) is not SubPlan or plan != action.plan.subplans[0]:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "Adapter refuses a different OpenAI Ads chat-card create plan.",
            )
        return action

    def _credentials(self) -> OpenAIPauseCredentials:
        action = self._checked_action()
        value = self._credential_supplier()
        if type(value) is not OpenAIPauseCredentials:
            raise AdhelmError(
                "PROVIDER_ACCESS_REQUIRED",
                "OpenAI Ads authorization is unavailable.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        binding = action.account_binding
        if value.profile_ref != binding.profile_ref or not hmac.compare_digest(
            _credential_binding(value), binding.credential_binding_ref
        ):
            raise AdhelmError(
                "CREDENTIAL_BINDING_DRIFT",
                "The current OpenAI account key differs from the approved create plan.",
            )
        return value

    def _reader(self, credentials: OpenAIPauseCredentials) -> OpenAIAdsCreateReader:
        return OpenAIAdsCreateReader(self._transport, lambda: credentials.secret)

    def _current_identity(
        self,
        action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
        credentials: OpenAIPauseCredentials,
    ) -> tuple[OpenAIAdsCreateReader, OpenAIAdsIdentity]:
        binding = action.account_binding
        reader = self._reader(credentials)
        identity = reader.discover_identity(expected_account_client_ref=binding.account_client_ref)
        if (
            identity.account_ref != binding.account_ref
            or identity.currency != binding.account_currency
            or identity.timezone != binding.account_timezone
            or identity.status.casefold() != binding.account_status
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Current OpenAI ad-account facts differ from the approved create action.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured OpenAI ad account and compile a new plan.",
            )
        return reader, identity

    def _fingerprint_key(self, action: OpenAIAdsChatCardHierarchyCreatePlannedAction) -> bytes:
        key = export_key()
        if fingerprint_key_marker(key) != action.observation_binding.fingerprint_key_marker:
            raise AdhelmError(
                "OPENAI_ADS_CREATE_FINGERPRINT_KEY_CHANGED",
                "The local fingerprint key differs from the one this plan was projected under.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Compile a new exact plan from a fresh bounded read.",
            )
        return key

    def _checked_states(
        self, action: OpenAIAdsChatCardHierarchyCreatePlannedAction
    ) -> tuple[bytes, StateRef, StateRef]:
        key = self._fingerprint_key(action)
        subplan = action.plan.subplans[0]
        before, after = predicted_states(action, fingerprint_key=key)
        if before != subplan.before or after != subplan.after:
            raise AdhelmError(
                "OPENAI_ADS_CREATE_STATE_UNREPRODUCIBLE",
                "The retained plan's semantic states are not reproducible locally.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Compile a new exact plan from a fresh bounded read.",
            )
        return key, before, after

    def _current_state(
        self,
        action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
        credentials: OpenAIPauseCredentials,
        *,
        campaign_client_ref: str = "",
    ) -> tuple[StateRef, OpenAIAdsCreateHierarchyObservation | None]:
        """Return the semantic state this workspace currently observes."""

        key, before, _after = self._checked_states(action)
        reader, identity = self._current_identity(action, credentials)
        name = action.observation_binding.campaign_name
        selected = campaign_client_ref
        if not selected:
            matches = reader.campaigns_named(identity, name)
            if not matches:
                return before, None
            if len(matches) != 1:
                raise AdhelmError(
                    "REMOTE_DRIFT",
                    "OpenAI Ads holds more than one campaign with the planned name.",
                )
            selected = matches[0].client_ref
        observation = reader.observe_hierarchy(
            identity,
            selected,
            uploaded_file_client_ref=self._uploaded_file_hint(),
            creative_content_digest=action.observation_binding.creative.content_digest,
        )
        state = semantic_state_from_members(
            observation.members,
            account_client_ref=action.account_binding.account_client_ref,
            campaign_name=name,
            fingerprint_key=key,
        )
        return state, observation

    def _uploaded_file_hint(self) -> str:
        """Return the file this attempt uploaded, or a value no response matches.

        Before the upload child runs there is nothing to compare, and after a
        crash a fresh process no longer knows it.  The empty-string default
        cannot equal a documented ``file_...`` identifier, so an unproven
        association stays unproven rather than being assumed.
        """

        value = self._uploaded_file_id
        return value if type(value) is str else ""

    def _vault_bytes(self, action: OpenAIAdsChatCardHierarchyCreatePlannedAction) -> bytes:
        """Read the declared creative from the private vault and re-verify it.

        The vault read already rehashes the object.  This additionally re-decodes
        the bytes, so a plan compiled against one image cannot upload another
        with the same declared media type, size or dimensions.
        """

        creative = action.observation_binding.creative
        data = read_image(self._ledger.root, creative.content_digest)
        decoded = decode_image(data)
        if (
            len(data) != creative.byte_length
            or len(data) > OPENAI_CREATE_MAX_UPLOAD_BYTES
            or decoded.mime_type != creative.media_type
            or decoded.width_px != creative.width_px
            or decoded.height_px != creative.height_px
        ):
            raise AdhelmError(
                "OPENAI_ADS_CREATE_CREATIVE_DRIFT",
                "The private vault object differs from the creative this plan bound.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Register the creative again and compile a new exact plan; nothing was "
                    "uploaded."
                ),
            )
        return data

    def _matches_prediction(
        self,
        action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
        observation: OpenAIAdsCreateHierarchyObservation,
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
            and observation.file_association_proven
        )

    def _readback_or_none(
        self,
        action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
        credentials: OpenAIPauseCredentials,
        *,
        campaign_client_ref: str,
    ) -> OpenAIAdsCreateHierarchyObservation | None:
        if not campaign_client_ref:
            return None
        try:
            _state, observation = self._current_state(
                action, credentials, campaign_client_ref=campaign_client_ref
            )
        except Exception:
            return None
        return observation

    def _kernel_started(
        self, action: OpenAIAdsChatCardHierarchyCreatePlannedAction, plan: SubPlan
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
            raise AdhelmError("CLOCK_INVALID", "OpenAI Ads create clock is unavailable.") from None
        if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
            raise AdhelmError("CLOCK_INVALID", "OpenAI Ads create clock is invalid.")
        return current.astimezone(UTC)

    def _check_plan_expiry(self, action: OpenAIAdsChatCardHierarchyCreatePlannedAction) -> None:
        if action.plan.expires_at <= self._checked_now():
            raise AdhelmError(
                "PLAN_EXPIRED",
                "OpenAI Ads create plan expired before provider egress.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Compile and approve a new exact OpenAI Ads create plan.",
            )

    @staticmethod
    def _child_evidence(
        action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
        child: OpenAIAdsCreateChild,
        identifier: str,
    ) -> str:
        """Digest one child acknowledgement without publishing its identifier."""

        return _EVIDENCE_PREFIX + digest(
            {
                "kind": "openai_ads_v1_chat_card_create_child_acknowledgement",
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
        action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
        acknowledged: tuple[str, ...],
        readback_record_digest: str,
    ) -> tuple[str, str]:
        sequence = _EVIDENCE_PREFIX + digest(
            {
                "kind": "openai_ads_v1_chat_card_create_child_sequence",
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
        observation: OpenAIAdsCreateHierarchyObservation | None,
    ) -> tuple[str, ...]:
        """Name every created resource as the kernel's opaque reference shape.

        The material is the same keyless pseudonym the readback record retains,
        re-prefixed as ``opaque:sha256:`` because that is the only reference
        form ``ApplyResponse`` accepts.  No native identifier is published.
        """

        if observation is None:
            return ()
        pseudonyms = [
            pseudonymized_resource_ref("campaign", observation.campaign.client_ref),
            *(
                pseudonymized_resource_ref("ad_group", group.client_ref)
                for group in observation.ad_groups
            ),
            *(pseudonymized_resource_ref("ad", ad.client_ref) for ad in observation.ads),
        ]
        return tuple("opaque:sha256:" + item.rsplit(":", 1)[1] for item in pseudonyms)

    def _retain_readback_observation(
        self,
        action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
        observation: OpenAIAdsCreateHierarchyObservation | None,
        *,
        attempt_ref: str,
        approved_child_count: int,
        acknowledged_child_labels: tuple[str, ...],
        complete: bool,
        matched: bool,
        file_id: str,
        observation_interval_start: datetime,
        observation_interval_end: datetime,
    ) -> str:
        """Append one dedicated post-acknowledgement observation, partial or not."""

        subplan = action.plan.subplans[0]
        creative = action.observation_binding.creative
        proven = observation is not None and observation.file_association_proven
        relation: Literal[
            "MATCHES_PREDICTED_SEMANTICS",
            "PARTIAL_HIERARCHY_OBSERVED",
            "NO_HIERARCHY_OBSERVED",
        ] = (
            "NO_HIERARCHY_OBSERVED"
            if observation is None
            else ("MATCHES_PREDICTED_SEMANTICS" if matched else "PARTIAL_HIERARCHY_OBSERVED")
        )
        payload = OpenAIAdsChatCardHierarchyCreateReadbackObservation(
            action_id=action.action_id,
            plan_digest=action.plan.plan_digest,
            subplan_digest=subplan.subplan_digest,
            attempt_ref=attempt_ref,
            account_ref=action.account_binding.account_ref,
            observation_interval_start=observation_interval_start,
            observation_interval_end=observation_interval_end,
            approved_child_count=approved_child_count,
            acknowledged_child_count=len(acknowledged_child_labels),
            acknowledged_child_labels=acknowledged_child_labels,
            attempt_completion="CHILDREN_COMPLETE" if complete else "CHILDREN_INCOMPLETE",
            observed_hierarchy_relation=relation,
            observed_ad_group_count=0 if observation is None else len(observation.ad_groups),
            observed_ad_count=0 if observation is None else len(observation.ads),
            observed_member_count=0 if observation is None else len(observation.members),
            every_entity_configured_status="NOT_OBSERVED" if observation is None else "paused",
            campaign_resource_ref=(
                None
                if observation is None
                else pseudonymized_resource_ref("campaign", observation.campaign.client_ref)
            ),
            ad_group_resource_refs=(
                ()
                if observation is None
                else tuple(
                    pseudonymized_resource_ref("ad_group", group.client_ref)
                    for group in observation.ad_groups
                )
            ),
            ad_resource_refs=(
                ()
                if observation is None
                else tuple(
                    pseudonymized_resource_ref("ad", ad.client_ref) for ad in observation.ads
                )
            ),
            creative_content_digest=creative.content_digest,
            creative_byte_length=creative.byte_length,
            creative_media_type=creative.media_type,
            creative_file_ref=(
                pseudonymized_resource_ref("file", file_id) if file_id and proven else None
            ),
            file_association="PROVEN_UPLOAD_ASSOCIATION" if proven else "NOT_PROVEN",
        ).model_dump(mode="json", warnings="error")
        record = self._ledger.append(
            "changes",
            OPENAI_CREATE_READBACK_RECORD_KIND,
            payload,
            provider="openai_ads",
            account_ref=action.account_binding.account_ref,
            run_id=action.plan.plan_id,
            observed_at=payload["observation_interval_end"],
            occurred_at=payload["observation_interval_end"],
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


_RESOURCE_KIND_FOR_CHILD: Final = {
    "UPLOAD_CREATIVE": "file",
    "CREATE_CAMPAIGN": "campaign",
    "CREATE_AD_GROUP": "ad_group",
    "CREATE_AD": "ad",
}


def _acknowledgement_for(child: OpenAIAdsCreateChild, response: object, *, file_id: str) -> str:
    if child.kind == "UPLOAD_CREATIVE":
        return upload_acknowledgement(response)
    if child.kind == "CREATE_CAMPAIGN":
        return campaign_acknowledgement(response)
    if child.kind == "CREATE_AD_GROUP":
        return ad_group_acknowledgement(response)
    del file_id
    return ad_acknowledgement(response)


def _openai_create_http_client() -> httpx.Client:
    """The one HTTP client seam this route opens; replaced only by tests."""

    return httpx.Client(trust_env=False)


@contextmanager
def _configured_openai_ads_create_adapter(
    directory: Path,
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
    ledger: Ledger,
    credential_supplier: Callable[[], OpenAIPauseCredentials],
    *,
    clock: Callable[[], datetime] = _now,
) -> Iterator[OpenAIAdsChatCardHierarchyCreateAdapter]:
    """Yield one configured fixed-route OpenAI Ads CREATE child-egress adapter."""

    if not isinstance(directory, Path) or type(ledger) is not Ledger:
        raise _adapter_invalid()
    if (
        ledger.root != directory
        or ledger.profile_ref != action.plan.profile_ref
        or ledger.profile_ref != action.workspace_binding.workspace_profile_ref
    ):
        raise _adapter_invalid()
    # Two fail-closed build checks, before any credential, header, or provider
    # request.  Neither is a fallback: no path continues without them.
    if not openai_create_readback_record_kind_installed():
        raise AdhelmError(
            "OPENAI_ADS_CREATE_READBACK_RECORD_KIND_NOT_INSTALLED",
            "This build's ledger does not register the OpenAI Ads CREATE readback record.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a build whose ledger registers "
                f"{OPENAI_CREATE_READBACK_RECORD_KIND}; no credential was read and no provider "
                "request was made."
            ),
        )
    if not openai_create_upload_transport_installed():
        raise AdhelmError(
            "OPENAI_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED",
            "This build's transport installs no bounded multipart image upload.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a build whose SafeHttpClient exposes the reviewed bounded "
                "upload_image seam; no credential was read and no provider request was made."
            ),
        )
    client = _openai_create_http_client()
    try:
        yield OpenAIAdsChatCardHierarchyCreateAdapter(
            action=action,
            transport=SafeHttpClient(client, {OPENAI_CREATE_HOST}),
            credential_supplier=credential_supplier,
            ledger=ledger,
            issuer=_ADAPTER_ISSUER,
            clock=clock,
        )
    finally:
        client.close()


def _adapter_invalid() -> AdhelmError:
    return AdhelmError(
        "OPENAI_ADS_CREATE_ADAPTER_INVALID",
        "OpenAI Ads chat-card create adapter state is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Start a new fixed OpenAI Ads create operation from local state.",
    )


def _post_egress_ambiguous() -> AdhelmError:
    return AdhelmError(
        "AMBIGUOUS_REMOTE_STATE",
        "OpenAI Ads hierarchy create outcome requires reconciliation before any new plan.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Reconcile remote state and compile a new exact plan before any retry; anything "
            "this attempt created is paused and must not be deleted."
        ),
    )


__all__ = [
    "OpenAIAdsChatCardHierarchyCreateAdapter",
    "ad_acknowledgement",
    "ad_group_acknowledgement",
    "ad_group_request_body",
    "ad_request_body",
    "campaign_acknowledgement",
    "campaign_request_body",
    "openai_create_upload_transport_installed",
    "upload_acknowledgement",
    "upload_request_parts",
]
