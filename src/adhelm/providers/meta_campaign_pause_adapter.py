"""Private guarded adapter for the fixed Meta campaign ``ACTIVE`` -> ``PAUSED`` route.

This is SDK-derived offline engineering over the reviewed v26 shape.  It is
not a claim that Graph has a live acknowledgement, conditional-write, or
idempotency contract.  The only accepted local acknowledgement subset is an
exact HTTP 200, duplicate-key-safe sole-member JSON object ``{\"success\":
true}``, followed by a fresh configured-status readback.  Every other outcome
after a kernel permit is consumed remains ambiguous and must not be retried.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Never

import httpx

from adhelm import meta_ads_token_edit_session as edit_session
from adhelm.canonical import digest, parse_json
from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.meta_pause_readback_records import MetaCampaignPauseReadbackObservation
from adhelm.privacy import validate_data
from adhelm.providers.meta_ads import MetaAdsIdentity
from adhelm.providers.meta_campaign_state import MetaCampaignState
from adhelm.providers.meta_pause_contract import (
    MetaCampaignPausePlannedAction,
    revalidate_meta_campaign_pause_planned_action,
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


def _now() -> datetime:
    """Private UTC clock seam used only by synthetic adapter tests."""

    return datetime.now(UTC)


class MetaCampaignPauseGuardedAdapter:
    """Factory-issued fixed-route adapter consumed only by ``SafetyKernel``.

    It exposes the ordinary guarded-adapter protocol but cannot be configured
    with a token, URL, HTTP client, raw acknowledgement, or caller clock.
    """

    provider: str = "meta_ads"

    __slots__ = ("_action", "_issuer", "_ledger", "_post", "_proof", "_proof_issuer", "_session")

    def __init__(
        self,
        *,
        action: MetaCampaignPausePlannedAction,
        session: edit_session.MetaAdsTokenEditSession,
        post: edit_session._FixedMetaCampaignPausePost,
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
        return "<MetaCampaignPauseGuardedAdapter private fixed route>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta campaign pause adapters cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta campaign pause adapters cannot be serialized")

    @property
    def action(self) -> MetaCampaignPausePlannedAction:
        """Return the reconstructed closed action for private composition only."""

        return self._checked_members()[0]

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        action = self._check_plan(plan)
        return AdapterCapabilities(
            provider_api_version=action.plan.subplans[0].provider_api_version,
            capability_version=action.plan.subplans[0].capability_version,
            supported_operations=(OperationKind.PAUSE,),
            # This is a factory-scoped guarded route exercised with fake HTTP.
            # It does not add a CLI, installed-default, or live-conformance claim.
            writes_enabled=True,
        )

    def read_identity(self, account_ref: str) -> RemoteIdentity:
        action, _session, _post, _ledger = self._checked_members()
        account = action.plan.subplans[0].account
        if type(account) is not AccountIdentity or account_ref != account.account_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "Meta account differs from the approved pause plan."
            )
        identity = self._current_identity(action)
        return RemoteIdentity(
            stable_account_id=identity.account_client_ref,
            auth_profile_ref=action.execution_binding.profile_ref,
            principal_ref=action.execution_binding.principal_ref,
            client_application_ref=action.execution_binding.client_application_ref,
            scopes=("ads_management",),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        action = self._check_plan(plan)
        state = self._current_campaign_state(action)
        return self._state_for_observation(action, state)

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError(
            "PERMIT_REQUIRED", "Meta campaign pause egress requires a kernel-issued permit."
        )

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        action = self._check_plan(plan)
        _action, _session, post, ledger = self._checked_members()
        if self._verified_proof() is not None:
            raise AdhelmError(
                "META_CAMPAIGN_PAUSE_ADAPTER_USED",
                "The fixed Meta campaign pause adapter has already handled this action.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Reconcile retained history and compile a new exact plan before any retry."
                ),
            )
        if not self._kernel_started(action, plan, ledger):
            raise AdhelmError(
                "KERNEL_START_REQUIRED",
                "Meta campaign pause egress requires the kernel's durable start.",
            )

        # The final remote baseline is an account GET followed by the one
        # campaign GET.  Both bind current EDIT continuity before egress.
        before = self._current_campaign_state(action)
        if self._state_for_observation(action, before).fingerprint != plan.before.fingerprint:
            raise AdhelmError(
                "REMOTE_DRIFT",
                "Meta campaign differs from the approved ACTIVE baseline before pause egress.",
            )
        self._check_plan_expiry(action)
        # This local-only check intentionally runs before permit consumption.
        # It holds neither configuration nor credential locks across egress.
        post._preflight()
        self._check_plan_expiry(action)
        # The factory-issued capability consumes the actual kernel permit at
        # its sole fixed POST handoff. A failure before that consumption is a
        # local permit/precondition failure and must remain distinguishable
        # from a potentially-dispatched request.
        try:
            response = post._post(permit=permit, attempt_ref=idempotency_key)
        except edit_session._FixedMetaCampaignPausePostDispatchUnverified:
            raise _post_egress_ambiguous() from None
        try:
            self._require_acknowledgement(response)
            # Capture the actual post-ack observation interval around the
            # fresh account identity and campaign readback.  This is factual
            # local history, not a provider acknowledgement contract.
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
                observation_interval_start=readback_started_at,
                observation_interval_end=readback_finished_at,
            )
            proof = self._proof_for(action, readback_record_digest)
            self._proof = proof
            self._proof_issuer = _PROOF_ISSUER
            return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=proof)
        except Exception:
            # The permit was consumed.  No transport, acknowledgement, parser,
            # account, campaign, or readback failure can prove no remote effect.
            raise _post_egress_ambiguous() from None

    def verify(self, plan: SubPlan) -> Verification:
        self._check_plan(plan)
        proof = self._verified_proof()
        return Verification(verified=proof is not None, evidence_refs=proof or ())

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        self._check_plan(plan)
        try:
            current = self.read_state(plan)
        except Exception:
            current = RemoteState(fingerprint="unavailable")
        # A later matching PAUSED read cannot attribute a missing/uncertain
        # acknowledgement to this request.  There is no reviewed settlement or
        # idempotency proof for this offline SDK-derived route.
        return Reconciliation(
            state=current,
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )

    def _checked_members(
        self,
    ) -> tuple[
        MetaCampaignPausePlannedAction,
        edit_session.MetaAdsTokenEditSession,
        edit_session._FixedMetaCampaignPausePost,
        Ledger,
    ]:
        if type(self) is not MetaCampaignPauseGuardedAdapter:
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
            or type(session) is not edit_session.MetaAdsTokenEditSession
            or type(ledger) is not Ledger
        ):
            raise _adapter_invalid()
        checked_action = revalidate_meta_campaign_pause_planned_action(action)
        checked_post = edit_session._checked_fixed_meta_campaign_pause_post(post)
        (
            post_core,
            post_campaign,
            post_plan_digest,
            post_subplan_digest,
        ) = checked_post._checked_members()
        try:
            session_core = session._checked_core()
        except AdhelmError:
            raise _adapter_invalid() from None
        if (
            post_core is not session_core
            or post_campaign != checked_action.observation_binding.campaign.campaign_client_ref
            or post_plan_digest != checked_action.plan.plan_digest
            or post_subplan_digest != checked_action.plan.subplans[0].subplan_digest
        ):
            raise _adapter_invalid()
        return checked_action, session, checked_post, ledger

    def _check_plan(self, plan: object) -> MetaCampaignPausePlannedAction:
        action, _session, _post, _ledger = self._checked_members()
        if type(plan) is not SubPlan or plan != action.plan.subplans[0]:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH",
                "Adapter refuses a different Meta campaign pause plan.",
            )
        return action

    def _current_identity(self, action: MetaCampaignPausePlannedAction) -> MetaAdsIdentity:
        _action, session, _post, _ledger = self._checked_members()
        binding = edit_session._checked_binding(session.binding)
        try:
            identity = session._refresh_identity_for_fixed_pause()
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None
        if type(identity) is not MetaAdsIdentity:
            raise _adapter_invalid()
        execution = action.execution_binding
        if (
            binding.profile_ref != execution.profile_ref
            or binding.client_application_ref != execution.client_application_ref
            or binding.principal_ref != execution.principal_ref
            or binding.required_scope != execution.required_scope
            or binding.credential_binding_ref != execution.credential_binding_ref
            or binding.configuration_digest != execution.configuration_digest
            or binding.profile_configuration_digest != execution.profile_configuration_digest
            or action.plan.expires_at > binding.expires_at
            or action.plan.expires_at > binding.data_access_expires_at
        ):
            raise AdhelmError(
                "META_TOKEN_EDIT_BINDING_DRIFT",
                "Current Meta EDIT token facts differ from the approved pause action.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Inspect local Meta credentials and compile a new exact plan.",
            )
        account = action.account_binding
        if (
            identity.account_ref != account.account_ref
            or identity.account_client_ref != account.account_client_ref
            or identity.timezone != account.timezone
            or identity.currency != account.currency
            or identity.status != account.account_status
            or identity.provider_api_version != account.provider_api_version
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Current Meta account facts differ from the approved pause action.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured Meta account and compile a new exact plan.",
            )
        return identity

    def _current_campaign_state(self, action: MetaCampaignPausePlannedAction) -> MetaCampaignState:
        _action, session, _post, _ledger = self._checked_members()
        self._current_identity(action)
        try:
            state = session.read_campaign_state(
                action.observation_binding.campaign.campaign_client_ref
            )
        except AdhelmError:
            raise
        except Exception:
            raise _adapter_invalid() from None
        if type(state) is not MetaCampaignState:
            raise _adapter_invalid()
        return state

    @staticmethod
    def _state_for_observation(
        action: MetaCampaignPausePlannedAction, state: MetaCampaignState
    ) -> RemoteState:
        campaign = action.observation_binding.campaign
        postcondition = action.postcondition
        if (
            state.campaign_client_ref != campaign.campaign_client_ref
            or state.campaign_ref != campaign.campaign_ref
            or state.account_client_ref != campaign.account_client_ref
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Meta campaign state is outside the approved account or campaign binding.",
            )
        if (
            state.configured_status == campaign.configured_status
            and state.effective_status == campaign.effective_status
        ):
            return RemoteState(
                fingerprint=action.plan.subplans[0].before.fingerprint,
                revision=action.plan.subplans[0].before.revision,
            )
        if state.configured_status == postcondition.configured_status:
            # Effective status is retained only as an observed evidence input;
            # PAUSED configuration does not predict effective delivery or spend.
            return RemoteState(
                fingerprint=action.plan.subplans[0].after.fingerprint,
                revision=action.plan.subplans[0].after.revision,
            )
        raise AdhelmError(
            "REMOTE_DRIFT",
            "Meta campaign state differs from the approved pause transition.",
        )

    @staticmethod
    def _kernel_started(
        action: MetaCampaignPausePlannedAction, plan: SubPlan, ledger: Ledger
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
                "CLOCK_INVALID", "Meta campaign pause clock is unavailable."
            ) from None
        if type(current) is not datetime or current.tzinfo is None or current.utcoffset() is None:
            raise AdhelmError("CLOCK_INVALID", "Meta campaign pause clock is invalid.")
        return current.astimezone(UTC)

    @classmethod
    def _check_plan_expiry(cls, action: MetaCampaignPausePlannedAction) -> None:
        current = cls._checked_now()
        if action.plan.expires_at <= current.astimezone(UTC):
            raise AdhelmError(
                "PLAN_EXPIRED",
                "Meta campaign pause plan expired before provider egress.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Compile and approve a new exact Meta campaign pause plan.",
            )

    @staticmethod
    def _require_acknowledgement(response: object) -> None:
        if type(response) is not _HTTPX_RESPONSE:
            raise ValueError("response type")
        try:
            if type(response.status_code) is not int or response.status_code != 200:
                raise ValueError("status")
            payload = parse_json(response.content, maximum_bytes=_MAX_ACK_BYTES)
            if type(payload) is not dict or set(payload) != {"success"}:
                raise ValueError("shape")
            validate_data(payload)
            if type(payload["success"]) is not bool or payload["success"] is not True:
                raise ValueError("success")
        except Exception:
            raise ValueError("acknowledgement") from None

    @staticmethod
    def _proof_for(
        action: MetaCampaignPausePlannedAction,
        readback_record_digest: str,
    ) -> tuple[str, str]:
        acknowledgement = _EVIDENCE_PREFIX + digest(
            {
                "kind": "meta_graph_v26_campaign_pause_acknowledgement_subset",
                "action_id": action.action_id,
                "plan_digest": action.plan.plan_digest,
                "http_status": 200,
                "success": True,
            }
        )
        if (
            type(readback_record_digest) is not str
            or len(readback_record_digest) != 64
            or any(character not in "0123456789abcdef" for character in readback_record_digest)
        ):
            raise ValueError("readback record")
        # The second proof binds the exact dedicated local observation record,
        # which preserves the actual effective status without claiming it
        # proves delivery or remote acknowledgement semantics.
        readback = _EVIDENCE_PREFIX + readback_record_digest
        return acknowledgement, readback

    def _retain_readback_observation(
        self,
        action: MetaCampaignPausePlannedAction,
        observation: MetaCampaignState,
        *,
        attempt_ref: str,
        observation_interval_start: datetime,
        observation_interval_end: datetime,
    ) -> str:
        """Append one dedicated post-ack observation under the session recheck.

        The network reads have already completed before entering
        ``retention_recheck``.  Its callback only rechecks the currently-held
        configuration and credential transaction immediately before the
        durable append; it performs no network or ledger I/O itself.
        """

        _action, session, _post, ledger = self._checked_members()
        subplan = action.plan.subplans[0]
        payload = MetaCampaignPauseReadbackObservation(
            action_id=action.action_id,
            plan_digest=action.plan.plan_digest,
            subplan_digest=subplan.subplan_digest,
            attempt_ref=attempt_ref,
            account_ref=subplan.account.account_ref,
            campaign_ref=action.observation_binding.campaign.campaign_ref,
            observation_interval_start=observation_interval_start,
            observation_interval_end=observation_interval_end,
            effective_status=observation.effective_status,
        ).model_dump(mode="json", warnings="error")
        with session.retention_recheck() as recheck:
            record = ledger.append(
                "changes",
                "META_CAMPAIGN_PAUSE_READBACK_OBSERVATION",
                payload,
                provider="meta_ads",
                account_ref=subplan.account.account_ref,
                run_id=action.plan.plan_id,
                observed_at=payload["observation_interval_end"],
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
def _configured_meta_campaign_pause_adapter(
    directory: Path,
    action: MetaCampaignPausePlannedAction,
    ledger: Ledger,
) -> Iterator[MetaCampaignPauseGuardedAdapter]:
    """Yield one configured fixed-route Meta PAUSE adapter.

    The composition uses only the action's closed EDIT profile and exact
    configuration digest.  It has no public token/client/URL/clock input and
    does not mutate until a safety-kernel permit reaches ``apply_guarded``.
    """

    if not isinstance(directory, Path) or type(ledger) is not Ledger:
        raise _adapter_invalid()
    checked_action = revalidate_meta_campaign_pause_planned_action(action)
    if ledger.root != directory or ledger.profile_ref != checked_action.plan.profile_ref:
        raise _adapter_invalid()
    MetaCampaignPauseGuardedAdapter._check_plan_expiry(checked_action)
    with edit_session.configured_meta_ads_token_edit_session(
        directory,
        checked_action.execution_binding.profile_ref,
        expected_configuration_digest=checked_action.execution_binding.configuration_digest,
    ) as session:
        # The initial session already performs one account GET.  It establishes
        # factory admission; execution performs fresh identity reads again.
        _assert_factory_facts(checked_action, session)
        post = edit_session._issue_fixed_meta_campaign_pause_post(
            session,
            campaign_id=checked_action.observation_binding.campaign.campaign_client_ref,
            plan_digest=checked_action.plan.plan_digest,
            subplan_digest=checked_action.plan.subplans[0].subplan_digest,
        )
        yield MetaCampaignPauseGuardedAdapter(
            action=checked_action,
            session=session,
            post=post,
            ledger=ledger,
            issuer=_ADAPTER_ISSUER,
        )


def _assert_factory_facts(
    action: MetaCampaignPausePlannedAction, session: edit_session.MetaAdsTokenEditSession
) -> None:
    """Validate the initial EDIT session without treating timestamps as immutable."""

    binding = edit_session._checked_binding(session.binding)
    identity = session.identity
    execution = action.execution_binding
    account = action.account_binding
    if (
        binding.profile_ref != execution.profile_ref
        or binding.client_application_ref != execution.client_application_ref
        or binding.principal_ref != execution.principal_ref
        or binding.required_scope != execution.required_scope
        or binding.credential_binding_ref != execution.credential_binding_ref
        or binding.configuration_digest != execution.configuration_digest
        or binding.profile_configuration_digest != execution.profile_configuration_digest
        or action.plan.expires_at > binding.expires_at
        or action.plan.expires_at > binding.data_access_expires_at
        or identity.account_ref != account.account_ref
        or identity.account_client_ref != account.account_client_ref
        or identity.timezone != account.timezone
        or identity.currency != account.currency
        or identity.status != account.account_status
        or identity.provider_api_version != account.provider_api_version
    ):
        raise AdhelmError(
            "META_TOKEN_EDIT_BINDING_DRIFT",
            "Current Meta EDIT facts differ from the approved pause action.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Inspect local Meta credentials and compile a new exact plan.",
        )


def _adapter_invalid() -> AdhelmError:
    return AdhelmError(
        "META_CAMPAIGN_PAUSE_ADAPTER_INVALID",
        "Meta campaign pause adapter state is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action="Start a new fixed Meta campaign pause operation from current local state.",
    )


def _post_egress_ambiguous() -> AdhelmError:
    return AdhelmError(
        "AMBIGUOUS_REMOTE_STATE",
        "Meta campaign pause outcome requires reconciliation before any new plan.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Reconcile remote state and compile a new exact plan before any retry.",
    )


__all__ = ["MetaCampaignPauseGuardedAdapter"]
