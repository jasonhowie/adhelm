"""Private guarded adapter for the fixed Google Ads paused Search hierarchy CREATE.

This is model-derived offline engineering over the reviewed v25 shape (no Google
Ads SDK is a dependency).  It is not a claim that Google Ads has a live
acknowledgement, partial-failure, conditional-write, or idempotency contract for
this repository.  The only accepted local acknowledgement subset is an exact
HTTP 200 JSON object whose sole member is ``mutateOperationResponses``, holding
exactly one result per planned operation, in order, each carrying exactly the
expected result field and a ``resourceName`` under this customer's path,
followed by a fresh GAQL readback of the created hierarchy.  Every other
outcome after a kernel permit is consumed remains ambiguous and must not be
retried.

The adapter owns the single mutation and nothing else.  Credential continuity,
configuration locking, the fixed v25 header callback, and the bounded GAQL reads
are the sealed :mod:`adhelm.google_ads_pause_planning_session` boundary's and
:mod:`adhelm.google_ads_create_planning_session`'s, reused here through their own
validated private accessors rather than reimplemented.

**Recovery disposition.**  Everything this route creates is ``PAUSED``, and rule
11 forbids deleting a resource that is not proven unreferenced.  A recovery
therefore retains the observed references and leaves the hierarchy paused.  This
adapter has no delete, remove, or archive path at all.
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
from adhelm.google_ads_create_planning_session import (
    GoogleAdsCreateHierarchyObservation,
    GoogleAdsSearchCampaignCreateReader,
)
from adhelm.google_ads_create_readback_records import (
    GOOGLE_ADS_CREATE_READBACK_RECORD_KIND,
    GoogleAdsSearchCampaignCreateReadbackObservation,
    google_ads_create_readback_record_kind_installed,
    pseudonymized_resource_ref,
)
from adhelm.ledger import Ledger
from adhelm.privacy import validate_data
from adhelm.providers.google_ads_campaign_state import (
    GoogleAdsEditIdentity,
    _headers_snapshot,
    _validate_transport,
)
from adhelm.providers.google_ads_compiler import (
    GoogleAdsCompiledCampaignSpec,
    GoogleAdsNativeLanguageCriterionPayload,
    GoogleAdsNativeLocationCriterionPayload,
    GoogleAdsNativeScheduleCriterionPayload,
)
from adhelm.providers.google_ads_create_contract import (
    GOOGLE_ADS_CREATE_API_VERSION,
    GOOGLE_ADS_CREATE_HOST,
    GOOGLE_ADS_CREATE_SCOPE,
    GoogleAdsSearchCampaignCreatePlannedAction,
    fingerprint_key_marker,
    predicted_states,
    projection_from_compiled_spec,
    revalidate_google_ads_search_campaign_create_planned_action,
    semantic_hierarchy_state,
)
from adhelm.safety import (
    AccountIdentity,
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    EffectStatus,
    OperationKind,
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

#: The three temporary resource IDs this route allocates, in the order the one
#: atomic mutate creates them.  Google resolves a negative temporary ID only
#: when the operation that defines it precedes every operation that names it.
_BUDGET_TEMPORARY_ID: Final = -1
_CAMPAIGN_TEMPORARY_ID: Final = -2
_AD_GROUP_TEMPORARY_ID: Final = -3

#: ``RESOURCE_NAME_ONLY`` is the v25 default; naming it keeps the
#: acknowledgement shape this parser accepts fixed rather than dependent on a
#: server default.  UNVERIFIED against a live response.
_RESPONSE_CONTENT_TYPE: Final = "RESOURCE_NAME_ONLY"


class _FixedGoogleAdsCreatePostDispatchUnverified(Exception):
    """Internal marker for an error after a kernel permit was consumed."""


def _now() -> datetime:
    """Private UTC clock seam used only by synthetic adapter tests."""

    return datetime.now(UTC)


def google_ads_search_campaign_create_mutate_body(
    specification: GoogleAdsCompiledCampaignSpec, *, customer_id: str
) -> dict[str, Any]:
    """Return the exact single ``googleAds:mutate`` body this route sends.

    The body is a pure function of the compiled candidate and the customer ID,
    so ``tests/test_google_ads_create_execution.py`` pins it as golden JSON and
    the guarded route cannot drift from the documented wire shape.
    """

    if type(specification) is not GoogleAdsCompiledCampaignSpec:
        raise ValueError("compiled candidate type")
    if type(customer_id) is not str or len(customer_id) != 10 or not customer_id.isdecimal():
        raise ValueError("customer id")
    budget_ref = f"customers/{customer_id}/campaignBudgets/{_BUDGET_TEMPORARY_ID}"
    campaign_ref = f"customers/{customer_id}/campaigns/{_CAMPAIGN_TEMPORARY_ID}"
    ad_group_ref = f"customers/{customer_id}/adGroups/{_AD_GROUP_TEMPORARY_ID}"
    campaign = specification.campaign_request
    network = campaign.network_settings
    operations: list[dict[str, Any]] = [
        {
            "campaignBudgetOperation": {
                "create": {
                    "resourceName": budget_ref,
                    # A non-shared budget has no required name in v25 and the
                    # compiled candidate does not invent one, so none is sent.
                    "explicitlyShared": specification.budget.request.explicitly_shared,
                    "period": specification.budget.request.period,
                    "deliveryMethod": specification.budget.request.delivery_method,
                    "totalAmountMicros": str(specification.budget.request.total_amount_micros),
                }
            }
        },
        {
            "campaignOperation": {
                "create": {
                    "resourceName": campaign_ref,
                    "name": campaign.name,
                    "advertisingChannelType": campaign.advertising_channel_type,
                    "status": campaign.status,
                    "campaignBudget": budget_ref,
                    "startDateTime": campaign.start_date_time,
                    "endDateTime": campaign.end_date_time,
                    "networkSettings": {
                        "targetGoogleSearch": network.target_google_search,
                        "targetSearchNetwork": network.target_search_network,
                        "targetContentNetwork": network.target_content_network,
                        "targetPartnerSearchNetwork": network.target_partner_search_network,
                        "targetYoutube": network.target_youtube,
                        "targetGoogleTvNetwork": network.target_google_tv_network,
                    },
                    "targetSpend": {},
                }
            }
        },
    ]
    for criterion in specification.campaign_criteria:
        request = criterion.request
        create: dict[str, Any] = {
            "campaign": campaign_ref,
            "status": request.status,
            "negative": request.negative,
        }
        if isinstance(request, GoogleAdsNativeLocationCriterionPayload):
            create["location"] = {"geoTargetConstant": request.location.geo_target_constant}
        elif isinstance(request, GoogleAdsNativeLanguageCriterionPayload):
            create["language"] = {"languageConstant": request.language.language_constant}
        elif isinstance(request, GoogleAdsNativeScheduleCriterionPayload):
            schedule = request.ad_schedule
            create["adSchedule"] = {
                "dayOfWeek": schedule.day_of_week,
                "startHour": schedule.start_hour,
                "startMinute": schedule.start_minute,
                "endHour": schedule.end_hour,
                "endMinute": schedule.end_minute,
            }
        else:  # pragma: no cover - the compiled union has exactly three members
            raise ValueError("unknown compiled campaign criterion")
        operations.append({"campaignCriterionOperation": {"create": create}})
    operations.append(
        {
            "adGroupOperation": {
                "create": {
                    "resourceName": ad_group_ref,
                    "name": specification.ad_group.request.name,
                    "campaign": campaign_ref,
                    "type": specification.ad_group.request.type,
                    "status": specification.ad_group.request.status,
                }
            }
        }
    )
    for keyword in specification.keywords:
        operations.append(
            {
                "adGroupCriterionOperation": {
                    "create": {
                        "adGroup": ad_group_ref,
                        "status": keyword.request.status,
                        "negative": keyword.request.negative,
                        "keyword": {
                            "text": keyword.request.keyword.text,
                            "matchType": keyword.request.keyword.match_type,
                        },
                    }
                }
            }
        )
    ad = specification.responsive_search_ad.request.ad
    operations.append(
        {
            "adGroupAdOperation": {
                "create": {
                    "adGroup": ad_group_ref,
                    "status": specification.responsive_search_ad.request.status,
                    "ad": {
                        "finalUrls": list(ad.final_urls),
                        "responsiveSearchAd": {
                            "headlines": [
                                {"text": item.text} for item in ad.responsive_search_ad.headlines
                            ],
                            "descriptions": [
                                {"text": item.text} for item in ad.responsive_search_ad.descriptions
                            ],
                        },
                    },
                }
            }
        }
    )
    return {
        "mutateOperations": operations,
        "partialFailure": False,
        "validateOnly": False,
        "responseContentType": _RESPONSE_CONTENT_TYPE,
    }


def expected_result_kinds(specification: GoogleAdsCompiledCampaignSpec) -> tuple[str, ...]:
    """Return the ordered acknowledgement result field this route requires."""

    return (
        "campaignBudgetResult",
        "campaignResult",
        *("campaignCriterionResult" for _ in specification.campaign_criteria),
        "adGroupResult",
        *("adGroupCriterionResult" for _ in specification.keywords),
        "adGroupAdResult",
    )


class _FixedGoogleAdsSearchCampaignCreatePost:
    """Factory-issued, one-route capability with no token, URL, or body surface."""

    __slots__ = ("_customer_id", "_plan_digest", "_session", "_specification", "_subplan_digest")

    def __init__(
        self,
        *,
        session: planning.GoogleAdsCampaignPausePlanningSession,
        customer_id: str,
        specification: GoogleAdsCompiledCampaignSpec,
        plan_digest: str,
        subplan_digest: str,
    ) -> None:
        self._session = session
        self._customer_id = customer_id
        self._specification = specification
        self._plan_digest = plan_digest
        self._subplan_digest = subplan_digest

    def __repr__(self) -> str:
        return "<_FixedGoogleAdsSearchCampaignCreatePost redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads create capabilities cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads create capabilities cannot be serialized")

    @property
    def expected_kinds(self) -> tuple[str, ...]:
        return expected_result_kinds(self._specification)

    def _preflight(self) -> None:
        """Validate the transport and the fixed route before any permit exists."""

        _transport, _getter, target = self._session._checked_reader()._checked_members()
        if target.customer_client_ref != self._customer_id:
            raise _adapter_invalid()
        # Build the body once before the permit exists so a body-construction
        # failure can never be an ambiguous post-egress outcome.
        google_ads_search_campaign_create_mutate_body(
            self._specification, customer_id=self._customer_id
        )

    def _post(self, *, permit: EgressPermit, attempt_ref: str) -> httpx.Response:
        """Consume one active kernel permit at the sole fixed POST handoff."""

        transport, headers_getter, target = self._session._checked_reader()._checked_members()
        if type(permit) is not EgressPermit:
            raise _permit_required()
        if target.customer_client_ref != self._customer_id:
            raise _adapter_invalid()
        body = google_ads_search_campaign_create_mutate_body(
            self._specification, customer_id=self._customer_id
        )
        headers = _headers_snapshot(headers_getter, target)
        # A credential callback can change mutable Python transport state.
        # Recheck the whole one-attempt/ambient-state boundary before egress.
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
                    f"https://{GOOGLE_ADS_CREATE_HOST}/{GOOGLE_ADS_CREATE_API_VERSION}"
                    f"/customers/{self._customer_id}/googleAds:mutate"
                ),
                headers=headers,
                json=body,
                mutation=True,
            )
        except Exception:
            # The permit has already been consumed.  No transport or response
            # failure can prove the request never reached the provider.
            raise _FixedGoogleAdsCreatePostDispatchUnverified() from None


class GoogleAdsSearchCampaignCreateGuardedAdapter:
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
    )

    def __init__(
        self,
        *,
        action: GoogleAdsSearchCampaignCreatePlannedAction,
        session: planning.GoogleAdsCampaignPausePlanningSession,
        reader: GoogleAdsSearchCampaignCreateReader,
        post: _FixedGoogleAdsSearchCampaignCreatePost,
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

    def __repr__(self) -> str:
        return "<GoogleAdsSearchCampaignCreateGuardedAdapter private fixed route>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads create adapters cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads create adapters cannot be serialized")

    @property
    def action(self) -> GoogleAdsSearchCampaignCreatePlannedAction:
        """Return the reconstructed closed action for private composition only."""

        return self._checked_members()[0]

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        action = self._check_plan(plan)
        return AdapterCapabilities(
            provider_api_version=action.plan.subplans[0].provider_api_version,
            capability_version=action.plan.subplans[0].capability_version,
            supported_operations=(OperationKind.CREATE,),
            # This is a factory-scoped guarded route exercised with fake HTTP.
            # It does not add an installed-default or live-conformance claim.
            writes_enabled=True,
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        action, _session, _reader, _post, _ledger = self._checked_members()
        account = action.plan.subplans[0].account
        if type(account) is not AccountIdentity or account_ref != account.account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Google Ads customer differs from the approved create plan."
            )
        identity = self._current_identity(action)
        return RemoteIdentity(
            stable_account_id=identity.customer_client_ref,
            auth_profile_ref=action.execution_binding.profile_ref,
            principal_ref=action.execution_binding.principal_ref,
            client_application_ref=action.execution_binding.client_application_ref,
            scopes=(GOOGLE_ADS_CREATE_SCOPE,),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        action = self._check_plan(plan)
        return self._current_state(action)[0]

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError(
            "PERMIT_REQUIRED",
            "Google Ads Search hierarchy create egress requires a kernel-issued permit.",
        )

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        action = self._check_plan(plan)
        _action, session, _reader, post, ledger = self._checked_members()
        if self._verified_proof() is not None:
            raise AdhelmError(
                "GOOGLE_ADS_CREATE_ADAPTER_USED",
                "The fixed Google Ads Search create adapter already handled this action.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Reconcile retained history and compile a new exact plan before any retry."
                ),
            )
        if not self._kernel_started(action, plan, ledger):
            raise AdhelmError(
                "KERNEL_START_REQUIRED",
                "Google Ads create egress requires the kernel's durable start.",
            )

        # The final remote baseline is one customer search, the exact
        # campaign-name collision search, and the account asset-automation
        # search.  All three bind current EDIT continuity before egress.
        before, _observed = self._current_state(action)
        if before.fingerprint != plan.before.fingerprint:
            raise AdhelmError(
                "REMOTE_DRIFT",
                "Google Ads already holds a campaign with this planned name.",
            )
        self._check_plan_expiry(action)
        # This local-only check intentionally runs before permit consumption.
        # It holds neither configuration nor credential locks across egress.
        post._preflight()
        self._check_plan_expiry(action)
        try:
            response = post._post(permit=permit, attempt_ref=idempotency_key)
        except _FixedGoogleAdsCreatePostDispatchUnverified:
            raise _post_egress_ambiguous() from None
        try:
            acknowledged = _required_acknowledgement(
                response, post.expected_kinds, customer_id=action.account_binding.account_client_ref
            )
            # Capture the actual post-acknowledgement observation interval
            # around the fresh hierarchy readback.  This is factual local
            # history, not a provider acknowledgement contract.
            readback_started_at = self._checked_now()
            observation = self._current_hierarchy(action)
            readback_finished_at = self._checked_now()
            after = self._state_for_hierarchy(action, observation)
            if after.fingerprint != plan.after.fingerprint or after.revision != plan.after.revision:
                raise ValueError("postcondition")
            readback_record_digest = self._retain_readback_observation(
                action,
                observation,
                acknowledged=acknowledged,
                attempt_ref=idempotency_key,
                observation_interval_start=readback_started_at,
                observation_interval_end=readback_finished_at,
            )
            proof = self._proof_for(action, acknowledged, readback_record_digest)
            self._proof = proof
            self._proof_issuer = _PROOF_ISSUER
            del session
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=proof)
        except Exception:
            # The permit was consumed.  No transport, acknowledgement, parser,
            # customer, hierarchy, or readback failure can prove no remote
            # effect.  Rule 11 forbids deleting whatever may now exist; the
            # hierarchy is paused and recovery retains it.
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
        # idempotency proof for this offline SDK-derived route, and a second
        # create would duplicate the hierarchy rather than converge.
        return Reconciliation(
            state=current,
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )

    # -- private members ----------------------------------------------------

    def _checked_members(
        self,
    ) -> tuple[
        GoogleAdsSearchCampaignCreatePlannedAction,
        planning.GoogleAdsCampaignPausePlanningSession,
        GoogleAdsSearchCampaignCreateReader,
        _FixedGoogleAdsSearchCampaignCreatePost,
        Ledger,
    ]:
        if type(self) is not GoogleAdsSearchCampaignCreateGuardedAdapter:
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
            or type(reader) is not GoogleAdsSearchCampaignCreateReader
            or type(post) is not _FixedGoogleAdsSearchCampaignCreatePost
            or type(ledger) is not Ledger
        ):
            raise _adapter_invalid()
        checked_action = revalidate_google_ads_search_campaign_create_planned_action(action)
        candidate = checked_action.candidate_binding
        if (
            post._session is not session
            or reader._session is not session
            or post._customer_id != checked_action.account_binding.account_client_ref
            or post._specification != candidate.specification
            or post._plan_digest != checked_action.plan.plan_digest
            or post._subplan_digest != checked_action.plan.subplans[0].subplan_digest
        ):
            raise _adapter_invalid()
        return checked_action, session, reader, post, ledger

    def _check_plan(self, plan: object) -> GoogleAdsSearchCampaignCreatePlannedAction:
        action, _session, _reader, _post, _ledger = self._checked_members()
        if type(plan) is not SubPlan or plan != action.plan.subplans[0]:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "Adapter refuses a different Google Ads Search create plan.",
            )
        return action

    @staticmethod
    def _fingerprint_key(action: GoogleAdsSearchCampaignCreatePlannedAction) -> bytes:
        key = export_key()
        if fingerprint_key_marker(key) != action.observation_binding.fingerprint_key_marker:
            raise AdhelmError(
                "GOOGLE_ADS_CREATE_FINGERPRINT_KEY_CHANGED",
                "The local fingerprint key differs from the one this plan was projected under.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Compile a new exact plan from a fresh bounded read.",
            )
        return key

    def _current_identity(
        self, action: GoogleAdsSearchCampaignCreatePlannedAction
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
                "Current Google Ads customer facts differ from the approved create action.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured Google Ads customer and compile a new plan.",
            )
        return identity

    def _current_state(
        self, action: GoogleAdsSearchCampaignCreatePlannedAction
    ) -> tuple[RemoteState, GoogleAdsCreateHierarchyObservation | None]:
        """Return the semantic state this workspace currently observes."""

        _action, _session, reader, _post, _ledger = self._checked_members()
        # The semantic states are locally keyed.  A workspace that can no longer
        # reproduce them cannot compare a readback either, so this refuses
        # before the identity read rather than after a create.
        self._checked_states(action)
        self._current_identity(action)
        name = action.observation_binding.campaign_name
        try:
            observed = reader.observe_account(name)
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None
        subplan = action.plan.subplans[0]
        if observed.campaign_name_near_matches != 0:
            # The plan asserted this precondition; a near match appearing after
            # planning is drift, refused before the single mutation.
            raise AdhelmError(
                "REMOTE_DRIFT",
                "Google Ads now holds a campaign whose name differs from the planned name "
                "only by case, surrounding whitespace, or Unicode normalization form.",
            )
        if observed.campaign_name_matches == 0:
            return RemoteState(
                fingerprint=subplan.before.fingerprint, revision=subplan.before.revision
            ), None
        if observed.campaign_name_matches != 1:
            raise AdhelmError(
                "REMOTE_DRIFT",
                "Google Ads holds more than one campaign with the planned name.",
            )
        observation = self._current_hierarchy(action)
        return self._state_for_hierarchy(action, observation), observation

    def _current_hierarchy(
        self, action: GoogleAdsSearchCampaignCreatePlannedAction
    ) -> GoogleAdsCreateHierarchyObservation:
        _action, _session, reader, _post, _ledger = self._checked_members()
        try:
            observation = reader.observe_created_hierarchy(action.observation_binding.campaign_name)
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None
        if type(observation) is not GoogleAdsCreateHierarchyObservation:
            raise _adapter_invalid()
        return observation

    def _checked_states(
        self, action: GoogleAdsSearchCampaignCreatePlannedAction
    ) -> tuple[bytes, StateRef, StateRef]:
        """Reproduce the plan's two semantic states under the current local key."""

        key = self._fingerprint_key(action)
        subplan = action.plan.subplans[0]
        before, after = predicted_states(action, fingerprint_key=key)
        if before != subplan.before or after != subplan.after:
            raise AdhelmError(
                "GOOGLE_ADS_CREATE_STATE_UNREPRODUCIBLE",
                "The retained plan's semantic states are not reproducible locally.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Compile a new exact plan from a fresh bounded read.",
            )
        return key, before, after

    def _state_for_hierarchy(
        self,
        action: GoogleAdsSearchCampaignCreatePlannedAction,
        observation: GoogleAdsCreateHierarchyObservation,
    ) -> RemoteState:
        key, _before, after = self._checked_states(action)
        observed = semantic_hierarchy_state(
            observation.projection,
            account_client_ref=action.account_binding.account_client_ref,
            campaign_name=action.observation_binding.campaign_name,
            fingerprint_key=key,
        )
        if observed != after:
            raise AdhelmError(
                "REMOTE_DRIFT",
                "The observed Google Ads hierarchy differs from the approved prediction.",
            )
        return RemoteState(fingerprint=after.fingerprint, revision=after.revision)

    @staticmethod
    def _kernel_started(
        action: GoogleAdsSearchCampaignCreatePlannedAction, plan: SubPlan, ledger: Ledger
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
            raise AdhelmError("CLOCK_INVALID", "Google Ads create clock is unavailable.") from None
        if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
            raise AdhelmError("CLOCK_INVALID", "Google Ads create clock is invalid.")
        return current.astimezone(UTC)

    @classmethod
    def _check_plan_expiry(cls, action: GoogleAdsSearchCampaignCreatePlannedAction) -> None:
        current = cls._checked_now()
        if action.plan.expires_at <= current:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "Google Ads Search create plan expired before provider egress.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Compile and approve a new exact Google Ads create plan.",
            )

    @staticmethod
    def _proof_for(
        action: GoogleAdsSearchCampaignCreatePlannedAction,
        acknowledged: _Acknowledgement,
        readback_record_digest: str,
    ) -> tuple[str, str]:
        acknowledgement = _EVIDENCE_PREFIX + digest(
            {
                "kind": "google_ads_v25_search_campaign_create_acknowledgement_subset",
                "action_id": action.action_id,
                "plan_digest": action.plan.plan_digest,
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
        action: GoogleAdsSearchCampaignCreatePlannedAction,
        observation: GoogleAdsCreateHierarchyObservation,
        *,
        acknowledged: _Acknowledgement,
        attempt_ref: str,
        observation_interval_start: datetime,
        observation_interval_end: datetime,
    ) -> str:
        """Append one dedicated post-acknowledgement observation under recheck."""

        _action, session, _reader, _post, ledger = self._checked_members()
        subplan = action.plan.subplans[0]
        payload = GoogleAdsSearchCampaignCreateReadbackObservation(
            action_id=action.action_id,
            plan_digest=action.plan.plan_digest,
            subplan_digest=subplan.subplan_digest,
            attempt_ref=attempt_ref,
            account_ref=subplan.account.account_ref,
            campaign_resource_ref=pseudonymized_resource_ref(
                "campaign", acknowledged.campaign_resource_name
            ),
            ad_group_resource_ref=pseudonymized_resource_ref(
                "ad_group", acknowledged.ad_group_resource_name
            ),
            observation_interval_start=observation_interval_start,
            observation_interval_end=observation_interval_end,
            observed_member_count=len(observation.projection.members),
            observed_account_status=action.account_binding.account_status,
            observed_positive_geo_target_type=observation.positive_geo_target_type,
            observed_negative_geo_target_type=observation.negative_geo_target_type,
            observed_asset_automation_settings=observation.asset_automation_settings,
            acknowledged_operation_count=len(acknowledged.result_kinds),
            acknowledged_result_kinds=acknowledged.result_kinds,
        ).model_dump(mode="json", warnings="error")
        with session.retention_recheck() as recheck:
            record = ledger.append(
                "changes",
                GOOGLE_ADS_CREATE_READBACK_RECORD_KIND,
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
    """The only three facts this route keeps from one mutate acknowledgement."""

    __slots__ = ("ad_group_resource_name", "campaign_resource_name", "result_kinds")

    def __init__(
        self,
        *,
        result_kinds: tuple[str, ...],
        campaign_resource_name: str,
        ad_group_resource_name: str,
    ) -> None:
        self.result_kinds = result_kinds
        self.campaign_resource_name = campaign_resource_name
        self.ad_group_resource_name = ad_group_resource_name


def _required_acknowledgement(
    response: object, expected: tuple[str, ...], *, customer_id: str
) -> _Acknowledgement:
    """Accept only the one closed v25 mutate acknowledgement subset.

    Anything else -- a partial-failure envelope, an extra member, a missing or
    extra result, an out-of-order result field, a resource name outside this
    customer, a non-200 status -- is refused.  The permit is already consumed,
    so the caller reports it as ambiguous.
    """

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
        campaign_resource_name = ""
        ad_group_resource_name = ""
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
                or not _valid_resource_name(resource_name)
            ):
                raise ValueError("resource name")
            if kind == "campaignResult":
                campaign_resource_name = resource_name
            elif kind == "adGroupResult":
                ad_group_resource_name = resource_name
        if not campaign_resource_name or not ad_group_resource_name:
            raise ValueError("required results")
        return _Acknowledgement(
            result_kinds=tuple(expected),
            campaign_resource_name=campaign_resource_name,
            ad_group_resource_name=ad_group_resource_name,
        )
    except Exception:
        raise ValueError("acknowledgement") from None


def _valid_resource_name(value: str) -> bool:
    parts = value.split("/")
    return (
        len(parts) == 4
        and parts[0] == "customers"
        and len(parts[1]) == 10
        and parts[1].isdecimal()
        and parts[2].isalpha()
        and parts[3].isdecimal()
        and parts[3][0] != "0"
    )


@contextmanager
def _configured_google_ads_search_campaign_create_adapter(
    directory: Path,
    action: GoogleAdsSearchCampaignCreatePlannedAction,
    ledger: Ledger,
) -> Iterator[GoogleAdsSearchCampaignCreateGuardedAdapter]:
    """Yield one configured fixed-route Google Ads CREATE adapter."""

    if not isinstance(directory, Path) or type(ledger) is not Ledger:
        raise _adapter_invalid()
    checked_action = revalidate_google_ads_search_campaign_create_planned_action(action)
    if (
        ledger.root != directory
        or ledger.profile_ref != checked_action.plan.profile_ref
        or ledger.profile_ref != checked_action.workspace_binding.workspace_profile_ref
    ):
        raise _adapter_invalid()
    # Refuse before any credential, header, or provider request when this build
    # cannot retain the observation that proves what the mutation did.  This is
    # a fail-closed build check, never a path that creates without a readback.
    if not google_ads_create_readback_record_kind_installed():
        raise AdhelmError(
            "GOOGLE_ADS_CREATE_READBACK_RECORD_KIND_NOT_INSTALLED",
            "This build's ledger does not register the Google Ads CREATE readback record.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a build whose ledger registers "
                f"{GOOGLE_ADS_CREATE_READBACK_RECORD_KIND}; no credential was read and no "
                "provider request was made."
            ),
        )
    # The candidate must still be fully paused and self-consistent.  The
    # compiler's own types already force this; restating it here keeps a
    # constructed or altered action from reaching a mutate body.
    projection_from_compiled_spec(checked_action.candidate_binding.specification)
    GoogleAdsSearchCampaignCreateGuardedAdapter._check_plan_expiry(checked_action)
    execution = checked_action.execution_binding
    with planning.configured_google_ads_pause_planning_session(
        directory,
        execution.profile_ref,
        expected_configuration_digest=execution.configuration_digest,
    ) as session:
        _assert_factory_facts(checked_action, session)
        post = _FixedGoogleAdsSearchCampaignCreatePost(
            session=session,
            customer_id=checked_action.account_binding.account_client_ref,
            specification=checked_action.candidate_binding.specification,
            plan_digest=checked_action.plan.plan_digest,
            subplan_digest=checked_action.plan.subplans[0].subplan_digest,
        )
        yield GoogleAdsSearchCampaignCreateGuardedAdapter(
            action=checked_action,
            session=session,
            reader=GoogleAdsSearchCampaignCreateReader(session),
            post=post,
            ledger=ledger,
            issuer=_ADAPTER_ISSUER,
        )


