"""Cross-provider isolation for the shared protected token lifecycle.

LinkedIn and Reddit share one lifecycle, one slot type, one context type and one
result type.  The only things that keep a LinkedIn object out of the Reddit
lifecycle (and the reverse) are the spec-identity checks on slots and contexts,
the provider check on results and status, and the pseudonym-prefix check in the
CLI projection.  Each negative case below fails if its guard is removed.

Everything uses fake credential stores; nothing here opens a socket or an OS
credential store.
"""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
import test_linkedin_token_credentials as linkedin_support
import test_reddit_token_lifecycle as reddit_support
from test_linkedin_token_credentials import MemoryStore

from adhelm import linkedin_token_credentials as linkedin
from adhelm import protected_token_cli as cli_projection
from adhelm import reddit_token_credentials as reddit
from adhelm.errors import AdhelmError
from adhelm.linkedin_token_profiles import (
    LINKEDIN_TOKEN_SERVICE,
    LinkedInTokenSlot,
    linkedin_token_reference,
)
from adhelm.oauth_enrollment import (
    OAuthAuthorizationTarget,
    OAuthInitialGrant,
    OAuthRefreshGrant,
)
from adhelm.protected_token_lifecycle import TokenLifecycleResult, TokenProviderSpec, TokenStatus
from adhelm.reddit_token_profiles import (
    REDDIT_OAUTH_CONTRACT,
    REDDIT_TOKEN_SERVICE,
    RedditTokenSlot,
    reddit_token_reference,
)


