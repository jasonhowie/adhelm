"""Explicit fixed-host HTTP transport with bounded reads and no mutation retry."""

import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from adhelm.errors import AdhelmError
from adhelm.privacy import validate_data
from adhelm.provider_diagnostics import provider_error_details

_MAXIMUM_DISCLOSED_RETRY_AFTER_SECONDS = 3_600


def _retry_after_seconds(value: str | None) -> int | None:
    """Parse a bounded whole-second `Retry-After` hint, or return None.

    Only the documented delay-seconds form is read.  An HTTP-date form, a
    negative value, a non-numeric value, or an absent header yields ``None``
    rather than a guess, and the result is clamped so that a hostile header
    cannot turn a diagnostic into an unbounded wait instruction.  Nothing from
    the response body is read here.

    ``str.isdigit`` is not the set ``int`` accepts: ``"\u00b2"`` is a legal
    latin-1 header byte that ``isdigit`` calls a digit and ``int`` rejects, so
    reading the header with ``isdigit`` alone raises an unsanitized
    ``ValueError`` on every response.  ``str.isdecimal`` alone is no better --
    it admits Arabic-Indic and other non-ASCII decimal digits that ``int``
    would silently accept -- so the ASCII bound is explicit here.
    """
    if value is None or not value.isascii() or not value.isdigit() or len(value) > 12:
        return None
    return min(int(value), _MAXIMUM_DISCLOSED_RETRY_AFTER_SECONDS)


def _validate_headers(headers: dict[str, str] | None) -> None:
    """Reject malformed explicit headers before HTTP construction or transmission."""
    if headers is None:
        return
    if type(headers) is not dict or len(headers) > 32:
        raise AdhelmError(
            "INVALID_REQUEST_HEADERS", "Provider request headers are invalid.", exit_code=2
        )
    seen: set[str] = set()
    for name, value in headers.items():
        if (
            type(name) is not str
            or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", name) is None
            or name.lower() in seen
            or type(value) is not str
            or not 1 <= len(value) <= 16_512
            or any(not 32 <= ord(character) <= 126 for character in value)
        ):
            raise AdhelmError(
                "INVALID_REQUEST_HEADERS", "Provider request headers are invalid.", exit_code=2
            )
        seen.add(name.lower())


_ERROR_BODY_LIMIT = 65_536
_ERROR_BODY_DEPTH = 8


def _allowlisted_error_code(response: httpx.Response, allowed: frozenset[str]) -> str | None:
    """Return the one allowlisted provider error code in a bounded refusal body.

    Only a caller-supplied closed set of constants can ever be returned, so no
    body text, message, or identifier is recorded.  A body that is oversized,
    not JSON, or that names none or several of the allowed codes yields
    ``None`` and the refusal stays classified by its HTTP status alone.
    """
    if not allowed:
        return None
    data = bytearray()
    try:
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > _ERROR_BODY_LIMIT:
                return None
        decoded = json.loads(bytes(data))
    except Exception:
        # Total on purpose: a malformed, truncated or absurdly nested body (for example
        # sixty thousand open brackets, which raises RecursionError) is "no code".
        return None
    found: set[str] = set()

    def walk(node: object, depth: int) -> None:
        if depth > _ERROR_BODY_DEPTH or len(found) > 1:
            return
        if isinstance(node, dict):
            code = node.get("errorCode")
            if isinstance(code, dict):
                found.update(
                    value for value in code.values() if isinstance(value, str) and value in allowed
                )
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(decoded, 0)
    return next(iter(found)) if len(found) == 1 else None


def _refusal_details(
    response: httpx.Response, allowed: frozenset[str], maximum_bytes: int
) -> dict[str, Any]:
    """Read the stream once for project classification and screened diagnostics."""
    if not allowed:
        return provider_error_details(response, maximum_bytes=maximum_bytes)
    raw = bytearray()
    try:
        for chunk in response.iter_bytes():
            if len(raw) + len(chunk) > min(maximum_bytes, _ERROR_BODY_LIMIT):
                return {}
            raw.extend(chunk)
    except (httpx.HTTPError, ValueError, RuntimeError):
        return {}
    # iter_bytes already decoded content. Copy only diagnostic headers, never
    # content-encoding, so neither parser decodes the cached bytes a second time.
    cached = httpx.Response(
        response.status_code,
        content=bytes(raw),
        request=response.request,
        headers={
            name: response.headers[name]
            for name in ("request-id", "x-request-id", "x-li-uuid")
            if name in response.headers
        },
    )
    details = provider_error_details(cached, maximum_bytes=maximum_bytes)
    code = _allowlisted_error_code(cached, allowed)
    if code is not None:
        details["provider_error_code"] = code
        details.setdefault("provider_code", code)
    return details


