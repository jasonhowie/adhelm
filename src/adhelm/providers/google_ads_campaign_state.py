"""Fixed Google Ads v25 EDIT-session campaign observations.

This module owns only two sealed ``googleAds:search`` reads: the configured
customer identity and one selected campaign state.  It deliberately has no
GAQL, resource-path, bearer, mutation, pagination, or retry
surface for callers.  A configured session supplies the guarded header
callback and closed target below.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Final, Literal, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import ConfigDict, Field, field_validator, model_validator

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Currency, SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.transport import SafeHttpClient

__all__ = ("GoogleAdsCampaignState", "GoogleAdsCampaignStateReader", "GoogleAdsEditIdentity")

_HOST: Final = "googleads.googleapis.com"
_API_VERSION: Final = "v25"
_MAX_RESPONSE_BYTES: Final = 65_536
_MAX_SECRET_BYTES: Final = 16_384
_MAX_INT64: Final = 9_223_372_036_854_775_807
_CUSTOMER_ID: Final = re.compile(r"^[0-9]{10}$", re.ASCII)
_CAMPAIGN_ID: Final = re.compile(r"^[1-9][0-9]{0,18}$", re.ASCII)
_ENUM: Final = re.compile(r"^[A-Z][A-Z0-9_]{0,79}$", re.ASCII)
_FIELD_MASK: Final = re.compile(r"^[A-Za-z0-9_.,]{1,4096}$", re.ASCII)
_DIGITS: Final = re.compile(r"^[0-9]{1,19}$", re.ASCII)
_HTTPX_CLIENT: Final = httpx.Client
_HTTPX_RESPONSE: Final = httpx.Response

_IDENTITY_QUERY: Final = (
    "SELECT customer.id, customer.resource_name, customer.currency_code, "
    "customer.time_zone, customer.status FROM customer"
)
_CAMPAIGN_QUERY_PREFIX: Final = (
    "SELECT campaign.id, campaign.resource_name, campaign.status FROM campaign WHERE campaign.id = "
)
_CUSTOMER_STATUSES: Final = frozenset({"CANCELED", "CLOSED", "ENABLED", "SUSPENDED"})
_CAMPAIGN_STATUSES: Final = frozenset({"ENABLED", "PAUSED", "REMOVED"})
_RESPONSE_KEYS: Final = frozenset(
    {
        "fieldMask",
        "metricAttributes",
        "nextPageToken",
        "queryResourceConsumption",
        "results",
        "summaryRow",
        "totalResultsCount",
    }
)
GoogleAdsCustomerStatus = Literal["CANCELED", "CLOSED", "ENABLED", "SUSPENDED"]
GoogleAdsCampaignStatus = Literal["ENABLED", "PAUSED", "REMOVED"]
HeaderGetter = Callable[[], dict[str, str]]


def _default_client_headers() -> tuple[tuple[str, str], ...]:
    """Capture this installed httpx version's clean client defaults once."""

    client = _HTTPX_CLIENT(trust_env=False)
    try:
        return tuple(
            sorted((str(name).lower(), str(value)) for name, value in client.headers.items())
        )
    finally:
        client.close()


_DEFAULT_CLIENT_HEADERS: Final = _default_client_headers()


