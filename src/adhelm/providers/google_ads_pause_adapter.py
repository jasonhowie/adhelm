"""Private guarded adapter for the fixed Google Ads campaign ``ENABLED`` -> ``PAUSED`` route.

This is model-derived offline engineering over the reviewed v25 shape (no Google
Ads SDK is a dependency).  It is not
a claim that Google Ads has a live acknowledgement, partial-failure,
conditional-write, or idempotency contract for this repository.  The only
accepted local acknowledgement subset is an exact HTTP 200 JSON object whose
sole member is ``results``, holding exactly one object whose sole member is the
``resourceName`` of the approved campaign, followed by a fresh configured-status
readback.  Every other outcome after a kernel permit is consumed remains
ambiguous and must not be retried.

The adapter owns the single mutation and nothing else.  Credential continuity,
configuration locking, the fixed v25 header callback, and the two fixed
``googleAds:search`` reads are the sealed
:mod:`adhelm.google_ads_pause_planning_session` boundary's, reused here through
its own validated private accessors rather than reimplemented.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Never

import httpx

from adhelm import google_ads_pause_planning_session as planning
from adhelm.canonical import digest, parse_json
from adhelm.egress_permit import EgressPermit
from adhelm.egress_permit import _required as _permit_required
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_pause_readback_records import (
    GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND,
    GoogleAdsCampaignPauseReadbackObservation,
    google_ads_pause_readback_record_kind_installed,
)
from adhelm.ledger import Ledger
from adhelm.privacy import validate_data
from adhelm.providers.google_ads_campaign_state import (
    GoogleAdsCampaignState,
    GoogleAdsEditIdentity,
    _headers_snapshot,
    _validate_transport,
)
from adhelm.providers.google_ads_pause_contract import (
    GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION,
    GOOGLE_ADS_CAMPAIGN_PAUSE_HOST,
    GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE,
    GoogleAdsCampaignPausePlannedAction,
    revalidate_google_ads_campaign_pause_planned_action,
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
    SubPlan,
    Verification,
)

_HTTPX_RESPONSE: Final = httpx.Response
_ADAPTER_ISSUER = object()
_PROOF_ISSUER = object()
_EVIDENCE_PREFIX: Final = "evidence:sha256:"
_MAX_ACK_BYTES: Final = 65_536

#: The one v25 mutation this repository sends.  The field mask is exactly
#: ``status``, so no other campaign field can be written by this route.
_UPDATE_MASK: Final = "status"
_TARGET_STATUS: Final = "PAUSED"
#: ``RESOURCE_NAME_ONLY`` is the v25 default; naming it keeps the acknowledgement
#: shape this parser accepts fixed rather than dependent on a server default.
_RESPONSE_CONTENT_TYPE: Final = "RESOURCE_NAME_ONLY"


class _FixedGoogleAdsCampaignPausePostDispatchUnverified(Exception):
    """Internal marker for an error after a kernel permit was consumed."""


def _now() -> datetime:
    """Private UTC clock seam used only by synthetic adapter tests."""

    return datetime.now(UTC)


class _FixedGoogleAdsCampaignPausePost:
    """Factory-issued, one-route capability with no token, URL, or body surface."""

    __slots__ = ("_campaign_id", "_customer_id", "_plan_digest", "_session", "_subplan_digest")

    def __init__(
        self,
        *,
        session: planning.GoogleAdsCampaignPausePlanningSession,
        customer_id: str,
        campaign_id: str,
        plan_digest: str,
        subplan_digest: str,
    ) -> None:
        self._session = session
        self._customer_id = customer_id
        self._campaign_id = campaign_id
        self._plan_digest = plan_digest
        self._subplan_digest = subplan_digest

    def __repr__(self) -> str:
        return "<_FixedGoogleAdsCampaignPausePost redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads campaign pause capabilities cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads campaign pause capabilities cannot be serialized")

    @property
    def resource_name(self) -> str:
        """Return the exact approved campaign resource path this route writes."""

        return f"customers/{self._customer_id}/campaigns/{self._campaign_id}"

    def _preflight(self) -> None:
        """Validate the transport and the fixed route before any permit exists."""

        _transport, _getter, target = self._session._checked_reader()._checked_members()
        if target.customer_client_ref != self._customer_id:
            raise _adapter_invalid()

    def _post(self, *, permit: EgressPermit, attempt_ref: str) -> httpx.Response:
        """Consume one active kernel permit at the sole fixed POST handoff.

        Credential continuity, the configuration transaction, and the header
        callback all complete *before* the permit is claimed, so a drift,
        expiry, or clock failure stays a pre-egress refusal.  Everything after
        the claim is potentially dispatched and is reported as unverified.
        """

        transport, headers_getter, target = self._session._checked_reader()._checked_members()
        if type(permit) is not EgressPermit:
            raise _permit_required()
        if target.customer_client_ref != self._customer_id:
            raise _adapter_invalid()
        headers = _headers_snapshot(headers_getter, target)
        # A credential callback can change mutable Python transport state.
        # Recheck the whole one-attempt/ambient-state boundary before egress.
        _validate_transport(transport)
        body = {
            "operations": [
                {
                    "updateMask": _UPDATE_MASK,
                    "update": {"resourceName": self.resource_name, "status": _TARGET_STATUS},
                }
            ],
            "partialFailure": False,
            "validateOnly": False,
            "responseContentType": _RESPONSE_CONTENT_TYPE,
        }
        permit.consume(
            plan_digest=self._plan_digest,
            subplan_digest=self._subplan_digest,
            attempt_ref=attempt_ref,
        )
        try:
            return transport.request(
                "POST",
                (
                    f"https://{GOOGLE_ADS_CAMPAIGN_PAUSE_HOST}/"
                    f"{GOOGLE_ADS_CAMPAIGN_PAUSE_API_VERSION}/customers/"
                    f"{self._customer_id}/campaigns:mutate"
                ),
                headers=headers,
                json=body,
                mutation=True,
            )
        except Exception:
            # The permit has already been consumed.  No transport or response
            # failure can prove the request never reached the provider.
            raise _FixedGoogleAdsCampaignPausePostDispatchUnverified() from None


class GoogleAdsCampaignPauseGuardedAdapter:
    """Factory-issued fixed-route adapter consumed only by ``SafetyKernel``.

    It exposes the ordinary guarded-adapter protocol but cannot be configured
    with a bearer, URL, HTTP client, raw acknowledgement, or
    caller clock.
    """

    provider: str = "google_ads"

    __slots__ = ("_action", "_issuer", "_ledger", "_post", "_proof", "_proof_issuer", "_session")

    def __init__(
        self,
        *,
        action: GoogleAdsCampaignPausePlannedAction,
        session: planning.GoogleAdsCampaignPausePlanningSession,
        post: _FixedGoogleAdsCampaignPausePost,
        ledger: Ledger,
        issuer: object,
    ) -> None:
        self._action = action
        self._session = session
        self._post = post
        self._ledger = ledger
        self._issuer = issuer
        self._proof: tuple[str, str] | None = None
        self._proof_issuer: object | None = None

    def __repr__(self) -> str:
        return "<GoogleAdsCampaignPauseGuardedAdapter private fixed route>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads campaign pause adapters cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads campaign pause adapters cannot be serialized")

    @property
    def action(self) -> GoogleAdsCampaignPausePlannedAction:
        """Return the reconstructed closed action for private composition only."""

        return self._checked_members()[0]

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        action = self._check_plan(plan)
        return AdapterCapabilities(
            provider_api_version=action.plan.subplans[0].provider_api_version,
            capability_version=action.plan.subplans[0].capability_version,
            supported_operations=(OperationKind.PAUSE,),
            # This is a factory-scoped guarded route exercised with fake HTTP.
            # It does not add an installed-default or live-conformance claim.
            writes_enabled=True,
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        action, _session, _post, _ledger = self._checked_members()
        account = action.plan.subplans[0].account
        if type(account) is not AccountIdentity or account_ref != account.account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Google Ads customer differs from the approved pause plan."
            )
        identity = self._current_identity(action)
        return RemoteIdentity(
            stable_account_id=identity.customer_client_ref,
            auth_profile_ref=action.execution_binding.profile_ref,
            principal_ref=action.execution_binding.principal_ref,
            client_application_ref=action.execution_binding.client_application_ref,
            scopes=(GOOGLE_ADS_CAMPAIGN_PAUSE_SCOPE,),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        action = self._check_plan(plan)
        state = self._current_campaign_state(action)
        return self._state_for_observation(action, state)

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError(
            "PERMIT_REQUIRED", "Google Ads campaign pause egress requires a kernel-issued permit."
        )

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        action = self._check_plan(plan)
        _action, _session, post, ledger = self._checked_members()
        if self._verified_proof() is not None:
            raise AdhelmError(
                "GOOGLE_ADS_CAMPAIGN_PAUSE_ADAPTER_USED",
                "The fixed Google Ads campaign pause adapter already handled this action.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Reconcile retained history and compile a new exact plan before any retry."
                ),
            )
        if not self._kernel_started(action, plan, ledger):
            raise AdhelmError(
                "KERNEL_START_REQUIRED",
                "Google Ads campaign pause egress requires the kernel's durable start.",
            )

        # The final remote baseline is one customer search followed by the one
        # campaign search.  Both bind current EDIT continuity before egress.
        before = self._current_campaign_state(action)
        if self._state_for_observation(action, before).fingerprint != plan.before.fingerprint:
            raise AdhelmError(
                "REMOTE_DRIFT",
                "Google Ads campaign differs from the approved ENABLED baseline before egress.",
            )
        self._check_plan_expiry(action)
        # This local-only check intentionally runs before permit consumption.
        # It holds neither configuration nor credential locks across egress.
        post._preflight()
        self._check_plan_expiry(action)
        try:
            response = post._post(permit=permit, attempt_ref=idempotency_key)
        except _FixedGoogleAdsCampaignPausePostDispatchUnverified:
            raise _post_egress_ambiguous() from None
        try:
            self._require_acknowledgement(response, post.resource_name)
            # Capture the actual post-acknowledgement observation interval
            # around the fresh customer identity and campaign readback.  This
            # is factual local history, not a provider acknowledgement contract.
            readback_started_at = self._checked_now()
            after_observation = self._current_campaign_state(action)
            readback_finished_at = self._checked_now()
            after = self._state_for_observation(action, after_observation)
            if after.fingerprint != plan.after.fingerprint or after.revision != plan.after.revision:
                raise ValueError("postcondition")
            readback_record_digest = self._retain_readback_observation(
                action,
                after_observation,
                attempt_ref=idempotency_key,
                acknowledged_resource_name=post.resource_name,
                observation_interval_start=readback_started_at,
                observation_interval_end=readback_finished_at,
            )
            proof = self._proof_for(action, readback_record_digest)
            self._proof = proof
            self._proof_issuer = _PROOF_ISSUER
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=proof)
        except Exception:
            # The permit was consumed.  No transport, acknowledgement, parser,
            # customer, campaign, or readback failure can prove no remote effect.
            raise _post_egress_ambiguous() from None

    def verify(self, plan: SubPlan) -> Verification:
        """Report the fresh-readback proof this adapter actually established.

        The comparison itself already happened twice against a fresh read:
        once inside ``apply_guarded``, which refuses to record a proof unless
        its own post-acknowledgement readback equals the approved ``after``
        fingerprint and revision, and once in the kernel, which re-reads state
        before calling this method.  ``verify`` therefore reports that proof
        rather than taking a third read, and it is false whenever this process
        did not establish one.
        """

        self._check_plan(plan)
        proof = self._verified_proof()
        return Verification(verified=proof is not None, evidence_refs=proof or ())

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        self._check_plan(plan)
        try:
            current = self.read_state(plan)
        except Exception:
            current = RemoteState(fingerprint="unavailable")
        # A later matching PAUSED read cannot attribute a missing or uncertain
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
        GoogleAdsCampaignPausePlannedAction,
        planning.GoogleAdsCampaignPausePlanningSession,
        _FixedGoogleAdsCampaignPausePost,
        Ledger,
    ]:
        if type(self) is not GoogleAdsCampaignPauseGuardedAdapter:
            raise _adapter_invalid()
        try:
            issuer = self._issuer
            action = self._action
            session = self._session
            post = self._post
            ledger = self._ledger
        except Exception:
            raise _adapter_invalid() from None
        if (
            issuer is not _ADAPTER_ISSUER
            or type(session) is not planning.GoogleAdsCampaignPausePlanningSession
            or type(post) is not _FixedGoogleAdsCampaignPausePost
            or type(ledger) is not Ledger
        ):
            raise _adapter_invalid()
        checked_action = revalidate_google_ads_campaign_pause_planned_action(action)
        campaign = checked_action.observation_binding.campaign
        if (
            post._session is not session
            or post._customer_id != campaign.account_client_ref
            or post._campaign_id != campaign.campaign_client_ref
            or post._plan_digest != checked_action.plan.plan_digest
            or post._subplan_digest != checked_action.plan.subplans[0].subplan_digest
        ):
            raise _adapter_invalid()
        return checked_action, session, post, ledger

    def _check_plan(self, plan: object) -> GoogleAdsCampaignPausePlannedAction:
        action, _session, _post, _ledger = self._checked_members()
        if type(plan) is not SubPlan or plan != action.plan.subplans[0]:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "Adapter refuses a different Google Ads campaign pause plan.",
            )
        return action

    def _current_identity(
        self, action: GoogleAdsCampaignPausePlannedAction
    ) -> GoogleAdsEditIdentity:
        _action, session, _post, _ledger = self._checked_members()
        try:
            identity = session._checked_reader().discover_identity()
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None
        if type(identity) is not GoogleAdsEditIdentity:
            raise _adapter_invalid()
        account = action.account_binding
        if (
            identity.customer_ref != account.account_ref
            or identity.customer_client_ref != account.account_client_ref
            or identity.currency != account.currency
            or identity.timezone != account.timezone
            or identity.status != account.account_status
            or identity.provider_api_version != account.provider_api_version
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Current Google Ads customer facts differ from the approved pause action.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured Google Ads customer and compile a new plan.",
            )
        return identity

    def _current_campaign_state(
        self, action: GoogleAdsCampaignPausePlannedAction
    ) -> GoogleAdsCampaignState:
        _action, session, _post, _ledger = self._checked_members()
        identity = self._current_identity(action)
        try:
            state = session._checked_reader().read_campaign_state(
                identity, action.observation_binding.campaign.campaign_client_ref
            )
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None
        if type(state) is not GoogleAdsCampaignState:
            raise _adapter_invalid()
        return state

    @staticmethod
    def _state_for_observation(
        action: GoogleAdsCampaignPausePlannedAction, state: GoogleAdsCampaignState
    ) -> RemoteState:
        campaign = action.observation_binding.campaign
        subplan = action.plan.subplans[0]
        if (
            state.campaign_client_ref != campaign.campaign_client_ref
            or state.campaign_ref != campaign.campaign_ref
            or state.customer_client_ref != campaign.account_client_ref
            or state.customer_ref != campaign.account_ref
            or state.provider_api_version != action.account_binding.provider_api_version
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Google Ads campaign state is outside the approved customer or campaign binding.",
            )
        if state.status == campaign.status:
            return RemoteState(
                fingerprint=subplan.before.fingerprint, revision=subplan.before.revision
            )
        if state.status == action.postcondition.configured_status:
            # Configured status only; PAUSED does not predict effective
            # delivery, spend, or the moment serving actually stops.
            return RemoteState(
                fingerprint=subplan.after.fingerprint, revision=subplan.after.revision
            )
        raise AdhelmError(
            "REMOTE_DRIFT",
            "Google Ads campaign state differs from the approved pause transition.",
        )

    @staticmethod
    def _kernel_started(
        action: GoogleAdsCampaignPausePlannedAction, plan: SubPlan, ledger: Ledger
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
                "CLOCK_INVALID", "Google Ads campaign pause clock is unavailable."
            ) from None
        if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
            raise AdhelmError("CLOCK_INVALID", "Google Ads campaign pause clock is invalid.")
        return current.astimezone(UTC)

    @classmethod
    def _check_plan_expiry(cls, action: GoogleAdsCampaignPausePlannedAction) -> None:
        current = cls._checked_now()
        if action.plan.expires_at <= current:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "Google Ads campaign pause plan expired before provider egress.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Compile and approve a new exact Google Ads campaign pause plan.",
            )

    @staticmethod
    def _require_acknowledgement(response: object, resource_name: str) -> None:
        """Accept only the one closed v25 mutate acknowledgement subset.

        Anything else -- a partial-failure envelope, an extra member, a second
        result, a different resource name, a non-200 status -- is refused.  The
        permit is already consumed, so the caller reports it as ambiguous.
        """

        if type(response) is not _HTTPX_RESPONSE:
            raise ValueError("response type")
        try:
            if type(response.status_code) is not int or response.status_code != 200:
                raise ValueError("status")
            payload = parse_json(response.content, maximum_bytes=_MAX_ACK_BYTES)
            if type(payload) is not dict or set(payload) != {"results"}:
                raise ValueError("shape")
            validate_data(payload)
            results = payload["results"]
            if type(results) is not list or len(results) != 1:
                raise ValueError("results")
            result = results[0]
            if type(result) is not dict or set(result) != {"resourceName"}:
                raise ValueError("result shape")
            if type(result["resourceName"]) is not str or result["resourceName"] != resource_name:
                raise ValueError("resource name")
        except Exception:
            raise ValueError("acknowledgement") from None

    @staticmethod
    def _proof_for(
        action: GoogleAdsCampaignPausePlannedAction, readback_record_digest: str
    ) -> tuple[str, str]:
        acknowledgement = _EVIDENCE_PREFIX + digest(
            {
                "kind": "google_ads_v25_campaign_pause_acknowledgement_subset",
                "action_id": action.action_id,
                "plan_digest": action.plan.plan_digest,
                "http_status": 200,
                "update_mask": _UPDATE_MASK,
                "results": 1,
            }
        )
        if (
            type(readback_record_digest) is not str
            or len(readback_record_digest) != 64
            or any(character not in "0123456789abcdef" for character in readback_record_digest)
        ):
            raise ValueError("readback record")
        # The second proof binds the exact dedicated local observation record,
        # which preserves the observed statuses and the acknowledged resource
        # name without claiming they prove delivery or provider semantics.
        return acknowledgement, _EVIDENCE_PREFIX + readback_record_digest

    def _retain_readback_observation(
        self,
        action: GoogleAdsCampaignPausePlannedAction,
        observation: GoogleAdsCampaignState,
        *,
        attempt_ref: str,
        acknowledged_resource_name: str,
        observation_interval_start: datetime,
        observation_interval_end: datetime,
    ) -> str:
        """Append one dedicated post-acknowledgement observation under recheck.

        The provider reads have already completed before entering
        ``retention_recheck``.  Its callback only rechecks the currently-held
        configuration and the three protected slots immediately before the
        durable append; it performs no network or ledger I/O itself.
        """

        _action, session, _post, ledger = self._checked_members()
        if observation.status != action.postcondition.configured_status:
            raise ValueError("readback status")
        subplan = action.plan.subplans[0]
        payload = GoogleAdsCampaignPauseReadbackObservation(
            action_id=action.action_id,
            plan_digest=action.plan.plan_digest,
            subplan_digest=subplan.subplan_digest,
            attempt_ref=attempt_ref,
            account_ref=subplan.account.account_ref,
            campaign_ref=action.observation_binding.campaign.campaign_ref,
            observation_interval_start=observation_interval_start,
            observation_interval_end=observation_interval_end,
            observed_account_status=action.account_binding.account_status,
            acknowledged_resource_name=acknowledged_resource_name,
        ).model_dump(mode="json", warnings="error")
        with session.retention_recheck() as recheck:
            record = ledger.append(
                "changes",
                GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND,
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


@contextmanager
def _configured_google_ads_campaign_pause_adapter(
    directory: Path,
    action: GoogleAdsCampaignPausePlannedAction,
    ledger: Ledger,
) -> Iterator[GoogleAdsCampaignPauseGuardedAdapter]:
    """Yield one configured fixed-route Google Ads PAUSE adapter.

    The composition uses only the action's closed EDIT profile reference and
    exact configuration digest.  It has no public bearer, URL,
    or clock input and does not mutate until a safety-kernel permit reaches
    ``apply_guarded``.
    """

    if not isinstance(directory, Path) or type(ledger) is not Ledger:
        raise _adapter_invalid()
    checked_action = revalidate_google_ads_campaign_pause_planned_action(action)
    if (
        ledger.root != directory
        or ledger.profile_ref != checked_action.plan.profile_ref
        or ledger.profile_ref != checked_action.workspace_binding.workspace_profile_ref
    ):
        raise _adapter_invalid()
    # Refuse before any credential, header, or provider request when this build
    # cannot retain the observation that proves what the mutation did.  This is
    # a fail-closed build check, never a path that pauses without a readback.
    if not google_ads_pause_readback_record_kind_installed():
        raise AdhelmError(
            "GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND_NOT_INSTALLED",
            "This build's ledger does not register the Google Ads PAUSE readback record.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a build whose ledger registers "
                f"{GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND}; no credential was read and no "
                "provider request was made."
            ),
        )
    GoogleAdsCampaignPauseGuardedAdapter._check_plan_expiry(checked_action)
    execution = checked_action.execution_binding
    with planning.configured_google_ads_pause_planning_session(
        directory,
        execution.profile_ref,
        expected_configuration_digest=execution.configuration_digest,
    ) as session:
        _assert_factory_facts(checked_action, session)
        campaign = checked_action.observation_binding.campaign
        post = _FixedGoogleAdsCampaignPausePost(
            session=session,
            customer_id=campaign.account_client_ref,
            campaign_id=campaign.campaign_client_ref,
            plan_digest=checked_action.plan.plan_digest,
            subplan_digest=checked_action.plan.subplans[0].subplan_digest,
        )
        yield GoogleAdsCampaignPauseGuardedAdapter(
            action=checked_action,
            session=session,
            post=post,
            ledger=ledger,
            issuer=_ADAPTER_ISSUER,
        )


def _assert_factory_facts(
    action: GoogleAdsCampaignPausePlannedAction,
    session: planning.GoogleAdsCampaignPausePlanningSession,
) -> None:
    """Require the exact protected-slot association the plan was compiled under.

    The session's own resolution may advance ``resolved_at``; every other
    declared fact -- profile, installed client, derived principal, scope,
    manager route, credential-binding HMAC, both configuration digests, and the
    finite bearer expiry -- must equal the approved action, and the plan must
    expire inside that credential window.
    """

    current = session.execution_binding
    if not planning._same_current_binding(current, action.execution_binding) or (
        action.plan.expires_at > current.expires_at
    ):
        raise AdhelmError(
            "GOOGLE_ADS_PAUSE_EXECUTION_BINDING_DRIFT",
            "Current Google Ads EDIT association differs from the approved pause action.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Inspect local Google Ads credentials and compile a new exact plan.",
        )


def _adapter_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_CAMPAIGN_PAUSE_ADAPTER_INVALID",
        "Google Ads campaign pause adapter state is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Start a new fixed Google Ads campaign pause operation from local state.",
    )


def _post_egress_ambiguous() -> AdhelmError:
    return AdhelmError(
        "AMBIGUOUS_REMOTE_STATE",
        "Google Ads campaign pause outcome requires reconciliation before any new plan.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Reconcile remote state and compile a new exact plan before any retry.",
    )


__all__ = ["GoogleAdsCampaignPauseGuardedAdapter"]