class SafeHttpClient:
    def __init__(
        self,
        client: httpx.Client,
        allowed_hosts: set[str],
        *,
        maximum_response_bytes: int = 8_388_608,
        read_attempts: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not allowed_hosts or not 1 <= read_attempts <= 3:
            raise AdhelmError(
                "TRANSPORT_CONFIG_INVALID", "Explicit hosts and bounded retries required."
            )
        self.client = client
        self.allowed_hosts = frozenset(allowed_hosts)
        self.maximum_response_bytes = maximum_response_bytes
        self.read_attempts = read_attempts
        self.sleep = sleep

    #: The one bounded multipart image upload this transport performs.  It
    #: exists for a documented provider route that accepts *only* multipart
    #: form data for a binary file (OpenAI Ads ``POST /v1/upload``), which
    #: neither ``json=`` nor the urlencoded ``form=`` body can express.  It is
    #: deliberately not a general file-upload API: exactly one part, a fixed
    #: field name, a neutral generated filename, a declared image media type,
    #: an in-memory byte bound, no caller-set content headers, and single-shot
    #: mutation semantics with no retry, exactly like ``request(mutation=True)``.
    MAX_UPLOAD_BYTES = 5 * 1024 * 1024
    UPLOAD_MEDIA_TYPES = frozenset({"image/png", "image/jpeg"})

    def upload_image(
        self,
        url: str,
        *,
        headers: dict[str, str],
        field_name: str,
        filename: str,
        media_type: str,
        content: bytes,
    ) -> httpx.Response:
        """POST exactly one bounded image part, once, with no retry."""
        _validate_headers(headers)
        if (
            type(field_name) is not str
            or re.fullmatch(r"[a-z][a-z0-9_]{0,31}", field_name) is None
            or type(filename) is not str
            or re.fullmatch(r"[a-z][a-z0-9]{0,31}\.(png|jpg|jpeg)", filename) is None
            or type(media_type) is not str
            or media_type not in self.UPLOAD_MEDIA_TYPES
            or type(content) is not bytes
            or not 1 <= len(content) <= self.MAX_UPLOAD_BYTES
            or self.client.params
            or any(
                name.lower() in {"content-type", "content-length", "transfer-encoding"}
                for name in (*self.client.headers, *headers)
            )
        ):
            raise AdhelmError(
                "INVALID_REQUEST_UPLOAD", "Fixed mutation upload data is invalid.", exit_code=2
            )
        try:
            parsed = urlsplit(url)
            allowed = (
                parsed.scheme == "https"
                and parsed.hostname in self.allowed_hosts
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and not parsed.query
                and "\\" not in url
                and not any(ord(character) < 32 for character in url)
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise AdhelmError(
                "UNAPPROVED_EGRESS", "HTTP destination is outside the adapter contract."
            )
        try:
            with self.client.stream(
                "POST",
                url,
                headers=headers,
                files={field_name: (filename, content, media_type)},
                follow_redirects=False,
                timeout=30.0,
            ) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise AdhelmError("UNAPPROVED_EGRESS", "Provider redirects are prohibited.")
                disclosed = _retry_after_seconds(response.headers.get("retry-after"))
                if status in {401, 403}:
                    raise AdhelmError(
                        "PROVIDER_ACCESS_REQUIRED",
                        "Provider denied this profile.",
                        next_action="Reauthorize locally with the required scope.",
                        status_code=status,
                        retry_after_seconds=disclosed,
                        **provider_error_details(
                            response, maximum_bytes=self.maximum_response_bytes
                        ),
                    )
                if status >= 500:
                    raise AdhelmError(
                        "AMBIGUOUS_REMOTE_STATE",
                        "Provider write outcome requires reconciliation.",
                        exit_code=6,
                        status_code=status,
                        retry_after_seconds=disclosed,
                        **provider_error_details(
                            response, maximum_bytes=self.maximum_response_bytes
                        ),
                    )
                if status >= 400:
                    raise AdhelmError(
                        "PROVIDER_REQUEST_FAILED",
                        "Provider rejected the fixed operation.",
                        status_code=status,
                        retry_after_seconds=disclosed,
                        **provider_error_details(
                            response, maximum_bytes=self.maximum_response_bytes
                        ),
                    )
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > self.maximum_response_bytes:
                        raise AdhelmError(
                            "RESPONSE_TOO_LARGE", "Provider response exceeds the byte limit."
                        )
                    data.extend(chunk)
                return httpx.Response(
                    status, content=bytes(data), headers={"content-type": "application/json"}
                )
        except httpx.HTTPError as exc:
            raise AdhelmError(
                "AMBIGUOUS_REMOTE_STATE",
                "Write transport interrupted; reconcile before any new plan.",
                exit_code=6,
            ) from exc

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        form: dict[str, str] | None = None,
        mutation: bool = False,
        opaque_read_query: bytes | None = None,
        classifiable_error_codes: frozenset[str] = frozenset(),
    ) -> httpx.Response:
        _validate_headers(headers)
        validate_data(json)
        validate_data(params)
        if form is not None:
            # Only fixed adapter-owned POST mutations use form encoding. Do not
            # admit sequences, files, mixed body encodings or hidden query data.
            if (
                method != "POST"
                or mutation is not True
                or json is not None
                or params is not None
                or opaque_read_query is not None
                or self.client.params
                or type(form) is not dict
                or not 1 <= len(form) <= 32
                or any(
                    type(name) is not str
                    or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", name) is None
                    or type(value) is not str
                    or len(value) > 4096
                    or any(not 32 <= ord(character) <= 126 for character in value)
                    for name, value in form.items()
                )
                or sum(len(name) + len(value) for name, value in form.items()) > 16_384
                or any(
                    name.lower() in {"content-type", "content-length", "transfer-encoding"}
                    for name in (*self.client.headers, *(headers or {}))
                )
            ):
                raise AdhelmError(
                    "INVALID_REQUEST_FORM", "Fixed mutation form data is invalid.", exit_code=2
                )
            validate_data(form)
            form = dict(form)
        try:
            parsed = urlsplit(url)
            allowed = (
                parsed.scheme == "https"
                and parsed.hostname in self.allowed_hosts
                and parsed.port in (None, 443)
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and not parsed.query
                and "\\" not in url
                and not any(ord(c) < 32 for c in url)
            )
        except ValueError:
            allowed = False
        if not allowed:
            raise AdhelmError(
                "UNAPPROVED_EGRESS", "HTTP destination is outside the adapter contract."
            )
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise AdhelmError("UNSUPPORTED_METHOD", "HTTP method is not supported.")
        request_url: str | httpx.URL = url
        if opaque_read_query is not None:
            if (
                method != "GET"
                or mutation
                or params is not None
                or json is not None
                or self.client.params
                or not opaque_read_query
                or len(opaque_read_query) > 4096
                or any(byte < 33 or byte > 126 for byte in opaque_read_query)
                or b"#" in opaque_read_query
                or b"\\" in opaque_read_query
            ):
                raise AdhelmError(
                    "INVALID_CURSOR_CONTEXT", "Opaque cursors require a fixed bounded GET read."
                )
            request_url = httpx.URL(url).copy_with(query=opaque_read_query)
            if request_url.query != opaque_read_query:
                raise AdhelmError(
                    "INVALID_CURSOR_CONTEXT", "Opaque read cursor cannot be preserved."
                )
        # POST is also used for fixed aggregate reads; callers must mark operation semantics.
        attempts = 1 if mutation else self.read_attempts
        for attempt in range(attempts):
            try:
                with self.client.stream(
                    method,
                    request_url,
                    headers=headers,
                    json=json,
                    params=params,
                    data=form,
                    follow_redirects=False,
                    timeout=30.0,
                ) as response:
                    status = response.status_code
                    if 300 <= status < 400:
                        raise AdhelmError("UNAPPROVED_EGRESS", "Provider redirects are prohibited.")
                    # Attach the observed status and the bounded retry hint so
                    # that a provider adapter can classify a refusal exactly.
                    # Neither value comes from the response body.
                    disclosed = _retry_after_seconds(response.headers.get("retry-after"))
                    if status in {401, 403}:
                        raise AdhelmError(
                            "PROVIDER_ACCESS_REQUIRED",
                            "Provider denied this profile.",
                            next_action="Reauthorize locally with the required scope.",
                            status_code=status,
                            retry_after_seconds=disclosed,
                            **_refusal_details(
                                response, classifiable_error_codes, self.maximum_response_bytes
                            ),
                        )
                    if status in {429, 502, 503, 504} and not mutation and attempt < attempts - 1:
                        # The same ASCII-digit bound as `_retry_after_seconds`:
                        # a header `str.isdigit` accepts but `float` rejects
                        # would otherwise raise an unsanitized `ValueError`
                        # here instead of falling back to the fixed delay.
                        hint = _retry_after_seconds(response.headers.get("retry-after"))
                        delay = min(float(hint), 10.0) if hint is not None else 1.0
                        self.sleep(delay)
                        continue
                    if status >= 400:
                        if mutation and status >= 500:
                            raise AdhelmError(
                                "AMBIGUOUS_REMOTE_STATE",
                                "Provider write outcome requires reconciliation.",
                                exit_code=6,
                                status_code=status,
                                retry_after_seconds=disclosed,
                                **provider_error_details(
                                    response, maximum_bytes=self.maximum_response_bytes
                                ),
                            )
                        raise AdhelmError(
                            "PROVIDER_REQUEST_FAILED",
                            "Provider rejected the fixed operation.",
                            status_code=status,
                            retry_after_seconds=disclosed,
                            **provider_error_details(
                                response, maximum_bytes=self.maximum_response_bytes
                            ),
                        )
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if len(data) + len(chunk) > self.maximum_response_bytes:
                            raise AdhelmError(
                                "RESPONSE_TOO_LARGE", "Provider response exceeds the byte limit."
                            )
                        data.extend(chunk)
                    return httpx.Response(
                        status, content=bytes(data), headers={"content-type": "application/json"}
                    )
            except httpx.HTTPError as exc:
                if mutation:
                    raise AdhelmError(
                        "AMBIGUOUS_REMOTE_STATE",
                        "Write transport interrupted; reconcile before any new plan.",
                        exit_code=6,
                    ) from exc
                if attempt == attempts - 1:
                    raise AdhelmError(
                        "PROVIDER_UNAVAILABLE", "Provider read transport failed."
                    ) from exc
                self.sleep(float(attempt + 1))
        raise AdhelmError("PROVIDER_UNAVAILABLE", "Provider read attempts exhausted.")
