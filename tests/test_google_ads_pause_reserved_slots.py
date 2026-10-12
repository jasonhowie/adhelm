"""The private Google Ads PAUSE key cannot become a provider credential."""

from typing import Never

import pytest
from pydantic import ValidationError
from test_google_ads_edit_profile import _draft_data, _profile_data

from adhelm import credential_setup, credentials, google_oauth_credentials
from adhelm.errors import AdhelmError
from adhelm.google_oauth_setup import GoogleOAuthProfileDraft
from adhelm.models import (
    GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE,
    Configuration,
    OpenAIAccountKeyProfile,
    Profile,
    validate_configured_profile,
)


@pytest.mark.parametrize(
    "reference",
    [
        GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE,
        GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE.upper(),
    ],
)
def test_google_draft_and_profile_reject_private_pause_slot(reference: str) -> None:
    slot = "credential_ref"
    with pytest.raises(ValidationError):
        GoogleOAuthProfileDraft.model_validate({**_draft_data(), slot: reference})
    with pytest.raises(ValidationError):
        Profile.model_validate({**_profile_data(), slot: reference})


@pytest.mark.parametrize("source", ["OS_STORE", "EXPLICIT_CI"])
def test_other_provider_and_ci_profiles_cannot_select_google_pause_key(source: str) -> None:
    with pytest.raises(ValidationError):
        OpenAIAccountKeyProfile.model_validate(
            {
                "profile_kind": "OPENAI_ADS_ACCOUNT_KEY",
                "profile_id": "local-openai",
                "account_ref": "local-account",
                "credential_source": source,
                "credential_ref": GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE.upper(),
            }
        )


def test_forged_private_slot_fails_before_credential_read_or_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> Never:
        pytest.fail("Reserved Google Ads binding reference reached a protected boundary")

    monkeypatch.setattr(credentials, "_backend", forbidden)
    monkeypatch.setattr(credential_setup, "os_store_backend", forbidden)
    monkeypatch.setattr(credential_setup, "prompt_secret_from_terminal", forbidden)
    monkeypatch.setattr(google_oauth_credentials, "_credential_backend", forbidden)
    monkeypatch.setattr(google_oauth_credentials, "credential_mutation_lock", forbidden)
    valid = Profile.model_validate(_profile_data())
    forged = valid.model_copy(
        update={"credential_ref": GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE.upper()}
    )
    with pytest.raises(ValidationError):
        validate_configured_profile(forged)
    with pytest.raises(AdhelmError) as generic_failure:
        credentials.credential(forged)
    assert generic_failure.value.code == "PROFILE_INVALID"
    with pytest.raises(AdhelmError):
        google_oauth_credentials._resolve_google_ads_edit_execution_snapshot(forged)

    configuration = Configuration.model_construct(profiles=[forged])
    with pytest.raises(AdhelmError) as status_failure:
        credential_setup.credential_status(configuration, valid.profile_id)
    assert status_failure.value.code == "PROFILE_CONFIGURATION_INVALID"
    for operation in (
        credential_setup.store_interactive,
        credential_setup.rotate_interactive,
        credential_setup.local_remove,
    ):
        with pytest.raises(AdhelmError) as mutation_failure:
            operation(configuration, valid.profile_id)
        assert mutation_failure.value.code == "PROFILE_CONFIGURATION_INVALID"