class _GoogleAdsEditTarget(StrictModel):
    """Private, nonsecret route and declared-account facts for the fixed reads."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    customer_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    login_customer_client_ref: str | None = Field(default=None, pattern=r"^[0-9]{10}$")
    declared_currency: Currency
    declared_timezone: str = Field(min_length=1, max_length=80)

    @field_validator("customer_client_ref", "login_customer_client_ref", mode="before")
    @classmethod
    def native_customer_id_is_ascii_decimal(cls, value: object) -> object:
        if value is None:
            return None
        if type(value) is not str or _CUSTOMER_ID.fullmatch(value) is None:
            raise ValueError("Google Ads customer ID is invalid")
        return value

    @field_validator("declared_currency", mode="before")
    @classmethod
    def declared_currency_is_exact(cls, value: object) -> str:
        if type(value) is not str or re.fullmatch(r"[A-Z]{3}", value, re.ASCII) is None:
            raise ValueError("Google Ads declared currency is invalid")
        return value

    @field_validator("declared_timezone", mode="before")
    @classmethod
    def declared_timezone_is_iana(cls, value: object) -> str:
        if type(value) is not str or not 1 <= len(value) <= 80:
            raise ValueError("Google Ads declared timezone is invalid")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Google Ads declared timezone is invalid") from None
        return value


class GoogleAdsEditIdentity(StrictModel):
    """One observed configured customer identity, without an eligibility claim.

    ``CLOSED`` is retained as an observed customer status.  It is not turned
    into a local eligibility or account-access claim because test customers
    may use that provider status.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    customer_ref: SafeRef
    customer_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    currency: Currency
    timezone: str = Field(min_length=1, max_length=80)
    status: GoogleAdsCustomerStatus
    provider_api_version: Literal["v25"] = "v25"

    @field_validator("customer_client_ref", mode="before")
    @classmethod
    def native_customer_id_is_ascii_decimal(cls, value: object) -> str:
        if type(value) is not str or _CUSTOMER_ID.fullmatch(value) is None:
            raise ValueError("Google Ads customer ID is invalid")
        return value

    @field_validator("currency", mode="before")
    @classmethod
    def currency_is_exact(cls, value: object) -> str:
        if type(value) is not str or re.fullmatch(r"[A-Z]{3}", value, re.ASCII) is None:
            raise ValueError("Google Ads customer currency is invalid")
        return value

    @field_validator("timezone", mode="before")
    @classmethod
    def timezone_is_iana(cls, value: object) -> str:
        if type(value) is not str or not 1 <= len(value) <= 80:
            raise ValueError("Google Ads customer timezone is invalid")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Google Ads customer timezone is invalid") from None
        return value

    @field_validator("status", mode="before")
    @classmethod
    def status_is_closed(cls, value: object) -> str:
        if type(value) is not str or value not in _CUSTOMER_STATUSES:
            raise ValueError("Google Ads customer status is invalid")
        return value

    @model_validator(mode="after")
    def reference_binds_customer(self) -> GoogleAdsEditIdentity:
        if self.customer_ref != f"google_ads.customer.{self.customer_client_ref}":
            raise ValueError("Google Ads customer reference does not bind the native ID")
        return self


