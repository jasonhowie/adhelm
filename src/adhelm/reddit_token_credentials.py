"""Protected local credential lifecycle for the Reddit Ads OAuth profile.

The bearer that the read adapter uses stays in the configured profile's
ordinary OS-store slot, so the existing ``auth status`` / ``auth remove-local``
contract keeps working unchanged.  Everything else this lifecycle needs — the
customer application's secret, the permanent refresh token, and the non-secret
expiry/scope/identity state — lives in separate derived slots under a private
credential-store service, so a refresh token can never be written where a bearer
is read.  Reddit's token exchange authenticates the application with its ID and
secret, so the secret is stored and sent again on every refresh.

The lifecycle itself is the provider-neutral
:class:`~adhelm.protected_token_lifecycle.ProtectedTokenLifecycle`; this module
supplies only Reddit's slot set, account-ID validator and diagnostics, and binds
the credential backend, mutation lock and clock seams.  No function here
performs provider egress.
"""

from __future__ import annotations

from pathlib import Path

from adhelm.credential_lock import credential_mutation_lock
from adhelm.oauth_enrollment import OAuthInitialGrant, OAuthRefreshGrant
from adhelm.protected_token_lifecycle import (
    ProtectedTokenLifecycle,
    TokenEnrollmentSlot,
    TokenLifecycleResult,
    TokenProviderSpec,
    TokenRefreshContext,
    TokenStatus,
    default_credential_backend,
    default_utc_now,
)
from adhelm.reddit_token_profiles import (
    REDDIT_ACCOUNT_ID,
    REDDIT_ADS_EDIT_SCOPE,
    REDDIT_ADS_SCOPES_BY_PURPOSE,
    REDDIT_OAUTH_CONTRACT,
    REDDIT_TOKEN_SERVICE,
    reddit_ads_scope_selection,
    reddit_token_reference,
    select_reddit_ads_profile,
)

RedditTokenLifecycleResult = TokenLifecycleResult
RedditTokenStatus = TokenStatus
RedditTokenEnrollmentSlot = TokenEnrollmentSlot
RedditTokenRefreshContext = TokenRefreshContext

REDDIT_TOKEN_SPEC = TokenProviderSpec(
    provider="reddit_ads",
    label="Reddit",
    code_prefix="REDDIT_TOKEN",
    command_group="reddit-token",
    service=REDDIT_TOKEN_SERVICE,
    contract=REDDIT_OAUTH_CONTRACT,
    scopes_by_purpose=REDDIT_ADS_SCOPES_BY_PURPOSE,
    edit_scope=REDDIT_ADS_EDIT_SCOPE,
    private_slots=("CLIENT_SECRET", "REFRESH_TOKEN", "STATE"),
    token_reference=reddit_token_reference,
    select_profile=select_reddit_ads_profile,
    scope_selection=reddit_ads_scope_selection,
    account_id_valid=lambda value: REDDIT_ACCOUNT_ID.fullmatch(value) is not None,
    pseudonym_prefix="reddit_ads_account_v1.",
    pseudonym_domain=b"adhelm/reddit/account-pseudonym/v1\x00",
    refresh_not_granted_message="Reddit did not return a refresh token for this authorization.",
    refresh_not_granted_action=(
        "Re-enroll through the browser flow when the one-hour access token expires; "
        "enrollment requests a permanent refresh token and Reddit declined to issue one."
    ),
)

# Local-only test seams.  The lifecycle reads these three names from this module
# at call time (hence the lambdas), so patching them here still takes effect.
_credential_backend = default_credential_backend
_utc_now = default_utc_now
_LIFECYCLE = ProtectedTokenLifecycle(
    REDDIT_TOKEN_SPEC,
    backend=lambda: _credential_backend(),
    lock=lambda reference: credential_mutation_lock(reference),
    clock=lambda: _utc_now(),
)


def new_identity_salt() -> bytes:
    """Create the private per-profile salt used to pseudonymize the account ID."""
    return _LIFECYCLE.new_identity_salt()


def reddit_account_pseudonym(salt: bytes, account_client_ref: str) -> str:
    """Derive the stable, salted, local-only label for one Reddit ad account."""
    return _LIFECYCLE.account_pseudonym(salt, account_client_ref)


def prepare_reddit_token_enrollment(
    directory: Path, profile_id: str, scopes: tuple[str, ...] | None = None
) -> RedditTokenEnrollmentSlot:
    """Snapshot the four empty slots before any prompt or browser handoff."""
    return _LIFECYCLE.prepare(directory, profile_id, scopes)


def install_reddit_token_grant(
    directory: Path,
    expected_slot: RedditTokenEnrollmentSlot,
    grant: OAuthInitialGrant,
    *,
    client_secret: str,
    identity_salt: bytes,
    identity_pseudonym: str,
) -> RedditTokenLifecycleResult:
    """Commit one verified browser grant into the four exact local slots."""
    return _LIFECYCLE.install(
        directory,
        expected_slot,
        grant,
        client_secret=client_secret,
        identity_salt=identity_salt,
        identity_pseudonym=identity_pseudonym,
    )


def begin_reddit_token_refresh(directory: Path, profile_id: str) -> RedditTokenRefreshContext:
    """Read refresh material and record a durable start, then release every lock."""
    return _LIFECYCLE.begin_refresh(directory, profile_id)


def complete_reddit_token_refresh(
    directory: Path, context: RedditTokenRefreshContext, grant: OAuthRefreshGrant
) -> RedditTokenLifecycleResult:
    """Store one verified refreshed bearer after every HTTP call has finished."""
    return _LIFECYCLE.complete_refresh(directory, context, grant)


def restore_reddit_token_refresh(directory: Path, context: RedditTokenRefreshContext) -> None:
    """Undo a durable refresh start that provably consumed nothing."""
    _LIFECYCLE.restore_refresh(directory, context)


def block_reddit_token_refresh(directory: Path, context: RedditTokenRefreshContext) -> None:
    """Mark the profile as needing browser reauthorization after a failed refresh."""
    _LIFECYCLE.block_refresh(directory, context)


def reddit_token_status(directory: Path, profile_id: str) -> RedditTokenStatus:
    """Return bounded local state without a secret, reference, or provider call."""
    return _LIFECYCLE.status(directory, profile_id)


def remove_local_reddit_token_slots(directory: Path, profile_id: str) -> tuple[str, int]:
    """Delete only the protected Reddit-owned slots for one configured profile.

    The configured bearer slot is deliberately untouched: it belongs to the
    shared ``auth remove-local`` contract.  Nothing here contacts Reddit, and
    a successful removal is never evidence of provider-side revocation.
    """
    return _LIFECYCLE.remove_local(directory, profile_id)