class Stores:
    """One independent fake store per provider, installed on each lifecycle."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.linkedin = MemoryStore()
        self.reddit = MemoryStore()
        linkedin_support.install_fake_store(monkeypatch, self.linkedin)
        monkeypatch.setattr(reddit, "_credential_backend", lambda: self.reddit)
        monkeypatch.setattr(reddit, "credential_mutation_lock", lambda _reference: nullcontext())


def reddit_target() -> OAuthAuthorizationTarget:
    return OAuthAuthorizationTarget(
        contract=REDDIT_OAUTH_CONTRACT,
        client_ref=reddit_support.CLIENT_ID,
        scopes=("adsread",),
    )


def reddit_initial_grant() -> OAuthInitialGrant:
    now = datetime.now(UTC)
    return OAuthInitialGrant(
        target=reddit_target(),
        access_token=reddit_support.ACCESS,
        access_token_expires_at=now + timedelta(hours=1),
        refresh_token=reddit_support.REFRESH,
        refresh_token_expires_at=None,
        refresh_status="GRANTED",
        granted_scopes=("adsread",),
    )


def reddit_refresh_grant() -> OAuthRefreshGrant:
    now = datetime.now(UTC)
    return OAuthRefreshGrant(
        target=reddit_target(),
        access_token=reddit_support.REFRESHED_ACCESS,
        access_token_expires_at=now + timedelta(hours=1),
        granted_scopes=None,
        rotated_refresh_token=None,
        rotated_refresh_token_expires_at=None,
    )


def enroll_reddit(directory: Path) -> None:
    slot = reddit.prepare_reddit_token_enrollment(directory, reddit_support.PROFILE_ID)
    salt = reddit.new_identity_salt()
    reddit.install_reddit_token_grant(
        directory,
        slot,
        reddit_initial_grant(),
        client_secret=reddit_support.CLIENT_SECRET,
        identity_salt=salt,
        identity_pseudonym=reddit.reddit_account_pseudonym(salt, reddit_support.ACCOUNT),
    )


def linkedin_directory(tmp_path: Path) -> Path:
    return linkedin_support.configured_directory(tmp_path / "linkedin")


def reddit_directory(tmp_path: Path) -> Path:
    return reddit_support.configured_directory(tmp_path / "reddit")


def state_value(store: MemoryStore) -> str | None:
    reference = linkedin_token_reference(linkedin_support.PROFILE_ID, "STATE")
    return store.values.get((LINKEDIN_TOKEN_SERVICE, reference))


def reddit_state_value(store: MemoryStore) -> str | None:
    reference = reddit_token_reference(reddit_support.PROFILE_ID, "STATE")
    return store.values.get((REDDIT_TOKEN_SERVICE, reference))


# --- (a) enrollment slots ---------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "slot_type"),
    [
        (linkedin.LINKEDIN_TOKEN_SPEC, LinkedInTokenSlot),
        (reddit.REDDIT_TOKEN_SPEC, RedditTokenSlot),
    ],
    ids=["linkedin", "reddit"],
)
def test_a_spec_declares_exactly_the_slots_its_reference_derivation_accepts(
    spec: TokenProviderSpec, slot_type: object
) -> None:
    """``private_slots`` is typed ``tuple[str, ...]``, so only this pins a typo."""

    assert len(spec.private_slots) == len(set(spec.private_slots))
    assert set(spec.private_slots) == set(get_args(slot_type))


def test_a_linkedin_slot_is_refused_by_the_reddit_install_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = Stores(monkeypatch)
    linkedin_slot = linkedin.prepare_linkedin_token_enrollment(
        linkedin_directory(tmp_path), linkedin_support.PROFILE_ID
    )
    stores.linkedin.set_calls.clear()
    salt = reddit.new_identity_salt()

    with pytest.raises(AdhelmError) as error:
        reddit.install_reddit_token_grant(
            reddit_directory(tmp_path),
            linkedin_slot,
            reddit_initial_grant(),
            client_secret=reddit_support.CLIENT_SECRET,
            identity_salt=salt,
            identity_pseudonym=reddit.reddit_account_pseudonym(salt, reddit_support.ACCOUNT),
        )

    assert error.value.code == "REDDIT_TOKEN_SLOT_INVALID"
    assert stores.reddit.set_calls == []
    assert stores.reddit.values == {}
    assert stores.linkedin.set_calls == []


def test_a_reddit_slot_is_refused_by_the_linkedin_install_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = Stores(monkeypatch)
    reddit_slot = reddit.prepare_reddit_token_enrollment(
        reddit_directory(tmp_path), reddit_support.PROFILE_ID
    )
    stores.reddit.set_calls.clear()
    salt = linkedin.new_identity_salt()

    with pytest.raises(AdhelmError) as error:
        linkedin.install_linkedin_token_grant(
            linkedin_directory(tmp_path),
            reddit_slot,
            linkedin_support.initial_grant(),
            client_secret=linkedin_support.CLIENT_SECRET,
            identity_salt=salt,
            identity_pseudonym=linkedin_support.pseudonym(salt),
        )

    assert error.value.code == "LINKEDIN_TOKEN_SLOT_INVALID"
    assert stores.linkedin.set_calls == []
    assert stores.linkedin.values == {}
    assert stores.reddit.set_calls == []


def test_a_slot_relabelled_with_the_other_providers_spec_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the issuing spec differs, so only the spec-identity check can refuse it."""
    stores = Stores(monkeypatch)
    directory = reddit_directory(tmp_path)
    genuine = reddit.prepare_reddit_token_enrollment(directory, reddit_support.PROFILE_ID)
    stores.reddit.set_calls.clear()
    relabelled = replace(genuine, _spec=linkedin.LINKEDIN_TOKEN_SPEC)
    salt = reddit.new_identity_salt()

    with pytest.raises(AdhelmError) as error:
        reddit.install_reddit_token_grant(
            directory,
            relabelled,
            reddit_initial_grant(),
            client_secret=reddit_support.CLIENT_SECRET,
            identity_salt=salt,
            identity_pseudonym=reddit.reddit_account_pseudonym(salt, reddit_support.ACCOUNT),
        )

    assert error.value.code == "REDDIT_TOKEN_SLOT_INVALID"
    assert stores.reddit.set_calls == []


# --- (b) refresh contexts ---------------------------------------------------------


