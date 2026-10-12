"""Independent review regressions for the P01-LI.1 LinkedIn token lifecycle.

Each test encodes one defect found while reviewing commit f8b9108.  They are
expected to fail against that commit and to pass once the product code is
corrected.  Nothing here opens a socket, a browser, or an OS credential store:
the fakes come from the implementer's own harness so the reproduction is exactly
the shipped code path.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import httpx
import pytest
from test_linkedin_token_credentials import (
    ACCOUNT,
    BEARER_REF,
    GENERIC_SERVICE,
    PROFILE_ID,
    MemoryStore,
    install_fake_store,
    linkedin_profile,
)
from test_linkedin_token_credentials import (
    configured_directory as _configured_directory,
)
from test_linkedin_token_workflow import (
    Harness,
    account_response,
    initial_token_body,
    refresh_token_body,
)
from typer.testing import CliRunner

from adhelm import linkedin_token_credentials as lifecycle
from adhelm import linkedin_token_workflow as workflow
from adhelm.errors import AdhelmError
from adhelm.oauth_enrollment import OAuthTokenDispatchState, OAuthTransportError
from adhelm.providers import linkedin_ads
from adhelm.transport import SafeHttpClient


def _two_identity_reads() -> list[object]:
    return [account_response(), account_response()]


def test_the_auth_linkedin_token_command_group_is_reachable_from_the_shipped_cli() -> None:
    """Finding 1 (BLOCKING): ``linkedin_token_cli.register_commands`` has no caller.

    ``src/adhelm/cli.py`` is unchanged by this commit, so none of ``enroll``,
    ``refresh``, ``status`` or ``remove-local`` exists in the installed CLI.  The
    implementation document describes all four as shipped commands and
    ``linkedin_ads._identity_get`` tells operators to run
    ``auth linkedin-token status``, so the capability must be registered rather
    than reachable only from a test-local Typer app.
    """
    from adhelm.cli import app

    result = CliRunner().invoke(app, ["auth", "linkedin-token", "--help"])

    assert result.exit_code == 0, result.output


def test_a_refresh_without_a_returned_expiry_keeps_the_recorded_refresh_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 2 (BLOCKING): a rotated refresh token with no
    ``refresh_token_expires_in`` erases the stored refresh-token expiry.

    ``complete_linkedin_token_refresh`` keeps the previous expiry only when the
    response returns no refresh token at all.  When it returns a refresh token
    without an expiry the recorded expiry becomes ``None``, which silently
    discards known lifecycle state, reports ``refresh_token_expires_at: null``
    in ``status``, and permanently disables the ``LINKEDIN_TOKEN_REFRESH_EXPIRED``
    fail-closed guard.  LinkedIn documents that the refresh-token TTL does not
    reset on use, so an absent field must not be read as "no expiry".
    """
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[
            initial_token_body(),
            refresh_token_body(refresh_token_expires_in=None),
        ],
        identity_responses=_two_identity_reads(),
    )
    harness.enroll()
    enrolled = lifecycle.linkedin_token_status(harness.directory, PROFILE_ID)
    assert enrolled.refresh_token_expires_at is not None

    harness.refresh()

    refreshed = lifecycle.linkedin_token_status(harness.directory, PROFILE_ID)
    assert refreshed.refresh_token_expires_at is not None


def test_a_read_profile_never_records_the_write_scope_from_a_broader_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3 (SHOULD_FIX): a READ enrollment persists ``rw_ads``.

    ``linkedin_ads_scope_selection`` refuses to *request* ``rw_ads`` for a READ
    profile, but nothing re-checks the scope set the provider echoes back before
    it is stored.  A member who previously consented to the write permission
    makes LinkedIn echo the union, and the READ profile then records ``rw_ads``
    as its enrolled contract, which ``check_linkedin_ads_scopes`` will later
    treat as a satisfied write precondition.  This is the least-privilege rule
    in AGENTS.md #5 and the scope clause of ADR 0006.
    """
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(scope="r_ads r_ads_reporting rw_ads")],
    )

    harness.enroll()

    status = lifecycle.linkedin_token_status(harness.directory, PROFILE_ID)
    assert status.purpose == "READ"
    assert "rw_ads" not in status.granted_scopes


def test_a_refresh_never_silently_widens_the_recorded_scope_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3 (SHOULD_FIX), refresh half.

    ``complete_linkedin_token_refresh`` only rejects a *narrower* refreshed
    grant.  A refresh response that echoes an extra permission replaces the
    enrolled scope set with the wider one without any browser consent step, so
    a READ profile can gain ``rw_ads`` during an ordinary token refresh.  ADR
    0006 says a normal refresh preserves plan identity only when the scopes do
    not change.
    """
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[
            initial_token_body(),
            refresh_token_body(scope="r_ads r_ads_reporting rw_ads"),
        ],
        identity_responses=_two_identity_reads(),
    )
    harness.enroll()

    harness.refresh()

    status = lifecycle.linkedin_token_status(harness.directory, PROFILE_ID)
    assert "rw_ads" not in status.granted_scopes


