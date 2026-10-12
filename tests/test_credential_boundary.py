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
    )


def test_private_export_key_cannot_be_selected_as_a_provider_credential() -> None:
    data = _profile().model_dump()
    data["credential_ref"] = "export-pseudonymization-v1"
    with pytest.raises(ValidationError):
        Profile.model_validate(data)


def test_copied_profile_cannot_read_the_export_key_before_provider_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden() -> None:
        pytest.fail("A forged profile must fail before touching the credential store")

    monkeypatch.setattr(credentials, "_backend", forbidden)
    forged = _profile().model_copy(update={"credential_ref": "export-pseudonymization-v1"})
    with pytest.raises(AdhelmError, match="profile failed validation") as error:
        credentials.credential(forged)
    assert error.value.code == "PROFILE_INVALID"


def test_a_retired_developer_token_reference_is_dropped_and_never_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = _profile().model_dump()
    data["secondary_credential_ref"] = "retired-developer-token"
    profile = Profile.model_validate(data)
    assert "secondary_credential_ref" not in profile.model_dump()
    reads: list[str] = []

    class Store:
        def get_password(self, service: str, username: str) -> str | None:
            reads.append(username)
            return "value"

    monkeypatch.setattr(credentials, "_backend", lambda: Store())
    credentials.credential(profile)
    assert reads == [profile.credential_ref]
