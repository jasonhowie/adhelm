"""Nonsecret Google enrollment draft and private installed-client file input."""

from __future__ import annotations

import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict, Field, field_validator, model_validator
from pydantic.config import JsonDict, JsonValue

from adhelm.canonical import parse_json
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    is_google_oauth_client_id,
)
from adhelm.models import Currency, SafeRef, StrictModel, is_reserved_credential_reference
from adhelm.strict_json import validate_json_input

if sys.platform == "win32":
    from adhelm import windows_filesystem


def _google_oauth_target_schema() -> JsonDict:
    """Expose only the source-backed provider/purpose/scope combinations.

    The draft keeps ``purpose=READ`` as a backward-compatible default.  Its
    READ alternatives therefore allow purpose to be absent, while EDIT
    alternatives require it explicitly.  Runtime validation still rechecks
    this same protocol mapping before any client file, credential store, or
    browser handoff.
    """
    alternatives: list[JsonValue] = []
    for (provider, purpose), scope in GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE.items():
        required: list[JsonValue] = ["provider", "scopes"]
        if purpose != "READ":
            required.append("purpose")
        provider_constraint: JsonDict = {"const": provider}
        purpose_constraint: JsonDict = {"const": purpose}
        scope_constraint: JsonDict = {"const": [scope]}
        properties: JsonDict = {
            "provider": provider_constraint,
            "purpose": purpose_constraint,
            "scopes": scope_constraint,
        }
        if provider == "google_ads" and purpose == "EDIT":
            required.extend(["currency", "timezone"])
            properties["currency"] = {"type": "string"}
        alternatives.append(
            {
                "properties": properties,
                "required": required,
            }
        )
    return {"allOf": [{"oneOf": alternatives}]}


class GoogleOAuthProfileDraft(StrictModel):
    """Requested resource/profile, with subject derived only after verified OIDC."""

    model_config = ConfigDict(json_schema_extra=_google_oauth_target_schema())
    schema_version: Literal[1] = 1
    profile_id: SafeRef
    provider: Literal["google_ads", "ga4", "ga4_admin", "gtm"]
    account_ref: SafeRef
    client_ref: SafeRef
    scopes: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        min_length=1, max_length=1
    )
    purpose: Literal["READ", "EDIT"] = "READ"
    currency: Currency | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=80)] = "UTC"
    credential_ref: SafeRef
    login_customer_ref: str | None = Field(default=None, pattern=r"^[0-9]{10}$")

    @model_validator(mode="before")
    @classmethod
    def explicit_google_ads_edit_expectations(cls, value: Any) -> Any:
        if isinstance(value, dict) and "secondary_credential_ref" in value:
            # A draft written for AdHelm 0.1.0 names a developer-token reference. Google
            # retired developer tokens on 2026-09-09: the value is dropped, never carried
            # into the profile and never read.
            value = {k: v for k, v in value.items() if k != "secondary_credential_ref"}
        if (
            isinstance(value, dict)
            and value.get("provider") == "google_ads"
            and value.get("purpose") == "EDIT"
            and not {"currency", "timezone"} <= value.keys()
        ):
            raise ValueError("Google Ads EDIT requires explicit currency and timezone")
        return value

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_schema_version(cls, value: object) -> int:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be the integer 1")
        return value

    @model_validator(mode="after")
    def closed_google_target(self) -> GoogleOAuthProfileDraft:
        expected_scope = GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE.get(
            (self.provider, self.purpose)
        )
        if expected_scope is None or self.scopes != [expected_scope]:
            raise ValueError("The selected profile requires its exact API scope")
        if not is_google_oauth_client_id(self.client_ref):
            raise ValueError("A Google installed client ID is required")
        account_pattern = r"[0-9]{10}" if self.provider == "google_ads" else r"[0-9]{1,20}"
        if re.fullmatch(account_pattern, self.account_ref) is None:
            raise ValueError("The provider resource ID is invalid")
        if is_reserved_credential_reference(self.credential_ref):
            raise ValueError("The credential slot requires a nonreserved reference")
        if self.provider == "google_ads":
            if self.purpose == "EDIT" and self.currency is None:
                raise ValueError("Google Ads EDIT requires the declared account currency")
        elif self.login_customer_ref is not None:
            raise ValueError("Only Google Ads supports the manager login customer field")
        if self.provider in {"ga4", "ga4_admin"} and self.currency is None:
            raise ValueError("GA4 requires the declared property currency")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone must be an IANA timezone") from None
        return self


@dataclass(frozen=True, repr=False)
class GoogleInstalledClient:
    client_ref: str = field(repr=False)
    client_secret: str | None = field(repr=False)


def _safe_input_flags() -> int:
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    nonblocking = getattr(os, "O_NONBLOCK", 0)
    if (
        type(no_follow) is not int
        or not no_follow
        or type(nonblocking) is not int
        or not nonblocking
    ):
        raise ValueError("Safe local input checking unavailable")
    return os.O_RDONLY | no_follow | nonblocking


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if not callable(getuid):
        raise ValueError("Current local user is unavailable")
    value = getuid()
    if type(value) is not int:
        raise ValueError("Current local user is unavailable")
    return value


