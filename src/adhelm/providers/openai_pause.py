"""Fixed, development-only OpenAI Ads campaign-pause safety adapter."""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from adhelm.canonical import digest, parse_json
from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.privacy import validate_data
from adhelm.providers.openai_ads import (
    OpenAIAdsCampaign,
    OpenAIAdsClient,
    OpenAIAdsIdentity,
    OpenAIAdsInventoryQuery,
)
from adhelm.providers.openai_pause_contract import (
    _WORKSPACE_PROFILE_REF_UNSET,
    OPENAI_PAUSE_API_VERSION,
    OPENAI_PAUSE_CAPABILITY_VERSION,
    OPENAI_PAUSE_HOST,
    OpenAIPauseBinding,
    OpenAIPauseCampaignBaseline,
    OpenAIPausePlannedAction,
    OpenAIPauseWorkspaceBinding,
    _baseline,
    _state,
    _workspace_binding_from_input,
    build_openai_pause_plan,
)
from adhelm.providers.openai_pause_contract import (
    _build_plan as _legacy_build_plan,
)
from adhelm.safety import (
    AdapterCapabilities,
    ApplyOutcome,
    ApplyResponse,
    Authorization,
    EffectStatus,
    ExecutionResult,
    OperationKind,
    Reconciliation,
    RemoteOpenAIAccountKeyIdentity,
    RemoteState,
    SafetyKernel,
    SubPlan,
    Verification,
)
from adhelm.transport import SafeHttpClient

__all__ = [
    "OpenAIPauseAdapter",
    "OpenAIPauseBinding",
    "OpenAIPauseCampaignBaseline",
    "OpenAIPauseCredentials",
    "OpenAIPausePlannedAction",
    "OpenAIPauseWorkspaceBinding",
    "build_openai_pause_plan",
    "execute_openai_campaign_pause",
    "plan_openai_campaign_pause",
]

_EVIDENCE = "evidence:sha256:"
_API_VERSION = OPENAI_PAUSE_API_VERSION
_CAPABILITY_VERSION = OPENAI_PAUSE_CAPABILITY_VERSION
_HOST = OPENAI_PAUSE_HOST
# Preserve the extraction-era direct import path used by existing callers.
_build_plan = _legacy_build_plan
_BINDING_DOMAIN = b"adhelm/openai-ads/credential-binding/v1\x00"
_NATIVE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]{1,120}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_MAXIMUM_INVENTORY_RECORDS = 5_000


def _now() -> datetime:
    return datetime.now(UTC)


def _input_campaign_client_ref(value: object) -> str:
    """Validate the fixed native path component before any provider read."""
    if type(value) is not str or _NATIVE_IDENTIFIER.fullmatch(value) is None:
        raise AdhelmError("INVALID_INPUT", "OpenAI campaign ID is invalid.", exit_code=2)
    return value


