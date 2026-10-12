"""Fixed Meta Graph v26.0 campaign-state observation.

This narrow reader exists for a later guarded campaign-pause workflow.  It
performs exactly one allowlisted campaign-node GET and returns only the
normalized campaign/account binding and the two documented v26 status facts.
It neither plans nor applies a mutation, and it does not establish provider
revision, compare-and-swap, spend, or execution authority.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import ConfigDict, field_validator, model_validator

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import SafeRef, StrictModel
from adhelm.providers.meta_ads import MetaAdsIdentity
from adhelm.transport import SafeHttpClient

__all__ = ("MetaCampaignState", "MetaCampaignStateReader")

_HOST: Final = "graph.facebook.com"
_API_VERSION: Final = "v26.0"
_FIELDS: Final = "id,account_id,configured_status,effective_status"
_MAX_RESPONSE_BYTES: Final = 65_536
_MAX_TOKEN_BYTES: Final = 16_384
_NATIVE_ID: Final = re.compile(r"^[1-9][0-9]{0,39}$", re.ASCII)
_ACCOUNT_STATUS: Final = re.compile(r"^(?:0|[1-9][0-9]{0,9}|-[1-9][0-9]{0,9})$", re.ASCII)
_ACCOUNT_PREFIX: Final = "act_"
_CAMPAIGN_REFERENCE_PREFIX: Final = "meta_ads.campaign."
_ACCOUNT_REFERENCE_PREFIX: Final = "meta_ads.account.act_"
_HTTPX_CLIENT: Final = httpx.Client
_HTTPX_RESPONSE: Final = httpx.Response

MetaCampaignConfiguredStatus = Literal["ACTIVE", "ARCHIVED", "DELETED", "PAUSED"]
MetaCampaignEffectiveStatus = Literal[
    "ACTIVE", "ARCHIVED", "DELETED", "IN_PROCESS", "PAUSED", "WITH_ISSUES"
]

_CONFIGURED_STATUSES: Final = frozenset({"ACTIVE", "ARCHIVED", "DELETED", "PAUSED"})
_EFFECTIVE_STATUSES: Final = frozenset(
    {"ACTIVE", "ARCHIVED", "DELETED", "IN_PROCESS", "PAUSED", "WITH_ISSUES"}
)
_RESPONSE_KEYS: Final = frozenset({"id", "account_id", "configured_status", "effective_status"})
_META_CURRENCIES: Final = frozenset(
    {
        "AUD",
        "CAD",
        "CHF",
        "DKK",
        "EUR",
        "GBP",
        "HKD",
        "JPY",
        "MXN",
        "NOK",
        "NZD",
        "SEK",
        "SGD",
        "USD",
    }
)

TokenGetter = Callable[[], str]


class MetaCampaignState(StrictModel):
    """Closed, development-only observation of one proved Meta campaign.

    ``account_client_ref`` intentionally contains the provider's native
    decimal account ID without its ``act_`` route prefix.  It is an observed
    association, not a version, provider revision, authority grant, or spend
    guarantee.
    """

    # Repeat the strict/frozen configuration here rather than depending on a
    # superclass detail: this is a standalone provider observation contract.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    campaign_client_ref: SafeRef
    campaign_ref: SafeRef
    account_client_ref: SafeRef
    configured_status: MetaCampaignConfiguredStatus
    effective_status: MetaCampaignEffectiveStatus

    @field_validator("campaign_client_ref", "account_client_ref", mode="before")
    @classmethod
    def native_reference_requires_exact_ascii_decimal(cls, value: object) -> str:
        if type(value) is not str or _NATIVE_ID.fullmatch(value) is None:
            raise ValueError("campaign state native reference is invalid")
        return value

    @field_validator("campaign_ref", mode="before")
    @classmethod
    def campaign_reference_requires_safe_scalar(cls, value: object) -> str:
        if type(value) is not str:
            raise ValueError("campaign state reference is invalid")
        return value

    @field_validator("configured_status", mode="before")
    @classmethod
    def configured_status_requires_exact_tagged_value(cls, value: object) -> str:
        if type(value) is not str or value not in _CONFIGURED_STATUSES:
            raise ValueError("campaign configured status is invalid")
        return value

    @field_validator("effective_status", mode="before")
    @classmethod
    def effective_status_requires_exact_tagged_value(cls, value: object) -> str:
        if type(value) is not str or value not in _EFFECTIVE_STATUSES:
            raise ValueError("campaign effective status is invalid")
        return value

    @model_validator(mode="after")
    def campaign_reference_binds_native_campaign(self) -> MetaCampaignState:
        if self.campaign_ref != _CAMPAIGN_REFERENCE_PREFIX + self.campaign_client_ref:
            raise ValueError("campaign state reference does not bind the native campaign")
        return self


class MetaCampaignStateReader:
    """One-attempt reader for the fixed Meta campaign-node state contract."""

    __slots__ = ("_token_getter", "_transport")

    def __init__(self, transport: SafeHttpClient, token_getter: TokenGetter) -> None:
        _validate_transport(transport)
        if not callable(token_getter):
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID",
                "Meta campaign state requires a guarded token callback.",
            )
        self._transport = transport
        self._token_getter = token_getter

    def read_campaign_state(self, identity: MetaAdsIdentity, campaign_id: str) -> MetaCampaignState:
        """Read exactly one campaign after binding it to a proved identity.

        Caller-controlled identity and campaign values are revalidated before
        the guarded subject-token callback or any network request.  The
        transport is also rechecked because its retry and destination settings
        are mutable Python state.
        """

        transport, token_getter = self._checked_members()
        expected_account = _bound_identity_account(identity)
        campaign = _input_campaign_id(campaign_id)
        token = _token_snapshot(token_getter)
        # A guarded callback can rotate or invalidate mutable transport state.
        # Recheck one-attempt egress immediately before this reader's sole GET.
        _validate_transport(transport)
        try:
            response = transport.request(
                "GET",
                f"https://{_HOST}/{_API_VERSION}/{campaign}",
                headers={"authorization": f"Bearer {token}"},
                params={"fields": _FIELDS},
            )
        except AdhelmError as exc:
            raise _sanitized_transport_error(exc) from None
        except Exception:
            raise AdhelmError(
                "PROVIDER_UNAVAILABLE", "Meta campaign-state read is unavailable."
            ) from None

        payload = _response_payload(response)
        returned_campaign = _response_native_id(payload.get("id"))
        returned_account = _response_account_native_id(payload.get("account_id"))
        configured = _configured_status(payload.get("configured_status"))
        effective = _effective_status(payload.get("effective_status"))
        if returned_campaign != campaign or returned_account != expected_account:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Meta campaign state is outside the proved account or campaign binding.",
            )
        try:
            return MetaCampaignState(
                campaign_client_ref=campaign,
                campaign_ref=_CAMPAIGN_REFERENCE_PREFIX + campaign,
                account_client_ref=expected_account,
                configured_status=cast(MetaCampaignConfiguredStatus, configured),
                effective_status=cast(MetaCampaignEffectiveStatus, effective),
            )
        except Exception:
            # The values above are closed scalars.  Keep an unexpected local
            # model failure out of a structured provider result nonetheless.
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "Meta campaign state is invalid."
            ) from None

    def _checked_members(self) -> tuple[SafeHttpClient, TokenGetter]:
        """Read the callback and transport without exposing constructed-object errors."""

        if type(self) is not MetaCampaignStateReader:
            raise AdhelmError("TRANSPORT_CONFIG_INVALID", "Meta campaign state reader is invalid.")
        try:
            transport = self._transport
            token_getter = self._token_getter
        except Exception:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID", "Meta campaign state reader is invalid."
            ) from None
        _validate_transport(transport)
        if not callable(token_getter):
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID",
                "Meta campaign state requires a guarded token callback.",
            )
        return transport, token_getter


def _validate_transport(transport: object) -> None:
    """Constrain this reader to one bounded fixed-host GET attempt."""

    if type(transport) is not SafeHttpClient:
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID", "Meta campaign state requires SafeHttpClient."
        )
    try:
        allowed_hosts = transport.allowed_hosts
        maximum_response_bytes = transport.maximum_response_bytes
        read_attempts = transport.read_attempts
        client = transport.client
    except Exception:
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID", "Meta campaign state transport is invalid."
        ) from None
    if type(allowed_hosts) is not frozenset or allowed_hosts != frozenset({_HOST}):
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID",
            "Meta campaign state requires its fixed Graph API host.",
        )
    if type(maximum_response_bytes) is not int or not (
        1 <= maximum_response_bytes <= _MAX_RESPONSE_BYTES
    ):
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID",
            "Meta campaign state requires a bounded response transport.",
        )
    if type(read_attempts) is not int or read_attempts != 1:
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID",
            "Meta campaign state requires one transport attempt.",
        )
    try:
        hooks = client.event_hooks
        ambient_state = (
            type(client) is not _HTTPX_CLIENT
            or bool(client.params)
            or client.trust_env is not False
            or client.auth is not None
            or bool(client.cookies)
            or type(hooks) is not dict
            or set(hooks) != {"request", "response"}
            or any(type(callbacks) is not list or callbacks for callbacks in hooks.values())
        )
    except Exception:
        ambient_state = True
    if ambient_state:
        raise AdhelmError(
            "TRANSPORT_CONFIG_INVALID",
            "Meta campaign state transport has undeclared request state.",
        )


def _input_campaign_id(value: object) -> str:
    if type(value) is not str or _NATIVE_ID.fullmatch(value) is None:
        raise AdhelmError(
            "INVALID_INPUT",
            "Campaign ID must be a nonzero ASCII decimal identifier.",
            exit_code=2,
        )
    return value


def _response_native_id(value: object) -> str:
    if type(value) is not str or _NATIVE_ID.fullmatch(value) is None:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta campaign state is invalid.")
    return value


def _response_account_native_id(value: object) -> str:
    # The tagged Campaign field establishes a native account ID.  The existing
    # account reader's local ``act_`` route normalization is not wire evidence
    # for this campaign-node field, so this boundary rejects prefixed values.
    return _response_native_id(value)


def _bound_identity_account(value: object) -> str:
    """Revalidate a public identity before it can influence request routing."""

    if type(value) is not MetaAdsIdentity:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Meta Ads identity is invalid.")
    try:
        identity = MetaAdsIdentity.model_validate(value.model_dump(mode="python", warnings="error"))
    except Exception:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Meta Ads identity is invalid.") from None
    client_ref = identity.account_client_ref
    if type(client_ref) is not str or not client_ref.startswith(_ACCOUNT_PREFIX):
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Meta Ads identity is invalid.")
    native = client_ref[len(_ACCOUNT_PREFIX) :]
    if _NATIVE_ID.fullmatch(native) is None:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Meta Ads identity is invalid.")
    if identity.account_ref != _ACCOUNT_REFERENCE_PREFIX + native:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Meta Ads identity is invalid.")
    if type(identity.currency) is not str or identity.currency not in _META_CURRENCIES:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Meta Ads identity is invalid.")
    if type(identity.timezone) is not str:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Meta Ads identity is invalid.")
    try:
        ZoneInfo(identity.timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Meta Ads identity is invalid.") from None
    if type(identity.status) is not str or _ACCOUNT_STATUS.fullmatch(identity.status) is None:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Meta Ads identity is invalid.")
    return native


def _token_snapshot(getter: object) -> str:
    if not callable(getter):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "Meta Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local Meta Ads profile.",
        )
    try:
        token = getter()
    except AdhelmError as exc:
        # The guarded session may surface one of these four local continuity
        # outcomes.  Rebuild it from an exact code allowlist rather than
        # relaying arbitrary callback text, cause, exit code, or next action.
        raise _guarded_token_error(exc.code) from None
    except Exception:
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "Meta Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local Meta Ads profile.",
        ) from None
    if (
        type(token) is not str
        or not token
        or len(token) > _MAX_TOKEN_BYTES
        or any(not character.isascii() or not 33 <= ord(character) <= 126 for character in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "Meta Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local Meta Ads profile.",
        )
    return token


def _guarded_token_error(code: object) -> AdhelmError:
    """Return only the fixed local failures meaningful at this read boundary."""

    if type(code) is str:
        if code == "CONFIGURATION_CHANGED":
            return AdhelmError(
                "CONFIGURATION_CHANGED",
                "Configured Meta read state changed.",
                next_action="Restart the fixed local read from current configuration.",
            )
        if code == "META_TOKEN_CREDENTIAL_CHANGED":
            return AdhelmError(
                "META_TOKEN_CREDENTIAL_CHANGED",
                "The Meta token local state changed during the fixed read.",
                exit_code=ExitCode.AMBIGUOUS,
                next_action="Inspect local Meta credential status before a deliberate follow-up.",
            )
        if code == "META_TOKEN_EXPIRED":
            return AdhelmError(
                "META_TOKEN_EXPIRED",
                "The inspected Meta token is no longer usable for this fixed read.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Inspect the configured Meta token again before a new fixed operation.",
            )
        if code == "CLOCK_INVALID":
            return AdhelmError(
                "CLOCK_INVALID",
                "Meta token session clock is invalid.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Inspect the configured Meta token again before a new fixed operation.",
            )
    return AdhelmError(
        "PROVIDER_ACCESS_REQUIRED",
        "Meta Ads authorization is unavailable.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Authorize the configured local Meta Ads profile.",
    )


def _sanitized_transport_error(error: AdhelmError) -> AdhelmError:
    """Keep the fixed reader from forwarding callback/transport text."""

    if error.code == "CONFIGURATION_CHANGED":
        return AdhelmError(
            "CONFIGURATION_CHANGED",
            "Configured Meta read state changed.",
            next_action="Restart the fixed local read from current configuration.",
        )
    if error.code == "PROVIDER_ACCESS_REQUIRED":
        return AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "Meta Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local Meta Ads profile.",
        )
    if error.code == "PROVIDER_UNAVAILABLE":
        return AdhelmError("PROVIDER_UNAVAILABLE", "Meta campaign-state read is unavailable.")
    if error.code == "RESPONSE_TOO_LARGE":
        return AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta campaign state is invalid.")
    if error.code == "PROVIDER_REQUEST_FAILED":
        return AdhelmError(
            "PROVIDER_REQUEST_FAILED", "Provider rejected the fixed campaign-state read."
        )
    return AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta campaign state is invalid.")


def _response_payload(response: object) -> dict[str, Any]:
    if type(response) is not _HTTPX_RESPONSE:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta campaign state is invalid.")
    try:
        payload = parse_json(response.content, maximum_bytes=_MAX_RESPONSE_BYTES)
    except (AdhelmError, UnicodeError, ValueError):
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta campaign state is invalid.") from None
    if type(payload) is not dict or set(payload) != _RESPONSE_KEYS:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta campaign state is invalid.")
    return cast(dict[str, Any], payload)


def _configured_status(value: object) -> str:
    if type(value) is not str or value not in _CONFIGURED_STATUSES:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta campaign state is invalid.")
    return value


def _effective_status(value: object) -> str:
    if type(value) is not str or value not in _EFFECTIVE_STATUSES:
        raise AdhelmError("PROVIDER_RESPONSE_INVALID", "Meta campaign state is invalid.")
    return value
