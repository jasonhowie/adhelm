"""Local client files are private inputs, never config or agent-visible payloads."""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE
from adhelm.google_oauth_setup import (
    GoogleOAuthProfileDraft,
    read_google_installed_client,
    read_google_oauth_draft,
)

CLIENT = "123456-fixture.apps.googleusercontent.com"

_REQUIRES_POSIX_MODES = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "O3.1: these cases drive POSIX mode bits and FIFOs. The Windows counterparts (DACL "
        "privacy, named pipes, reparse points) are in tests/test_windows_filesystem.py."
    ),
)


def draft_data() -> dict[str, object]:
    return {
        "profile_id": "ga4-read",
        "provider": "ga4",
        "account_ref": "12345",
        "client_ref": CLIENT,
        "scopes": ["https://www.googleapis.com/auth/analytics.readonly"],
        "credential_ref": "google-read-oauth",
        "currency": "USD",
    }


def client_file(tmp_path: Path, installed: object | None = None) -> Path:
    path = tmp_path / "private-google-client.json"
    if installed is None:
        installed = {"client_id": CLIENT, "client_secret": "fixture-private-client-value"}
    path.write_text(json.dumps({"installed": installed}))
    path.chmod(0o600)
    return path


def test_draft_has_no_pending_principal_or_secret_slot() -> None:
    draft = GoogleOAuthProfileDraft.model_validate(draft_data())
    assert "principal_ref" not in draft.model_dump()
    assert "client_secret" not in draft.model_dump()
    assert "credential_source" not in draft.model_dump()


def test_draft_schema_advertises_only_the_closed_provider_purpose_scope_map() -> None:
    schema = GoogleOAuthProfileDraft.model_json_schema()
    alternatives = schema["allOf"][0]["oneOf"]
    observed = {
        (
            choice["properties"]["provider"]["const"],
            choice["properties"]["purpose"]["const"],
            tuple(choice["properties"]["scopes"]["const"]),
            tuple(choice["required"]),
        )
        for choice in alternatives
    }
    expected = {
        (
            provider,
            purpose,
            (scope,),
            ("provider", "scopes")
            if purpose == "READ"
            else ("provider", "scopes", "purpose", "currency", "timezone")
            if provider == "google_ads"
            else ("provider", "scopes", "purpose"),
        )
        for (provider, purpose), scope in GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE.items()
    }

    assert observed == expected
    assert schema["properties"]["purpose"]["default"] == "READ"
    assert "purpose" not in schema["required"]
    assert not {"principal_ref", "credential_source", "client_secret"} & set(schema["properties"])


@pytest.mark.parametrize(
    ("provider", "purpose", "scope"),
    [
        (provider, purpose, scope)
        for (provider, purpose), scope in GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE.items()
    ],
)
def test_every_advertised_target_also_passes_runtime_draft_validation(
    provider: str, purpose: str, scope: str
) -> None:
    values = draft_data()
    values.update(
        {
            "provider": provider,
            "purpose": purpose,
            "scopes": [scope],
            "account_ref": "1234567890" if provider == "google_ads" else "12345",
        }
    )
    if provider == "google_ads":
        values["secondary_credential_ref"] = "google-ads-developer-token"
        if purpose == "EDIT":
            values["timezone"] = "UTC"

    draft = GoogleOAuthProfileDraft.model_validate(values)

    assert (draft.provider, draft.purpose, tuple(draft.scopes)) == (provider, purpose, (scope,))


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_draft_schema_version_is_exact(value: object) -> None:
    with pytest.raises(ValidationError):
        GoogleOAuthProfileDraft.model_validate({**draft_data(), "schema_version": value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scopes", ["openid"]),
        ("purpose", "EDIT"),
        ("account_ref", "not-a-property"),
        ("client_ref", "arbitrary-client"),
        ("principal_ref", "invented-subject"),
        ("currency", None),
        ("timezone", "not/a-zone"),
        ("secondary_credential_ref", "unexpected-slot"),
        ("credential_ref", "export-pseudonymization-v1"),
    ],
)
def test_draft_rejects_unsupported_target_before_side_effects(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        GoogleOAuthProfileDraft.model_validate({**draft_data(), field: value})


def test_private_client_input_does_not_expose_secret_in_representation(tmp_path: Path) -> None:
    result = read_google_installed_client(client_file(tmp_path), expected_client_ref=CLIENT)
    assert result.client_ref == CLIENT
    assert result.client_secret == "fixture-private-client-value"
    assert "fixture-private-client-value" not in repr(result)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o666])
@_REQUIRES_POSIX_MODES
def test_shared_client_file_is_rejected(tmp_path: Path, mode: int) -> None:
    path = client_file(tmp_path)
    path.chmod(mode)
    with pytest.raises(AdhelmError) as error:
        read_google_installed_client(path, expected_client_ref=CLIENT)
    assert error.value.code == "GOOGLE_OAUTH_CLIENT_FILE_INVALID"


def test_symlink_and_hardlink_clients_are_rejected(tmp_path: Path, make_link) -> None:
    path = client_file(tmp_path)
    alias = tmp_path / "alias.json"
    make_link(alias, path)
    with pytest.raises(AdhelmError):
        read_google_installed_client(alias, expected_client_ref=CLIENT)
    alias.unlink()
    alias.hardlink_to(path)
    with pytest.raises(AdhelmError):
        read_google_installed_client(path, expected_client_ref=CLIENT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_id", "different.apps.googleusercontent.com"),
        ("token_uri", "https://attacker.invalid/token"),
        ("auth_uri", "https://attacker.invalid/auth"),
        ("redirect_uris", ["https://attacker.invalid/callback"]),
        ("client_secret", None),
        ("unknown", "fixture-private-client-value"),
    ],
)
def test_untrusted_client_fields_fail_without_reflection(
    tmp_path: Path, field: str, value: object
) -> None:
    path = client_file(tmp_path, {"client_id": CLIENT, field: value})
    with pytest.raises(AdhelmError) as error:
        read_google_installed_client(path, expected_client_ref=CLIENT)
    rendered = str(error.value)
    assert "fixture-private-client-value" not in rendered
    assert "attacker.invalid" not in rendered
    assert error.value.__cause__ is None


def test_duplicate_keys_in_client_document_fail(tmp_path: Path) -> None:
    path = client_file(tmp_path)
    path.write_text('{"installed":{"client_id":"one","client_id":"two"}}')
    with pytest.raises(AdhelmError):
        read_google_installed_client(path, expected_client_ref=CLIENT)


@_REQUIRES_POSIX_MODES
def test_draft_fifo_is_rejected_without_waiting_for_a_writer(tmp_path: Path) -> None:
    path = tmp_path / "draft-pipe"
    os.mkfifo(path, mode=0o600)
    with pytest.raises(AdhelmError) as error:
        read_google_oauth_draft(path)
    assert error.value.code == "GOOGLE_OAUTH_DRAFT_INVALID"


def test_draft_symlink_is_rejected_and_regular_file_remains_usable(
    tmp_path: Path, make_link
) -> None:
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft_data()))
    link = tmp_path / "draft-link.json"
    make_link(link, path)
    with pytest.raises(AdhelmError):
        read_google_oauth_draft(link)
    assert read_google_oauth_draft(path).profile_id == "ga4-read"
