from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from adhelm import inventory
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_profiles import (
    MetaAccountTokenProfileDraft,
    finalize_meta_account_token_profile,
    validate_meta_account_token_profile_draft,
)
from adhelm.models import (
    Configuration,
    MetaAccountTokenProfile,
    Profile,
    meta_token_credential_reference,
    validate_configured_profile,
)


def _draft(**updates: object) -> MetaAccountTokenProfileDraft:
    values: dict[str, object] = {
        "profile_id": "meta-read",
        "account_ref": "12345",
        "client_application_ref": "67890",
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "READ",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
    }
    values.update(updates)
    return MetaAccountTokenProfileDraft.model_validate(values)


def test_finalized_profile_has_one_derived_private_bundle_and_exact_scope() -> None:
    profile = finalize_meta_account_token_profile(_draft())

    assert profile.credential_source == "OS_META_TOKEN"
    assert profile.credential_ref == meta_token_credential_reference("meta-read")
    assert profile.required_scope == "ads_read"
    assert profile.model_dump(mode="json") == {
        "schema_version": 1,
        "profile_kind": "META_ADS_ACCOUNT_TOKEN",
        "provider": "meta_ads",
        "profile_id": "meta-read",
        "account_ref": "12345",
        "client_application_ref": "67890",
        "currency": "USD",
        "timezone": "UTC",
        "purpose": "READ",
        "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
        "credential_source": "OS_META_TOKEN",
        "credential_ref": meta_token_credential_reference("meta-read"),
    }
    assert Configuration(profiles=[profile]).profiles == [profile]


def test_edit_profile_derives_ads_management_without_a_caller_scope_field() -> None:
    profile = finalize_meta_account_token_profile(
        _draft(
            profile_id="meta-edit",
            purpose="EDIT",
            inspector_credential_kind="CUSTOMER_APP_DEVELOPER_USER_TOKEN",
        )
    )

    assert profile.required_scope == "ads_management"
    assert "required_scope" not in profile.model_dump(mode="json")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("account_ref", "act_123"),
        ("account_ref", "0123"),
        ("client_application_ref", "١٢٣"),
        ("currency", "usd"),
        ("timezone", "not/an-iana-zone"),
        ("inspector_credential_kind", "APP_SECRET"),
        ("unexpected", "value"),
    ],
)
def test_draft_rejects_unclosed_or_noncanonical_fields(field: str, value: object) -> None:
    raw = _draft().model_dump(mode="python")
    raw[field] = value
    with pytest.raises(ValidationError):
        MetaAccountTokenProfileDraft.model_validate(raw)


def test_profile_rejects_caller_selected_or_constructed_bundle_reference() -> None:
    profile = finalize_meta_account_token_profile(_draft())
    raw = profile.model_dump(mode="python")
    raw["credential_ref"] = "customer-selected-slot"
    with pytest.raises(ValidationError):
        MetaAccountTokenProfile.model_validate(raw)

    forged = profile.model_copy(update={"credential_ref": "customer-selected-slot"})
    with pytest.raises(ValidationError):
        validate_configured_profile(forged)


def test_generic_profiles_cannot_select_a_derived_meta_bundle_slot() -> None:
    with pytest.raises(ValidationError):
        Profile(
            profile_id="generic",
            provider="meta_ads",
            account_ref="123",
            principal_ref="principal",
            client_ref="client",
            scopes=["ads_read"],
            credential_ref=meta_token_credential_reference("meta-read").upper(),
        )


def test_draft_revalidation_rejects_model_copy_bypass() -> None:
    forged = _draft().model_copy(update={"account_ref": "act_123"})
    with pytest.raises(ValidationError):
        validate_meta_account_token_profile_draft(forged)


def test_meta_bundle_ci_mode_is_rejected_before_inventory_prerequisites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = finalize_meta_account_token_profile(_draft())
    save_configuration(tmp_path, Configuration(profiles=[profile]), replace=True)
    monkeypatch.setattr(
        inventory,
        "export_key",
        lambda: pytest.fail("unsupported Meta CI mode must stop before sharing-key access"),
    )

    with pytest.raises(AdhelmError) as failure:
        inventory.run_inventory(tmp_path, "meta_ads", profile.profile_id, ci_enabled=True)

    assert failure.value.code == "META_TOKEN_INTERACTIVE_STORE_REQUIRED"