def test_a_linkedin_refresh_context_cannot_drive_the_reddit_refresh_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = Stores(monkeypatch)
    directory = linkedin_directory(tmp_path)
    linkedin_support.enroll(directory)
    context = linkedin.begin_linkedin_token_refresh(directory, linkedin_support.PROFILE_ID)
    started = state_value(stores.linkedin)
    assert started is not None and '"local_state":"REFRESH_STARTED"' in started
    stores.linkedin.set_calls.clear()
    stores.linkedin.deleted.clear()

    with pytest.raises(AdhelmError) as completed:
        reddit.complete_reddit_token_refresh(directory, context, reddit_refresh_grant())
    # restore and block are best effort and never raise; they must simply do nothing.
    reddit.restore_reddit_token_refresh(directory, context)
    reddit.block_reddit_token_refresh(directory, context)

    assert completed.value.code == "REDDIT_TOKEN_SLOT_INVALID"
    assert state_value(stores.linkedin) == started
    assert stores.linkedin.set_calls == []
    assert stores.linkedin.deleted == []
    assert stores.reddit.set_calls == []


def test_a_reddit_refresh_context_cannot_drive_the_linkedin_refresh_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = Stores(monkeypatch)
    directory = reddit_directory(tmp_path)
    enroll_reddit(directory)
    context = reddit.begin_reddit_token_refresh(directory, reddit_support.PROFILE_ID)
    started = reddit_state_value(stores.reddit)
    assert started is not None and '"local_state":"REFRESH_STARTED"' in started
    stores.reddit.set_calls.clear()
    stores.reddit.deleted.clear()

    with pytest.raises(AdhelmError) as completed:
        linkedin.complete_linkedin_token_refresh(
            directory, context, linkedin_support.refresh_grant()
        )
    linkedin.restore_linkedin_token_refresh(directory, context)
    linkedin.block_linkedin_token_refresh(directory, context)

    assert completed.value.code == "LINKEDIN_TOKEN_SLOT_INVALID"
    assert reddit_state_value(stores.reddit) == started
    assert stores.reddit.set_calls == []
    assert stores.reddit.deleted == []
    assert stores.linkedin.set_calls == []