def test_a_refresh_that_never_reached_linkedin_leaves_the_profile_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 4 (SHOULD_FIX): ``dispatch_state`` is computed and then discarded.

    ``OAuthTransportError`` classifies whether a token request could have
    reached the provider, but ``refresh_linkedin_token`` blocks the profile on
    any ``BaseException``.  A ``NOT_DISPATCHED`` failure provably issued no
    request and left the stored refresh token valid, yet the profile is marked
    ``REAUTHORIZATION_REQUIRED`` and can only be recovered with a full browser
    re-enrollment.  A refusal that made no egress must not consume the grant.
    """
    not_dispatched = OAuthTransportError(
        "OAUTH_TOKEN_FORM_INVALID",
        "The OAuth token request cannot be prepared.",
        dispatch_state=OAuthTokenDispatchState.NOT_DISPATCHED,
        exit_code=2,
    )
    harness = Harness(
        tmp_path,
        monkeypatch,
        token_responses=[initial_token_body(), not_dispatched],
        identity_responses=_two_identity_reads(),
    )
    harness.enroll()

    with pytest.raises(AdhelmError):
        harness.refresh()

    status = lifecycle.linkedin_token_status(harness.directory, PROFILE_ID)
    assert status.local_state == "READY"


def test_an_out_of_range_client_id_is_a_typed_local_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 5 (SHOULD_FIX): an untyped ``ValueError`` escapes enrollment.

    ``prompt_hidden_secret`` accepts up to 16384 printable characters while
    ``OAuthAuthorizationTarget`` bounds ``client_ref`` at 200, so an operator who
    mistypes the client ID gets a bare ``ValueError``.  ``cli.guarded`` catches
    only ``AdhelmError``, ``ValidationError`` and ``OSError``, so the agent-facing
    command would exit on a traceback instead of the bounded diagnostic envelope
    required by AGENTS.md #27.
    """
    harness = Harness(tmp_path, monkeypatch, token_responses=[initial_token_body()])
    monkeypatch.setattr(workflow, "_prompt_client_id", lambda: "x" * 201)

    with pytest.raises(AdhelmError) as error:
        harness.enroll()

    assert error.value.exit_code == 2


def test_existing_linkedin_identity_callers_keep_the_shared_access_error_code() -> None:
    """Finding 6 (SHOULD_FIX): the 401/403 remap is not opt-in.

    ``discover_identity`` routes through ``_identity_get`` unconditionally, so
    every pre-existing caller that passes no ``granted_scopes`` -- ``doctor``
    via ``inventory.py`` and ``run_report`` via ``workflows.py:289`` -- now
    receives ``LINKEDIN_ADS_AUTHORIZATION_REQUIRED`` instead of the shared
    ``PROVIDER_ACCESS_REQUIRED`` used by every other provider, with a
    ``next_action`` that names ``auth linkedin-token status`` (see finding 1).
    The design note for this change claims existing read behavior is unchanged.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "denied"})

    client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        adapter = linkedin_ads.LinkedInAdsClient(
            SafeHttpClient(client, {linkedin_ads.LINKEDIN_ADS_API_HOST}), lambda: "review-bearer"
        )
        with pytest.raises(AdhelmError) as error:
            adapter.discover_identity(ACCOUNT, expected_account_client_ref=ACCOUNT)
    finally:
        client.close()

    assert error.value.code == "PROVIDER_ACCESS_REQUIRED"


def test_the_reporting_scope_constant_has_a_production_precondition() -> None:
    """Finding 7 (NOTE): ``r_ads_reporting`` is never actually checked.

    ``LINKEDIN_ADS_REPORTING_SCOPE`` is introduced and documented as the
    precondition for the Ad Analytics report, but ``check_linkedin_ads_scopes``
    has exactly one production call site and it requires only
    ``LINKEDIN_ADS_IDENTITY_SCOPE``.  Neither ``run_campaign_report`` nor
    ``run_ad_report`` accepts or checks the recorded permissions, so the
    reporting half of the documented contract is unimplemented.
    """
    source = Path(inspect.getfile(linkedin_ads)).read_text(encoding="utf-8")

    assert source.count("LINKEDIN_ADS_REPORTING_SCOPE") > 1


def test_local_state_is_not_stranded_when_a_second_enrollment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 9 (NOTE): an interrupted enrollment can only be cleared by two
    separate commands, and ``remove-local`` is not one that clears the bearer.

    A partial install leaves the bearer slot written while the state slot still
    reads ``ENROLL_STARTED``.  ``prepare_linkedin_token_enrollment`` then refuses
    every retry with ``LINKEDIN_TOKEN_CREDENTIAL_EXISTS`` because it requires all
    four slots to be empty, and ``auth linkedin-token remove-local`` deliberately
    leaves the bearer alone, so recovery needs the shared ``auth remove-local``
    as well.  The refusal's ``next_action`` names only the LinkedIn command.
    """
    store = MemoryStore()
    install_fake_store(monkeypatch, store)
    directory = _configured_directory(tmp_path, linkedin_profile())
    store.values[(GENERIC_SERVICE, BEARER_REF)] = "stranded-bearer"

    with pytest.raises(AdhelmError) as error:
        lifecycle.prepare_linkedin_token_enrollment(directory, PROFILE_ID)

    assert error.value.code == "LINKEDIN_TOKEN_CREDENTIAL_EXISTS"
    assert "auth remove-local" in error.value.next_action
