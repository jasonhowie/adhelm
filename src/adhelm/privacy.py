"""Bounded fail-closed data screening and export pseudonymization.

Screening supplements closed schemas. It is not a claim to detect every sensitive meaning.
"""

import hashlib
import hmac
import re
import unicodedata
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from adhelm.errors import AdhelmError

_FORBIDDEN_KEYS = frozenset(
    {
        "email",
        "phone",
        "password",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "authorization",
        "cookie",
        "cookies",
        "client_secret",
        "private_key",
        "user_id",
        "user_data",
        "client_id",
        "contact_id",
        "lead_id",
        "customer_id",
        "patient_id",
        "gclid",
        "fbclid",
        "ttclid",
        "msclkid",
        "raw_response",
        "raw_request",
        "dom_text",
        "page_text",
        "form_values",
        "html",
        "javascript",
        "custom_html",
        "audience_file",
        "ssn",
        "health_condition",
        "financial_hardship",
        "underwriting",
        "eligibility",
    }
)
# Start only at a whitespace/@-delimited run boundary.  A legacy match that
# begins later in such a run can also begin at that boundary, because its
# local-part accepts the preceding non-whitespace, non-@ characters.  The
# boundary prevents a failed no-@ candidate from being retried at every
# character of a long run.
_EMAIL = re.compile(r"(?<![^\s@])[^\s@]+@[^\s@]+\.[^\s@]+")
_SECRET = re.compile(
    r"(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY|\bBearer\s+\S+|"
    r"\b(?:sk-[a-zA-Z0-9_-]{16,}|AKIA[A-Z0-9]{16})\b|"
    r"(?:access_token|refresh_token|api_key|client_secret)\s*[=:])"
)
_PHONE = re.compile(r"(?<!\d)(?:\+\d{1,3}[ .-])?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?!\d)")
_SENSITIVE_PATH = re.compile(
    r"(?i)(?:^|/)(?:login|patient|patients|member|members|"
    r"account|accounts|eligibility|underwriting|care|checkout)(?:/|$)"
)
_UTMS = frozenset({"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"})


def validate_data(value: Any, *, depth: int = 0) -> None:
    if depth > 32:
        raise AdhelmError("INPUT_TOO_DEEP", "Data nesting exceeds the allowed depth.", exit_code=2)
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = unicodedata.normalize("NFKC", str(key)).lower().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS:
                raise AdhelmError(
                    "SENSITIVE_DATA_RISK", "Prohibited data field detected.", exit_code=2
                )
            validate_data(child, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 100_000:
            raise AdhelmError(
                "INPUT_TOO_LARGE", "Collection exceeds the allowed size.", exit_code=2
            )
        for child in value:
            validate_data(child, depth=depth + 1)
    elif isinstance(value, str):
        normalized = unicodedata.normalize("NFKC", value)
        if _EMAIL.search(normalized) or _SECRET.search(normalized) or _PHONE.search(normalized):
            raise AdhelmError(
                "SENSITIVE_DATA_RISK", "Prohibited sensitive text detected.", exit_code=2
            )


def opaque_ref(key: bytes, value: str) -> str:
    if len(key) < 32:
        raise AdhelmError(
            "EXPORT_KEY_REQUIRED", "Export pseudonymization needs a local 32-byte key."
        )
    return "opaque:" + hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def validate_destination(url: str, *, allowed_hosts: set[str], allowed_paths: set[str]) -> str:
    validate_data(url)
    try:
        parts = urlsplit(url)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise AdhelmError(
            "INVALID_DESTINATION", "Destination URL is invalid.", exit_code=2
        ) from exc
    if (
        parts.scheme != "https"
        or host not in allowed_hosts
        or port not in (None, 443)
        or parts.username
        or parts.password
        or parts.fragment
        or "\\" in url
        or any(ord(c) < 32 for c in url)
    ):
        raise AdhelmError(
            "UNAPPROVED_EGRESS", "Destination is outside the declared HTTPS contract."
        )
    path = parts.path or "/"
    for _ in range(3):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if (
        path not in allowed_paths
        or _SENSITIVE_PATH.search(path)
        or any(segment in {".", ".."} for segment in path.split("/"))
        or "%" in path
    ):
        raise AdhelmError("SENSITIVE_DATA_RISK", "Destination path is undeclared or sensitive.")
    try:
        params = parse_qsl(
            parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=5
        )
    except ValueError as exc:
        raise AdhelmError("INVALID_DESTINATION", "Invalid controlled campaign parameters.") from exc
    keys = [k for k, _ in params]
    if len(keys) != len(set(keys)):
        raise AdhelmError("INVALID_DESTINATION", "Duplicate campaign parameters are prohibited.")
    for name, value in params:
        if name not in _UTMS or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", value):
            raise AdhelmError("SENSITIVE_DATA_RISK", "Only declared controlled UTMs are permitted.")
        validate_data(value)
    return url
