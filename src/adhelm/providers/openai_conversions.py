"""Bounded, read-only OpenAI Ads conversion-setting inventory.

The documented Ads endpoint lists conversion definitions for its current ad
account.  This module deliberately excludes the recent-event stream: that
stream carries event payloads and is not an inventory contract.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, cast

import httpx
from pydantic import Field, model_validator

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError
from adhelm.models import SafeRef, StrictModel
from adhelm.privacy import validate_data
from adhelm.providers.openai_ads import (
    OPENAI_ADS_API_HOST,
    OPENAI_ADS_API_VERSION,
    OpenAIAdsIdentity,
    classify_openai_ads_transport_error,
)
from adhelm.transport import SafeHttpClient

# One pinned host and API version for every OpenAI Ads read in this build.
_HOST = OPENAI_ADS_API_HOST
_API_VERSION = OPENAI_ADS_API_VERSION
_MAX_PAGES = 10
_MAX_RESPONSE_BYTES = 8_388_608
_MAX_SAFE_INTEGER = 9_007_199_254_740_991

OpenAIAdsStandardConversionEvent = Literal[
    "app_installed",
    "app_opened",
    "appointment_scheduled",
    "checkout_started",
    "contents_viewed",
    "items_added",
    "lead_created",
    "order_created",
    "page_viewed",
    "registration_completed",
    "subscription_created",
    "trial_started",
]
ConversionSettingStatus = Literal["NOT_ARCHIVED", "ARCHIVED"]
# Whether this account may read the documented conversion-setup surface at all.
# The conversion and pixel features are account-enabled, so a refusal of this
# exact surface is a feature fact about the account, not a fact about the key.
OpenAIAdsConversionFeatureStatus = Literal["AVAILABLE", "NOT_ENABLED_FOR_ACCOUNT"]

_STANDARD_EVENTS = frozenset(
    {
        "app_installed",
        "app_opened",
        "appointment_scheduled",
        "checkout_started",
        "contents_viewed",
        "items_added",
        "lead_created",
        "order_created",
        "page_viewed",
        "registration_completed",
        "subscription_created",
        "trial_started",
    }
)

TokenGetter = Callable[[], str]
"""Returns the configured local read-profile token only at egress time."""

IdentitySessionVerifier = Callable[[OpenAIAdsIdentity], bool]
"""Proves that an identity is the exact object from a current discovery session."""

ReferenceFor = Callable[[Literal["account", "conversion_setting"], str], str]


def _default_reference_for(kind: Literal["account", "conversion_setting"], value: str) -> str:
    return f"openai_ads.{kind}.{value}"


def _safe_token(getter: TokenGetter) -> str:
    token = getter()
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 16_384
        or not token.isascii()
        or any(ord(char) < 33 for char in token)
    ):
        raise AdhelmError(
            "PROVIDER_ACCESS_REQUIRED",
            "OpenAI Ads authorization is unavailable.",
            exit_code=4,
            next_action="Authorize the configured local OpenAI Ads read profile.",
        )
    return token


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = parse_json(
            response.content,
            maximum_bytes=_MAX_RESPONSE_BYTES,
            decimal_numbers=True,
        )
    except (ValueError, UnicodeDecodeError, AdhelmError) as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID",
            "OpenAI Ads conversion-settings response is not valid JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID",
            "OpenAI Ads conversion-settings response has an invalid shape.",
        )
    # The provider payload is transient and never returned or persisted.  Scan
    # it before extracting the small scalar inventory subset.
    validate_data(payload)
    return payload


def _keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in value):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID",
            f"OpenAI Ads conversion setting {context} included an unsupported field.",
        )


def _safe_string(value: Any, context: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads conversion setting {context} is invalid."
        )
    try:
        validate_data(value)
    except AdhelmError as exc:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID",
            "OpenAI Ads conversion-settings response contains prohibited data.",
        ) from exc
    return value


def _opaque_id(value: Any, context: str) -> str:
    identifier = _safe_string(value, context, maximum=120)
    if not all(char.isascii() and (char.isalnum() or char in "_-") for char in identifier):
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads conversion setting {context} is invalid."
        )
    return identifier


def _positive_integer(value: Any, context: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SAFE_INTEGER:
        raise AdhelmError(
            "PROVIDER_RESPONSE_INVALID", f"OpenAI Ads conversion setting {context} is invalid."
        )
    return value


class OpenAIAdsConversionSettingsQuery(StrictModel):
    """Fixed, forward-only list request for one discovered OpenAI Ads account."""

    account_ref: SafeRef
    maximum_records: int = Field(default=500, ge=1, le=5_000)
    page_size: int = Field(default=500, ge=1, le=500)
    order: Literal["asc", "desc"] = "desc"

    @model_validator(mode="after")
    def bounded_pages(self) -> OpenAIAdsConversionSettingsQuery:
        if self.maximum_records > self.page_size * _MAX_PAGES:
            raise ValueError("OpenAI Ads conversion-settings inventory exceeds ten pages")
        return self


class OpenAIAdsConversionSetting(StrictModel):
    """Safe scalar subset of one current-account native conversion definition."""

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    conversion_setting_ref: SafeRef
    conversion_setting_client_ref: SafeRef
    event_type: OpenAIAdsStandardConversionEvent
    attribution_window_days: Literal[30]
    status: ConversionSettingStatus
    source_count: Literal[1] = 1
    campaign_count: int = Field(ge=0, le=500)
    version: int = Field(ge=1, le=_MAX_SAFE_INTEGER)
    provider_api_version: Literal["v1"] = OPENAI_ADS_API_VERSION


class OpenAIAdsConversionSettingsAvailability(StrictModel):
    """Whether the account-enabled conversion surface answered, and what it held.

    `NOT_ENABLED_FOR_ACCOUNT` is a typed outcome, never an error about the
    stored key: a `403` or `404` from this exact fixed route is read as the ad
    account not exposing the feature.

    UNVERIFIED: no reviewed OpenAI page documents the meaning of a `403` or a
    `404` on this route, and none states that OpenAI documents conversion setup
    and pixel behaviour as account-enabled features.  That reading is this
    build's own bounded choice about one fixed route, not documented OpenAI
    behaviour, and it is deliberately narrow: a `401` is not classified here at
    all, because a key the provider refused is an authorization failure with
    exit code 4 and classifying it as a missing feature would hide a credential
    problem.  Whether an OpenAI Ads key can expire at all is itself absent from
    every reviewed page, so nothing here reasons about an expired key.

    Nothing about the provider response body reaches this model.
    """

    schema_version: Literal[1] = 1
    account_ref: SafeRef
    status: OpenAIAdsConversionFeatureStatus
    observed_http_status: Literal[403, 404] | None = None
    settings: tuple[OpenAIAdsConversionSetting, ...] = ()
    provider_api_version: Literal["v1"] = OPENAI_ADS_API_VERSION

    @model_validator(mode="after")
    def unavailable_surfaces_return_nothing(self) -> OpenAIAdsConversionSettingsAvailability:
        if self.status == "NOT_ENABLED_FOR_ACCOUNT":
            if self.settings:
                raise ValueError("A not-enabled conversion surface returns no settings")
            if self.observed_http_status is None:
                raise ValueError("A not-enabled conversion surface names the observed status")
        elif self.observed_http_status is not None:
            raise ValueError("An available conversion surface records no refusal status")
        return self


class OpenAIAdsConversionSettingsClient:
    """Fixture-tested fixed read of ``GET /conversions/event_settings``.

    ``identity_session_verifier`` is deliberately mandatory.  A caller-made
    identity is insufficient: composition must prove that this exact object
    came from the same current OpenAI Ads identity-discovery session before a
    token is retrieved or a conversion-settings request is sent.
    """

    def __init__(
        self,
        transport: SafeHttpClient,
        token_getter: TokenGetter,
        *,
        identity_session_verifier: IdentitySessionVerifier,
        reference_for: ReferenceFor = _default_reference_for,
    ) -> None:
        response_limit = transport.maximum_response_bytes
        if (
            _HOST not in transport.allowed_hosts
            or not callable(identity_session_verifier)
            or type(response_limit) is not int
            or not 1 <= response_limit <= _MAX_RESPONSE_BYTES
        ):
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID",
                "OpenAI Ads conversion inventory requires its fixed host, bounded response, and "
                "identity verifier.",
            )
        self._transport = transport
        self._token_getter = token_getter
        self._identity_session_verifier = identity_session_verifier
        self._reference_for = reference_for

    def list_event_settings(
        self,
        query: OpenAIAdsConversionSettingsQuery,
        *,
        identity: OpenAIAdsIdentity,
    ) -> list[OpenAIAdsConversionSetting]:
        """Return every setting within the complete configured result bound.

        The implementation sends only documented ``limit``, ``order``, and
        forward ``after`` parameters.  It never calls the event stream or any
        create, update, archive, key, pixel, upload, or event-delivery route.
        """

        # ``model_copy`` and ``model_construct`` can bypass Pydantic validators.
        # This public adapter boundary must never turn a forged query into an
        # out-of-contract request parameter.
        query = OpenAIAdsConversionSettingsQuery.model_validate(query.model_dump(mode="python"))
        self._identity_matches(query.account_ref, identity)
        settings: list[OpenAIAdsConversionSetting] = []
        after: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(_MAX_PAGES):
            remaining = query.maximum_records - len(settings)
            if remaining < 1:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "OpenAI Ads conversion-settings inventory exceeds the configured "
                    "complete-result bound.",
                )
            requested_limit = min(query.page_size, remaining)
            params: dict[str, str | int] = {
                "limit": requested_limit,
                "order": query.order,
            }
            if after is not None:
                params["after"] = after
            data = _json(self._get(params=params))
            page, next_after = self._parse_page(data, identity=identity)
            if len(page) > requested_limit:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads conversion-settings response exceeded the requested page limit.",
                )
            if len(settings) + len(page) > query.maximum_records:
                raise AdhelmError(
                    "RESULT_LIMIT_REQUIRED",
                    "OpenAI Ads conversion-settings inventory exceeds the configured "
                    "complete-result bound.",
                )
            identifiers = {item.conversion_setting_client_ref for item in [*settings, *page]}
            if len(identifiers) != len(settings) + len(page):
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads conversion-settings response contains duplicate native "
                    "identifiers.",
                )
            settings.extend(page)
            if next_after is None:
                return settings
            if next_after in seen_cursors:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads conversion-settings pagination made no progress.",
                )
            seen_cursors.add(next_after)
            after = next_after
        raise AdhelmError(
            "PAGINATION_LIMIT", "OpenAI Ads conversion-settings inventory exceeded ten pages."
        )

    def read_event_settings(
        self,
        query: OpenAIAdsConversionSettingsQuery,
        *,
        identity: OpenAIAdsIdentity,
    ) -> OpenAIAdsConversionSettingsAvailability:
        """Read the settings, or report the surface as not enabled for the account.

        This wraps :meth:`list_event_settings` and classifies exactly two
        refusals of this one fixed route as a feature fact: `403` and `404`.
        Every other failure -- an unusable key, a rate limit, a malformed
        response, a pagination bound -- propagates unchanged, so a real problem
        is never reported as a disabled feature.
        """
        try:
            settings = self.list_event_settings(query, identity=identity)
        except AdhelmError as error:
            status = getattr(error, "status_code", None)
            if status not in {403, 404}:
                raise
            return OpenAIAdsConversionSettingsAvailability(
                account_ref=identity.account_ref,
                status="NOT_ENABLED_FOR_ACCOUNT",
                observed_http_status=cast(Literal[403, 404], status),
            )
        return OpenAIAdsConversionSettingsAvailability(
            account_ref=identity.account_ref,
            status="AVAILABLE",
            settings=tuple(settings),
        )

    def _get(self, *, params: dict[str, str | int]) -> httpx.Response:
        try:
            return self._transport.request(
                "GET",
                f"https://{_HOST}/{_API_VERSION}/conversions/event_settings",
                headers={"authorization": f"Bearer {_safe_token(self._token_getter)}"},
                params=params,
            )
        except AdhelmError as error:
            raise classify_openai_ads_transport_error(error) from None

    def _identity_matches(self, account_ref: str, identity: OpenAIAdsIdentity) -> None:
        account = _opaque_id(identity.account_client_ref, "identity account ID")
        if (
            identity.provider_api_version != _API_VERSION
            or account_ref != identity.account_ref
            or identity.account_ref != self._reference_for("account", account)
        ):
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "OpenAI Ads request differs from the configured account identity.",
            )
        try:
            verified = self._identity_session_verifier(identity)
        except Exception as exc:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "OpenAI Ads identity session could not be verified.",
            ) from exc
        if verified is not True:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "OpenAI Ads identity was not returned by the current discovery session.",
            )

    def _parse_page(
        self,
        data: dict[str, Any],
        *,
        identity: OpenAIAdsIdentity,
    ) -> tuple[list[OpenAIAdsConversionSetting], str | None]:
        expected = {"object", "data", "first_id", "last_id", "has_more"}
        if set(data) != expected:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI Ads conversion-settings response has an invalid list envelope.",
            )
        if data.get("object") != "list":
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI Ads conversion-settings response object is invalid.",
            )
        raw_page = data.get("data")
        if not isinstance(raw_page, list) or len(raw_page) > 500:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI Ads conversion-settings response collection is invalid.",
            )
        has_more = data.get("has_more")
        if not isinstance(has_more, bool):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI Ads conversion-settings pagination flag is invalid.",
            )
        if not raw_page:
            if has_more or data.get("first_id") is not None or data.get("last_id") is not None:
                raise AdhelmError(
                    "PROVIDER_RESPONSE_INVALID",
                    "OpenAI Ads conversion-settings pagination made no progress.",
                )
            return [], None

        settings = [self._parse_setting(item, identity=identity) for item in raw_page]
        first_id = _opaque_id(data.get("first_id"), "first-page identifier")
        last_id = _opaque_id(data.get("last_id"), "last-page identifier")
        if (
            first_id != settings[0].conversion_setting_client_ref
            or last_id != settings[-1].conversion_setting_client_ref
        ):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI Ads conversion-settings pagination identifiers are inconsistent.",
            )
        return settings, last_id if has_more else None

    def _parse_setting(
        self,
        item: Any,
        *,
        identity: OpenAIAdsIdentity,
    ) -> OpenAIAdsConversionSetting:
        if not isinstance(item, dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "OpenAI Ads conversion setting is invalid."
            )
        required = {
            "id",
            "name",
            "event_type",
            "custom_event_name",
            "attribution_window_days",
            "ad_account_id",
            "source_ids",
            "sources",
            "campaigns",
            "archived",
            "version",
        }
        _keys(item, required, "response")
        if not required.issubset(item):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI Ads conversion setting omitted a required response field.",
            )
        # Validate, then deliberately discard the provider display name.  It
        # is not needed to establish a native conversion-setting inventory.
        _safe_string(item["name"], "display name", maximum=1_000)
        setting_id = _opaque_id(item["id"], "ID")
        account_id = _opaque_id(item["ad_account_id"], "account ID")
        if account_id != identity.account_client_ref:
            raise AdhelmError(
                "IDENTITY_MISMATCH",
                "OpenAI Ads conversion setting belongs to a different account.",
            )
        event_type = _safe_string(item["event_type"], "event type", maximum=80)
        if event_type not in _STANDARD_EVENTS or item["custom_event_name"] is not None:
            raise AdhelmError(
                "UNSUPPORTED_CONVERSION_VARIANT",
                "OpenAI Ads conversion setting is outside the reviewed standard-event contract.",
            )
        if (
            type(item["attribution_window_days"]) is not int
            or item["attribution_window_days"] != 30
        ):
            raise AdhelmError(
                "UNSUPPORTED_CONVERSION_VARIANT",
                "OpenAI Ads conversion setting does not use the documented 30-day window.",
            )
        source_ids = item["source_ids"]
        if not isinstance(source_ids, list) or len(source_ids) != 1:
            raise AdhelmError(
                "UNSUPPORTED_CONVERSION_VARIANT",
                "OpenAI Ads conversion setting does not have exactly one source.",
            )
        source_id = _opaque_id(source_ids[0], "source ID")
        sources = item["sources"]
        if not isinstance(sources, list) or len(sources) != 1 or not isinstance(sources[0], dict):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "OpenAI Ads conversion setting source is invalid."
            )
        source = sources[0]
        _keys(source, {"id", "name"}, "source")
        if set(source) != {"id", "name"}:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID", "OpenAI Ads conversion setting source is invalid."
            )
        if _opaque_id(source["id"], "source ID") != source_id:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI Ads conversion setting source identifiers are inconsistent.",
            )
        _safe_string(source["name"], "source display name", maximum=1_000)
        campaigns = item["campaigns"]
        if not isinstance(campaigns, list) or len(campaigns) > 500:
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI Ads conversion setting campaign associations are invalid.",
            )
        archived = item["archived"]
        if not isinstance(archived, bool):
            raise AdhelmError(
                "PROVIDER_RESPONSE_INVALID",
                "OpenAI Ads conversion setting archive state is invalid.",
            )
        return OpenAIAdsConversionSetting(
            account_ref=identity.account_ref,
            conversion_setting_ref=self._reference_for("conversion_setting", setting_id),
            conversion_setting_client_ref=setting_id,
            event_type=cast(OpenAIAdsStandardConversionEvent, event_type),
            attribution_window_days=30,
            status="ARCHIVED" if archived else "NOT_ARCHIVED",
            campaign_count=len(campaigns),
            version=_positive_integer(item["version"], "version"),
        )