class GoogleAdsCampaignState(StrictModel):
    """One observed fixed campaign state bound to a proved customer identity."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, validate_default=True)

    campaign_ref: SafeRef
    campaign_client_ref: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    customer_ref: SafeRef
    customer_client_ref: str = Field(pattern=r"^[0-9]{10}$")
    status: GoogleAdsCampaignStatus
    provider_api_version: Literal["v25"] = "v25"

    @field_validator("campaign_client_ref", mode="before")
    @classmethod
    def campaign_id_is_positive_int64(cls, value: object) -> str:
        if type(value) is not str or _CAMPAIGN_ID.fullmatch(value) is None:
            raise ValueError("Google Ads campaign ID is invalid")
        if int(value) > _MAX_INT64:
            raise ValueError("Google Ads campaign ID is invalid")
        return value

    @field_validator("customer_client_ref", mode="before")
    @classmethod
    def customer_id_is_ascii_decimal(cls, value: object) -> str:
        if type(value) is not str or _CUSTOMER_ID.fullmatch(value) is None:
            raise ValueError("Google Ads customer ID is invalid")
        return value

    @field_validator("status", mode="before")
    @classmethod
    def status_is_closed(cls, value: object) -> str:
        if type(value) is not str or value not in _CAMPAIGN_STATUSES:
            raise ValueError("Google Ads campaign status is invalid")
        return value

    @model_validator(mode="after")
    def references_bind_native_ids(self) -> GoogleAdsCampaignState:
        if (
            self.customer_ref != f"google_ads.customer.{self.customer_client_ref}"
            or self.campaign_ref
            != f"google_ads.campaign.{self.customer_client_ref}.{self.campaign_client_ref}"
        ):
            raise ValueError("Google Ads campaign references do not bind native IDs")
        return self


class GoogleAdsCampaignStateReader:
    """One-attempt reader for the two fixed Google Ads EDIT observations."""

    __slots__ = ("_headers_getter", "_target", "_transport")

    def __init__(
        self,
        transport: SafeHttpClient,
        headers_getter: HeaderGetter,
        target: _GoogleAdsEditTarget,
    ) -> None:
        _validate_transport(transport)
        checked_target = _checked_target(target)
        if not callable(headers_getter):
            raise _transport_invalid("Google Ads state requires a guarded credential callback.")
        self._transport = transport
        self._headers_getter = headers_getter
        self._target = checked_target

    def discover_identity(self) -> GoogleAdsEditIdentity:
        """Read exactly one customer and bind it to the configured declaration."""

        transport, headers_getter, target = self._checked_members()
        row = _one_result(
            _fixed_search(transport, headers_getter, target, _IDENTITY_QUERY), "customer identity"
        )
        _exact_keys(row, {"customer"})
        customer = _mapping(row.get("customer"))
        _exact_keys(customer, {"id", "resourceName", "currencyCode", "timeZone", "status"})
        customer_id = _customer_id(customer.get("id"))
        _required_resource_name(customer.get("resourceName"), f"customers/{customer_id}")
        currency = _currency(customer.get("currencyCode"))
        timezone = _timezone(customer.get("timeZone"))
        status = _customer_status(customer.get("status"))
        if (
            customer_id != target.customer_client_ref
            or currency != target.declared_currency
            or timezone != target.declared_timezone
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Google Ads customer differs from the configured EDIT profile.",
                exit_code=ExitCode.PRECONDITION,
            )
        try:
            return GoogleAdsEditIdentity(
                customer_ref=f"google_ads.customer.{customer_id}",
                customer_client_ref=customer_id,
                currency=currency,
                timezone=timezone,
                status=cast(GoogleAdsCustomerStatus, status),
            )
        except Exception:
            raise _provider_response_invalid() from None

    def read_campaign_state(
        self, identity: GoogleAdsEditIdentity, campaign_id: str
    ) -> GoogleAdsCampaignState:
        """Read one fixed campaign after local identity and ID revalidation."""

        transport, headers_getter, target = self._checked_members()
        bound_identity = _bound_identity(identity, target)
        campaign = _positive_int64(campaign_id, response=False)
        query = _CAMPAIGN_QUERY_PREFIX + campaign
        row = _one_result(_fixed_search(transport, headers_getter, target, query), "campaign state")
        _exact_keys(row, {"campaign"})
        observed = _mapping(row.get("campaign"))
        _exact_keys(observed, {"id", "resourceName", "status"})
        returned_campaign = _positive_int64(observed.get("id"), response=True)
        _required_resource_name(
            observed.get("resourceName"),
            f"customers/{target.customer_client_ref}/campaigns/{returned_campaign}",
        )
        status = _campaign_status(observed.get("status"))
        if returned_campaign != campaign:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "Google Ads campaign state differs from the requested campaign.",
                exit_code=ExitCode.PRECONDITION,
            )
        try:
            return GoogleAdsCampaignState(
                campaign_ref=(
                    f"google_ads.campaign.{target.customer_client_ref}.{returned_campaign}"
                ),
                campaign_client_ref=returned_campaign,
                customer_ref=bound_identity.customer_ref,
                customer_client_ref=bound_identity.customer_client_ref,
                status=cast(GoogleAdsCampaignStatus, status),
            )
        except Exception:
            raise _provider_response_invalid() from None

    def _checked_members(self) -> tuple[SafeHttpClient, HeaderGetter, _GoogleAdsEditTarget]:
        if type(self) is not GoogleAdsCampaignStateReader:
            raise _transport_invalid("Google Ads state reader is invalid.")
        try:
            transport = self._transport
            headers_getter = self._headers_getter
            target = self._target
        except Exception:
            raise _transport_invalid("Google Ads state reader is invalid.") from None
        _validate_transport(transport)
        target = _checked_target(target)
        if not callable(headers_getter):
            raise _transport_invalid("Google Ads state requires a guarded credential callback.")
        return transport, headers_getter, target


def _fixed_search(
    transport: SafeHttpClient,
    headers_getter: HeaderGetter,
    target: _GoogleAdsEditTarget,
    query: str,
) -> dict[str, object]:
    """Issue one fixed v25 search after the final credential/transport check."""

    headers = _headers_snapshot(headers_getter, target)
    # A credential callback can change mutable Python transport state.  Check
    # the complete one-attempt/ambient-state boundary immediately before POST.
    _validate_transport(transport)
    try:
        response = transport.request(
            "POST",
            (
                f"https://{_HOST}/{_API_VERSION}/customers/"
                f"{target.customer_client_ref}/googleAds:search"
            ),
            headers=headers,
            json={"query": query, "searchSettings": {"returnTotalResultsCount": True}},
            mutation=False,
        )
    except AdhelmError as error:
        raise _sanitized_transport_error(error) from None
    except Exception:
        raise AdhelmError(
            "PROVIDER_UNAVAILABLE", "Google Ads observation is unavailable."
        ) from None
    return _search_response(response)


def _headers_snapshot(getter: HeaderGetter, target: _GoogleAdsEditTarget) -> dict[str, str]:
    try:
        value = getter()
    except AdhelmError as error:
        raise _sanitized_callback_error(error) from None
    except Exception:
        raise _provider_access_required() from None
    try:
        if type(value) is not dict:
            raise ValueError("headers")
        expected = {"authorization"}
        if target.login_customer_client_ref is not None:
            expected.add("login-customer-id")
        if set(value) != expected or any(type(key) is not str for key in value):
            raise ValueError("header keys")
        authorization = value["authorization"]
        if (
            type(authorization) is not str
            or not authorization.startswith("Bearer ")
            or not _secret(authorization[7:])
        ):
            raise ValueError("credentials")
        result = {"authorization": authorization}
        if target.login_customer_client_ref is not None:
            login = value["login-customer-id"]
            if type(login) is not str or login != target.login_customer_client_ref:
                raise ValueError("login")
            result["login-customer-id"] = login
        return result
    except Exception:
        raise _provider_access_required() from None


def _search_response(value: object) -> dict[str, object]:
    if type(value) is not _HTTPX_RESPONSE:
        raise _provider_response_invalid()
    try:
        payload = parse_json(value.content, maximum_bytes=_MAX_RESPONSE_BYTES, decimal_numbers=True)
        if type(payload) is not dict:
            raise ValueError("response")
        validate_data(payload)
        result = cast(dict[str, object], payload)
        if any(type(key) is not str or key not in _RESPONSE_KEYS for key in result):
            raise ValueError("response keys")
        if "results" not in result or "totalResultsCount" not in result:
            raise ValueError("required response fields")
        total = result["totalResultsCount"]
        if type(total) is not str or total != "1":
            raise ValueError("total")
        next_page = result.get("nextPageToken")
        if next_page not in (None, ""):
            raise ValueError("pagination")
        if result.get("summaryRow") is not None:
            raise ValueError("summary")
        metric_attributes = result.get("metricAttributes")
        if metric_attributes not in (None, []):
            raise ValueError("metrics")
        field_mask = result.get("fieldMask")
        if field_mask is not None and (
            type(field_mask) is not str or _FIELD_MASK.fullmatch(field_mask) is None
        ):
            raise ValueError("field mask")
        consumption = result.get("queryResourceConsumption")
        if consumption is not None:
            _bounded_int64(consumption)
        return result
    except Exception:
        raise _provider_response_invalid() from None


def _one_result(response: dict[str, object], context: str) -> dict[str, object]:
    try:
        rows = response["results"]
        if type(rows) is not list or len(rows) != 1 or type(rows[0]) is not dict:
            raise ValueError(context)
        return cast(dict[str, object], rows[0])
    except Exception:
        raise _provider_response_invalid() from None


def _exact_keys(value: dict[str, object], allowed: set[str]) -> None:
    if set(value) != allowed or any(type(key) is not str for key in value):
        raise _provider_response_invalid()


def _mapping(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _provider_response_invalid()
    return cast(dict[str, object], value)


def _customer_id(value: object) -> str:
    if type(value) is not str or _CUSTOMER_ID.fullmatch(value) is None:
        raise _provider_response_invalid()
    return value


def _positive_int64(value: object, *, response: bool) -> str:
    if type(value) is not str or _CAMPAIGN_ID.fullmatch(value) is None or int(value) > _MAX_INT64:
        if response:
            raise _provider_response_invalid()
        raise AdhelmError(
            "INVALID_INPUT",
            "Campaign ID must be a positive ASCII signed-64-bit decimal identifier.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return value


def _bounded_int64(value: object) -> None:
    if type(value) is not str or _DIGITS.fullmatch(value) is None or int(value) > _MAX_INT64:
        raise _provider_response_invalid()


def _required_resource_name(value: object, expected: str) -> None:
    """Require the selected resource name to bind the returned native IDs."""

    if type(value) is not str or value != expected:
        raise _provider_response_invalid()


def _currency(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[A-Z]{3}", value, re.ASCII) is None:
        raise _provider_response_invalid()
    return value


def _timezone(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 80:
        raise _provider_response_invalid()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise _provider_response_invalid() from None
    return value


def _customer_status(value: object) -> str:
    if type(value) is not str or _ENUM.fullmatch(value) is None or value not in _CUSTOMER_STATUSES:
        raise _provider_response_invalid()
    return value


def _campaign_status(value: object) -> str:
    if type(value) is not str or _ENUM.fullmatch(value) is None or value not in _CAMPAIGN_STATUSES:
        raise _provider_response_invalid()
    return value


def _bound_identity(value: object, target: _GoogleAdsEditTarget) -> GoogleAdsEditIdentity:
    if type(value) is not GoogleAdsEditIdentity:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Google Ads identity is invalid.")
    try:
        identity = GoogleAdsEditIdentity.model_validate(
            value.model_dump(mode="python", warnings="error")
        )
    except Exception:
        raise AdhelmError("IDENTITY_BINDING_INVALID", "Google Ads identity is invalid.") from None
    if (
        identity.customer_client_ref != target.customer_client_ref
        or identity.customer_ref != f"google_ads.customer.{target.customer_client_ref}"
        or identity.currency != target.declared_currency
        or identity.timezone != target.declared_timezone
    ):
        raise AdhelmError(
            "IDENTITY_BINDING_INVALID",
            "Google Ads identity is outside the configured EDIT profile.",
        )
    return identity


def _checked_target(value: object) -> _GoogleAdsEditTarget:
    if type(value) is not _GoogleAdsEditTarget:
        raise _transport_invalid("Google Ads state target is invalid.")
    try:
        return _GoogleAdsEditTarget.model_validate(
            value.model_dump(mode="python", warnings="error")
        )
    except Exception:
        raise _transport_invalid("Google Ads state target is invalid.") from None


def _validate_transport(value: object) -> None:
    """Require one bounded, clean fixed-host POST-read transport."""

    if type(value) is not SafeHttpClient:
        raise _transport_invalid("Google Ads state requires SafeHttpClient.")
    try:
        allowed_hosts = value.allowed_hosts
        maximum_response_bytes = value.maximum_response_bytes
        read_attempts = value.read_attempts
        client = value.client
        headers = tuple(
            sorted((str(name).lower(), str(content)) for name, content in client.headers.items())
        )
        hooks = client.event_hooks
        ambient = (
            type(client) is not _HTTPX_CLIENT
            or allowed_hosts != frozenset({_HOST})
            or type(maximum_response_bytes) is not int
            or not 1 <= maximum_response_bytes <= _MAX_RESPONSE_BYTES
            or type(read_attempts) is not int
            or read_attempts != 1
            or client.trust_env is not False
            or client.follow_redirects is not False
            or bool(client.params)
            or client.auth is not None
            or bool(client.cookies)
            or type(hooks) is not dict
            or set(hooks) != {"request", "response"}
            or any(type(callbacks) is not list or callbacks for callbacks in hooks.values())
            or headers != _DEFAULT_CLIENT_HEADERS
        )
    except Exception:
        ambient = True
    if ambient:
        raise _transport_invalid("Google Ads state transport has undeclared request state.")


def _secret(value: object) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= _MAX_SECRET_BYTES
        and value.isascii()
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _sanitized_callback_error(error: AdhelmError) -> AdhelmError:
    code = getattr(error, "code", None)
    if type(code) is not str:
        return _provider_access_required()
    if code == "CONFIGURATION_CHANGED":
        return AdhelmError(
            "CONFIGURATION_CHANGED",
            "Local configuration changed during the Google Ads EDIT observation.",
            next_action="Review the current configuration before another fixed observation.",
        )
    if code == "GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED":
        return AdhelmError(
            "GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED",
            "Local Google Ads EDIT credentials changed during the fixed observation.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Reauthorize the configured Google Ads EDIT profile before another read.",
        )
    if code == "GOOGLE_ADS_EDIT_TOKEN_EXPIRED":
        return AdhelmError(
            "GOOGLE_ADS_EDIT_TOKEN_EXPIRED",
            "The local Google Ads EDIT OAuth token is no longer usable.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Reauthorize the configured Google Ads EDIT profile before another read.",
        )
    if code == "CLOCK_INVALID":
        return AdhelmError(
            "CLOCK_INVALID",
            "A trusted local clock is unavailable for the Google Ads EDIT observation.",
            exit_code=ExitCode.AUTHORIZATION,
        )
    return _provider_access_required()


def _sanitized_transport_error(error: AdhelmError) -> AdhelmError:
    code = getattr(error, "code", None)
    if type(code) is str and code == "PROVIDER_ACCESS_REQUIRED":
        return _provider_access_required()
    if type(code) is str and code == "RESPONSE_TOO_LARGE":
        return AdhelmError(
            "PROVIDER_RESPONSE_INVALID", "Google Ads observation response is invalid."
        )
    return AdhelmError("PROVIDER_UNAVAILABLE", "Google Ads observation is unavailable.")


def _transport_invalid(message: str) -> AdhelmError:
    return AdhelmError("TRANSPORT_CONFIG_INVALID", message)


def _provider_access_required() -> AdhelmError:
    return AdhelmError(
        "PROVIDER_ACCESS_REQUIRED",
        "Google Ads EDIT authorization is unavailable.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Authorize the configured local Google Ads EDIT profile.",
    )


def _provider_response_invalid() -> AdhelmError:
    return AdhelmError("PROVIDER_RESPONSE_INVALID", "Google Ads observation response is invalid.")