def _input_inventory_bound(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAXIMUM_INVENTORY_RECORDS:
        raise AdhelmError(
            "INVALID_INPUT", "OpenAI campaign inventory bound is invalid.", exit_code=2
        )
    return value


def _input_expiry(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AdhelmError("INVALID_INPUT", "OpenAI pause expiry is invalid.", exit_code=2)
    return value.astimezone(UTC)


def _input_expected_account_metadata(
    expected_account_currency: object | None,
    expected_account_timezone: object | None,
) -> tuple[str, str] | None:
    """Validate optional configured metadata before the first account read."""
    if expected_account_currency is None and expected_account_timezone is None:
        return None
    if (
        type(expected_account_currency) is not str
        or _CURRENCY.fullmatch(expected_account_currency) is None
        or type(expected_account_timezone) is not str
        or not expected_account_timezone
        or len(expected_account_timezone) > 80
    ):
        raise AdhelmError(
            "INVALID_INPUT",
            "OpenAI pause expected account currency and timezone are invalid.",
            exit_code=2,
        )
    try:
        ZoneInfo(expected_account_timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise AdhelmError(
            "INVALID_INPUT",
            "OpenAI pause expected account currency and timezone are invalid.",
            exit_code=2,
        ) from None
    return expected_account_currency, expected_account_timezone


def _clock_now(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception as exc:
        raise AdhelmError("CLOCK_INVALID", "OpenAI pause clock is unavailable.") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AdhelmError("CLOCK_INVALID", "OpenAI pause clock is unavailable.")
    return value.astimezone(UTC)


@dataclass(frozen=True, repr=False, slots=True)
class OpenAIPauseCredentials:
    """One in-memory credential snapshot; repr deliberately excludes secrets."""

    profile_ref: str
    secret: str
    binding_key: bytes


def _credential_binding(credentials: OpenAIPauseCredentials) -> str:
    """Derive, never accept, the public credential binding identifier."""
    if (
        not isinstance(credentials.profile_ref, str)
        or not credentials.profile_ref
        or len(credentials.profile_ref) > 160
        or not isinstance(credentials.secret, str)
        or not credentials.secret
        or len(credentials.secret) > 16_384
        or any(not 33 <= ord(char) <= 126 for char in credentials.secret)
        or not isinstance(credentials.binding_key, bytes)
        or len(credentials.binding_key) < 32
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "OpenAI Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local OpenAI Ads edit profile.",
        )
    message = (
        _BINDING_DOMAIN
        + credentials.profile_ref.encode("utf-8")
        + b"\x00"
        + credentials.secret.encode("utf-8")
    )
    return (
        "credential-binding:sha256:"
        + hmac.new(credentials.binding_key, message, sha256).hexdigest()
    )


def plan_openai_campaign_pause(
    *,
    client: OpenAIAdsClient,
    credentials: OpenAIPauseCredentials,
    expected_account_client_ref: str,
    campaign_client_ref: str,
    expires_at: datetime,
    workspace_profile_ref: object = _WORKSPACE_PROFILE_REF_UNSET,
    expected_account_currency: str | None = None,
    expected_account_timezone: str | None = None,
    maximum_inventory_records: int = 500,
    clock: Callable[[], datetime] = _now,
) -> OpenAIPausePlannedAction:
    """Build a single exact pause action from complete bounded current reads."""
    campaign_id = _input_campaign_client_ref(campaign_client_ref)
    inventory_bound = _input_inventory_bound(maximum_inventory_records)
    plan_expiry = _input_expiry(expires_at)
    expected_metadata = _input_expected_account_metadata(
        expected_account_currency, expected_account_timezone
    )
    _workspace_binding_from_input(workspace_profile_ref)
    if plan_expiry <= _clock_now(clock):
        raise AdhelmError("PLAN_EXPIRED", "OpenAI pause plan has expired.", exit_code=4)
    binding_ref = _credential_binding(credentials)
    identity = client.discover_identity(expected_account_client_ref=expected_account_client_ref)
    if expected_metadata is not None and expected_metadata != (
        identity.currency,
        identity.timezone,
    ):
        raise AdhelmError(
            "IDENTITY_MISMATCH",
            "OpenAI account settings differ from the configured pause profile.",
        )
    if identity.status.casefold() != "active":
        raise AdhelmError(
            "PAUSE_NOT_ELIGIBLE", "Only an active OpenAI account may pause campaigns."
        )
    campaigns = client.list_campaigns(
        OpenAIAdsInventoryQuery(account_ref=identity.account_ref, maximum_records=inventory_bound),
        identity=identity,
    )
    if not any(item.campaign_client_ref == campaign_id for item in campaigns):
        raise AdhelmError(
            "PARENT_NOT_DISCOVERED", "Requested OpenAI campaign is absent from complete inventory."
        )
    campaign = client.get_campaign(identity, campaign_id)
    binding = OpenAIPauseBinding(
        account_ref=identity.account_ref,
        account_client_ref=identity.account_client_ref,
        profile_ref=credentials.profile_ref,
        credential_binding_ref=binding_ref,
        account_timezone=identity.timezone,
        account_currency=identity.currency,
        account_status="active",
        campaign=_baseline(campaign),
    )
    return build_openai_pause_plan(
        binding,
        expires_at=plan_expiry,
        workspace_profile_ref=workspace_profile_ref,
    )


def execute_openai_campaign_pause(
    *,
    kernel: SafetyKernel,
    action: OpenAIPausePlannedAction,
    authorization: Authorization,
    adapter: OpenAIPauseAdapter,
) -> ExecutionResult:
    """Public kernel-only wrapper; this module exposes no direct egress API."""
    if not isinstance(kernel, SafetyKernel):
        raise AdhelmError("SAFETY_KERNEL_REQUIRED", "OpenAI pause execution requires SafetyKernel.")
    checked = OpenAIPausePlannedAction.model_validate(action.model_dump(mode="python"))
    if adapter.action != checked:
        raise AdhelmError(
            "PLAN_BINDING_MISMATCH", "OpenAI adapter differs from the planned action."
        )
    return kernel.execute(checked.plan, authorization, {adapter.provider: adapter})


class OpenAIPauseAdapter:
    """Private fixed-route adapter consumed only by :class:`SafetyKernel`."""

    provider = "openai_ads"

    def __init__(
        self,
        action: OpenAIPausePlannedAction,
        transport: SafeHttpClient,
        credential_supplier: Callable[[], OpenAIPauseCredentials],
        ledger: Ledger,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self._action = OpenAIPausePlannedAction.model_validate(action.model_dump(mode="python"))
        self._transport, self._credential_supplier, self._ledger = (
            transport,
            credential_supplier,
            ledger,
        )
        self._clock = clock
        self._proof: tuple[str, str] | None = None

    @property
    def action(self) -> OpenAIPausePlannedAction:
        return self._action

    def _credentials(self) -> OpenAIPauseCredentials:
        value = self._credential_supplier()
        if not isinstance(value, OpenAIPauseCredentials):
            raise AdhelmError(
                "PROVIDER_ACCESS_REQUIRED", "OpenAI Ads authorization is unavailable.", exit_code=4
            )
        actual = _credential_binding(value)
        if value.profile_ref != self._action.binding.profile_ref or not hmac.compare_digest(
            actual, self._action.binding.credential_binding_ref
        ):
            raise AdhelmError("CREDENTIAL_BINDING_DRIFT", "OpenAI credential binding changed.")
        return value

    @staticmethod
    def _same_credentials(first: OpenAIPauseCredentials, second: OpenAIPauseCredentials) -> bool:
        return (
            first.profile_ref == second.profile_ref
            and hmac.compare_digest(first.secret, second.secret)
            and hmac.compare_digest(first.binding_key, second.binding_key)
        )

    def _current(
        self, credentials: OpenAIPauseCredentials
    ) -> tuple[OpenAIAdsClient, OpenAIAdsIdentity, OpenAIAdsCampaign]:
        client = OpenAIAdsClient(self._transport, lambda: credentials.secret)
        binding = self._action.binding
        identity = client.discover_identity(expected_account_client_ref=binding.account_client_ref)
        if (
            identity.account_ref != binding.account_ref
            or identity.timezone != binding.account_timezone
            or identity.currency != binding.account_currency
            or identity.status.casefold() != binding.account_status
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH", "OpenAI account metadata differs from the approved baseline."
            )
        client.list_campaigns(
            OpenAIAdsInventoryQuery(account_ref=identity.account_ref), identity=identity
        )
        return client, identity, client.get_campaign(identity, binding.campaign.campaign_client_ref)

    def _matches_baseline(self, identity: OpenAIAdsIdentity, campaign: OpenAIAdsCampaign) -> bool:
        binding = self._action.binding
        if campaign.status.casefold() != "active":
            return False
        return (
            identity.account_ref == binding.account_ref
            and identity.account_client_ref == binding.account_client_ref
            and identity.timezone == binding.account_timezone
            and identity.currency == binding.account_currency
            and identity.status.casefold() == binding.account_status
            and _baseline(campaign) == binding.campaign
        )

    def _remote_state(
        self, identity: OpenAIAdsIdentity, campaign: OpenAIAdsCampaign
    ) -> RemoteState:
        binding = self._action.binding
        if self._matches_baseline(identity, campaign):
            return RemoteState(fingerprint=_state(binding, paused=False).fingerprint)
        # A confirmed post-pause state must retain every baseline value except
        # status. Anything else is drift, never an inferred successful pause.
        same_except_status = campaign.model_copy(update={"status": "active"})
        if (
            _baseline(same_except_status) != binding.campaign
            or campaign.status.casefold() != "paused"
        ):
            raise AdhelmError("REMOTE_DRIFT", "OpenAI campaign differs from the approved baseline.")
        return RemoteState(fingerprint=_state(binding, paused=True).fingerprint)

    def _parse_pause_response(
        self, client: OpenAIAdsClient, identity: OpenAIAdsIdentity, raw: bytes
    ) -> OpenAIAdsCampaign:
        try:
            payload = parse_json(raw, maximum_bytes=8_388_608, decimal_numbers=True)
            validate_data(payload)
            campaign = client._parse_campaign(  # pyright: ignore[reportPrivateUsage]
                payload,
                account_ref=identity.account_ref,
                expected_account_client_ref=identity.account_client_ref,
            )
            if campaign.campaign_client_ref != self._action.binding.campaign.campaign_client_ref:
                raise ValueError("response campaign mismatch")
            if (
                self._remote_state(identity, campaign).fingerprint
                != self._action.plan.subplans[0].after.fingerprint
            ):
                raise ValueError("response did not confirm pause")
            return campaign
        except Exception as exc:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI pause response is not a trusted paused campaign.",
            ) from exc

    def _kernel_started(self, plan: SubPlan) -> bool:
        return any(
            record.get("payload", {}).get("event_type") == "APPLY_STARTED"
            and record.get("payload", {}).get("plan_digest") == self._action.plan.plan_digest
            and record.get("payload", {}).get("subplan_digest") == plan.subplan_digest
            for record in self._ledger.records("changes")
        )

    def _check_plan(self, plan: SubPlan) -> None:
        action = OpenAIPausePlannedAction.model_validate(self._action.model_dump(mode="python"))
        if plan != action.plan.subplans[0]:
            raise AdhelmError(
                "PLAN_BINDING_MISMATCH", "Adapter refuses a different OpenAI pause plan."
            )

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        self._check_plan(plan)
        return AdapterCapabilities(
            provider_api_version=_API_VERSION,
            capability_version=_CAPABILITY_VERSION,
            supported_operations=(OperationKind.PAUSE,),
            writes_enabled=True,
        )

    def read_identity(self, account_ref: str) -> RemoteOpenAIAccountKeyIdentity:
        credentials = self._credentials()
        _, identity, _ = self._current(credentials)
        if account_ref != self._action.binding.account_ref:
            raise AdhelmError("IDENTITY_MISMATCH", "OpenAI account differs from planned binding.")
        return RemoteOpenAIAccountKeyIdentity(
            stable_account_id=identity.account_client_ref,
            auth_profile_ref=credentials.profile_ref,
            credential_binding_ref=_credential_binding(credentials),
        )

    def read_state(self, plan: SubPlan) -> RemoteState:
        self._check_plan(plan)
        _, identity, campaign = self._current(self._credentials())
        return self._remote_state(identity, campaign)

    def apply(self, plan: SubPlan, *, idempotency_key: str) -> ApplyResponse:
        del plan, idempotency_key
        raise AdhelmError("PERMIT_REQUIRED", "OpenAI pause egress requires a kernel-issued permit.")

    def apply_guarded(
        self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit
    ) -> ApplyResponse:
        self._check_plan(plan)
        if not self._kernel_started(plan):
            raise AdhelmError(
                "KERNEL_START_REQUIRED", "OpenAI pause egress requires the kernel's durable start."
            )
        captured = self._credentials()
        client, identity, campaign = self._current(captured)
        if not self._matches_baseline(identity, campaign):
            raise AdhelmError(
                "REMOTE_DRIFT", "OpenAI campaign changed immediately before pause egress."
            )
        refreshed = self._credentials()
        if not self._same_credentials(captured, refreshed):
            raise AdhelmError(
                "CREDENTIAL_BINDING_DRIFT", "OpenAI credential changed before egress."
            )
        if self._action.plan.expires_at <= _clock_now(self._clock):
            raise AdhelmError(
                "PLAN_EXPIRED",
                "OpenAI pause plan expired before provider egress.",
                exit_code=4,
            )
        permit.consume(
            plan_digest=self._action.plan.plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref=idempotency_key,
        )
        response = self._transport.request(
            "POST",
            f"https://{_HOST}/{_API_VERSION}/campaigns/{self._action.binding.campaign.campaign_client_ref}/pause",
            headers={"authorization": f"Bearer {captured.secret}"},
            mutation=True,
        )
        returned = self._parse_pause_response(client, identity, response.content)
        _, after_identity, after_campaign = self._current(captured)
        after = self._remote_state(after_identity, after_campaign)
        if after.fingerprint != plan.after.fingerprint:
            raise AdhelmError(
                "REMOTE_DRIFT", "OpenAI pause read-back differs from the approved state."
            )
        response_ref = _EVIDENCE + digest(
            {
                "kind": "openai_pause_response",
                "state": self._remote_state(identity, returned).fingerprint,
            }
        )
        readback_ref = _EVIDENCE + digest(
            {"kind": "openai_pause_independent_read", "state": after.fingerprint}
        )
        self._proof = (response_ref, readback_ref)
        return ApplyResponse(outcome=ApplyOutcome.CONFIRMED, evidence_refs=self._proof)

    def verify(self, plan: SubPlan) -> Verification:
        self._check_plan(plan)
        return Verification(verified=self._proof is not None, evidence_refs=self._proof or ())

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        self._check_plan(plan)
        # No documented provider settlement or idempotency proof exists. An
        # unchanged snapshot cannot prove a lost pause had no effect.
        try:
            current = self.read_state(plan)
        except Exception:
            current = RemoteState(fingerprint="unavailable")
        return Reconciliation(
            state=current,
            status=EffectStatus.AMBIGUOUS_REMOTE_STATE,
            settled=False,
        )
