"""Google Ads local EDIT identity without invented OAuth scope separation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_google_oauth_edit_profile import (
    _CLIENT,
    _SUBJECT,
    _fake_lifecycle_store,
    _grant,
    _install,
    _private_client_file,
)

import adhelm.google_oauth_credentials as oauth_credentials
import adhelm.google_oauth_workflow as workflow
from adhelm.canonical import canonical_bytes
from adhelm.config import load_configuration
from adhelm.errors import AdhelmError
from adhelm.google_oauth_protocol import (
    GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE,
    GOOGLE_OIDC_CANONICAL_ISSUER,
    derive_google_oidc_principal_ref,
)
from adhelm.google_oauth_setup import GoogleOAuthProfileDraft
from adhelm.models import Profile

_SCOPE = "https://www.googleapis.com/auth/adwords"


def _draft_data(*, purpose: str = "EDIT") -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile_id": "google-ads-local-profile",
        "provider": "google_ads",
        "account_ref": "1234567890",
        "client_ref": _CLIENT,
        "scopes": [_SCOPE],
        "purpose": purpose,
        "currency": "USD",
        "timezone": "America/Chicago",
        "credential_ref": "google-ads-primary-oauth",
        "secondary_credential_ref": "google-ads-developer-token",
        "login_customer_ref": "9876543210",
    }


def _profile_data(*, purpose: str = "EDIT") -> dict[str, Any]:
    return {
        **_draft_data(purpose=purpose),
        "credential_source": "OS_OAUTH_GOOGLE",
        "principal_ref": derive_google_oidc_principal_ref(GOOGLE_OIDC_CANONICAL_ISSUER, _SUBJECT),
    }


def test_google_ads_edit_is_local_separation_with_same_exact_provider_scope() -> None:
    draft = GoogleOAuthProfileDraft.model_validate(_draft_data())
    profile = Profile.model_validate(_profile_data())
    assert draft.purpose == profile.purpose == "EDIT"
    assert profile.scopes == draft.scopes == [_SCOPE]
    assert GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("google_ads", "READ")] == _SCOPE
    assert GOOGLE_OAUTH_PROFILE_SCOPE_BY_PROVIDER_PURPOSE[("google_ads", "EDIT")] == _SCOPE
    assert profile.currency == "USD"
    assert profile.timezone == "America/Chicago"


@pytest.mark.parametrize("model", [GoogleOAuthProfileDraft, Profile])
@pytest.mark.parametrize("field", ["currency", "timezone"])
@pytest.mark.parametrize("omitted", [True, False])
def test_edit_expectations_cannot_be_omitted_or_null(
    model: type[GoogleOAuthProfileDraft] | type[Profile],
    field: str,
    omitted: bool,
) -> None:
    raw = _draft_data() if model is GoogleOAuthProfileDraft else _profile_data()
    if omitted:
        del raw[field]
    else:
        raw[field] = None
    with pytest.raises(ValidationError):
        model.model_validate(raw)


@pytest.mark.parametrize("model", [GoogleOAuthProfileDraft, Profile])
def test_google_ads_read_retains_legacy_defaults_and_canonical_bytes(
    model: type[GoogleOAuthProfileDraft] | type[Profile],
) -> None:
    raw = (
        _draft_data(purpose="READ")
        if model is GoogleOAuthProfileDraft
        else _profile_data(purpose="READ")
    )
    del raw["currency"]
    del raw["timezone"]
    defaulted = model.model_validate(raw)
    explicit = model.model_validate({**raw, "currency": None, "timezone": "UTC"})
    assert defaulted.currency is None
    assert defaulted.timezone == "UTC"
    assert canonical_bytes(defaulted) == canonical_bytes(explicit)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_ref", "123-456-7890"),
        ("login_customer_ref", "987-654-3210"),
        ("secondary_credential_ref", None),
        ("secondary_credential_ref", "google-ads-primary-oauth"),
        ("secondary_credential_ref", "export-pseudonymization-v1"),
        ("timezone", "Invalid/Timezone"),
        ("scopes", [_SCOPE, "openid"]),
        ("purpose", "ACTIVATE"),
    ],
)
def test_edit_profile_rejects_unsupported_targets_and_credential_aliases(
    field: str,
    value: object,
) -> None:
    with pytest.raises((ValidationError, AdhelmError)) as raised:
        Profile.model_validate({**_profile_data(), field: value})
    if isinstance(raised.value, AdhelmError):
        assert raised.value.code == "SENSITIVE_DATA_RISK"


def test_draft_and_profile_schemas_require_explicit_edit_expectations() -> None:
    draft_schema = GoogleOAuthProfileDraft.model_json_schema()
    branch = next(
        item
        for item in draft_schema["allOf"][0]["oneOf"]
        if item["properties"]["provider"]["const"] == "google_ads"
        and item["properties"]["purpose"]["const"] == "EDIT"
    )
    assert {"purpose", "currency", "timezone"} <= set(branch["required"])
    assert branch["properties"]["currency"]["type"] == "string"
    profile_schema = Profile.model_json_schema()
    condition = profile_schema["allOf"][0]
    assert condition["if"]["properties"]["credential_source"]["const"] == "OS_OAUTH_GOOGLE"
    assert condition["then"]["required"] == ["currency", "timezone"]
    assert condition["then"]["properties"]["currency"]["type"] == "string"


@pytest.mark.parametrize(
    ("stored_purpose", "requested_purpose"), [("READ", "EDIT"), ("EDIT", "READ")]
)
def test_identical_provider_scope_does_not_allow_cross_purpose_bundle_use(
    monkeypatch: pytest.MonkeyPatch,
    stored_purpose: str,
    requested_purpose: str,
) -> None:
    stored = Profile.model_validate(_profile_data(purpose=stored_purpose))
    requested = Profile.model_validate(_profile_data(purpose=requested_purpose))
    assert stored.scopes == requested.scopes
    store, _ = _install(monkeypatch, stored)
    writes = store.writes
    with pytest.raises(AdhelmError) as raised:
        oauth_credentials.resolve_google_oauth_access_token(requested)
    assert raised.value.code == "GOOGLE_OAUTH_CREDENTIAL_INVALID"
    assert store.writes == writes


def test_fake_edit_enrollment_retains_local_purpose_without_claiming_account_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_lifecycle_store(monkeypatch)
    monkeypatch.setattr(workflow, "_require_interactive", lambda _: None)
    profile = Profile.model_validate(_profile_data())
    monkeypatch.setattr(workflow, "_browser_grant", lambda *_: _grant(profile))
    request = tmp_path / "edit-profile.json"
    request.write_text(json.dumps(_draft_data()))
    result = workflow.enroll_google_oauth(
        tmp_path,
        request,
        _private_client_file(tmp_path),
        interactive=True,
    )
    stored = load_configuration(tmp_path).profiles[0]
    assert stored.purpose == "EDIT"
    assert stored.currency == "USD"
    assert stored.timezone == "America/Chicago"
    assert result["resource_access_verified"] is False
    assert result["execution_authority"] is False
    assert result["secondary_credential_status"] == "REQUIRED_NOT_EVALUATED"
    assert result["credential_state"] == "PRIMARY_OAUTH_READY_SECONDARY_UNVERIFIED"
