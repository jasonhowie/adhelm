"""Fixed property and selected-web-stream GETs, without permission inference."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal

from pydantic import Field

from adhelm.errors import AdhelmError
from adhelm.models import Currency, StrictModel
from adhelm.providers.ga4 import (
    _json,
    _keys,
    _safe_token,
    _timezone,
    _validate_property_response,
    _validate_stream,
)
from adhelm.transport import SafeHttpClient

_HOST = "analyticsadmin.googleapis.com"
_NATIVE_ID = re.compile(r"[0-9]{1,20}")
_PROPERTY_FIELDS = "name,timeZone,currencyCode,serviceLevel,deleteTime"
_STREAM_FIELDS = "name,type"
Ga4ServiceLevel = Literal["GOOGLE_ANALYTICS_STANDARD", "GOOGLE_ANALYTICS_360", "NOT_AVAILABLE"]


class Ga4AdminAccessIdentity(StrictModel):
    """Local operational facts; callers expose only a bounded redacted projection."""

    property_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    web_stream_client_ref: str = Field(pattern=r"^[0-9]{1,20}$")
    timezone: str = Field(min_length=1, max_length=80)
    currency: Currency
    service_level: Ga4ServiceLevel


class Ga4AdminAccessClient:
    """Exactly two GETs under one token snapshot, with no collection enumeration."""

    def __init__(self, transport: SafeHttpClient, token_getter: Callable[[], str]) -> None:
        if transport.allowed_hosts != frozenset({_HOST}) or transport.client.params:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID",
                "GA4 access checks require the fixed Admin host and no default query parameters.",
            )
        self._transport = transport
        self._token_getter = token_getter

    def read_identity(self, *, property_id: str, stream_id: str) -> Ga4AdminAccessIdentity:
        if any(
            type(value) is not str or _NATIVE_ID.fullmatch(value) is None
            for value in (property_id, stream_id)
        ):
            raise AdhelmError(
                "INVALID_WORKFLOW_INPUT", "GA4 access-check target is invalid.", exit_code=2
            )
        token = _safe_token(self._token_getter)
        headers = {"authorization": f"Bearer {token}"}
        property_name = f"properties/{property_id}"
        prop = _json(
            self._transport.request(
                "GET",
                f"https://{_HOST}/v1beta/{property_name}",
                headers=headers,
                params={"fields": _PROPERTY_FIELDS},
            ),
            "property",
        )
        _keys(prop, set(_PROPERTY_FIELDS.split(",")), "projected property")
        _validate_property_response(prop)
        if prop.get("name") != property_name or prop.get("deleteTime") is not None:
            raise AdhelmError(
                "IDENTITY_MISMATCH", "GA4 property does not match the active configured target."
            )
        timezone = prop.get("timeZone")
        currency = prop.get("currencyCode")
        if (
            type(timezone) is not str
            or not 1 <= len(timezone) <= 80
            or type(currency) is not str
            or re.fullmatch(r"[A-Z]{3}", currency) is None
        ):
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 property settings are unavailable.")
        _timezone(timezone, "property timezone")
        raw_level = prop.get("serviceLevel")
        level: Ga4ServiceLevel
        if raw_level in {None, "SERVICE_LEVEL_UNSPECIFIED"}:
            level = "NOT_AVAILABLE"
        elif raw_level in {"GOOGLE_ANALYTICS_STANDARD", "GOOGLE_ANALYTICS_360"}:
            level = raw_level
        else:
            raise AdhelmError("PROVIDER_RESPONSE_INVALID", "GA4 service level is unsupported.")

        stream_name = f"{property_name}/dataStreams/{stream_id}"
        stream = _json(
            self._transport.request(
                "GET",
                f"https://{_HOST}/v1beta/{stream_name}",
                headers=headers,
                params={"fields": _STREAM_FIELDS},
            ),
            "selected web stream",
        )
        _keys(stream, set(_STREAM_FIELDS.split(",")), "projected web stream")
        _validate_stream(stream)
        if stream.get("name") != stream_name or stream.get("type") != "WEB_DATA_STREAM":
            raise AdhelmError(
                "IDENTITY_MISMATCH", "GA4 web stream does not match the configured target."
            )
        return Ga4AdminAccessIdentity(
            property_client_ref=property_id,
            web_stream_client_ref=stream_id,
            timezone=timezone,
            currency=currency,
            service_level=level,
        )
