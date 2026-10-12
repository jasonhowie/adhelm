"""Protected local credential lifecycle for the LinkedIn Ads OAuth profile.

The bearer that the read adapter uses stays in the configured profile's
ordinary OS-store slot, so the existing ``auth status`` / ``auth remove-local``
contract keeps working unchanged.  Everything else this lifecycle needs — the
customer application's client secret, the programmatic refresh token, and the
non-secret expiry/scope/identity state — lives in separate derived slots under a
private credential-store service.  Separate slots are the durable fix for the
pilot failure in which a refresh token was pasted into the bearer slot.

The lifecycle itself is the provider-neutral
:class:`~adhelm.protected_token_lifecycle.ProtectedTokenLifecycle`; this module
supplies only LinkedIn's slot set, account-ID validator and diagnostics, and
binds the credential backend, mutation lock and clock seams.  No function here
performs provider egress.
"""

from __future__ import annotations

from pathlib import Path

from adhelm.credential_lock import credential_mutation_lock
from adhelm.linkedin_token_profiles import (
    LINKEDIN_ADS_EDIT_SCOPE,
    LINKEDIN_ADS_SCOPES_BY_PURPOSE,
    LINKEDIN_OAUTH_CONTRACT,
    LINKEDIN_TOKEN_SERVICE,
    linkedin_ads_scope_selection,
    linkedin_token_reference,
    select_linkedin_ads_profile,
)
from adhelm.oauth_enrollment import OAuthInitialGrant, OAuthRefreshGrant
from adhelm.protected_token_lifecycle import (
    ProtectedTokenLifecycle,
    TokenEnrollmentSlot,
    TokenLifecycleResult,
    TokenLocalState,
    TokenProviderSpec,
    TokenRefreshContext,
    TokenReportedState,
    TokenStatus,
    default_credential_backend,
    default_utc_now,
)

LinkedInTokenLocalState = TokenLocalState
LinkedInTokenReportedState = TokenReportedState
LinkedInTokenLifecycleResult = TokenLifecycleResult
LinkedInTokenStatus = TokenStatus
LinkedInTokenEnrollmentSlot = TokenEnrollmentSlot
LinkedInTokenRefreshContext = TokenRefreshContext


def _valid_account_id(value: str) -> bool:
    return 1 <= len(value) <= 20 and value.isdecimal()


LINKEDIN_TOKEN_SPEC = TokenProviderSpec(
    provider="linkedin_ads",
    label="LinkedIn",
    code_prefix="LINKEDIN_TOKEN",
    command_group="linkedin-token",
    service=LINKEDIN_TOKEN_SERVICE,
    contract=LINKEDIN_OAUTH_CONTRACT,
    scopes_by_purpose=LINKEDIN_ADS_SCOPES_BY_PURPOSE,
    edit_scope=LINKEDIN_ADS_EDIT_SCOPE,
    private_slots=("CLIENT_SECRET", "REFRESH_TOKEN", "STATE"),
    token_reference=linkedin_token_reference,
    select_profile=select_linkedin_ads_profile,
    scope_selection=linkedin_ads_scope_selection,
    account_id_valid=_valid_account_id,
    pseudonym_prefix="linkedin_ads_account_v1.",
    pseudonym_domain=b"adhelm/linkedin/account-pseudonym/v1\x00",
    refresh_not_granted_message=(
        "LinkedIn did not grant this application a programmatic refresh token."
    ),
    refresh_not_granted_action=(
        "Re-enroll through the browser flow when the access token expires; programmatic "
        "refresh requires LinkedIn to enable it for your application."
    ),
)

# Local-only test seams.  The lifecycle reads these three names from this module
# at call time (hence the lambdas), so patching them here still takes effect.
_credential_backend = default_credential_backend
_utc_now = default_utc_now
_LIFECYCLE = ProtectedTokenLifecycle(
    LINKEDIN_TOKEN_SPEC,
    backend=lambda: _credential_backend(),
    lock=lambda reference: credential_mutation_lock(reference),
    clock=lambda: _utc_now(),
)


def new_identity_salt() -> bytes:
    """Create the private per-profile salt used to pseudonymize the account ID."""
    return _LIFECYCLE.new_identity_salt()


def linkedin_account_pseudonym(salt: bytes, account_client_ref: str) -> str:
    """Derive the stable, salted, local-only label for one LinkedIn ad account."""
    return _LIFECYCLE.account_pseudonym(salt, account_client_ref)


def prepare_linkedin_token_enrollment(
    directory: Path, profile_id: str, scopes: tuple[str, ...] | None = None
) -> LinkedInTokenEnrollmentSlot:
    """Snapshot the four empty slots before any prompt or browser handoff."""
    return _LIFECYCLE.prepare(directory, profile_id, scopes)


def install_linkedin_token_grant(
    directory: Path,
    expected_slot: LinkedInTokenEnrollmentSlot,
    grant: OAuthInitialGrant,
    *,
    client_secret: str,
    identity_salt: bytes,
    identity_pseudonym: str,
) -> LinkedInTokenLifecycleResult:
    """Commit one verified browser grant into the four exact local slots."""
    return _LIFECYCLE.install(
        directory,
        expected_slot,
        grant,
        client_secret=client_secret,
        identity_salt=identity_salt,
        identity_pseudonym=identity_pseudonym,
    )


def begin_linkedin_token_refresh(directory: Path, profile_id: str) -> LinkedInTokenRefreshContext:
    """Read refresh material and record a durable start, then release every lock."""
    return _LIFECYCLE.begin_refresh(directory, profile_id)


def complete_linkedin_token_refresh(
    directory: Path, context: LinkedInTokenRefreshContext, grant: OAuthRefreshGrant
) -> LinkedInTokenLifecycleResult:
    """Store one verified refreshed bearer after every HTTP call has finished."""
    return _LIFECYCLE.complete_refresh(directory, context, grant)


def restore_linkedin_token_refresh(directory: Path, context: LinkedInTokenRefreshContext) -> None:
    """Undo a durable refresh start that provably consumed nothing."""
    _LIFECYCLE.restore_refresh(directory, context)


def block_linkedin_token_refresh(directory: Path, context: LinkedInTokenRefreshContext) -> None:
    """Mark the profile as needing browser reauthorization after a failed refresh."""
    _LIFECYCLE.block_refresh(directory, context)


def linkedin_token_status(directory: Path, profile_id: str) -> LinkedInTokenStatus:
    """Return bounded local state without a secret, reference, or provider call."""
    return _LIFECYCLE.status(directory, profile_id)


def remove_local_linkedin_token_slots(directory: Path, profile_id: str) -> tuple[str, int]:
    """Delete only the protected LinkedIn-owned slots for one configured profile.

    The configured bearer slot is deliberately untouched: it belongs to the
    shared ``auth remove-local`` contract.  Nothing here contacts LinkedIn, and
    a successful removal is never evidence of provider-side revocation.
    """
    return _LIFECYCLE.remove_local(directory, profile_id)


def _require_purpose_allowlist(purpose: str, scopes: tuple[str, ...]) -> None:
    _LIFECYCLE.require_purpose_allowlist(purpose, scopes)
