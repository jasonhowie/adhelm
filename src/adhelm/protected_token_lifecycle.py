"""Provider-neutral protected local credential lifecycle for OAuth token profiles.

The bearer that a read adapter uses stays in the configured profile's ordinary
OS-store slot, so the existing ``auth status`` / ``auth remove-local`` contract
keeps working unchanged.  Everything else a refreshable OAuth lifecycle needs —
an optional application secret, the programmatic refresh token, and the
non-secret expiry/scope/identity state — lives in separate derived slots under a
private credential-store service, so a refresh token can never be written where
a bearer is read.

This module owns the one copy of that security-critical state machine: slot
derivation and ownership, lock ordering, read-back verification of every write,
and the ``ENROLL_STARTED`` / ``REFRESH_STARTED`` / ``REAUTHORIZATION_REQUIRED``
journal.  A provider supplies only a small frozen :class:`TokenProviderSpec`
(its credential-store service, reserved reference prefix, slot set, account-ID
validator, pseudonym domain, edit-scope name and diagnostic-code prefix) and
binds the three runtime seams — credential backend, mutation lock, clock — in its
own thin wrapper module.  A fix to the lifecycle is therefore made exactly once.

No function here performs provider egress.  Refresh is split into an explicit
begin/complete pair precisely so that no credential mutation lock is ever held
across an HTTP call.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, Never

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.config import configuration_transaction
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Configuration, Profile, validate_configuration
from adhelm.oauth_enrollment import (
    OAuthAuthorizationTarget,
    OAuthInitialGrant,
    OAuthProviderContract,
    OAuthRefreshGrant,
    RefreshStatus,
)

GENERIC_SERVICE: Final = "adhelm"
_MAX_STATE_BYTES = 16_384
_MAX_SECRET_CHARS = 16_384
_SECRET_TEXT = re.compile(r"^[\x21-\x7e]{1,16384}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_HEX_SALT = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_UTC_TIME = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{6})?\+00:00$"
)
_SALT_BYTES = 32
_STATE_ISSUER = object()
_LOCAL_STATES = frozenset(
    {"READY", "ENROLL_STARTED", "REFRESH_STARTED", "REAUTHORIZATION_REQUIRED"}
)

TokenLocalState = Literal["READY", "ENROLL_STARTED", "REFRESH_STARTED", "REAUTHORIZATION_REQUIRED"]
TokenReportedState = Literal["READY", "MISSING", "UNVERIFIED", "REAUTHORIZATION_REQUIRED"]


@dataclass(frozen=True, slots=True, eq=False)
class TokenProviderSpec:
    """The closed, non-secret description of one provider's protected token slots.

    Identity comparison is deliberate: an enrollment slot or refresh context
    carries the spec that issued it, and another provider's lifecycle refuses it.
    """

    provider: str
    label: str
    code_prefix: str
    command_group: str
    service: str
    contract: OAuthProviderContract
    scopes_by_purpose: Mapping[str, frozenset[str]]
    edit_scope: str
    private_slots: tuple[str, ...]
    # The slot parameter is Any so a provider's public selector can narrow it to its
    # own closed Literal; the shared derivation still rejects unknown slots at runtime.
    token_reference: Callable[[str, Any], str]
    select_profile: Callable[[object, object], Profile]
    scope_selection: Callable[[str, tuple[str, ...] | None], tuple[str, ...]]
    account_id_valid: Callable[[str], bool]
    pseudonym_prefix: str
    pseudonym_domain: bytes
    refresh_not_granted_message: str
    refresh_not_granted_action: str

    @property
    def stores_client_secret(self) -> bool:
        return "CLIENT_SECRET" in self.private_slots


@dataclass(frozen=True, slots=True)
class TokenLifecycleResult:
    """A safe local outcome; it is not provider authorization or account authority."""

    provider: str
    profile_configuration_digest: str
    purpose: Literal["READ", "EDIT"]
    operation: Literal["ENROLL", "REFRESH"]
    granted_scopes: tuple[str, ...]
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None
    refresh_status: RefreshStatus
    identity_pseudonym: str
    unrequested_granted_scopes: tuple[str, ...] = ()
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    local_state: Literal["READY"] = "READY"
    app_review_verified: Literal[False] = False
    execution_authority: Literal[False] = False


@dataclass(frozen=True, slots=True)
class TokenStatus:
    """Bounded local status; it contains no secret and contacts no provider."""

    provider: str
    profile_configuration_digest: str
    purpose: Literal["READ", "EDIT"]
    local_state: TokenReportedState
    granted_scopes: tuple[str, ...]
    access_token_expires_at: datetime | None
    access_token_expired: bool | None
    refresh_token_expires_at: datetime | None
    refresh_status: RefreshStatus | None
    identity_pseudonym: str | None
    bearer_slot_present: bool
    refresh_slot_present: bool
    credential_source: Literal["OS_STORE"] = "OS_STORE"
    app_review_verified: Literal[False] = False
    execution_authority: Literal[False] = False


@dataclass(frozen=True, slots=True, repr=False)
class TokenEnrollmentSlot:
    """Opaque pre-browser snapshot of the exact slots one profile owns."""

    _spec: TokenProviderSpec
    _configuration_digest: str
    _profile: Profile
    _scopes: tuple[str, ...]
    _expected: tuple[tuple[str, str], ...]
    _issuer: object

    def __repr__(self) -> str:
        return f"<{self._spec.label}TokenEnrollmentSlot redacted>"

    def __reduce__(self) -> Never:
        raise TypeError(f"{self._spec.label} token slots cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError(f"{self._spec.label} token slots cannot be serialized")


@dataclass(frozen=True, slots=True, repr=False)
class TokenRefreshContext:
    """Secret-bearing refresh context issued under a lock that is then released."""

    _spec: TokenProviderSpec
    _configuration_digest: str
    _profile: Profile
    _client_ref: str
    _client_secret: str | None
    _refresh_token: str
    _scopes: tuple[str, ...]
    _identity_salt: bytes
    _identity_pseudonym: str
    _started_state: str
    _issuer: object

    def __repr__(self) -> str:
        return f"<{self._spec.label}TokenRefreshContext redacted>"

    def __reduce__(self) -> Never:
        raise TypeError(f"{self._spec.label} refresh contexts cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError(f"{self._spec.label} refresh contexts cannot be serialized")

    def target(self) -> OAuthAuthorizationTarget:
        """Return the non-secret authorization target for the refresh grant."""
        return OAuthAuthorizationTarget(
            contract=self._spec.contract, client_ref=self._client_ref, scopes=self._scopes
        )

    @property
    def identity_pseudonym(self) -> str:
        return self._identity_pseudonym

    @property
    def identity_salt(self) -> bytes:
        return self._identity_salt


@dataclass(frozen=True, slots=True, repr=False)
class _TokenState:
    """The one canonical non-secret record stored in the private state slot."""

    profile_id: str
    profile_digest: str
    client_ref: str
    granted_scopes: tuple[str, ...]
    access_token_expires_at: datetime
    refresh_token_expires_at: datetime | None
    refresh_status: RefreshStatus
    identity_salt: bytes
    identity_pseudonym: str
    local_state: TokenLocalState
    updated_at: datetime


def default_credential_backend() -> object:
    """Return the configured OS credential backend (late import, as elsewhere)."""
    from adhelm.credentials import _backend

    return _backend()


def default_utc_now() -> datetime:
    """Private trusted-time seam for deterministic local-only tests."""
    return datetime.now(UTC)


class ProtectedTokenLifecycle:
    """The protected-slot lifecycle for one provider spec.

    ``backend``, ``lock`` and ``clock`` are late-bound callables.  A provider
    wrapper passes lambdas that read its own module attributes at call time, so
    those module attributes stay the single patch points for local-only tests.
    """

    def __init__(
        self,
        spec: TokenProviderSpec,
        *,
        backend: Callable[[], object],
        lock: Callable[[str], AbstractContextManager[object]],
        clock: Callable[[], datetime],
    ) -> None:
        self._spec = spec
        self._backend = backend
        self._lock = lock
        self._clock = clock

    # ------------------------------------------------------------------ public

    def new_identity_salt(self) -> bytes:
        """Create the private per-profile salt used to pseudonymize the account ID."""
        try:
            value = secrets.token_bytes(_SALT_BYTES)
        except Exception:
            raise self._randomness_unavailable() from None
        if type(value) is not bytes or len(value) != _SALT_BYTES:
            raise self._randomness_unavailable()
        return value

    def account_pseudonym(self, salt: bytes, account_client_ref: str) -> str:
        """Derive the stable, salted, local-only label for one ad account."""
        spec = self._spec
        if type(salt) is not bytes or len(salt) != _SALT_BYTES:
            raise self._state_invalid()
        if type(account_client_ref) is not str or not spec.account_id_valid(account_client_ref):
            raise self._state_invalid()
        material = spec.pseudonym_domain + account_client_ref.encode("ascii")
        return spec.pseudonym_prefix + hmac.new(salt, material, hashlib.sha256).hexdigest()

    def prepare(
        self, directory: Path, profile_id: str, scopes: tuple[str, ...] | None = None
    ) -> TokenEnrollmentSlot:
        """Snapshot the empty slots before any prompt or browser handoff."""
        spec = self._spec
        with configuration_transaction(directory) as transaction:
            configuration = self._configuration(transaction.read())
            profile = spec.select_profile(configuration, profile_id)
            selection = spec.scope_selection(profile.purpose, scopes)
            configuration_digest = digest(configuration)
            references = self._references(profile)
            expected: list[tuple[str, str]] = []
            with ExitStack() as stack:
                backend = self._backend()
                for reference in sorted(references.values()):
                    stack.enter_context(self._lock(reference))
                for reference in sorted(references.values()):
                    existing = self._read_slot(
                        backend, self._service_for(references, reference), reference
                    )
                    if existing is not None:
                        raise self._credential_already_exists()
                    expected.append((reference, self._slot_value_hmac(reference, existing)))
        return TokenEnrollmentSlot(
            _spec=spec,
            _configuration_digest=configuration_digest,
            _profile=profile,
            _scopes=selection,
            _expected=tuple(expected),
            _issuer=_STATE_ISSUER,
        )

    def install(
        self,
        directory: Path,
        expected_slot: TokenEnrollmentSlot,
        grant: OAuthInitialGrant,
        *,
        client_secret: str | None,
        identity_salt: bytes,
        identity_pseudonym: str,
    ) -> TokenLifecycleResult:
        """Commit one verified browser grant into the exact local slots.

        The state slot records a durable ``ENROLL_STARTED`` before any secret is
        written, so an interrupted install is reported as unverified local state
        rather than as a usable profile.
        """
        spec = self._spec
        slot = self._checked_enrollment_slot(expected_slot)
        if spec.stores_client_secret:
            self._validate_secret(client_secret)
        elif client_secret is not None:
            raise self._state_invalid()
        if type(grant) is not OAuthInitialGrant or grant.target.scopes != slot._scopes:
            raise self._grant_invalid()
        if type(identity_pseudonym) is not str or not identity_pseudonym.startswith(
            spec.pseudonym_prefix
        ):
            raise self._state_invalid()
        self.require_purpose_allowlist(self._purpose(slot._profile), grant.granted_scopes)
        now = self._checked_now()
        references = self._references(slot._profile)
        write_started = False
        try:
            with configuration_transaction(directory) as transaction:
                configuration = self._configuration(
                    transaction.require_unchanged(slot._configuration_digest)
                )
                profile = spec.select_profile(configuration, slot._profile.profile_id)
                if not hmac.compare_digest(digest(profile), digest(slot._profile)):
                    raise self._credential_changed()
                with ExitStack() as stack:
                    backend = self._backend()
                    for reference in sorted(references.values()):
                        stack.enter_context(self._lock(reference))
                    self._require_expected(backend, references, slot._expected)
                    state = _TokenState(
                        profile_id=profile.profile_id,
                        profile_digest=digest(profile),
                        client_ref=grant.target.client_ref,
                        granted_scopes=grant.granted_scopes,
                        access_token_expires_at=grant.access_token_expires_at,
                        refresh_token_expires_at=grant.refresh_token_expires_at,
                        refresh_status=grant.refresh_status,
                        identity_salt=identity_salt,
                        identity_pseudonym=identity_pseudonym,
                        local_state="ENROLL_STARTED",
                        updated_at=now,
                    )
                    write_started = True
                    self._write_slot(
                        backend, spec.service, references["STATE"], self._encode(state)
                    )
                    if client_secret is not None:
                        self._write_slot(
                            backend, spec.service, references["CLIENT_SECRET"], client_secret
                        )
                    if grant.refresh_token is not None:
                        self._write_slot(
                            backend, spec.service, references["REFRESH_TOKEN"], grant.refresh_token
                        )
                    self._write_slot(
                        backend, GENERIC_SERVICE, references["ACCESS"], grant.access_token
                    )
                    ready = self._replace_state(
                        state, local_state="READY", updated_at=self._checked_now()
                    )
                    self._write_slot(
                        backend, spec.service, references["STATE"], self._encode(ready)
                    )
                transaction.require_unchanged(slot._configuration_digest)
        except AdhelmError:
            if write_started:
                raise self._unverified_local_change() from None
            raise
        except Exception:
            if write_started:
                raise self._unverified_local_change() from None
            raise self._store_unavailable() from None
        return TokenLifecycleResult(
            provider=spec.provider,
            profile_configuration_digest=digest(slot._profile),
            purpose=self._purpose(slot._profile),
            operation="ENROLL",
            granted_scopes=grant.granted_scopes,
            access_token_expires_at=grant.access_token_expires_at,
            refresh_token_expires_at=grant.refresh_token_expires_at,
            refresh_status=grant.refresh_status,
            identity_pseudonym=identity_pseudonym,
            unrequested_granted_scopes=grant.unrequested_granted_scopes,
        )

    def begin_refresh(self, directory: Path, profile_id: str) -> TokenRefreshContext:
        """Read refresh material and record a durable start, then release every lock.

        The returned context carries secrets in memory only.  The caller performs
        provider egress with no credential mutation lock held.
        """
        spec = self._spec
        try:
            with configuration_transaction(directory) as transaction:
                configuration = self._configuration(transaction.read())
                profile = spec.select_profile(configuration, profile_id)
                configuration_digest = digest(configuration)
                references = self._references(profile)
                with ExitStack() as stack:
                    backend = self._backend()
                    for reference in sorted(references.values()):
                        stack.enter_context(self._lock(reference))
                    raw_state = self._read_slot(backend, spec.service, references["STATE"])
                    if raw_state is None:
                        raise self._enrollment_required()
                    state = self._decode_state(raw_state, profile)
                    if state.local_state != "READY":
                        raise self._reauthorization_required()
                    if state.refresh_status != "GRANTED":
                        raise self._refresh_not_granted()
                    refresh_token = self._read_slot(
                        backend, spec.service, references["REFRESH_TOKEN"]
                    )
                    client_secret = (
                        self._read_slot(backend, spec.service, references["CLIENT_SECRET"])
                        if spec.stores_client_secret
                        else None
                    )
                    if refresh_token is None or (
                        spec.stores_client_secret and client_secret is None
                    ):
                        raise self._enrollment_required()
                    self._validate_secret(refresh_token)
                    if client_secret is not None:
                        self._validate_secret(client_secret)
                    now = self._checked_now()
                    if (
                        state.refresh_token_expires_at is not None
                        and state.refresh_token_expires_at <= now
                    ):
                        raise self._refresh_token_expired()
                    started = self._replace_state(
                        state, local_state="REFRESH_STARTED", updated_at=now
                    )
                    encoded = self._encode(started)
                    self._write_slot(backend, spec.service, references["STATE"], encoded)
                transaction.require_unchanged(configuration_digest)
        except AdhelmError:
            raise
        except Exception:
            raise self._store_unavailable() from None
        return TokenRefreshContext(
            _spec=spec,
            _configuration_digest=configuration_digest,
            _profile=profile,
            _client_ref=state.client_ref,
            _client_secret=client_secret,
            _refresh_token=refresh_token,
            _scopes=state.granted_scopes,
            _identity_salt=state.identity_salt,
            _identity_pseudonym=state.identity_pseudonym,
            _started_state=encoded,
            _issuer=_STATE_ISSUER,
        )

    def complete_refresh(
        self, directory: Path, context: TokenRefreshContext, grant: OAuthRefreshGrant
    ) -> TokenLifecycleResult:
        """Store one verified refreshed bearer after every HTTP call has finished."""
        spec = self._spec
        label = spec.label
        checked = self._checked_refresh_context(context)
        if type(grant) is not OAuthRefreshGrant or grant.target.client_ref != checked._client_ref:
            raise self._grant_invalid()
        returned = checked._scopes if grant.granted_scopes is None else grant.granted_scopes
        if not set(checked._scopes) <= set(returned):
            raise AdhelmError(
                f"{spec.code_prefix}_SCOPE_REDUCED",
                f"The refreshed {label} grant no longer covers the enrolled permissions.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=f"Re-enroll the {label} profile with the required permissions.",
            )
        # A refresh renews an existing consent; it is never a new consent step. The
        # recorded set therefore stays exactly the enrolled set, and any wider echo
        # is reported rather than persisted.
        scopes = checked._scopes
        unrequested = tuple(
            sorted((set(returned) - set(scopes)) | set(grant.unrequested_granted_scopes))
        )
        self.require_purpose_allowlist(self._purpose(checked._profile), scopes)
        references = self._references(checked._profile)
        now = self._checked_now()
        write_started = False
        try:
            with configuration_transaction(directory) as transaction:
                configuration = self._configuration(
                    transaction.require_unchanged(checked._configuration_digest)
                )
                profile = spec.select_profile(configuration, checked._profile.profile_id)
                if not hmac.compare_digest(digest(profile), digest(checked._profile)):
                    raise self._credential_changed()
                with ExitStack() as stack:
                    backend = self._backend()
                    for reference in sorted(references.values()):
                        stack.enter_context(self._lock(reference))
                    observed = self._read_slot(backend, spec.service, references["STATE"])
                    if observed is None or not hmac.compare_digest(
                        observed, checked._started_state
                    ):
                        raise self._credential_changed()
                    started = self._decode_state(observed, profile)
                    write_started = True
                    self._write_slot(
                        backend, GENERIC_SERVICE, references["ACCESS"], grant.access_token
                    )
                    if grant.rotated_refresh_token is not None:
                        self._write_slot(
                            backend,
                            spec.service,
                            references["REFRESH_TOKEN"],
                            grant.rotated_refresh_token,
                        )
                    completed = _TokenState(
                        profile_id=started.profile_id,
                        profile_digest=started.profile_digest,
                        client_ref=started.client_ref,
                        granted_scopes=scopes,
                        access_token_expires_at=grant.access_token_expires_at,
                        refresh_token_expires_at=(
                            # A refresh response that restates no refresh-token
                            # expiry is missing information, never "no expiry".
                            # Dropping a recorded expiry would silently disable
                            # the expiry guard.
                            grant.rotated_refresh_token_expires_at
                            if grant.rotated_refresh_token_expires_at is not None
                            else started.refresh_token_expires_at
                        ),
                        refresh_status="GRANTED",
                        identity_salt=started.identity_salt,
                        identity_pseudonym=started.identity_pseudonym,
                        local_state="READY",
                        updated_at=now,
                    )
                    self._write_slot(
                        backend, spec.service, references["STATE"], self._encode(completed)
                    )
                transaction.require_unchanged(checked._configuration_digest)
        except AdhelmError:
            if write_started:
                raise self._unverified_local_change() from None
            raise
        except Exception:
            if write_started:
                raise self._unverified_local_change() from None
            raise self._store_unavailable() from None
        return TokenLifecycleResult(
            provider=spec.provider,
            profile_configuration_digest=digest(checked._profile),
            purpose=self._purpose(checked._profile),
            operation="REFRESH",
            granted_scopes=completed.granted_scopes,
            access_token_expires_at=completed.access_token_expires_at,
            refresh_token_expires_at=completed.refresh_token_expires_at,
            refresh_status="GRANTED",
            identity_pseudonym=completed.identity_pseudonym,
            unrequested_granted_scopes=unrequested,
        )

    def restore_refresh(self, directory: Path, context: TokenRefreshContext) -> None:
        """Undo a durable refresh start that provably consumed nothing.

        This is used only when the failure classified itself as ``NOT_DISPATCHED``:
        no token request left this machine, so the stored refresh token is
        untouched and the profile must stay usable.  It is best effort and never
        returns a credential.
        """
        self._rewrite_started_refresh_state(directory, context, "READY")

    def block_refresh(self, directory: Path, context: TokenRefreshContext) -> None:
        """Mark the profile as needing browser reauthorization after a failed refresh.

        This is best effort: it never replaces the original failure, and it never
        returns a usable credential.
        """
        self._rewrite_started_refresh_state(directory, context, "REAUTHORIZATION_REQUIRED")

    def status(self, directory: Path, profile_id: str) -> TokenStatus:
        """Return bounded local state without a secret, reference, or provider call."""
        spec = self._spec
        try:
            with configuration_transaction(directory) as transaction:
                configuration = self._configuration(transaction.read())
                profile = spec.select_profile(configuration, profile_id)
                configuration_digest = digest(configuration)
                references = self._references(profile)
                with ExitStack() as stack:
                    backend = self._backend()
                    for reference in sorted(references.values()):
                        stack.enter_context(self._lock(reference))
                    raw_state = self._read_slot(backend, spec.service, references["STATE"])
                    bearer_present = (
                        self._read_slot(backend, GENERIC_SERVICE, references["ACCESS"]) is not None
                    )
                    refresh_present = (
                        self._read_slot(backend, spec.service, references["REFRESH_TOKEN"])
                        is not None
                    )
                transaction.require_unchanged(configuration_digest)
        except AdhelmError:
            raise
        except Exception:
            raise self._store_unavailable() from None
        purpose = self._purpose(profile)
        if raw_state is None:
            return self._unreported_status(
                profile, purpose, "MISSING", bearer_present, refresh_present
            )
        try:
            state = self._decode_state(raw_state, profile)
        except AdhelmError:
            return self._unreported_status(
                profile, purpose, "UNVERIFIED", bearer_present, refresh_present
            )
        reported: TokenReportedState
        if state.local_state == "READY":
            reported = "READY" if bearer_present else "UNVERIFIED"
        elif state.local_state == "REAUTHORIZATION_REQUIRED":
            reported = "REAUTHORIZATION_REQUIRED"
        else:
            # An interrupted enroll or refresh is never reported as usable.
            reported = "UNVERIFIED"
        return TokenStatus(
            provider=spec.provider,
            profile_configuration_digest=digest(profile),
            purpose=purpose,
            local_state=reported,
            granted_scopes=state.granted_scopes,
            access_token_expires_at=state.access_token_expires_at,
            access_token_expired=state.access_token_expires_at <= self._checked_now(),
            refresh_token_expires_at=state.refresh_token_expires_at,
            refresh_status=state.refresh_status,
            identity_pseudonym=state.identity_pseudonym,
            bearer_slot_present=bearer_present,
            refresh_slot_present=refresh_present,
        )

    def remove_local(self, directory: Path, profile_id: str) -> tuple[str, int]:
        """Delete only the protected provider-owned slots for one configured profile.

        The configured bearer slot is deliberately untouched: it belongs to the
        shared ``auth remove-local`` contract.  Nothing here contacts the
        provider, and a successful removal is never evidence of provider-side
        revocation.
        """
        spec = self._spec
        removed = 0
        try:
            with configuration_transaction(directory) as transaction:
                configuration = self._configuration(transaction.read())
                profile = spec.select_profile(configuration, profile_id)
                configuration_digest = digest(configuration)
                private = sorted(
                    reference
                    for name, reference in self._references(profile).items()
                    if name != "ACCESS"
                )
                with ExitStack() as stack:
                    backend = self._backend()
                    for reference in private:
                        stack.enter_context(self._lock(reference))
                    for reference in private:
                        if self._raw_slot(backend, spec.service, reference) is None:
                            continue
                        self._delete_slot(backend, spec.service, reference)
                        removed += 1
                transaction.require_unchanged(configuration_digest)
        except AdhelmError:
            raise
        except Exception:
            raise self._store_unavailable() from None
        return digest(profile), removed

    def require_purpose_allowlist(self, purpose: str, scopes: tuple[str, ...]) -> None:
        """Refuse to record a permission this profile purpose may never hold.

        The generic flow already clamps a wider echoed grant to the requested set.
        This is the second, provider-specific check: a READ profile can never
        store the edit scope, whatever the provider echoed and whatever an earlier
        consent granted.
        """
        spec = self._spec
        allowed = spec.scopes_by_purpose.get(purpose)
        if allowed is None or not set(scopes) <= allowed:
            # Deliberately a different code from {prefix}_SCOPE_REJECTED. That
            # one means "the arguments you passed named a scope outside the
            # allowlist" and is fixed by changing the request (exit 2). This one
            # means "what the provider actually granted may never be recorded
            # against this profile purpose" and cannot be fixed by changing
            # arguments (exit 4). One code must never carry two recovery
            # contracts.
            raise AdhelmError(
                f"{spec.code_prefix}_SCOPE_NOT_RECORDABLE",
                f"The {spec.label} grant covers permissions this profile purpose may never record.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    f"Enroll a separate EDIT profile for {spec.edit_scope}; a READ profile "
                    "never records the write permission, whatever an earlier consent granted."
                ),
            )

    # ----------------------------------------------------------------- helpers

    def _rewrite_started_refresh_state(
        self,
        directory: Path,
        context: TokenRefreshContext,
        local_state: TokenLocalState,
    ) -> None:
        """Replace only the exact ``REFRESH_STARTED`` value this context wrote."""
        spec = self._spec
        try:
            checked = self._checked_refresh_context(context)
            references = self._references(checked._profile)
            with configuration_transaction(directory) as transaction:
                configuration = self._configuration(transaction.read())
                profile = spec.select_profile(configuration, checked._profile.profile_id)
                with ExitStack() as stack:
                    backend = self._backend()
                    for reference in sorted(references.values()):
                        stack.enter_context(self._lock(reference))
                    observed = self._read_slot(backend, spec.service, references["STATE"])
                    if observed is None or not hmac.compare_digest(
                        observed, checked._started_state
                    ):
                        return
                    rewritten = self._replace_state(
                        self._decode_state(observed, profile),
                        local_state=local_state,
                        updated_at=self._checked_now(),
                    )
                    self._write_slot(
                        backend, spec.service, references["STATE"], self._encode(rewritten)
                    )
        except Exception:
            return

    def _unreported_status(
        self,
        profile: Profile,
        purpose: Literal["READ", "EDIT"],
        local_state: TokenReportedState,
        bearer_present: bool,
        refresh_present: bool,
    ) -> TokenStatus:
        return TokenStatus(
            provider=self._spec.provider,
            profile_configuration_digest=digest(profile),
            purpose=purpose,
            local_state=local_state,
            granted_scopes=(),
            access_token_expires_at=None,
            access_token_expired=None,
            refresh_token_expires_at=None,
            refresh_status=None,
            identity_pseudonym=None,
            bearer_slot_present=bearer_present,
            refresh_slot_present=refresh_present,
        )

    def _references(self, profile: Profile) -> dict[str, str]:
        references = {"ACCESS": profile.credential_ref}
        for slot in self._spec.private_slots:
            references[slot] = self._spec.token_reference(profile.profile_id, slot)
        return references

    def _service_for(self, references: dict[str, str], reference: str) -> str:
        return GENERIC_SERVICE if reference == references["ACCESS"] else self._spec.service

    def _require_expected(
        self, backend: object, references: dict[str, str], expected: tuple[tuple[str, str], ...]
    ) -> None:
        if len(expected) != len(references):
            raise self._credential_changed()
        for reference, value_hmac in expected:
            observed = self._read_slot(backend, self._service_for(references, reference), reference)
            if not hmac.compare_digest(self._slot_value_hmac(reference, observed), value_hmac):
                raise self._credential_changed()

    def _slot_value_hmac(self, reference: str, value: str | None) -> str:
        material = f"{reference}\x00".encode() + (b"" if value is None else value.encode("utf-8"))
        marker = b"absent" if value is None else b"present"
        return hashlib.sha256(self._spec.pseudonym_domain + marker + b"\x00" + material).hexdigest()

    @staticmethod
    def _replace_state(
        state: _TokenState, *, local_state: TokenLocalState, updated_at: datetime
    ) -> _TokenState:
        return _TokenState(
            profile_id=state.profile_id,
            profile_digest=state.profile_digest,
            client_ref=state.client_ref,
            granted_scopes=state.granted_scopes,
            access_token_expires_at=state.access_token_expires_at,
            refresh_token_expires_at=state.refresh_token_expires_at,
            refresh_status=state.refresh_status,
            identity_salt=state.identity_salt,
            identity_pseudonym=state.identity_pseudonym,
            local_state=local_state,
            updated_at=updated_at,
        )

    def _encode(self, state: _TokenState) -> str:
        data: dict[str, object] = {
            "access_token_expires_at": self._time_text(state.access_token_expires_at),
            "client_ref": state.client_ref,
            "granted_scopes": list(state.granted_scopes),
            "identity_pseudonym": state.identity_pseudonym,
            "identity_salt": state.identity_salt.hex(),
            "local_state": state.local_state,
            "profile_digest": state.profile_digest,
            "profile_id": state.profile_id,
            "refresh_status": state.refresh_status,
            "refresh_token_expires_at": (
                None
                if state.refresh_token_expires_at is None
                else self._time_text(state.refresh_token_expires_at)
            ),
            "schema_version": 1,
            "updated_at": self._time_text(state.updated_at),
        }
        try:
            return canonical_bytes(data).decode("utf-8")
        except Exception:
            raise self._state_invalid() from None

    def _decode_state(self, raw: str, profile: Profile) -> _TokenState:
        spec = self._spec
        if type(raw) is not str or not 1 <= len(raw.encode("utf-8")) <= _MAX_STATE_BYTES:
            raise self._state_invalid()
        try:
            data = parse_json(raw.encode("utf-8"), maximum_bytes=_MAX_STATE_BYTES)
        except Exception:
            raise self._state_invalid() from None
        if type(data) is not dict or data.get("schema_version") != 1:
            raise self._state_invalid()
        allowed = {
            "access_token_expires_at",
            "client_ref",
            "granted_scopes",
            "identity_pseudonym",
            "identity_salt",
            "local_state",
            "profile_digest",
            "profile_id",
            "refresh_status",
            "refresh_token_expires_at",
            "schema_version",
            "updated_at",
        }
        if set(data) != allowed:
            raise self._state_invalid()
        scopes = data["granted_scopes"]
        # A recorded permission is only usable if this profile purpose may hold it.
        # The contract's full supported set is not enough: a state slot written by
        # an earlier build, or by anything else with credential-store write access,
        # must not be able to present the edit scope to a READ profile as a usable
        # local contract.
        recordable = spec.scopes_by_purpose.get(self._purpose(profile), frozenset())
        salt = data["identity_salt"]
        pseudonym = data["identity_pseudonym"]
        local_state = data["local_state"]
        refresh_status = data["refresh_status"]
        client_ref = data["client_ref"]
        prefix = spec.pseudonym_prefix
        if (
            type(scopes) is not list
            or not 1 <= len(scopes) <= 8
            or any(type(scope) is not str for scope in scopes)
            or list(scopes) != sorted(set(scopes))
            or not set(scopes) <= recordable
            or type(salt) is not str
            or _HEX_SALT.fullmatch(salt) is None
            or type(pseudonym) is not str
            or not pseudonym.startswith(prefix)
            or _DIGEST.fullmatch(pseudonym[len(prefix) :]) is None
            or local_state not in _LOCAL_STATES
            or refresh_status not in {"GRANTED", "REFRESH_NOT_GRANTED"}
            or type(client_ref) is not str
            or not 1 <= len(client_ref) <= 200
            or any(not 33 <= ord(character) <= 126 for character in client_ref)
            or type(data["profile_id"]) is not str
            or data["profile_id"] != profile.profile_id
            or type(data["profile_digest"]) is not str
            or _DIGEST.fullmatch(data["profile_digest"]) is None
        ):
            raise self._state_invalid()
        if not hmac.compare_digest(data["profile_digest"], digest(profile)):
            raise AdhelmError(
                f"{spec.code_prefix}_PROFILE_CHANGED",
                f"The configured {spec.label} profile changed after this credential was stored.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    f"Remove the local {spec.label} credential and enroll the current profile "
                    "again."
                ),
            )
        try:
            return _TokenState(
                profile_id=data["profile_id"],
                profile_digest=data["profile_digest"],
                client_ref=client_ref,
                granted_scopes=tuple(scopes),
                access_token_expires_at=self._parse_time(data["access_token_expires_at"]),
                refresh_token_expires_at=(
                    None
                    if data["refresh_token_expires_at"] is None
                    else self._parse_time(data["refresh_token_expires_at"])
                ),
                refresh_status=refresh_status,
                identity_salt=bytes.fromhex(salt),
                identity_pseudonym=pseudonym,
                local_state=local_state,
                updated_at=self._parse_time(data["updated_at"]),
            )
        except AdhelmError:
            raise
        except Exception:
            raise self._state_invalid() from None

    def _checked_enrollment_slot(self, value: object) -> TokenEnrollmentSlot:
        if (
            type(value) is not TokenEnrollmentSlot
            or value._issuer is not _STATE_ISSUER
            or value._spec is not self._spec
        ):
            raise self._slot_invalid()
        if (
            type(value._configuration_digest) is not str
            or _DIGEST.fullmatch(value._configuration_digest) is None
            or type(value._profile) is not Profile
            or type(value._scopes) is not tuple
            or type(value._expected) is not tuple
            or not value._expected
        ):
            raise self._slot_invalid()
        return value

    def _checked_refresh_context(self, value: object) -> TokenRefreshContext:
        if (
            type(value) is not TokenRefreshContext
            or value._issuer is not _STATE_ISSUER
            or value._spec is not self._spec
        ):
            raise self._slot_invalid()
        if (
            type(value._configuration_digest) is not str
            or _DIGEST.fullmatch(value._configuration_digest) is None
            or type(value._profile) is not Profile
            or type(value._started_state) is not str
        ):
            raise self._slot_invalid()
        return value

    def _configuration(self, value: Configuration) -> Configuration:
        try:
            return validate_configuration(value)
        except Exception:
            raise AdhelmError(
                f"{self._spec.code_prefix}_CONFIGURATION_INVALID",
                f"{self._spec.label} token setup requires valid local configuration.",
                exit_code=ExitCode.INVALID_INPUT,
            ) from None

    @staticmethod
    def _purpose(profile: Profile) -> Literal["READ", "EDIT"]:
        return "EDIT" if profile.purpose == "EDIT" else "READ"

    def _read_slot(self, backend: object, service: str, reference: str) -> str | None:
        try:
            value = backend.get_password(service, reference)  # type: ignore[attr-defined]
        except Exception:
            raise self._store_unavailable() from None
        if value is None:
            return None
        if type(value) is not str or not value:
            raise self._state_invalid()
        return value

    def _write_slot(self, backend: object, service: str, reference: str, value: str) -> None:
        try:
            backend.set_password(service, reference, value)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            observed = backend.get_password(service, reference)  # type: ignore[attr-defined]
        except Exception:
            raise self._unverified_local_change() from None
        if type(observed) is not str or not hmac.compare_digest(observed, value):
            raise self._unverified_local_change()

    def _raw_slot(self, backend: object, service: str, reference: str) -> str | None:
        try:
            value = backend.get_password(service, reference)  # type: ignore[attr-defined]
        except Exception:
            raise self._store_unavailable() from None
        return value if type(value) is str and value else None

    def _delete_slot(self, backend: object, service: str, reference: str) -> None:
        try:
            backend.delete_password(service, reference)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            observed = backend.get_password(service, reference)  # type: ignore[attr-defined]
        except Exception:
            raise self._unverified_local_change() from None
        if observed is not None:
            raise self._unverified_local_change()

    def _validate_secret(self, value: object) -> None:
        if (
            type(value) is not str
            or not 1 <= len(value) <= _MAX_SECRET_CHARS
            or _SECRET_TEXT.fullmatch(value) is None
        ):
            raise self._state_invalid()

    def _parse_time(self, value: object) -> datetime:
        if type(value) is not str or _UTC_TIME.fullmatch(value) is None:
            raise self._state_invalid()
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            raise self._state_invalid() from None
        if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
            raise self._state_invalid()
        return parsed.astimezone(UTC)

    def _time_text(self, value: datetime) -> str:
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise self._state_invalid()
        text = value.astimezone(UTC).isoformat()
        if _UTC_TIME.fullmatch(text) is None:
            raise self._state_invalid()
        return text

    def _checked_now(self) -> datetime:
        value = self._clock()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise AdhelmError(
                f"{self._spec.code_prefix}_CLOCK_INVALID",
                f"The {self._spec.label} token lifecycle requires an aware local clock.",
                exit_code=ExitCode.PRECONDITION,
            )
        return value.astimezone(UTC)

    # ------------------------------------------------------------ diagnostics

    def _randomness_unavailable(self) -> AdhelmError:
        return AdhelmError(
            f"{self._spec.code_prefix}_RANDOMNESS_UNAVAILABLE",
            "Secure local randomness is unavailable.",
            exit_code=ExitCode.PRECONDITION,
        )

    def _credential_already_exists(self) -> AdhelmError:
        spec = self._spec
        group = spec.command_group
        return AdhelmError(
            f"{spec.code_prefix}_CREDENTIAL_EXISTS",
            f"A local {spec.label} credential already exists for this profile.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                f"Review `auth {group} status`, then clear both local halves deliberately "
                f"before a new enrollment: `auth {group} remove-local --profile <id>` for the "
                f"protected {spec.label} slots and `auth remove-local --profile <id>` for the "
                "stored access-token entry."
            ),
        )

    def _credential_changed(self) -> AdhelmError:
        spec = self._spec
        return AdhelmError(
            f"{spec.code_prefix}_CREDENTIAL_CHANGED",
            f"Local {spec.label} credential state changed during this operation.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action=(
                f"Inspect `auth {spec.command_group} status` before a deliberate follow-up."
            ),
        )

    def _enrollment_required(self) -> AdhelmError:
        spec = self._spec
        return AdhelmError(
            f"{spec.code_prefix}_ENROLLMENT_REQUIRED",
            f"This {spec.label} profile has no complete local OAuth enrollment.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                f"Run `auth {spec.command_group} enroll --profile <id> --interactive` locally."
            ),
        )

    def _refresh_not_granted(self) -> AdhelmError:
        spec = self._spec
        return AdhelmError(
            "REFRESH_NOT_GRANTED",
            spec.refresh_not_granted_message,
            exit_code=ExitCode.AUTHORIZATION,
            next_action=spec.refresh_not_granted_action,
        )

    def _refresh_token_expired(self) -> AdhelmError:
        spec = self._spec
        return AdhelmError(
            f"{spec.code_prefix}_REFRESH_EXPIRED",
            f"The stored {spec.label} refresh token has passed its recorded expiry.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=f"Re-enroll the {spec.label} profile through the local browser flow.",
        )

    def _reauthorization_required(self) -> AdhelmError:
        spec = self._spec
        return AdhelmError(
            f"{spec.code_prefix}_REAUTHORIZATION_REQUIRED",
            f"The stored {spec.label} credential is blocked pending a new browser authorization.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=f"Re-enroll the {spec.label} profile through the local browser flow.",
        )

    def _grant_invalid(self) -> AdhelmError:
        spec = self._spec
        return AdhelmError(
            f"{spec.code_prefix}_GRANT_INVALID",
            f"The {spec.label} OAuth grant does not match the selected local profile.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=f"Start a new local {spec.label} authorization.",
        )

    def _slot_invalid(self) -> AdhelmError:
        spec = self._spec
        return AdhelmError(
            f"{spec.code_prefix}_SLOT_INVALID",
            f"The {spec.label} credential lifecycle context is invalid.",
            exit_code=ExitCode.PRECONDITION,
            next_action=f"Start the {spec.label} credential operation again from its own command.",
        )

    def _state_invalid(self) -> AdhelmError:
        spec = self._spec
        return AdhelmError(
            f"{spec.code_prefix}_STATE_INVALID",
            f"The protected {spec.label} credential state cannot be used.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=f"Re-enroll the {spec.label} profile through the local browser flow.",
        )

    @staticmethod
    def _store_unavailable() -> AdhelmError:
        return AdhelmError(
            "CREDENTIAL_STORE_UNAVAILABLE",
            "The OS credential store is unavailable.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Check the local OS credential store and retry deliberately.",
        )

    def _unverified_local_change(self) -> AdhelmError:
        spec = self._spec
        return AdhelmError(
            "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED",
            f"A local {spec.label} credential change could not be verified after writing.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action=(
                f"Inspect `auth {spec.command_group} status` before a deliberate new action."
            ),
        )
