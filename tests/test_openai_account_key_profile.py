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
    # Recomputed when the retired `secondary_credential_ref` field (the Google Ads
    # developer-token reference) was removed from Profile: a profile no longer serializes
    # that null, so every profile digest changed once, and Google OAuth grants sealed by
    # AdHelm 0.1.0 must be reconnected. Before that this digest was b876da947bca...
    assert digest(legacy) == "eaa7a8f572bc2b5868cbf0ccd75f805176e364927211c0058f1d1059caeaa047"
    # Recomputed when O3.2 removed the `edition` field from Configuration, and again for
    # the profile change above.
    assert (
        digest(Configuration(profiles=[legacy]))
        == "3e3f9c15552b2e04cd2448cb32c39bf3a87d95c9a433bfd0594e63b6e332e6c8"
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