def _assert_factory_facts(
    action: GoogleAdsSearchCampaignCreatePlannedAction,
    session: planning.GoogleAdsCampaignPausePlanningSession,
) -> None:
    """Require the exact protected-slot association the plan was compiled under."""

    current = session.execution_binding
    if not planning._same_current_binding(current, action.execution_binding) or (
        action.plan.expires_at > current.expires_at
    ):
        raise AdhelmError(
            "GOOGLE_ADS_CREATE_EXECUTION_BINDING_DRIFT",
            "Current Google Ads EDIT association differs from the approved create action.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Inspect local Google Ads credentials and compile a new exact plan.",
        )


def _adapter_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_CREATE_ADAPTER_INVALID",
        "Google Ads Search create adapter state is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Start a new fixed Google Ads Search create operation from local state.",
    )


def _post_egress_ambiguous() -> AdhelmError:
    return AdhelmError(
        "AMBIGUOUS_REMOTE_STATE",
        "Google Ads Search create outcome requires reconciliation before any new plan.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Reconcile remote state and compile a new exact plan before any retry; anything "
            "this attempt created is paused and must not be deleted."
        ),
    )


__all__ = [
    "GoogleAdsSearchCampaignCreateGuardedAdapter",
    "expected_result_kinds",
    "google_ads_search_campaign_create_mutate_body",
]
