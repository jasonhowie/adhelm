"""Bounded extraction of provider codes and request identifiers, never error prose."""

import re
from typing import Any

import httpx

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError
from adhelm.privacy import validate_data

_MAX_ERROR_BYTES = 16_384
# Reviewed against googleapis/googleapis google/ads/googleads/v25/errors/*.proto,
# 2026-10-12. Unknown enum values are deliberately not reflected as provider text.
_GOOGLE_CODES = {
    "queryError": {
        "UNRECOGNIZED_FIELD",
        "PROHIBITED_FIELD_COMBINATION_IN_SELECT_CLAUSE",
        "PROHIBITED_SEGMENT_WITH_METRIC_IN_SELECT_OR_WHERE_CLAUSE",
        "BAD_FIELD_NAME",
    },
    "authorizationError": {
        "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION",
        "USER_PERMISSION_DENIED",
        "CUSTOMER_NOT_ENABLED",
        "ACTION_NOT_PERMITTED",
    },
    "authenticationError": {
        "OAUTH_TOKEN_INVALID",
        "OAUTH_TOKEN_EXPIRED",
        "NOT_ADS_USER",
        "CUSTOMER_NOT_FOUND",
        "GOOGLE_ACCOUNT_DELETED",
    },
    "quotaError": {
        "RESOURCE_EXHAUSTED",
        "RESOURCE_TEMPORARILY_EXHAUSTED",
        "EXCESSIVE_SHORT_TERM_QUERY_RESOURCE_CONSUMPTION",
    },
    "requestError": {"REQUIRED_FIELD_MISSING", "RESOURCE_NAME_MALFORMED"},
}


def _identifier(value: object, forbidden: tuple[str, ...]) -> str | None:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) is None:
        return None
    if any(secret and (secret in value or value in secret) for secret in forbidden):
        return None
    try:
        validate_data(value)
    except AdhelmError:
        return None
    return value


def _number(value: object) -> str | None:
    return str(value) if type(value) is int and 0 <= value <= 2_147_483_647 else None


def provider_error_details(
    response: httpx.Response, *, maximum_bytes: int = _MAX_ERROR_BYTES
) -> dict[str, Any]:
    """Keep the original HTTP outcome even when its diagnostic body is invalid."""
    host = response.request.url.host
    if host not in {
        "googleads.googleapis.com",
        "analyticsdata.googleapis.com",
        "analyticsadmin.googleapis.com",
        "tagmanager.googleapis.com",
        "graph.facebook.com",
        "api.linkedin.com",
        "business-api.tiktok.com",
        "ads-api.reddit.com",
        "api.ads.openai.com",
    }:
        return {}
    forbidden = tuple(response.request.headers.values()) + tuple(
        value.removeprefix("Bearer ") for value in response.request.headers.values()
    )
    try:
        raw = bytearray()
        for chunk in response.iter_bytes():
            if len(raw) + len(chunk) > min(maximum_bytes, _MAX_ERROR_BYTES):
                return {}
            raw.extend(chunk)
        payload = parse_json(bytes(raw), maximum_bytes=_MAX_ERROR_BYTES)
        if type(payload) is not dict:
            return {}
    except (AdhelmError, ValueError, httpx.HTTPError):
        return {}
    code: str | None = None
    request_id: object = response.headers.get("request-id") or response.headers.get("x-request-id")
    if host == "googleads.googleapis.com":
        error = payload.get("error")
        details = error.get("details", []) if type(error) is dict else []
        if type(details) is list:
            for detail in details[:16]:
                if (
                    type(detail) is not dict
                    or detail.get("@type")
                    != "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure"
                ):
                    continue
                request_id = detail.get("requestId", request_id)
                errors = detail.get("errors", [])
                for item in errors[:16] if type(errors) is list else []:
                    codes = item.get("errorCode") if type(item) is dict else None
                    if type(codes) is dict:
                        for category, allowed in _GOOGLE_CODES.items():
                            if type(codes.get(category)) is str and codes[category] in allowed:
                                code = f"{category}.{codes[category]}"
                                break
                    if code:
                        break
    elif host == "graph.facebook.com":
        error = payload.get("error")
        if type(error) is dict:
            code = _number(error.get("code"))
            subcode = _number(error.get("error_subcode"))
            if code and subcode:
                code += ":" + subcode
            request_id = error.get("fbtrace_id", request_id)
    elif host == "api.linkedin.com":
        code = _number(payload.get("serviceErrorCode"))
        request_id = response.headers.get("x-li-uuid", request_id)
    elif host == "business-api.tiktok.com":
        code = _number(payload.get("code"))
        request_id = payload.get("request_id", request_id)
    else:
        error = payload.get("error")
        code = _number(error.get("code")) if type(error) is dict else _number(error)
        if (
            type(error) is dict
            and type(error.get("code")) is str
            and error["code"]
            in {
                "INVALID_ARGUMENT",
                "PERMISSION_DENIED",
                "UNAUTHENTICATED",
                "RESOURCE_EXHAUSTED",
                "NOT_FOUND",
                "INTERNAL",
                "UNAVAILABLE",
            }
        ):
            code = error["code"]
    result: dict[str, Any] = {}
    if code:
        result["provider_code"] = code
    identifier = _identifier(request_id, forbidden)
    if identifier:
        result["provider_request_id"] = identifier
    return result
