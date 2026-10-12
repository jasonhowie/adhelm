"""Explicit local Reddit Ads OAuth enrollment and refresh; no advertising operations.

Enrollment and refresh both perform provider egress, so both are written so
that no credential mutation lock is held across an HTTP call: the credential
lifecycle module opens and closes each locked section, and every network step
happens between those sections.

Two transports are used deliberately.  The account identity re-verification is
an ordinary fixed provider read through :class:`~adhelm.transport.SafeHttpClient`.
The token exchange is not: an OAuth token form carries ``refresh_token`` field
names that the shared transport correctly rejects at its generic privacy
boundary, so the token POST uses the fixed-endpoint
:class:`~adhelm.oauth_enrollment.FixedOAuthTokenTransport`, in the same way the
LinkedIn lifecycle does.  Reddit authenticates the token exchange and every
refresh with the developer application's ID and secret (HTTP Basic), so
enrollment prompts for both without echo and stores the secret in its own slot.
"""

from __future__ import annotations

import hmac
import sys
from pathlib import Path
from typing import Final

import httpx

from adhelm.config import load_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Configuration, Profile, validate_configuration
from adhelm.oauth_enrollment import (
    FixedOAuthTokenTransport,
    LoopbackRedirectReceiver,
    OAuthAuthorizationTarget,
    OAuthInitialGrant,
    OAuthProviderContract,
    OAuthTokenDispatchState,
    OAuthTransportError,
    consume_callback,
    exchange_authorization_code,
    open_system_browser,
    prompt_hidden_secret,
    refresh_access_token,
    start_authorization,
)
from adhelm.providers.reddit_ads import (
    REDDIT_ADS_API_HOST,
    RedditAdsClient,
    RedditAdsIdentity,
)
from adhelm.reddit_token_credentials import (
    RedditTokenLifecycleResult,
    RedditTokenStatus,
    begin_reddit_token_refresh,
    block_reddit_token_refresh,
    complete_reddit_token_refresh,
    install_reddit_token_grant,
    new_identity_salt,
    prepare_reddit_token_enrollment,
    reddit_account_pseudonym,
    reddit_token_status,
    restore_reddit_token_refresh,
)
from adhelm.reddit_token_profiles import (
    REDDIT_OAUTH_CONTRACT,
    checked_account_ref,
    reddit_ads_scope_selection,
    require_scope_selection_covers_profile,
    select_reddit_ads_profile,
)
from adhelm.transport import SafeHttpClient

# Reddit matches a registered redirect URI exactly, so the local callback path
# is fixed and the port is chosen by the operator to match what they registered.
REDDIT_CALLBACK_PATH: Final = "/oauth/callback"
_MIN_REDIRECT_PORT: Final = 1_024
_MAX_REDIRECT_PORT: Final = 65_535
_CLIENT_ID_PROMPT: Final = "Reddit client ID: "
_CLIENT_SECRET_PROMPT: Final = "Reddit client secret: "


def enroll_reddit_token(
    directory: Path,
    profile_id: str,
    *,
    redirect_port: int,
    scopes: tuple[str, ...] | None = None,
    interactive: bool = False,
) -> RedditTokenLifecycleResult:
    """Authorize one configured Reddit Ads profile through the local browser."""
    _require_interactive(interactive)
    port = _checked_redirect_port(redirect_port)
    configuration = _configuration(directory)
    profile = select_reddit_ads_profile(configuration, profile_id)
    selection = reddit_ads_scope_selection(profile.purpose, scopes)
    require_scope_selection_covers_profile(profile, selection)
    checked_account_ref(profile)
    slot = prepare_reddit_token_enrollment(directory, profile_id, scopes)
    client_ref = _prompt_client_id()
    client_secret = _prompt_client_secret()
    try:
        target = OAuthAuthorizationTarget(
            contract=REDDIT_OAUTH_CONTRACT, client_ref=client_ref, scopes=selection
        )
    except ValueError:
        # The hidden prompt accepts far more text than an OAuth client ID can
        # be. Turn a mistyped value into the bounded agent-facing diagnostic
        # instead of an untyped error that escapes the CLI guard.
        raise AdhelmError(
            "REDDIT_TOKEN_CLIENT_ID_INVALID",
            "The entered Reddit client ID is not a usable OAuth client identifier.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action=(
                "Re-run enrollment and enter only the app ID (the client ID) exactly as "
                "Reddit's Developer Portal shows it for the application you registered, "
                "not the secret and not ID:secret."
            ),
        ) from None
    grant = _browser_grant(target, redirect_port=port, client_secret=client_secret)
    identity = _verify_identity(profile, grant.access_token)
    salt = new_identity_salt()
    pseudonym = reddit_account_pseudonym(salt, identity.account_client_ref)
    return install_reddit_token_grant(
        directory,
        slot,
        grant,
        client_secret=client_secret,
        identity_salt=salt,
        identity_pseudonym=pseudonym,
    )