def test_a_context_relabelled_with_the_other_providers_spec_is_still_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolate the spec-identity check from every other mismatch.

    The relabelled context carries the right profile, digest and started-state
    for the Reddit slots in its own directory; only the issuing provider spec is
    wrong.  Without the spec check, restore would rewrite the Reddit state.
    """
    stores = Stores(monkeypatch)
    directory = reddit_directory(tmp_path)
    enroll_reddit(directory)
    genuine = reddit.begin_reddit_token_refresh(directory, reddit_support.PROFILE_ID)
    started = reddit_state_value(stores.reddit)
    assert started is not None and '"local_state":"REFRESH_STARTED"' in started
    stores.reddit.set_calls.clear()
    relabelled = replace(genuine, _spec=linkedin.LINKEDIN_TOKEN_SPEC)

    with pytest.raises(AdhelmError) as completed:
        reddit.complete_reddit_token_refresh(directory, relabelled, reddit_refresh_grant())
    reddit.restore_reddit_token_refresh(directory, relabelled)
    reddit.block_reddit_token_refresh(directory, relabelled)

    assert completed.value.code == "REDDIT_TOKEN_SLOT_INVALID"
    assert reddit_state_value(stores.reddit) == started
    assert stores.reddit.set_calls == []


# --- (c) CLI projection -----------------------------------------------------------


def lifecycle_result(provider: str, pseudonym: str) -> TokenLifecycleResult:
    now = datetime.now(UTC)
    return TokenLifecycleResult(
        provider=provider,
        profile_configuration_digest="0" * 64,
        purpose="READ",
        operation="ENROLL",
        granted_scopes=("adsread",),
        access_token_expires_at=now + timedelta(hours=1),
        refresh_token_expires_at=None,
        refresh_status="GRANTED",
        identity_pseudonym=pseudonym,
    )


def token_status(provider: str, pseudonym: str | None) -> TokenStatus:
    now = datetime.now(UTC)
    return TokenStatus(
        provider=provider,
        profile_configuration_digest="0" * 64,
        purpose="READ",
        local_state="READY",
        granted_scopes=("adsread",),
        access_token_expires_at=now + timedelta(hours=1),
        access_token_expired=False,
        refresh_token_expires_at=None,
        refresh_status="GRANTED",
        identity_pseudonym=pseudonym,
        bearer_slot_present=True,
        refresh_slot_present=True,
    )


GUIDANCE = ("Revoke access in the provider console; local removal does not.",)
REDDIT_PSEUDONYM = "reddit_ads_account_v1." + "a" * 64
LINKEDIN_PSEUDONYM = "linkedin_ads_account_v1." + "b" * 64


def test_the_projection_accepts_its_own_providers_result_and_status() -> None:
    spec = reddit.REDDIT_TOKEN_SPEC

    data = cli_projection.lifecycle_data(spec, lifecycle_result("reddit_ads", REDDIT_PSEUDONYM))
    status = cli_projection.status_data(
        spec, GUIDANCE, token_status("reddit_ads", REDDIT_PSEUDONYM)
    )

    assert data["provider"] == "reddit_ads"
    assert data["identity_pseudonym"] == REDDIT_PSEUDONYM
    assert status["provider"] == "reddit_ads"
    assert status["identity_pseudonym"] == REDDIT_PSEUDONYM


def test_the_projection_refuses_a_result_whose_provider_is_another_providers() -> None:
    spec = reddit.REDDIT_TOKEN_SPEC

    with pytest.raises(AdhelmError) as lifecycle_error:
        cli_projection.lifecycle_data(spec, lifecycle_result("linkedin_ads", REDDIT_PSEUDONYM))
    with pytest.raises(AdhelmError) as status_error:
        cli_projection.status_data(spec, GUIDANCE, token_status("linkedin_ads", REDDIT_PSEUDONYM))

    assert lifecycle_error.value.code == "REDDIT_TOKEN_RESULT_INVALID"
    assert status_error.value.code == "REDDIT_TOKEN_RESULT_INVALID"


def test_the_projection_refuses_a_matching_provider_with_another_providers_pseudonym() -> None:
    spec = reddit.REDDIT_TOKEN_SPEC

    with pytest.raises(AdhelmError) as lifecycle_error:
        cli_projection.lifecycle_data(spec, lifecycle_result("reddit_ads", LINKEDIN_PSEUDONYM))
    with pytest.raises(AdhelmError) as status_error:
        cli_projection.status_data(spec, GUIDANCE, token_status("reddit_ads", LINKEDIN_PSEUDONYM))

    assert lifecycle_error.value.code == "REDDIT_TOKEN_RESULT_INVALID"
    assert status_error.value.code == "REDDIT_TOKEN_RESULT_INVALID"


def test_the_projection_refuses_a_reddit_result_under_the_linkedin_spec() -> None:
    spec = linkedin.LINKEDIN_TOKEN_SPEC

    with pytest.raises(AdhelmError) as lifecycle_error:
        cli_projection.lifecycle_data(spec, lifecycle_result("reddit_ads", REDDIT_PSEUDONYM))
    with pytest.raises(AdhelmError) as status_error:
        cli_projection.status_data(spec, GUIDANCE, token_status("reddit_ads", REDDIT_PSEUDONYM))

    assert lifecycle_error.value.code == "LINKEDIN_TOKEN_RESULT_INVALID"
    assert status_error.value.code == "LINKEDIN_TOKEN_RESULT_INVALID"


def test_a_non_result_object_is_refused_by_the_projection() -> None:
    spec = reddit.REDDIT_TOKEN_SPEC

    with pytest.raises(AdhelmError) as lifecycle_error:
        cli_projection.lifecycle_data(spec, object())
    with pytest.raises(AdhelmError) as status_error:
        cli_projection.status_data(spec, GUIDANCE, object())

    assert lifecycle_error.value.code == "REDDIT_TOKEN_RESULT_INVALID"
    assert status_error.value.code == "REDDIT_TOKEN_RESULT_INVALID"
