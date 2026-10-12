"""Independent re-review regressions for the P01-LI.1 repair (commit 1180b02).

The repair itself is sound: all eleven earlier review regressions pass.  These
two tests encode defects introduced by the repair.  Nothing here opens a socket,
a browser, or an OS credential store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_linkedin_token_credentials import (
    ACCOUNT,
    CLIENT_SECRET,
    PROFILE_ID,
    MemoryStore,
    configured_directory,
    install_fake_store,
    linkedin_profile,
)
from test_linkedin_token_workflow import CLIENT_ID, Harness, initial_token_body

from adhelm import linkedin_token_credentials as lifecycle
from adhelm.errors import AdhelmError
from adhelm.linkedin_token_profiles import (
    LINKEDIN_OAUTH_CONTRACT,
    LINKEDIN_TOKEN_SERVICE,
    linkedin_ads_scope_selection,
    linkedin_token_reference,
    require_scope_selection_covers_profile,
)
from adhelm.oauth_enrollment import OAuthAuthorizationTarget, OAuthInitialGrant


def _read_target() -> OAuthAuthorizationTarget:
    return OAuthAuthorizationTarget(
        contract=LINKEDIN_OAUTH_CONTRACT,
        client_ref=CLIENT_ID,
        scopes=("r_ads", "r_ads_reporting"),
    )


def test_one_scope_rejection_code_does_not_carry_two_exit_codes() -> None:
    """Re-review finding 1 (SHOULD_FIX): ``LINKEDIN_TOKEN_SCOPE_REJECTED`` is
    now raised at two different exit codes for two different situations.

    ``linkedin_ads_scope_selection`` still raises it at exit 2 ("you asked for
    scopes outside the allowlist" -- a caller-input problem).  The repair's new
    ``_require_purpose_allowlist`` raises the same code at exit 4 ("the provider
    granted something this profile purpose may never record" -- an authorization
    problem the caller cannot fix by changing arguments).  The diagnostics table
    in docs/implementation/linkedin-token-lifecycle.md documents only exit 2, so
    an agent branching on the code gets two incompatible recovery contracts.
    The two situations need two codes, or the contract needs to say both.

    Remedy chosen (coordinator decision, 2026-09-08): two codes.  The original
    assertions here required the two sites to share a code *and* an exit code,
    which is only satisfiable by the other remedy, so they are restated below to
    encode the same invariant under the chosen one -- each code carries exactly
    one exit code, and the two situations are distinguishable.
    """
    with pytest.raises(AdhelmError) as selection_error:
        linkedin_ads_scope_selection("READ", ("r_ads", "r_ads_reporting", "rw_ads"))

    with pytest.raises(AdhelmError) as allowlist_error:
        lifecycle._require_purpose_allowlist("READ", ("r_ads", "r_ads_reporting", "rw_ads"))

    assert selection_error.value.code != allowlist_error.value.code
    assert selection_error.value.code == "LINKEDIN_TOKEN_SCOPE_REJECTED"
    assert selection_error.value.exit_code == 2
    assert allowlist_error.value.code == "LINKEDIN_TOKEN_SCOPE_NOT_RECORDABLE"
    assert allowlist_error.value.exit_code == 4

    # Every other raise site of each code must agree on that code's exit code,
    # so an agent branching on a code gets exactly one recovery contract.
    caller_input_sites = (
        lambda: linkedin_ads_scope_selection("READ", ("rw_ads",)),
        lambda: linkedin_ads_scope_selection("ACTIVATE", None),
        lambda: require_scope_selection_covers_profile(
            linkedin_profile(purpose="EDIT", scopes=["r_ads", "r_ads_reporting", "rw_ads"]),
            ("r_ads", "r_ads_reporting"),
        ),
    )
    for site in caller_input_sites:
        with pytest.raises(AdhelmError) as error:
            site()
        assert (error.value.code, error.value.exit_code) in {
            ("LINKEDIN_TOKEN_SCOPE_REJECTED", 2),
            ("LINKEDIN_TOKEN_PURPOSE_UNSUPPORTED", 2),
        }

    with pytest.raises(AdhelmError) as unknown_purpose:
        lifecycle._require_purpose_allowlist("ACTIVATE", ("r_ads",))
    assert unknown_purpose.value.code == "LINKEDIN_TOKEN_SCOPE_NOT_RECORDABLE"
    assert unknown_purpose.value.exit_code == 4


def test_recorded_state_outside_the_purpose_allowlist_is_never_reported_ready(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review finding 2 (NOTE): the purpose allowlist guards the write path
    only; the read path still accepts and reports a forbidden scope.

    ``install_linkedin_token_grant`` and ``complete_linkedin_token_refresh`` both
    refuse to *persist* ``rw_ads`` for a READ profile -- confirmed.  But
    ``_decode_state`` validates the stored scope list only against the contract's
    full ``supported_scopes``, so a state slot that already contains ``rw_ads``
    (written by the pre-repair build, or by anything with credential-store write
    access) still decodes and ``status`` reports it as ``READY`` with ``rw_ads``
    in ``granted_scopes``.  A recorded permission the profile may never hold must
    be reported as ``UNVERIFIED``, not as a usable local contract.
    """
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    harness.enroll()
    reference = linkedin_token_reference(PROFILE_ID, "STATE")
    stored = harness.store.values[(LINKEDIN_TOKEN_SERVICE, reference)]
    tampered = stored.replace('["r_ads","r_ads_reporting"]', '["r_ads","r_ads_reporting","rw_ads"]')
    assert tampered != stored
    harness.store.values[(LINKEDIN_TOKEN_SERVICE, reference)] = tampered

    status = lifecycle.linkedin_token_status(harness.directory, PROFILE_ID)

    assert status.purpose == "READ"
    assert "rw_ads" not in status.granted_scopes


def test_a_hand_built_grant_outside_the_allowlist_is_refused_before_any_write(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirmation, not a defect: the direct-constructor path is closed.

    Kept as a standing regression for the repair's second-layer check.
    """
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = configured_directory(tmp_path, linkedin_profile())
    slot = lifecycle.prepare_linkedin_token_enrollment(directory, PROFILE_ID)
    now = datetime.now(UTC)
    salt = lifecycle.new_identity_salt()
    grant = OAuthInitialGrant(
        target=_read_target(),
        access_token="a" * 20,
        access_token_expires_at=now + timedelta(days=60),
        refresh_token="r" * 20,
        refresh_token_expires_at=now + timedelta(days=365),
        refresh_status="GRANTED",
        granted_scopes=("r_ads", "r_ads_reporting", "rw_ads"),
    )

    with pytest.raises(AdhelmError) as error:
        lifecycle.install_linkedin_token_grant(
            directory,
            slot,
            grant,
            client_secret=CLIENT_SECRET,
            identity_salt=salt,
            identity_pseudonym=lifecycle.linkedin_account_pseudonym(salt, ACCOUNT),
        )

    assert error.value.code == "LINKEDIN_TOKEN_SCOPE_NOT_RECORDABLE"
    assert store.set_calls == []
