from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from adhelm.canonical import digest
from adhelm.models import (
    Configuration,
    OpenAIAccountKeyProfile,
    Profile,
    validate_configured_profile_json,
)


def test_account_key_profile_is_closed_and_preserves_legacy_profile_serialization() -> None:
    legacy = Profile(
        profile_id="legacy",
        provider="ga4",
        account_ref="property",
        principal_ref="person",
        client_ref="client",
        scopes=["scope"],
        credential_ref="credential",
    )
    assert legacy.model_dump(mode="json") == Profile.model_validate(
        legacy.model_dump(mode="json")
    ).model_dump(mode="json")
    # Computed from the installed wheel of exact passing M11 source 2d679cb.
    assert digest(legacy) == "b876da947bcacbc17ca3306a6fe58de1e24bb519b65711458bf220ff1c353bc1"
    # Recomputed when O3.2 removed the `edition` field from Configuration; the
    # profile digest above is unchanged.
    assert (
        digest(Configuration(profiles=[legacy]))
        == "d95ae3099b93dc80cba350dd9f9b7e6a69ef9b08f7981cc8e6ad650a86a006b6"
    )
    account_key = OpenAIAccountKeyProfile(
        profile_kind="OPENAI_ADS_ACCOUNT_KEY",
        profile_id="openai-read",
        account_ref="account_fixture",
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="openai-key",
    )
    config = Configuration(profiles=[legacy, account_key])
    assert isinstance(config.profiles[1], OpenAIAccountKeyProfile)
    assert "scopes" not in account_key.model_dump(mode="json")
    with pytest.raises(ValidationError):
        OpenAIAccountKeyProfile.model_validate({**account_key.model_dump(), "scopes": ["invented"]})


def test_account_key_json_requires_explicit_discriminator() -> None:
    raw = json.dumps(
        {
            "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
            "profile_id": "p",
            "account_ref": "a",
            "credential_ref": "key",
        }
    )
    assert isinstance(validate_configured_profile_json(raw), OpenAIAccountKeyProfile)
    with pytest.raises(ValidationError):
        validate_configured_profile_json(
            json.dumps({"profile_id": "p", "account_ref": "a", "credential_ref": "key"})
        )
