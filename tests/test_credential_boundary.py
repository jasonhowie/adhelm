import pytest
from pydantic import ValidationError

from adhelm import credentials
from adhelm.errors import AdhelmError
from adhelm.models import Profile


def _profile() -> Profile:
    return Profile(
        profile_id="google-read",
        provider="google_ads",
        account_ref="account-1",
        principal_ref="principal-1",
        client_ref="client-1",
        scopes=["read"],
        credential_ref="google-oauth",
        secondary_credential_ref="google-developer",
    )


@pytest.mark.parametrize("slot", ["credential_ref", "secondary_credential_ref"])
def test_private_export_key_cannot_be_selected_as_a_provider_credential(slot: str) -> None:
    data = _profile().model_dump()
    data[slot] = "export-pseudonymization-v1"
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


@pytest.mark.parametrize("secondary", [False, True])
def test_copied_profile_cannot_read_the_export_key_before_provider_egress(
    monkeypatch: pytest.MonkeyPatch, secondary: bool
) -> None:
    def forbidden() -> None:
        pytest.fail("A forged profile must fail before touching the credential store")

    monkeypatch.setattr(credentials, "_backend", forbidden)
    slot = "secondary_credential_ref" if secondary else "credential_ref"
    forged = _profile().model_copy(update={slot: "export-pseudonymization-v1"})
    with pytest.raises(AdhelmError, match="profile failed validation") as error:
        credentials.credential(forged, secondary=secondary)
    assert error.value.code == "PROFILE_INVALID"


def test_primary_and_developer_token_slots_cannot_alias() -> None:
    data = _profile().model_dump()
    data["secondary_credential_ref"] = data["credential_ref"]
    with pytest.raises(ValidationError):
        Profile.model_validate(data)
