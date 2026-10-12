"""Closed, pure OpenAI Ads pause-plan contract.

This module deliberately has no ledger, credential, transport, or provider
egress dependency.  It reconstructs the exact fixture-only pause action from a
complete allowlisted baseline so the action can be retained without creating an
execution path or an import cycle through the ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.models import StrictModel
from adhelm.providers.openai_ads import OpenAIAdsCampaign
from adhelm.safety import (
    CompositePlan,
    EgressTarget,
    FieldChange,
    Money,
    OpenAIAccountKeyIdentity,
    OperationKind,
    StateRef,
    SubPlan,
)

OPENAI_PAUSE_HOST: Final = "api.ads.openai.com"
OPENAI_PAUSE_API_VERSION: Final = "v1"
OPENAI_PAUSE_CAPABILITY_VERSION: Final = "openai-ads-pause-development-contract-2026-09-05"
OPENAI_PAUSE_EGRESS_PURPOSE: Final = "fixed OpenAI campaign pause"
OPENAI_PAUSE_STATUS: Final[Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]] = (
    "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"
)


class _WorkspaceProfileRefUnset:
    """Private sentinel that distinguishes omitted legacy routing from ``None``."""

    __slots__ = ()


_WORKSPACE_PROFILE_REF_UNSET: Final = _WorkspaceProfileRefUnset()


class OpenAIPauseCampaignBaseline(StrictModel):
    """Every allowlisted campaign value needed to reconstruct the transition."""

    campaign_ref: str = Field(min_length=1, max_length=160)
    campaign_client_ref: str = Field(min_length=1, max_length=120)
    status: Literal["active"]
    bidding_type: str = Field(min_length=1, max_length=80)
    lifetime_spend_limit_micros: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    start_at: datetime
    end_at: datetime | None = None

    @model_validator(mode="after")
    def ordered_schedule(self) -> OpenAIPauseCampaignBaseline:
        if self.start_at.tzinfo is None or self.start_at.utcoffset() is None:
            raise ValueError("campaign start must be timezone-aware")
        if self.end_at is not None:
            if self.end_at.tzinfo is None or self.end_at.utcoffset() is None:
                raise ValueError("campaign end must be timezone-aware")
            if self.end_at < self.start_at:
                raise ValueError("campaign end cannot precede start")
        return self


class OpenAIPauseBinding(StrictModel):
    """Closed public baseline. No remote response or credential is retained."""

    account_ref: str = Field(min_length=1, max_length=160)
    account_client_ref: str = Field(min_length=1, max_length=120)
    profile_ref: str = Field(min_length=1, max_length=160)
    credential_binding_ref: str = Field(pattern=r"^credential-binding:sha256:[0-9a-f]{64}$")
    account_timezone: str = Field(min_length=1, max_length=80)
    account_currency: str = Field(pattern=r"^[A-Z]{3}$")
    account_status: Literal["active"]
    campaign: OpenAIPauseCampaignBaseline

    @field_validator("account_timezone")
    @classmethod
    def iana_account_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError("account timezone must be an IANA timezone") from exc
        return value


class OpenAIPauseWorkspaceBinding(StrictModel):
    """Versioned local-ledger routing for a newly compiled pause action.

    This binding identifies the local workspace ledger only. It must never
    replace the credential profile retained in :class:`OpenAIPauseBinding` or
    in the native account identity inside the subplan.
    """

    schema_version: Literal[1] = 1
    binding_kind: Literal["LOCAL_LEDGER_WORKSPACE"] = "LOCAL_LEDGER_WORKSPACE"
    workspace_profile_ref: str = Field(min_length=1, max_length=160)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("workspace binding schema_version must be the integer 1")
        return value

    @field_validator("workspace_profile_ref", mode="before")
    @classmethod
    def safe_workspace_profile_ref(cls, value: object) -> str:
        if (
            type(value) is not str
            or not value
            or len(value) > 160
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("workspace profile reference is not a safe local identifier")
        return value


def _workspace_binding_from_input(value: object) -> OpenAIPauseWorkspaceBinding | None:
    """Make a new explicit workspace binding or retain legacy routing on omission."""
    if value is _WORKSPACE_PROFILE_REF_UNSET:
        return None
    if type(value) is not str:
        raise AdhelmError(
            "INVALID_INPUT", "OpenAI pause workspace profile is invalid.", exit_code=2
        )
    try:
        return OpenAIPauseWorkspaceBinding(workspace_profile_ref=value)
    except ValueError:
        raise AdhelmError(
            "INVALID_INPUT", "OpenAI pause workspace profile is invalid.", exit_code=2
        ) from None


def _input_plan_expiry(value: object) -> datetime:
    """Validate a pure-builder expiry without exposing an untrusted value."""
    try:
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expiry is not an aware datetime")
        return value.astimezone(UTC)
    except Exception:
        raise AdhelmError("INVALID_INPUT", "OpenAI pause expiry is invalid.", exit_code=2) from None


def _baseline(campaign: OpenAIAdsCampaign) -> OpenAIPauseCampaignBaseline:
    if (
        campaign.bidding_type is None
        or campaign.lifetime_spend_limit_micros is None
        or campaign.start_at is None
    ):
        raise AdhelmError(
            "CURRENT_STATE_INCOMPLETE",
            "OpenAI campaign baseline lacks required bid, budget, or schedule fields.",
        )
    if campaign.status.casefold() != "active":
        raise AdhelmError("PAUSE_NOT_ELIGIBLE", "Only an active OpenAI campaign may be paused.")
    try:
        return OpenAIPauseCampaignBaseline(
            campaign_ref=campaign.campaign_ref,
            campaign_client_ref=campaign.campaign_client_ref,
            status="active",
            bidding_type=campaign.bidding_type,
            lifetime_spend_limit_micros=campaign.lifetime_spend_limit_micros,
            start_at=campaign.start_at,
            end_at=campaign.end_at,
        )
    except ValueError as exc:
        raise AdhelmError(
            "CURRENT_STATE_INCOMPLETE", "OpenAI campaign baseline is incomplete."
        ) from exc


def _state(binding: OpenAIPauseBinding, *, paused: bool) -> StateRef:
    campaign = binding.campaign
    return StateRef(
        fingerprint=digest(
            {
                "account_ref": binding.account_ref,
                "account_client_ref": binding.account_client_ref,
                "account_timezone": binding.account_timezone,
                "account_currency": binding.account_currency,
                "account_status": binding.account_status,
                "campaign_ref": campaign.campaign_ref,
                "campaign_client_ref": campaign.campaign_client_ref,
                "status": "paused" if paused else campaign.status,
                "bidding_type": campaign.bidding_type,
                "lifetime_spend_limit_micros": campaign.lifetime_spend_limit_micros,
                "start_at": campaign.start_at.isoformat(),
                "end_at": campaign.end_at.isoformat() if campaign.end_at else None,
            }
        )
    )


def _build_plan(
    binding: OpenAIPauseBinding,
    *,
    expires_at: datetime,
    workspace_profile_ref: str | None = None,
) -> CompositePlan:
    before, after = _state(binding, paused=False), _state(binding, paused=True)
    campaign_id = binding.campaign.campaign_client_ref
    # A structural plan ID must not concatenate a native identifier because it
    # appears in local ledger/resource fields and projection artifacts.
    action_ref = digest({"account": binding.account_ref, "campaign": campaign_id})[:32]
    account = OpenAIAccountKeyIdentity(
        account_ref=binding.account_ref,
        stable_account_id=binding.account_client_ref,
        auth_profile_ref=binding.profile_ref,
        credential_binding_ref=binding.credential_binding_ref,
    )
    subplan = SubPlan(
        subplan_id=f"openai-pause-{action_ref}",
        provider="openai_ads",
        operation=OperationKind.PAUSE,
        account=account,
        provider_api_version=OPENAI_PAUSE_API_VERSION,
        capability_version=OPENAI_PAUSE_CAPABILITY_VERSION,
        before=before,
        after=after,
        field_changes=(
            # These are structural paths. The native ID remains only inside the
            # local action/state binding, never in an exported free-text path.
            FieldChange(path="campaign/identity", before=before, after=before),
            FieldChange(path="campaign/status", before=before, after=after),
        ),
        # A pause grants no incremental spend. It does not promise zero residual
        # delivery or billing because the API supplies no conditional-write proof.
        max_spend=Money(minor_units=0, currency=binding.account_currency),
        egress=(EgressTarget(host=OPENAI_PAUSE_HOST, purpose=OPENAI_PAUSE_EGRESS_PURPOSE),),
        recovery_actions=(OperationKind.PAUSE,),
        expires_at=expires_at,
    )
    return CompositePlan(
        plan_id=f"openai-pause-{action_ref}",
        profile_ref=(
            binding.profile_ref if workspace_profile_ref is None else workspace_profile_ref
        ),
        subplans=(subplan,),
        expires_at=expires_at,
    )


class OpenAIPausePlannedAction(StrictModel):
    schema_version: Literal[1] = 1
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"] = OPENAI_PAUSE_STATUS
    binding: OpenAIPauseBinding
    workspace_binding: OpenAIPauseWorkspaceBinding | None = None
    plan: CompositePlan

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_serializer(mode="wrap")
    def preserve_legacy_wire_shape(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """Omit the new field from historical v1 actions that never supplied it."""
        output = handler(self)
        if not isinstance(output, dict):
            raise ValueError("OpenAI pause action serialization must be an object")
        if "workspace_binding" not in self.model_fields_set:
            output.pop("workspace_binding", None)
        return output

    @model_validator(mode="after")
    def exact_fixed_pause_plan(self) -> OpenAIPausePlannedAction:
        # This rejects rehashed cross-account/resource/version/egress changes:
        # every plan field is deterministically rebuilt from the full baseline.
        if "workspace_binding" in self.model_fields_set and self.workspace_binding is None:
            raise ValueError("workspace_binding must be a closed object when supplied")
        workspace_profile_ref = (
            self.workspace_binding.workspace_profile_ref
            if self.workspace_binding is not None
            else None
        )
        expected = _build_plan(
            self.binding,
            expires_at=self.plan.expires_at,
            workspace_profile_ref=workspace_profile_ref,
        )
        if self.plan != expected:
            raise ValueError("OpenAI pause action must equal its reconstructed fixed plan")
        return self


def build_openai_pause_plan(
    binding: OpenAIPauseBinding,
    *,
    expires_at: datetime,
    workspace_profile_ref: object = _WORKSPACE_PROFILE_REF_UNSET,
) -> OpenAIPausePlannedAction:
    """Build the fixed action from a closed baseline without provider I/O.

    Omit ``workspace_profile_ref`` only for byte-compatible legacy actions.
    New callers must provide one exact local ledger profile string; ``None``
    and every non-string value are rejected rather than becoming legacy data.
    """
    if type(binding) is not OpenAIPauseBinding:
        raise AdhelmError("INVALID_INPUT", "OpenAI pause binding is invalid.", exit_code=2)
    try:
        checked_binding = OpenAIPauseBinding.model_validate_json(
            binding.model_dump_json(warnings="error")
        )
    except Exception:
        raise AdhelmError(
            "INVALID_INPUT", "OpenAI pause binding is invalid.", exit_code=2
        ) from None
    checked_expiry = _input_plan_expiry(expires_at)
    try:
        workspace_binding = _workspace_binding_from_input(workspace_profile_ref)
        plan_workspace_profile_ref = (
            workspace_binding.workspace_profile_ref if workspace_binding is not None else None
        )
        action: dict[str, object] = {
            "binding": checked_binding,
            "plan": _build_plan(
                checked_binding,
                expires_at=checked_expiry,
                workspace_profile_ref=plan_workspace_profile_ref,
            ),
        }
        if workspace_binding is not None:
            action["workspace_binding"] = workspace_binding
        return OpenAIPausePlannedAction.model_validate(action)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError("INVALID_INPUT", "OpenAI pause action is invalid.", exit_code=2) from None
