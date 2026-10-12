"""Private guarded adapter for the four fixed Google Ads Search maintenance routes.

This is model-derived offline engineering over the reviewed v25 shape (no Google
Ads SDK is a dependency).  It is not a claim that Google Ads has a live
acknowledgement, partial-failure, conditional-write, or idempotency contract for
this repository.  The only accepted local acknowledgement subset is an exact
HTTP 200 JSON object whose sole member is ``mutateOperationResponses``, holding
exactly one result per planned operation, in order, each carrying exactly the
expected result field and a ``resourceName`` under this customer's path,
followed by a fresh GAQL readback.  Every other outcome after a kernel permit is
consumed remains ambiguous and must not be retried.

Four bodies, one shape:

* a budget amount update, ``update_mask=totalAmountMicros``;
* an ad-group bid update, ``update_mask=cpcBidMicros``;
* an ad-schedule replacement -- ``CampaignCriterion.ad_schedule`` is
  ``Immutable`` in v25, so every existing schedule criterion is *removed* and
  the replacements *created* in the same atomic mutate.  No temporary resource
  ID is used, because no created criterion is referenced by a later operation
  in the same request;
* an archive, ``update_mask=status`` to ``REMOVED``.  This one is irreversible
  and this adapter refuses to build it for an ``ENABLED`` campaign.

**Recovery disposition.**  The previous value is recorded in the readback.
Nothing here reverts automatically, and rule 11 forbids deleting anything.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Never

import httpx

from adhelm import google_ads_pause_planning_session as planning
from adhelm.canonical import digest, parse_json
from adhelm.credentials import export_key
from adhelm.egress_permit import EgressPermit
from adhelm.egress_permit import _required as _permit_required
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_activate_planning_session import (
    GoogleAdsLiveHierarchyObservation,
    GoogleAdsLiveHierarchyReader,
)
from adhelm.google_ads_maintain_planning_session import (
    describe_surfaces,
    unproven_campaign_reference_surfaces,
)
from adhelm.google_ads_maintain_readback_records import (
    GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND,
    GoogleAdsSearchCampaignMaintainReadbackObservation,
    google_ads_maintain_readback_record_kind_installed,
    pseudonymized_maintenance_ref,
)
from adhelm.ledger import Ledger
from adhelm.privacy import validate_data
from adhelm.providers.google_ads_activate_adapter import valid_resource_name
from adhelm.providers.google_ads_activate_contract import (
    ACTIVATED_STATUS,
    fingerprint_key_marker,
    semantic_hierarchy_state,
)
from adhelm.providers.google_ads_campaign_state import (
    GoogleAdsEditIdentity,
    _headers_snapshot,
    _validate_transport,
)
from adhelm.providers.google_ads_maintain_contract import (
    GOOGLE_ADS_MAINTAIN_API_VERSION,
    GOOGLE_ADS_MAINTAIN_HOST,
    GOOGLE_ADS_MAINTAIN_SCOPE,
    GoogleAdsAdGroupBidIntent,
    GoogleAdsAdScheduleReplacementIntent,
    GoogleAdsBudgetAmountIntent,
    GoogleAdsCampaignReferenceSurfaceReading,
    GoogleAdsSearchCampaignMaintainPlannedAction,
    MaintenanceIntent,
    maintenance_values,
    predicted_states,
    revalidate_google_ads_search_campaign_maintain_planned_action,
    unproven_archive_state,
)
from adhelm.safety import (
    AccountIdentity,
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    EffectStatus,
    Reconciliation,
    RemoteIdentity,
    RemoteState,
    StateRef,
    SubPlan,
    Verification,
)

_HTTPX_RESPONSE: Final = httpx.Response
_ADAPTER_ISSUER = object()
_PROOF_ISSUER = object()
_EVIDENCE_PREFIX: Final = "evidence:sha256:"
_MAX_ACK_BYTES: Final = 65_536
_RESPONSE_CONTENT_TYPE: Final = "RESOURCE_NAME_ONLY"
_ARCHIVED_STATUS: Final = "REMOVED"

#: The proto3 JSON encoding of a ``FieldMask`` renders each field path in
#: lowerCamelCase, so a v25 ``update_mask`` of ``total_amount_micros`` is sent
#: as ``totalAmountMicros``.  See the execution contract's wire-fact table.
BUDGET_UPDATE_MASK: Final = "totalAmountMicros"
BID_UPDATE_MASK: Final = "cpcBidMicros"
STATUS_UPDATE_MASK: Final = "status"


class _FixedGoogleAdsMaintainPostDispatchUnverified(Exception):
    """Internal marker for an error after a kernel permit was consumed."""


def _now() -> datetime:
    """Private UTC clock seam used only by synthetic adapter tests."""

    return datetime.now(UTC)


def google_ads_search_campaign_maintain_mutate_body(
    observation: GoogleAdsLiveHierarchyObservation,
    intent: MaintenanceIntent,
    *,
    customer_id: str,
) -> dict[str, Any]:
    """Return the exact single ``googleAds:mutate`` body one maintenance sends.

    The body is a pure function of the observed hierarchy, the intent, and the
    customer ID, so ``tests/test_google_ads_maintain_execution.py`` pins each
    of the four as golden JSON and the guarded route cannot drift from the
    documented wire shape.
    """

    if type(observation) is not GoogleAdsLiveHierarchyObservation:
        raise ValueError("live hierarchy observation type")
    if type(customer_id) is not str or len(customer_id) != 10 or not customer_id.isdecimal():
        raise ValueError("customer id")
    prefix = f"customers/{customer_id}/"
    names = (
        observation.campaign_resource_name,
        observation.campaign_budget_resource_name,
        observation.ad_group_resource_name,
        *observation.ad_schedule_criterion_resource_names,
    )
    if any(type(name) is not str or not name.startswith(prefix) for name in names):
        raise ValueError("resource name outside the planned customer")
    kind = intent.maintenance_kind
    operations: list[dict[str, Any]]
    if kind == "CAMPAIGN_BUDGET_AMOUNT":
        assert isinstance(intent, GoogleAdsBudgetAmountIntent)
        operations = [
            {
                "campaignBudgetOperation": {
                    "update": {
                        "resourceName": observation.campaign_budget_resource_name,
                        "totalAmountMicros": str(intent.new_total_amount_micros),
                    },
                    "updateMask": BUDGET_UPDATE_MASK,
                }
            }
        ]
    elif kind == "AD_GROUP_CPC_BID":
        assert isinstance(intent, GoogleAdsAdGroupBidIntent)
        operations = [
            {
                "adGroupOperation": {
                    "update": {
                        "resourceName": observation.ad_group_resource_name,
                        "cpcBidMicros": str(intent.new_cpc_bid_micros),
                    },
                    "updateMask": BID_UPDATE_MASK,
                }
            }
        ]
    elif kind == "AD_SCHEDULE_REPLACEMENT":
        assert isinstance(intent, GoogleAdsAdScheduleReplacementIntent)
        if not observation.ad_schedule_criterion_resource_names:
            raise ValueError("no ad schedule criterion to replace")
        status = observation.projection.configured("CAMPAIGN")["status"]
        operations = [
            {"campaignCriterionOperation": {"remove": resource_name}}
            for resource_name in sorted(observation.ad_schedule_criterion_resource_names)
        ]
        for schedule in intent.schedules:
            operations.append(
                {
                    "campaignCriterionOperation": {
                        "create": {
                            "campaign": observation.campaign_resource_name,
                            "status": status,
                            "negative": False,
                            "adSchedule": {
                                "dayOfWeek": schedule.day_of_week,
                                "startHour": schedule.start_hour,
                                "startMinute": schedule.start_minute,
                                "endHour": schedule.end_hour,
                                "endMinute": schedule.end_minute,
                            },
                        }
                    }
                }
            )
    else:
        operations = [
            {
                "campaignOperation": {
                    "update": {
                        "resourceName": observation.campaign_resource_name,
                        "status": _ARCHIVED_STATUS,
                    },
                    "updateMask": STATUS_UPDATE_MASK,
                }
            }
        ]
    return {
        "mutateOperations": operations,
        "partialFailure": False,
        "validateOnly": False,
        "responseContentType": _RESPONSE_CONTENT_TYPE,
    }


def expected_result_kinds(
    observation: GoogleAdsLiveHierarchyObservation, intent: MaintenanceIntent
) -> tuple[str, ...]:
    """Return the ordered acknowledgement result fields this route requires."""

    kind = intent.maintenance_kind
    if kind == "CAMPAIGN_BUDGET_AMOUNT":
        return ("campaignBudgetResult",)
    if kind == "AD_GROUP_CPC_BID":
        return ("adGroupResult",)
    if kind == "AD_SCHEDULE_REPLACEMENT":
        assert isinstance(intent, GoogleAdsAdScheduleReplacementIntent)
        count = len(observation.ad_schedule_criterion_resource_names) + len(intent.schedules)
        return tuple("campaignCriterionResult" for _ in range(count))
    return ("campaignResult",)


class _FixedGoogleAdsSearchCampaignMaintainPost:
    """Factory-issued, one-route capability with no token, URL, or body surface."""

    __slots__ = ("_customer_id", "_intent", "_plan_digest", "_session", "_subplan_digest")

    def __init__(
        self,
        *,
        session: planning.GoogleAdsCampaignPausePlanningSession,
        customer_id: str,
        intent: MaintenanceIntent,
        plan_digest: str,
        subplan_digest: str,
    ) -> None:
        self._session = session
        self._customer_id = customer_id
        self._intent = intent
        self._plan_digest = plan_digest
        self._subplan_digest = subplan_digest

    def __repr__(self) -> str:
        return "<_FixedGoogleAdsSearchCampaignMaintainPost redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads maintain capabilities cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads maintain capabilities cannot be serialized")

    def _preflight(self, observation: GoogleAdsLiveHierarchyObservation) -> None:
        """Validate the transport and the fixed route before any permit exists."""

        _transport, _getter, target = self._session._checked_reader()._checked_members()
        if target.customer_client_ref != self._customer_id:
            raise _adapter_invalid()
        google_ads_search_campaign_maintain_mutate_body(
            observation, self._intent, customer_id=self._customer_id
        )

    def _post(
        self,
        observation: GoogleAdsLiveHierarchyObservation,
        *,
        permit: EgressPermit,
        attempt_ref: str,
    ) -> httpx.Response:
        """Consume one active kernel permit at the sole fixed POST handoff."""

        transport, headers_getter, target = self._session._checked_reader()._checked_members()
        if type(permit) is not EgressPermit:
            raise _permit_required()
        if target.customer_client_ref != self._customer_id:
            raise _adapter_invalid()
        body = google_ads_search_campaign_maintain_mutate_body(
            observation, self._intent, customer_id=self._customer_id
        )
        headers = _headers_snapshot(headers_getter, target)
        _validate_transport(transport)
        permit.consume(
            plan_digest=self._plan_digest,
            subplan_digest=self._subplan_digest,
            attempt_ref=attempt_ref,
        )
        try:
            return transport.request(
                "POST",
                (
                    f"https://{GOOGLE_ADS_MAINTAIN_HOST}/{GOOGLE_ADS_MAINTAIN_API_VERSION}"
                    f"/customers/{self._customer_id}/googleAds:mutate"
                ),
                headers=headers,
                json=body,
                mutation=True,
            )
        except Exception:
            raise _FixedGoogleAdsMaintainPostDispatchUnverified() from None


class GoogleAdsSearchCampaignMaintainGuardedAdapter:
    """Factory-issued fixed-route adapter consumed only by ``SafetyKernel``."""

    provider: str = "google_ads"

    __slots__ = (
        "_action",
        "_issuer",
        "_ledger",
        "_post",
        "_proof",
        "_proof_issuer",
        "_reader",
        "_session",
        "_unproven_surfaces",
    )

    def __init__(
        self,
        *,
        action: GoogleAdsSearchCampaignMaintainPlannedAction,
        session: planning.GoogleAdsCampaignPausePlanningSession,
        reader: GoogleAdsLiveHierarchyReader,
        post: _FixedGoogleAdsSearchCampaignMaintainPost,
        ledger: Ledger,
        issuer: object,
    ) -> None:
        self._action = action
        self._session = session
        self._reader = reader
        self._post = post
        self._ledger = ledger
        self._issuer = issuer
        self._proof: tuple[str, str] | None = None
        self._proof_issuer: object | None = None
        self._unproven_surfaces: tuple[GoogleAdsCampaignReferenceSurfaceReading, ...] = ()

    def __repr__(self) -> str:
        return "<GoogleAdsSearchCampaignMaintainGuardedAdapter private fixed route>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads maintain adapters cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads maintain adapters cannot be serialized")

    @property
    def unproven_reference_surfaces(self) -> tuple[GoogleAdsCampaignReferenceSurfaceReading, ...]:
        """Return what the most recent state read found not proven empty.

        A diagnostic only.  Rule 11 gates the archive's precondition, so
        ``read_state`` reports a distinct UNPROVEN state and never raises for it;
        this is how the surfaces behind that state are named.
        """

        return self._unproven_surfaces

    @property
    def action(self) -> GoogleAdsSearchCampaignMaintainPlannedAction:
        """Return the reconstructed closed action for private composition only."""

        return self._checked_members()[0]

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        action = self._check_plan(plan)
        subplan = action.plan.subplans[0]
        return AdapterCapabilities(
            provider_api_version=subplan.provider_api_version,
            capability_version=subplan.capability_version,
            supported_operations=(subplan.operation,),
            writes_enabled=True,
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        action, _session, _reader, _post, _ledger = self._checked_members()
        account = action.plan.subplans[0].account
        if type(account) is not AccountIdentity or account_ref != account.account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Google Ads customer differs from the approved maintenance plan.",
            )
        identity = self._current_identity(action)
        return RemoteIdentity(
            stable_account_id=identity.customer_client_ref,
            auth_profile_ref=action.execution_binding.profile_ref,
            principal_ref=action.execution_binding.principal_ref,
            client_application_ref=action.execution_binding.client_application_ref,
            scopes=(GOOGLE_ADS_MAINTAIN_SCOPE,),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        action = self._check_plan(plan)
        return self._current_state(action)[0]

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError(
            "PERMIT_REQUIRED",
            "Google Ads Search maintenance egress requires a kernel-issued permit.",
        )

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        action = self._check_plan(plan)
        _action, session, _reader, post, ledger = self._checked_members()
        if self._verified_proof() is not None:
            raise AdhelmError(
                "GOOGLE_ADS_MAINTAIN_ADAPTER_USED",
                "The fixed Google Ads maintenance adapter already handled this action.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Reconcile retained history and compile a new exact plan before any retry."
                ),
            )
        if not self._kernel_started(action, plan, ledger):
            raise AdhelmError(
                "KERNEL_START_REQUIRED",
                "Google Ads maintenance egress requires the kernel's durable start.",
            )
        before, observation = self._current_state(action)
        if before.fingerprint != plan.before.fingerprint or observation is None:
            raise AdhelmError(
                "REMOTE_DRIFT",
                "The Google Ads hierarchy differs from the state this plan was approved for.",
            )
        if action.intent.maintenance_kind == "CAMPAIGN_ARCHIVE" and (
            observation.campaign_status == ACTIVATED_STATUS
        ):
            raise AdhelmError(
                "GOOGLE_ADS_ARCHIVE_REQUIRES_PAUSED_CAMPAIGN",
                "An ENABLED Google Ads campaign is paused before it is archived.",
                exit_code=ExitCode.PRECONDITION,
            )
        self._check_plan_expiry(action)
        post._preflight(observation)
        self._check_plan_expiry(action)
        try:
            response = post._post(observation, permit=permit, attempt_ref=idempotency_key)
        except _FixedGoogleAdsMaintainPostDispatchUnverified:
            raise _post_egress_ambiguous() from None
        try:
            acknowledged = _required_acknowledgement(
                response,
                expected_result_kinds(observation, action.intent),
                customer_id=action.account_binding.account_client_ref,
            )
            readback_started_at = self._checked_now()
            member_count, resource_name = self._verified_readback(action, plan, observation)
            readback_finished_at = self._checked_now()
            readback_record_digest = self._retain_readback_observation(
                action,
                acknowledged=acknowledged,
                attempt_ref=idempotency_key,
                member_count=member_count,
                campaign_resource_name=resource_name,
                observation_interval_start=readback_started_at,
                observation_interval_end=readback_finished_at,
            )
            proof = self._proof_for(action, acknowledged, readback_record_digest)
            self._proof = proof
            self._proof_issuer = _PROOF_ISSUER
            del session
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=proof)
        except Exception:
            raise _post_egress_ambiguous() from None

    def verify(self, plan: SubPlan) -> Verification:
        """Report the fresh-readback proof this adapter actually established."""

        self._check_plan(plan)
        proof = self._verified_proof()
        return Verification(verified=proof is not None, evidence_refs=proof or ())

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        self._check_plan(plan)
        try:
            current = self.read_state(plan)
        except Exception:
            current = RemoteState(fingerprint="unavailable")
        # A later matching read cannot attribute a missing or uncertain
        # acknowledgement to this request.  There is no reviewed settlement or
        # idempotency proof for this offline SDK-derived route.
        return Reconciliation(
            state=current,
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )

    # -- private members ----------------------------------------------------

    def _checked_members(
        self,
    ) -> tuple[
        GoogleAdsSearchCampaignMaintainPlannedAction,
        planning.GoogleAdsCampaignPausePlanningSession,
        GoogleAdsLiveHierarchyReader,
        _FixedGoogleAdsSearchCampaignMaintainPost,
        Ledger,
    ]:
        if type(self) is not GoogleAdsSearchCampaignMaintainGuardedAdapter:
            raise _adapter_invalid()
        try:
            issuer = self._issuer
            action = self._action
            session = self._session
            reader = self._reader
            post = self._post
            ledger = self._ledger
        except Exception:
            raise _adapter_invalid() from None
        if (
            issuer is not _ADAPTER_ISSUER
            or type(session) is not planning.GoogleAdsCampaignPausePlanningSession
            or type(reader) is not GoogleAdsLiveHierarchyReader
            or type(post) is not _FixedGoogleAdsSearchCampaignMaintainPost
            or type(ledger) is not Ledger
        ):
            raise _adapter_invalid()
        checked_action = revalidate_google_ads_search_campaign_maintain_planned_action(action)
        if (
            post._session is not session
            or reader._session is not session
            or post._customer_id != checked_action.account_binding.account_client_ref
            or post._intent != checked_action.intent
            or post._plan_digest != checked_action.plan.plan_digest
            or post._subplan_digest != checked_action.plan.subplans[0].subplan_digest
        ):
            raise _adapter_invalid()
        return checked_action, session, reader, post, ledger

    def _check_plan(self, plan: object) -> GoogleAdsSearchCampaignMaintainPlannedAction:
        action, _session, _reader, _post, _ledger = self._checked_members()
        if type(plan) is not SubPlan or plan != action.plan.subplans[0]:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "Adapter refuses a different Google Ads Search maintenance plan.",
            )
        return action

    @staticmethod
    def _fingerprint_key(action: GoogleAdsSearchCampaignMaintainPlannedAction) -> bytes:
        key = export_key()
        if fingerprint_key_marker(key) != action.observation_binding.fingerprint_key_marker:
            raise AdhelmError(
                "GOOGLE_ADS_MAINTAIN_FINGERPRINT_KEY_CHANGED",
                "The local fingerprint key differs from the one this plan was projected under.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Compile a new exact plan from a fresh bounded read.",
            )
        return key

    def _current_identity(
        self, action: GoogleAdsSearchCampaignMaintainPlannedAction
    ) -> GoogleAdsEditIdentity:
        _action, _session, reader, _post, _ledger = self._checked_members()
        try:
            identity = reader.discover_identity()
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None
        account = action.account_binding
        if (
            identity.customer_ref != account.account_ref
            or identity.customer_client_ref != account.account_client_ref
            or identity.currency != account.currency
            or identity.timezone != account.timezone
            or identity.status != account.account_status
            or identity.status != "ENABLED"
            or identity.provider_api_version != account.provider_api_version
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Current Google Ads customer facts differ from the approved maintenance action.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured Google Ads customer and compile a new plan.",
            )
        return identity

    def _current_state(
        self, action: GoogleAdsSearchCampaignMaintainPlannedAction
    ) -> tuple[RemoteState, GoogleAdsLiveHierarchyObservation | None]:
        """Return the semantic state this workspace currently observes.

        An archive is the one kind whose ``after`` is the *absence* of a live
        hierarchy under this binding, so it is probed by the campaign's own
        status first: an already-``REMOVED`` (or absent) campaign is reported
        as that ``after`` state and carries no observation, and the caller
        refuses rather than trying to build a mutate body from nothing.  This
        is also what lets the kernel's post-apply verification see the archived
        postcondition instead of a hierarchy read that no longer describes a
        live campaign.
        """

        key, _before, after = self._checked_states(action)
        self._current_identity(action)
        archive = action.intent.maintenance_kind == "CAMPAIGN_ARCHIVE"
        self._unproven_surfaces = ()
        if archive:
            status = self._campaign_status(action)
            if status is None or status == _ARCHIVED_STATUS:
                return RemoteState(fingerprint=after.fingerprint, revision=after.revision), None
        observation = self._observe(action)
        observed = semantic_hierarchy_state(
            observation.projection,
            account_client_ref=action.account_binding.account_client_ref,
            campaign_name=action.observation_binding.campaign_name,
            fingerprint_key=key,
        )
        if archive:
            # AGENTS rules 6 and 11: the plan-time proof describes a moment that
            # has passed, so a live archive is re-proven against every fresh
            # read.  The proof gates the archive's *precondition*; it is not a
            # condition of observing the campaign.  A surface that is no longer
            # READ_EMPTY therefore yields a distinct UNPROVEN state -- the
            # kernel's pre-start comparison refuses it as drift before the
            # durable start, and a recovery read still records an honest
            # observation -- and never raises from here.
            self._unproven_surfaces = unproven_campaign_reference_surfaces(observation)
            if self._unproven_surfaces:
                unproven = unproven_archive_state(observed, fingerprint_key=key)
                return (
                    RemoteState(fingerprint=unproven.fingerprint, revision=unproven.revision),
                    observation,
                )
        return RemoteState(
            fingerprint=observed.fingerprint, revision=observed.revision
        ), observation

    def _campaign_status(self, action: GoogleAdsSearchCampaignMaintainPlannedAction) -> str | None:
        _action, _session, reader, _post, _ledger = self._checked_members()
        try:
            return reader.observe_campaign_status(action.observation_binding.campaign_name)
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None

    def _observe(
        self, action: GoogleAdsSearchCampaignMaintainPlannedAction
    ) -> GoogleAdsLiveHierarchyObservation:
        _action, _session, reader, _post, _ledger = self._checked_members()
        try:
            observation = reader.observe_live_hierarchy(action.observation_binding.campaign_name)
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None
        if type(observation) is not GoogleAdsLiveHierarchyObservation:
            raise _adapter_invalid()
        return observation

    def _checked_states(
        self, action: GoogleAdsSearchCampaignMaintainPlannedAction
    ) -> tuple[bytes, StateRef, StateRef]:
        key = self._fingerprint_key(action)
        subplan = action.plan.subplans[0]
        before, after = predicted_states(action, fingerprint_key=key)
        if before != subplan.before or after != subplan.after:
            raise AdhelmError(
                "GOOGLE_ADS_MAINTAIN_STATE_UNREPRODUCIBLE",
                "The retained plan's semantic states are not reproducible locally.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Compile a new exact plan from a fresh bounded read.",
            )
        return key, before, after

    def _verified_readback(
        self,
        action: GoogleAdsSearchCampaignMaintainPlannedAction,
        plan: SubPlan,
        observed: GoogleAdsLiveHierarchyObservation,
    ) -> tuple[int | None, str]:
        """Read the result back and compare it with the approved postcondition.

        An archive is the one kind whose ``after`` is the *absence* of a live
        hierarchy under this binding, so it is verified by the campaign's own
        status rather than by a hierarchy projection.  GAQL's implicit filtering
        of removed resources is undocumented on any page this build could reach,
        so both "one row, ``REMOVED``" and "no rows" are accepted.
        """

        _action, _session, reader, _post, _ledger = self._checked_members()
        key, _before, after = self._checked_states(action)
        name = action.observation_binding.campaign_name
        if action.intent.maintenance_kind == "CAMPAIGN_ARCHIVE":
            status = reader.observe_campaign_status(name)
            if status is not None and status != _ARCHIVED_STATUS:
                raise ValueError("postcondition")
            if after.fingerprint != plan.after.fingerprint:
                raise ValueError("postcondition")
            return None, observed.campaign_resource_name
        observation = self._observe(action)
        state = semantic_hierarchy_state(
            observation.projection,
            account_client_ref=action.account_binding.account_client_ref,
            campaign_name=name,
            fingerprint_key=key,
        )
        if state != after or after.fingerprint != plan.after.fingerprint:
            raise ValueError("postcondition")
        return len(observation.projection.members), observation.campaign_resource_name

    @staticmethod
    def _kernel_started(
        action: GoogleAdsSearchCampaignMaintainPlannedAction, plan: SubPlan, ledger: Ledger
    ) -> bool:
        try:
            return any(
                row.get("payload", {}).get("event_type") == "APPLY_STARTED"
                and row.get("payload", {}).get("plan_digest") == action.plan.plan_digest
                and row.get("payload", {}).get("subplan_digest") == plan.subplan_digest
                for row in ledger.records("changes")
                if type(row) is dict and type(row.get("payload")) is dict
            )
        except Exception:
            return False

    @staticmethod
    def _checked_now() -> datetime:
        try:
            current = _now()
        except Exception:
            raise AdhelmError(
                "CLOCK_INVALID", "Google Ads maintain clock is unavailable."
            ) from None
        if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
            raise AdhelmError("CLOCK_INVALID", "Google Ads maintain clock is invalid.")
        return current.astimezone(UTC)

    @classmethod
    def _check_plan_expiry(cls, action: GoogleAdsSearchCampaignMaintainPlannedAction) -> None:
        current = cls._checked_now()
        if action.plan.expires_at <= current:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "Google Ads Search maintenance plan expired before provider egress.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Compile and approve a new exact Google Ads maintenance plan.",
            )

    @staticmethod
    def _proof_for(
        action: GoogleAdsSearchCampaignMaintainPlannedAction,
        acknowledged: _Acknowledgement,
        readback_record_digest: str,
    ) -> tuple[str, str]:
        acknowledgement = _EVIDENCE_PREFIX + digest(
            {
                "kind": "google_ads_v25_search_campaign_maintain_acknowledgement_subset",
                "action_id": action.action_id,
                "plan_digest": action.plan.plan_digest,
                "maintenance_kind": action.intent.maintenance_kind,
                "http_status": 200,
                "partial_failure": False,
                "validate_only": False,
                "result_kinds": list(acknowledged.result_kinds),
            }
        )
        if (
            type(readback_record_digest) is not str
            or len(readback_record_digest) != 64
            or any(character not in "0123456789abcdef" for character in readback_record_digest)
        ):
            raise ValueError("readback record")
        return acknowledgement, _EVIDENCE_PREFIX + readback_record_digest

    def _retain_readback_observation(
        self,
        action: GoogleAdsSearchCampaignMaintainPlannedAction,
        *,
        acknowledged: _Acknowledgement,
        attempt_ref: str,
        member_count: int | None,
        campaign_resource_name: str,
        observation_interval_start: datetime,
        observation_interval_end: datetime,
    ) -> str:
        """Append one dedicated post-acknowledgement observation under recheck."""

        _action, session, _reader, _post, ledger = self._checked_members()
        subplan = action.plan.subplans[0]
        archive = action.intent.maintenance_kind == "CAMPAIGN_ARCHIVE"
        previous, new = maintenance_values(action.observation_binding, action.intent)
        payload = GoogleAdsSearchCampaignMaintainReadbackObservation(
            action_id=action.action_id,
            plan_digest=action.plan.plan_digest,
            subplan_digest=subplan.subplan_digest,
            attempt_ref=attempt_ref,
            account_ref=subplan.account.account_ref,
            campaign_resource_ref=pseudonymized_maintenance_ref("campaign", campaign_resource_name),
            maintenance_kind=action.intent.maintenance_kind,
            operation=subplan.operation.value,  # type: ignore[arg-type]
            observation_interval_start=observation_interval_start,
            observation_interval_end=observation_interval_end,
            previous_value=previous,
            new_value=new,
            observed_after=(
                "CAMPAIGN_STATUS_REMOVED"
                if archive
                else "LIVE_HIERARCHY_MATCHES_PREDICTED_SEMANTICS"
            ),
            observed_member_count=member_count,
            observed_account_status=action.account_binding.account_status,
            authorized_maximum_spend_minor_units=subplan.max_spend.minor_units,
            authorized_currency=subplan.max_spend.currency,
            reversibility=action.postcondition.reversibility,
            acknowledged_operation_count=len(acknowledged.result_kinds),
            acknowledged_result_kinds=acknowledged.result_kinds,
        ).model_dump(mode="json", warnings="error")
        with session.retention_recheck() as recheck:
            record = ledger.append(
                "changes",
                GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND,
                payload,
                provider="google_ads",
                account_ref=subplan.account.account_ref,
                run_id=action.plan.plan_id,
                observed_at=payload["observation_interval_end"],
                occurred_at=payload["observation_interval_end"],
                _before_durable_append=recheck,
            )
        try:
            record_digest = record["record_digest"]
        except (KeyError, TypeError):
            raise ValueError("readback record") from None
        if type(record_digest) is not str:
            raise ValueError("readback record")
        return record_digest

    def _verified_proof(self) -> tuple[str, str] | None:
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


class _Acknowledgement:
    """The only fact this route keeps from one mutate acknowledgement."""

    __slots__ = ("result_kinds",)

    def __init__(self, *, result_kinds: tuple[str, ...]) -> None:
        self.result_kinds = result_kinds


def _required_acknowledgement(
    response: object, expected: tuple[str, ...], *, customer_id: str
) -> _Acknowledgement:
    """Accept only the one closed v25 mutate acknowledgement subset."""

    if type(response) is not _HTTPX_RESPONSE:
        raise ValueError("response type")
    try:
        if type(response.status_code) is not int or response.status_code != 200:
            raise ValueError("status")
        payload = parse_json(response.content, maximum_bytes=_MAX_ACK_BYTES)
        if type(payload) is not dict or set(payload) != {"mutateOperationResponses"}:
            raise ValueError("shape")
        validate_data(payload)
        results = payload["mutateOperationResponses"]
        if type(results) is not list or len(results) != len(expected):
            raise ValueError("results")
        prefix = f"customers/{customer_id}/"
        for index, item in enumerate(results):
            kind = expected[index]
            if type(item) is not dict or set(item) != {kind}:
                raise ValueError("result shape")
            result = item[kind]
            if type(result) is not dict or set(result) != {"resourceName"}:
                raise ValueError("result member")
            resource_name = result["resourceName"]
            if (
                type(resource_name) is not str
                or not resource_name.startswith(prefix)
                or not valid_resource_name(resource_name)
            ):
                raise ValueError("resource name")
        return _Acknowledgement(result_kinds=tuple(expected))
    except Exception:
        raise ValueError("acknowledgement") from None


@contextmanager
def _configured_google_ads_search_campaign_maintain_adapter(
    directory: Path,
    action: GoogleAdsSearchCampaignMaintainPlannedAction,
    ledger: Ledger,
) -> Iterator[GoogleAdsSearchCampaignMaintainGuardedAdapter]:
    """Yield one configured fixed-route Google Ads maintenance adapter."""

    if not isinstance(directory, Path) or type(ledger) is not Ledger:
        raise _adapter_invalid()
    checked_action = revalidate_google_ads_search_campaign_maintain_planned_action(action)
    if (
        ledger.root != directory
        or ledger.profile_ref != checked_action.plan.profile_ref
        or ledger.profile_ref != checked_action.workspace_binding.workspace_profile_ref
    ):
        raise _adapter_invalid()
    if not google_ads_maintain_readback_record_kind_installed():
        raise AdhelmError(
            "GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND_NOT_INSTALLED",
            "This build's ledger does not register the Google Ads MAINTAIN readback record.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a build whose ledger registers "
                f"{GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND}; no credential was read and no "
                "provider request was made."
            ),
        )
    GoogleAdsSearchCampaignMaintainGuardedAdapter._check_plan_expiry(checked_action)
    execution = checked_action.execution_binding
    with planning.configured_google_ads_pause_planning_session(
        directory,
        execution.profile_ref,
        expected_configuration_digest=execution.configuration_digest,
    ) as session:
        _assert_factory_facts(checked_action, session)
        post = _FixedGoogleAdsSearchCampaignMaintainPost(
            session=session,
            customer_id=checked_action.account_binding.account_client_ref,
            intent=checked_action.intent,
            plan_digest=checked_action.plan.plan_digest,
            subplan_digest=checked_action.plan.subplans[0].subplan_digest,
        )
        adapter = GoogleAdsSearchCampaignMaintainGuardedAdapter(
            action=checked_action,
            session=session,
            reader=GoogleAdsLiveHierarchyReader(session),
            post=post,
            ledger=ledger,
            issuer=_ADAPTER_ISSUER,
        )
        try:
            yield adapter
        except AdhelmError as error:
            unproven = adapter.unproven_reference_surfaces
            if error.code != "REMOTE_DRIFT" or not unproven:
                raise
            # The kernel's generic drift refusal cannot say why; the adapter can.
            raise AdhelmError(
                error.code,
                error.message,
                exit_code=error.exit_code,
                next_action=(
                    "This Google Ads campaign is no longer proven unreferenced "
                    f"({describe_surfaces(unproven)}). Archiving is delete-class and "
                    "requires every reference surface to read empty; compile a new plan "
                    "only once it does."
                ),
            ) from None


def _assert_factory_facts(
    action: GoogleAdsSearchCampaignMaintainPlannedAction,
    session: planning.GoogleAdsCampaignPausePlanningSession,
) -> None:
    """Require the exact protected-slot association the plan was compiled under."""

    current = session.execution_binding
    if not planning._same_current_binding(current, action.execution_binding) or (
        action.plan.expires_at > current.expires_at
    ):
        raise AdhelmError(
            "GOOGLE_ADS_MAINTAIN_EXECUTION_BINDING_DRIFT",
            "Current Google Ads EDIT association differs from the approved maintenance action.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Inspect local Google Ads credentials and compile a new exact plan.",
        )


def _adapter_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_MAINTAIN_ADAPTER_INVALID",
        "Google Ads Search maintenance adapter state is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Start a new fixed Google Ads Search maintenance operation from local state.",
    )


def _post_egress_ambiguous() -> AdhelmError:
    return AdhelmError(
        "AMBIGUOUS_REMOTE_STATE",
        "Google Ads maintenance outcome requires reconciliation before any new plan.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Reconcile remote state and compile a new exact plan before any retry; the "
            "previous value is retained in local history and nothing reverts automatically."
        ),
    )


__all__ = [
    "BID_UPDATE_MASK",
    "BUDGET_UPDATE_MASK",
    "STATUS_UPDATE_MASK",
    "GoogleAdsSearchCampaignMaintainGuardedAdapter",
    "expected_result_kinds",
    "google_ads_search_campaign_maintain_mutate_body",
]