def _open_checked_input(path: Path, *, private: bool, maximum_bytes: int) -> int:
    """Open one bounded regular file for reading and return its descriptor.

    Nothing is read before the checks pass. ``private`` additionally requires a
    single-link file reachable only by the current OS user: owner and mode on
    POSIX, owner and DACL on Windows.
    """
    if sys.platform == "win32":
        descriptor = windows_filesystem.open_regular_file(os.fspath(path), private=private)
        try:
            state = windows_filesystem.fd_state(descriptor)
            if state.size > maximum_bytes or (private and state.links != 1):
                raise ValueError("Bounded regular input file required")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    if private and (os.name != "posix" or not hasattr(os, "O_NOFOLLOW")):
        raise ValueError("Private client-file checking unavailable")
    descriptor = os.open(path, _safe_input_flags())
    try:
        status = os.fstat(descriptor)
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_size > maximum_bytes
            or (
                private
                and (
                    status.st_uid != _current_uid()
                    or stat.S_IMODE(status.st_mode) & 0o077
                    or status.st_nlink != 1
                )
            )
        ):
            raise ValueError("Bounded regular input file required")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_google_oauth_draft(path: Path) -> GoogleOAuthProfileDraft:
    """Read bounded ordinary JSON without blocking on pipes or following links."""
    descriptor: int | None = None
    try:
        descriptor = _open_checked_input(path, private=False, maximum_bytes=1_048_576)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return validate_json_input(GoogleOAuthProfileDraft, handle.read(1_048_577))
    except Exception:
        raise AdhelmError(
            "GOOGLE_OAUTH_DRAFT_INVALID",
            "A bounded regular JSON file matching the Google OAuth draft schema is required.",
            exit_code=2,
            next_action="Inspect schema google-oauth-profile-draft and correct the local input.",
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def read_google_installed_client(path: Path, *, expected_client_ref: str) -> GoogleInstalledClient:
    """Read a caller-selected private desktop client JSON file, without retaining it.

    Downloaded endpoint metadata is checked and never used to route any request.
    Web/service-account client documents and secret input through JSON CLI output
    are unsupported. The file must be a single-link regular file private to the
    current OS user: owner and mode on POSIX, owner and DACL on Windows.
    """
    descriptor: int | None = None
    try:
        descriptor = _open_checked_input(path, private=True, maximum_bytes=32_768)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            document = parse_json(handle.read(32_769), maximum_bytes=32_768)
        if type(document) is not dict or set(document) != {"installed"}:
            raise ValueError("Installed client document required")
        client = document["installed"]
        allowed = {
            "client_id",
            "client_secret",
            "project_id",
            "auth_uri",
            "token_uri",
            "auth_provider_x509_cert_url",
            "redirect_uris",
        }
        if type(client) is not dict or not set(client) <= allowed:
            raise ValueError("Unexpected client fields")
        client_ref = client.get("client_id")
        if (
            type(client_ref) is not str
            or not is_google_oauth_client_id(client_ref)
            or client_ref != expected_client_ref
        ):
            raise ValueError("Client identity differs")
        for name, endpoint in {
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        }.items():
            # Google's downloaded installed-client metadata uses the original
            # auth URI; runtime always selects the current fixed v2 endpoint.
            if name in client and client[name] != endpoint:
                raise ValueError("Unexpected endpoint metadata")
        if "project_id" in client and (
            type(client["project_id"]) is not str
            or re.fullmatch(r"[a-z][a-z0-9-]{4,61}[a-z0-9]", client["project_id"]) is None
        ):
            raise ValueError("Invalid project metadata")
        if "redirect_uris" in client and (
            type(client["redirect_uris"]) is not list
            or not 1 <= len(client["redirect_uris"]) <= 4
            or any(
                uri not in {"http://localhost", "http://127.0.0.1"}
                for uri in client["redirect_uris"]
            )
        ):
            raise ValueError("Unsupported redirect metadata")
        secret = client.get("client_secret")
        if secret is not None and (
            type(secret) is not str
            or not 1 <= len(secret) <= 4096
            or any(ord(c) < 33 or ord(c) > 126 for c in secret)
        ):
            raise ValueError("Invalid client secret")
        if "client_secret" in client and secret is None:
            raise ValueError("Explicit null secret is invalid")
        return GoogleInstalledClient(client_ref=client_ref, client_secret=secret)
    except Exception:
        raise AdhelmError(
            "GOOGLE_OAUTH_CLIENT_FILE_INVALID",
            "A private Google desktop-client JSON file matching the draft is required.",
            exit_code=2,
            next_action=(
                "Use your own installed-app client JSON, restrict it to your OS user, "
                "and keep its contents out of chat."
            ),
        ) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