def refresh_reddit_token(directory: Path, profile_id: str) -> RedditTokenLifecycleResult:
    """Exchange the stored refresh token and re-verify the same ad account.

    The credential mutation lock is released before the refresh POST and before
    the identity read, and is reacquired only to store a verified result.
    """
    context = begin_reddit_token_refresh(directory, profile_id)
    try:
        with _open_token_transport(REDDIT_OAUTH_CONTRACT) as transport:
            grant = refresh_access_token(
                context.target(),
                refresh_token=context._refresh_token,
                client_secret=context._client_secret,
                token_transport=transport,
            )
        identity = _verify_identity(context._profile, grant.access_token)
        observed = reddit_account_pseudonym(context.identity_salt, identity.account_client_ref)
        if not hmac.compare_digest(observed, context.identity_pseudonym):
            raise AdhelmError(
                "REDDIT_TOKEN_IDENTITY_MISMATCH",
                "The refreshed Reddit credential resolves to a different ad account.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Do not use this credential. Enroll the intended Reddit account again "
                    "through the local browser flow."
                ),
            )
    except BaseException as failure:
        # A refusal that provably issued no request cannot have consumed the
        # grant, so the profile stays usable and only the durable start is
        # undone. Every other outcome -- a definite provider rejection, an
        # uncertain dispatch, or an unverified identity -- blocks the profile
        # instead of leaving a started refresh behind.
        if _refresh_made_no_egress(failure):
            restore_reddit_token_refresh(directory, context)
        else:
            block_reddit_token_refresh(directory, context)
        raise
    return complete_reddit_token_refresh(directory, context, grant)


def reddit_token_local_status(directory: Path, profile_id: str) -> RedditTokenStatus:
    """Return bounded local status; this makes no Reddit request."""
    return reddit_token_status(directory, profile_id)


def _refresh_made_no_egress(failure: BaseException) -> bool:
    """Return whether the token transport proved that nothing was dispatched."""
    return (
        isinstance(failure, OAuthTransportError)
        and failure.dispatch_state is OAuthTokenDispatchState.NOT_DISPATCHED
    )


def _browser_grant(
    target: OAuthAuthorizationTarget, *, redirect_port: int, client_secret: str
) -> OAuthInitialGrant:
    with LoopbackRedirectReceiver(port=redirect_port, path=REDDIT_CALLBACK_PATH) as receiver:
        attempt = start_authorization(target, receiver.redirect_uri)
        open_system_browser(attempt)
        raw_query = receiver.receive_query()
        receiver.require_unexpired()
        callback = consume_callback(attempt, raw_query)
        with _open_token_transport(target.contract) as transport:
            receiver.require_unexpired()
            return exchange_authorization_code(
                attempt, callback, token_transport=transport, client_secret=client_secret
            )


def _verify_identity(profile: Profile, access_token: str) -> RedditAdsIdentity:
    """Read the one fixed account identity with the exact in-memory bearer."""
    account = checked_account_ref(profile)
    client = _open_read_client()
    try:
        adapter = RedditAdsClient(
            SafeHttpClient(client, {REDDIT_ADS_API_HOST}), lambda: access_token
        )
        return adapter.discover_identity(account, expected_account_client_ref=account)
    finally:
        client.close()


def _open_token_transport(contract: OAuthProviderContract) -> FixedOAuthTokenTransport:
    """Production token transport seam; tests supply a fake client instead."""
    return FixedOAuthTokenTransport.open(contract)


def _open_read_client() -> httpx.Client:
    """Production identity-read client seam; tests supply a mock transport."""
    return httpx.Client(trust_env=False, follow_redirects=False)


def _prompt_client_id() -> str:
    """Collect the customer application's client ID without echo."""
    return prompt_hidden_secret(_CLIENT_ID_PROMPT)


def _prompt_client_secret() -> str:
    """Collect the customer application's client secret without echo."""
    return prompt_hidden_secret(_CLIENT_SECRET_PROMPT)


def _require_interactive(interactive: object) -> None:
    if interactive is not True:
        raise AdhelmError(
            "EXPLICIT_BROWSER_HANDOFF_REQUIRED",
            "Reddit enrollment requires an explicitly requested local browser handoff.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Run locally with --interactive; keep client IDs, secrets and tokens out of chat."
            ),
        )
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        raise AdhelmError(
            "INTERACTIVE_TERMINAL_REQUIRED",
            "Reddit enrollment requires a local interactive terminal and desktop browser.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Run enrollment in a local terminal with a desktop browser available.",
        )


def _checked_redirect_port(value: object) -> int:
    if type(value) is not int or not _MIN_REDIRECT_PORT <= value <= _MAX_REDIRECT_PORT:
        raise AdhelmError(
            "REDDIT_TOKEN_REDIRECT_PORT_INVALID",
            "Reddit enrollment needs the exact local callback port you registered.",
            exit_code=ExitCode.INVALID_INPUT,
            next_action=(
                "Register http://127.0.0.1:<port>/oauth/callback as the redirect URI of the "
                "Reddit developer application and pass the same port with --redirect-port."
            ),
        )
    return value


def _configuration(directory: Path) -> Configuration:
    try:
        return validate_configuration(load_configuration(directory))
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None
