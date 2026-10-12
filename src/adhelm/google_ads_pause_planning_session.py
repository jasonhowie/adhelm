"""Sealed Google Ads campaign-PAUSE planning observations.

This module is the narrow preparation boundary between the protected
three-slot Google Ads EDIT association and the pure M55 PAUSE contract.  It
performs only the two fixed v25 search requests needed for one fresh customer
and campaign observation.  It has no plan retention, ledger, approval,
mutation, generic credential, provider-client, or serialization surface.

The older :mod:`adhelm.google_ads_edit_session` remains a read-only M54
observer.  This separate facade deliberately does not reuse its random
per-session marker: PAUSE planning needs a stable protected binding reference
and an owner-scoped retention recheck over all three protected slots.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock, get_ident
from typing import Final, Never

import httpx

from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import ConfigurationTransaction, configuration_transaction
from adhelm.errors import AdhelmError, ExitCode
from adhelm.models import Configuration, Profile, validate_configuration
from adhelm.providers.google_ads_campaign_state import (
    GoogleAdsCampaignState,
    GoogleAdsCampaignStateReader,
    GoogleAdsEditIdentity,
    _GoogleAdsEditTarget,
)
from adhelm.providers.google_ads_pause_contract import (
    GoogleAdsCampaignPauseAccountBinding,
    GoogleAdsCampaignPauseCampaignBaseline,
    GoogleAdsCampaignPauseExecutionBinding,
    GoogleAdsCampaignPauseObservationBinding,
)
from adhelm.strict_json import validate_json_value
from adhelm.transport import SafeHttpClient

__all__ = (
    "GoogleAdsCampaignPausePlanningObservation",
    "GoogleAdsCampaignPausePlanningSession",
    "configured_google_ads_pause_planning_session",
)

_HOST: Final = "googleads.googleapis.com"
_MAX_RESPONSE_BYTES: Final = 65_536
_DIGEST: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_SAFE_REF: Final = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$", re.ASCII)
_CAMPAIGN_ID: Final = re.compile(r"^[1-9][0-9]{0,18}$", re.ASCII)
_MAX_INT64: Final = 9_223_372_036_854_775_807
_SCOPE: Final = "https://www.googleapis.com/auth/adwords"
_SESSION_ISSUER = object()
_OBSERVATION_ISSUER = object()
_RLOCK_TYPE: Final = type(RLock())


def _now() -> datetime:
    """Private trusted-clock seam for deterministic fake-only tests."""

    return datetime.now(UTC)


def _google_ads_http_client() -> httpx.Client:
    """Create the sole clean client used by the two fixed v25 searches."""

    return httpx.Client(trust_env=False)


@dataclass(frozen=True, slots=True, repr=False)
class _GoogleAdsPausePlanningContext:
    """Safe detached configuration and M55 binding facts for one facade."""

    _directory: Path
    _profile_id: str
    _configuration_digest: str
    _profile_digest: str
    _target: _GoogleAdsEditTarget
    _execution_binding: GoogleAdsCampaignPauseExecutionBinding

    def __repr__(self) -> str:
        return "<_GoogleAdsPausePlanningContext redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads PAUSE planning contexts cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads PAUSE planning contexts cannot be serialized")


@dataclass(frozen=True, slots=True, repr=False)
class GoogleAdsCampaignPausePlanningObservation:
    """One detached, non-authorizing paired Google Ads state observation.

    The account and campaign facts are reconstructed closed M55 models.  This
    wrapper intentionally has no state-reader, credential, URL, or retention
    capability and cannot be serialized as a generic provider observation.
    """

    _account_binding: GoogleAdsCampaignPauseAccountBinding
    _observation_binding: GoogleAdsCampaignPauseObservationBinding
    _issuer: object

    def __repr__(self) -> str:
        return "<GoogleAdsCampaignPausePlanningObservation redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads PAUSE planning observations cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads PAUSE planning observations cannot be serialized")

    @property
    def account_binding(self) -> GoogleAdsCampaignPauseAccountBinding:
        """Return the detached M55 account baseline for this exact pair."""

        account, _observation = _checked_observation(self)
        return account

    @property
    def observation_binding(self) -> GoogleAdsCampaignPauseObservationBinding:
        """Return the detached M55 campaign baseline and bounded interval."""

        _account, observation = _checked_observation(self)
        return observation


class GoogleAdsCampaignPausePlanningSession:
    """A sealed fixed-observation facade for one future Google Ads PAUSE plan."""

    __slots__ = (
        "_client",
        "_context",
        "_protected_snapshot",
        "_issuer",
        "_lock",
        "_closed",
        "_reader",
        "_retention_token",
        "_retention_owner",
        "_last_clock",
    )

    def __init__(
        self,
        *,
        client: httpx.Client,
        context: _GoogleAdsPausePlanningContext,
        protected_snapshot: oauth_credentials._GoogleAdsPauseProtectedSnapshot,
        issuer: object,
    ) -> None:
        if issuer is not _SESSION_ISSUER:
            raise TypeError("Google Ads PAUSE planning sessions are factory-issued only")
        self._client = client
        self._context = context
        self._protected_snapshot: oauth_credentials._GoogleAdsPauseProtectedSnapshot | None = (
            protected_snapshot
        )
        self._issuer = issuer
        self._lock = RLock()
        self._closed = False
        self._last_clock = context._execution_binding.resolved_at
        self._retention_token: object | None = None
        self._retention_owner: int | None = None
        self._reader = GoogleAdsCampaignStateReader(
            SafeHttpClient(
                client,
                {_HOST},
                maximum_response_bytes=_MAX_RESPONSE_BYTES,
                read_attempts=1,
            ),
            self._headers_for_fixed_request,
            context._target,
        )

    def __repr__(self) -> str:
        return "<GoogleAdsCampaignPausePlanningSession redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Google Ads PAUSE planning sessions cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Google Ads PAUSE planning sessions cannot be serialized")

    @property
    def execution_binding(self) -> GoogleAdsCampaignPauseExecutionBinding:
        """Return only the detached M55 OAuth/developer association declaration."""

        self._require_not_retaining_fast()
        with self._checked_lock():
            self._require_open()
            self._require_not_retaining()
            context = self._checked_context()
            return _checked_execution_binding(context._execution_binding)

    def observe_campaign(self, campaign_id: str) -> GoogleAdsCampaignPausePlanningObservation:
        """Read one fresh customer then one fixed campaign as a bounded pair.

        Each resource POST invokes the no-refresh protected snapshot callback,
        which releases the configuration and credential locks before HTTP.  A
        caller cannot combine a previously exposed identity with this campaign
        state because identity is never a public session property.
        """

        checked_campaign_id = _checked_campaign_id(campaign_id)
        # A retention context holds the session RLock across its callback and
        # ledger append.  Check its token before trying that lock so another
        # thread (and an owner-thread re-entry) fails promptly instead of
        # waiting until the protected retention window has ended.
        self._require_not_retaining_fast()
        with self._checked_lock():
            self._require_open()
            self._require_not_retaining()
            context = self._checked_context()
            # Validate the factory-issued private association before the
            # reader can translate a callback error into a provider-facing
            # diagnostic.  A constructed/altered session must fail locally
            # and before the first identity POST.
            self._checked_protected_snapshot(context)
            reader = self._checked_reader()
            binding = _checked_execution_binding(context._execution_binding)
            started_at = self._sample_clock()
            _check_planning_window(binding, started_at, reader_callback=False)
            identity = _checked_identity(reader.discover_identity())
            state = _checked_campaign_state(
                reader.read_campaign_state(identity, checked_campaign_id)
            )
            finished_at = self._sample_clock()
            _check_planning_window(binding, finished_at, reader_callback=False)
            return _new_observation(
                identity=identity,
                state=state,
                execution_binding=binding,
                read_started_at=started_at,
                read_finished_at=finished_at,
            )

    @contextmanager
    def retention_recheck(self) -> Iterator[Callable[[], None]]:
        """Hold config then all three protected slots for one pre-append check.

        The yielded callback re-reads configuration and the three protected
        slots through handles already held by this context, then validates
        owner, liveness, and clock.  It never reads a ledger, opens a provider
        transport, acquires a backend or lock, refreshes, or writes.
        """

        self._require_not_retaining_fast()
        with self._checked_lock():
            self._require_open()
            self._require_not_retaining()
            context = self._checked_context()
            self._sample_clock()
            token = object()
            owner_thread = get_ident()
            self._retention_token = token
            self._retention_owner = owner_thread
            callback_count = 0
            try:
                with configuration_transaction(context._directory) as transaction:
                    profile = _current_profile(transaction, context)
                    # The private lifecycle helper takes the three credential
                    # locks in its exact casefold ordering and validates all
                    # no-refresh facts before its callback becomes available.
                    with oauth_credentials._google_ads_pause_retention_recheck(
                        profile,
                        context._configuration_digest,
                        self._checked_protected_snapshot(context),
                    ) as held_recheck:
                        self._sample_clock()

                        def recheck() -> None:
                            nonlocal callback_count
                            if (
                                get_ident() != owner_thread
                                or self._retention_owner != owner_thread
                                or self._retention_token is not token
                                or self._closed is not False
                            ):
                                raise _retention_recheck_required()
                            try:
                                # The configuration transaction is already
                                # held.  Re-read it around the held-slot
                                # callback so an uncoordinated manual edit is
                                # caught before the ledger append, without
                                # acquiring another lock.
                                self._sample_clock()
                                _current_profile(transaction, context)
                                self._sample_clock(held_recheck())
                                self._sample_clock()
                                _current_profile(transaction, context)
                            except AdhelmError:
                                raise
                            except Exception:
                                raise _retention_recheck_required() from None
                            callback_count += 1

                        yield recheck
                        if callback_count == 0:
                            raise _retention_recheck_required()
            finally:
                self._retention_token = None
                self._retention_owner = None

    def _headers_for_fixed_request(self) -> dict[str, str]:
        """Make one current no-refresh snapshot into fixed request headers.

        This callback finishes every configuration and protected-slot check
        before it returns.  ``GoogleAdsCampaignStateReader`` therefore sends
        the resource POST only after those locks are released.
        """

        self._require_not_retaining_fast()
        with self._checked_lock():
            self._require_open()
            self._require_not_retaining()
            context = self._checked_context()
        with configuration_transaction(context._directory) as transaction:
            profile = _current_profile(transaction, context)
            binding = _checked_execution_binding(context._execution_binding)
            before_snapshot_now = self._sample_clock()
            try:
                _check_planning_window(binding, before_snapshot_now, reader_callback=True)
                snapshot = oauth_credentials._current_google_ads_pause_protected_snapshot(
                    profile,
                    context._configuration_digest,
                    self._checked_protected_snapshot(context),
                )
                try:
                    # The no-refresh helper obtains its own trusted timestamp
                    # while holding slots.  Obtain a fresh value after it
                    # returns, rather than comparing that later resolved_at
                    # against a clock sample from before the local reads.
                    self._sample_clock(snapshot._resolved_at)
                    _verify_current_snapshot(snapshot, context, binding, self._sample_clock())
                    headers = _headers_from_snapshot(snapshot, context._target)
                    transaction.require_unchanged(context._configuration_digest)
                    return headers
                finally:
                    # Never retain a current no-refresh snapshot outside this
                    # one header callback; the sealed initial snapshot stays
                    # private to the session for future continuity checks.
                    del snapshot
            except AdhelmError as error:
                raise _reader_callback_error(error) from None

    def _sample_clock(self, observed: datetime | None = None) -> datetime:
        """Keep one nondecreasing floor across the complete locked session."""

        try:
            current = _checked_now() if observed is None else _utc_time(observed)
            previous = _utc_time(self._last_clock)
            if current < previous:
                raise _clock_invalid()
        except AdhelmError:
            raise
        except Exception:
            raise _clock_invalid() from None
        self._last_clock = current
        return current

    def _checked_context(self) -> _GoogleAdsPausePlanningContext:
        try:
            issuer = self._issuer
            context = self._context
        except Exception:
            raise _session_invalid() from None
        if issuer is not _SESSION_ISSUER or type(context) is not _GoogleAdsPausePlanningContext:
            raise _session_invalid()
        try:
            if (
                not isinstance(context._directory, Path)
                or type(context._profile_id) is not str
                or _SAFE_REF.fullmatch(context._profile_id) is None
                or type(context._configuration_digest) is not str
                or _DIGEST.fullmatch(context._configuration_digest) is None
                or type(context._profile_digest) is not str
                or _DIGEST.fullmatch(context._profile_digest) is None
            ):
                raise ValueError("context")
            _checked_target(context._target)
            binding = _checked_execution_binding(context._execution_binding)
            if (
                not hmac.compare_digest(binding.configuration_digest, context._configuration_digest)
                or not hmac.compare_digest(
                    binding.profile_configuration_digest, context._profile_digest
                )
                or binding.profile_ref != context._profile_id
            ):
                raise ValueError("binding")
        except AdhelmError:
            raise
        except Exception:
            raise _session_invalid() from None
        return context

    def _checked_protected_snapshot(
        self, context: _GoogleAdsPausePlanningContext
    ) -> oauth_credentials._GoogleAdsPauseProtectedSnapshot:
        try:
            snapshot = self._protected_snapshot
        except Exception:
            raise _session_invalid() from None
        if type(snapshot) is not oauth_credentials._GoogleAdsPauseProtectedSnapshot:
            raise _session_invalid()
        try:
            checked_snapshot = oauth_credentials._checked_google_ads_pause_protected_snapshot(
                snapshot
            )
            binding = _execution_binding_from_snapshot(checked_snapshot)
            expected = _checked_execution_binding(context._execution_binding)
            if not _same_initial_binding(binding, expected):
                raise ValueError("snapshot binding")
        except AdhelmError:
            raise
        except Exception:
            raise _session_invalid() from None
        return checked_snapshot

    def _checked_reader(self) -> GoogleAdsCampaignStateReader:
        try:
            reader = self._reader
        except Exception:
            raise _session_invalid() from None
        if type(reader) is not GoogleAdsCampaignStateReader:
            raise _session_invalid()
        return reader

    def _checked_lock(self) -> RLock:
        if type(self) is not GoogleAdsCampaignPausePlanningSession:
            raise _session_invalid()
        try:
            lock = self._lock
        except Exception:
            raise _session_invalid() from None
        if type(lock) is not _RLOCK_TYPE:
            raise _session_invalid()
        return lock

    def _require_open(self) -> None:
        try:
            closed = self._closed
        except Exception:
            raise _session_invalid() from None
        if closed is not False:
            raise AdhelmError(
                "GOOGLE_ADS_PAUSE_PLANNING_SESSION_CLOSED",
                "The Google Ads PAUSE planning session is closed.",
                exit_code=ExitCode.PRECONDITION,
            )

    def _require_not_retaining(self) -> None:
        try:
            retention = self._retention_token
        except Exception:
            raise _session_invalid() from None
        if retention is not None:
            raise AdhelmError(
                "GOOGLE_ADS_PAUSE_PLANNING_SESSION_BUSY",
                "Google Ads PAUSE reads are unavailable during local plan retention.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Finish the current local plan retention before another fixed read.",
            )

    def _require_not_retaining_fast(self) -> None:
        """Refuse an active retention window without blocking on its RLock.

        This deliberately performs no I/O and is repeated under the session
        lock by ``_require_not_retaining``.  It closes the otherwise surprising
        case where a second thread waits for retention to finish, then sends a
        resource read that was requested during the protected append window.
        """

        try:
            retention = self._retention_token
        except Exception:
            raise _session_invalid() from None
        if retention is not None:
            raise AdhelmError(
                "GOOGLE_ADS_PAUSE_PLANNING_SESSION_BUSY",
                "Google Ads PAUSE reads are unavailable during local plan retention.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Finish the current local plan retention before another fixed read.",
            )

    def _close(self) -> None:
        with self._checked_lock():
            if self._closed:
                return
            self._closed = True
            self._retention_token = None
            self._retention_owner = None
            # Drop the sole retained private bearer/developer association once
            # the facade ends.  The safe execution binding remains available
            # only through the now-closed object and cannot reopen a route.
            self._protected_snapshot = None
            try:
                self._client.close()
            except Exception:
                # Closing a fake-only fixed reader must not hide a prior
                # validation or provider result.
                pass


@contextmanager
def configured_google_ads_pause_planning_session(
    directory: Path,
    profile_id: str,
    expected_configuration_digest: str | None = None,
) -> Iterator[GoogleAdsCampaignPausePlanningSession]:
    """Open the sealed Google Ads PAUSE planning facade.

    The initial protected preparation may refresh the OAuth bundle or create
    and read back the fixed binding-key slot.  All later resource reads and
    retention checks are no-refresh and fail closed on any association drift.
    """

    root = _checked_directory(directory)
    selected_id = _checked_profile_id(profile_id)
    expected = _checked_expected_digest(expected_configuration_digest)
    with configuration_transaction(root) as transaction:
        configuration = _checked_configuration(transaction.read())
        configuration_digest = digest(configuration)
        if expected is not None and not hmac.compare_digest(expected, configuration_digest):
            raise _configuration_changed()
        profile = _selected_profile(configuration, selected_id)
        protected_snapshot = oauth_credentials._prepare_google_ads_pause_protected_snapshot(
            profile, configuration_digest
        )
        context = _new_context(root, configuration_digest, profile, protected_snapshot)
        transaction.require_unchanged(configuration_digest)

    with _google_ads_http_client() as client:
        session = GoogleAdsCampaignPausePlanningSession(
            client=client,
            context=context,
            protected_snapshot=protected_snapshot,
            issuer=_SESSION_ISSUER,
        )
        del protected_snapshot
        try:
            yield session
        finally:
            session._close()


def _new_context(
    directory: Path,
    configuration_digest: str,
    profile: Profile,
    snapshot: oauth_credentials._GoogleAdsPauseProtectedSnapshot,
) -> _GoogleAdsPausePlanningContext:
    """Detach fixed target and M55 facts from the lifecycle-issued snapshot."""

    checked_profile = _checked_profile(profile)
    checked_snapshot = oauth_credentials._checked_google_ads_pause_protected_snapshot(snapshot)
    binding = _execution_binding_from_snapshot(checked_snapshot)
    try:
        currency = checked_profile.currency
        if type(currency) is not str:
            raise ValueError("currency")
        target = _GoogleAdsEditTarget(
            customer_client_ref=checked_profile.account_ref,
            login_customer_client_ref=checked_profile.login_customer_ref,
            declared_currency=currency,
            declared_timezone=checked_profile.timezone,
        )
        profile_digest = digest(checked_profile)
        expected_values = (
            (binding.profile_ref, checked_profile.profile_id),
            (binding.client_application_ref, checked_profile.client_ref),
            (binding.principal_ref, checked_profile.principal_ref),
            (binding.required_scope, _SCOPE),
            (binding.login_customer_client_ref, checked_profile.login_customer_ref),
            (binding.configuration_digest, configuration_digest),
            (binding.profile_configuration_digest, profile_digest),
            (checked_snapshot.account_client_ref, checked_profile.account_ref),
            (checked_snapshot.currency, currency),
            (checked_snapshot.timezone, checked_profile.timezone),
        )
        if not all(_same_exact(left, right) for left, right in expected_values):
            raise ValueError("snapshot association")
        if binding.resolved_at >= binding.expires_at:
            raise ValueError("snapshot expiry")
        return _GoogleAdsPausePlanningContext(
            _directory=directory,
            _profile_id=checked_profile.profile_id,
            _configuration_digest=configuration_digest,
            _profile_digest=profile_digest,
            _target=target,
            _execution_binding=binding,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _session_invalid() from None


def _current_profile(
    transaction: ConfigurationTransaction, context: _GoogleAdsPausePlanningContext
) -> Profile:
    configuration = _checked_configuration(
        transaction.require_unchanged(context._configuration_digest)
    )
    profile = _selected_profile(configuration, context._profile_id)
    if not hmac.compare_digest(digest(profile), context._profile_digest):
        raise _pause_credential_changed()
    target = context._target
    if (
        profile.account_ref != target.customer_client_ref
        or profile.login_customer_ref != target.login_customer_client_ref
        or profile.currency != target.declared_currency
        or profile.timezone != target.declared_timezone
    ):
        raise _pause_credential_changed()
    return profile


def _execution_binding_from_snapshot(
    snapshot: oauth_credentials._GoogleAdsPauseProtectedSnapshot,
) -> GoogleAdsCampaignPauseExecutionBinding:
    """Copy only allowlisted nonsecret facts from one private lifecycle object."""

    if type(snapshot) is not oauth_credentials._GoogleAdsPauseProtectedSnapshot:
        raise _session_invalid()
    try:
        return GoogleAdsCampaignPauseExecutionBinding(
            profile_ref=snapshot.profile_ref,
            client_application_ref=snapshot.client_application_ref,
            principal_ref=snapshot.principal_ref,
            required_scope=snapshot.required_scope,
            login_customer_client_ref=snapshot.login_customer_client_ref,
            credential_binding_ref=snapshot.credential_binding_ref,
            configuration_digest=snapshot.configuration_digest,
            profile_configuration_digest=snapshot.profile_configuration_digest,
            resolved_at=_utc_time(snapshot._resolved_at),
            expires_at=_utc_time(snapshot._expires_at),
        )
    except Exception:
        raise _session_invalid() from None


def _verify_current_snapshot(
    snapshot: oauth_credentials._GoogleAdsPauseProtectedSnapshot,
    context: _GoogleAdsPausePlanningContext,
    binding: GoogleAdsCampaignPauseExecutionBinding,
    now: datetime,
) -> None:
    """Require full stable association and exact bearer-expiry continuity."""

    checked_snapshot = oauth_credentials._checked_google_ads_pause_protected_snapshot(snapshot)
    current = _execution_binding_from_snapshot(checked_snapshot)
    if not _same_current_binding(current, binding):
        raise _pause_credential_changed()
    _check_planning_window(current, now, reader_callback=True)
    target = context._target
    try:
        if (
            checked_snapshot.account_client_ref != target.customer_client_ref
            or checked_snapshot.currency != target.declared_currency
            or checked_snapshot.timezone != target.declared_timezone
        ):
            raise ValueError("target")
    except Exception:
        raise _pause_credential_changed() from None


def _same_initial_binding(
    actual: GoogleAdsCampaignPauseExecutionBinding,
    expected: GoogleAdsCampaignPauseExecutionBinding,
) -> bool:
    """Compare every initial nonsecret execution declaration exactly."""

    try:
        return hmac.compare_digest(
            canonical_bytes(actual.model_dump(mode="json", warnings="error")),
            canonical_bytes(expected.model_dump(mode="json", warnings="error")),
        )
    except Exception:
        return False


def _same_current_binding(
    actual: GoogleAdsCampaignPauseExecutionBinding,
    expected: GoogleAdsCampaignPauseExecutionBinding,
) -> bool:
    """Compare stable association plus expiry, excluding volatile resolved_at."""

    try:
        actual_data = actual.model_dump(mode="json", warnings="error")
        expected_data = expected.model_dump(mode="json", warnings="error")
        if type(actual_data) is not dict or type(expected_data) is not dict:
            return False
        actual_data.pop("resolved_at", None)
        expected_data.pop("resolved_at", None)
        return hmac.compare_digest(canonical_bytes(actual_data), canonical_bytes(expected_data))
    except Exception:
        return False


def _new_observation(
    *,
    identity: GoogleAdsEditIdentity,
    state: GoogleAdsCampaignState,
    execution_binding: GoogleAdsCampaignPauseExecutionBinding,
    read_started_at: datetime,
    read_finished_at: datetime,
) -> GoogleAdsCampaignPausePlanningObservation:
    """Build the one M55-compatible pair without any provider capability."""

    try:
        if state.status != "ENABLED":
            raise ValueError("campaign status")
        if (
            identity.customer_ref != state.customer_ref
            or identity.customer_client_ref != state.customer_client_ref
        ):
            raise ValueError("customer association")
        account = GoogleAdsCampaignPauseAccountBinding(
            account_ref=identity.customer_ref,
            account_client_ref=identity.customer_client_ref,
            currency=identity.currency,
            timezone=identity.timezone,
            account_status=identity.status,
            provider_api_version=identity.provider_api_version,
        )
        campaign = GoogleAdsCampaignPauseCampaignBaseline(
            campaign_ref=state.campaign_ref,
            campaign_client_ref=state.campaign_client_ref,
            account_ref=state.customer_ref,
            account_client_ref=state.customer_client_ref,
            status="ENABLED",
        )
        observation = GoogleAdsCampaignPauseObservationBinding(
            campaign=campaign,
            read_started_at=read_started_at,
            read_finished_at=read_finished_at,
        )
        if execution_binding.resolved_at > observation.read_started_at:
            raise ValueError("binding after observation")
        _check_planning_window(
            execution_binding, observation.read_finished_at, reader_callback=False
        )
        return GoogleAdsCampaignPausePlanningObservation(
            _account_binding=_checked_account_binding(account),
            _observation_binding=_checked_observation_binding(observation),
            _issuer=_OBSERVATION_ISSUER,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _observation_invalid() from None


def _checked_observation(
    value: object,
) -> tuple[GoogleAdsCampaignPauseAccountBinding, GoogleAdsCampaignPauseObservationBinding]:
    if type(value) is not GoogleAdsCampaignPausePlanningObservation:
        raise _session_invalid()
    try:
        issuer = value._issuer
        account = value._account_binding
        observation = value._observation_binding
    except Exception:
        raise _session_invalid() from None
    if issuer is not _OBSERVATION_ISSUER:
        raise _session_invalid()
    checked_account = _checked_account_binding(account)
    checked_observation = _checked_observation_binding(observation)
    campaign = checked_observation.campaign
    if (
        campaign.account_ref != checked_account.account_ref
        or campaign.account_client_ref != checked_account.account_client_ref
    ):
        raise _session_invalid()
    return checked_account, checked_observation


def _checked_execution_binding(value: object) -> GoogleAdsCampaignPauseExecutionBinding:
    if type(value) is not GoogleAdsCampaignPauseExecutionBinding:
        raise _session_invalid()
    try:
        raw = value.model_dump(mode="json", warnings="error")
        if type(raw) is not dict:
            raise ValueError("execution data")
        # Validate original JSON scalar types before JCS canonicalization: it
        # would otherwise turn schema_version=1.0 into the integer 1.
        parsed = validate_json_value(GoogleAdsCampaignPauseExecutionBinding, raw)
        if canonical_bytes(raw) != canonical_bytes(
            parsed.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("execution canonical shape")
    except Exception:
        raise _session_invalid() from None
    if type(parsed) is not GoogleAdsCampaignPauseExecutionBinding:
        raise _session_invalid()
    return parsed


def _checked_account_binding(value: object) -> GoogleAdsCampaignPauseAccountBinding:
    if type(value) is not GoogleAdsCampaignPauseAccountBinding:
        raise _session_invalid()
    try:
        raw = value.model_dump(mode="json", warnings="error")
        if type(raw) is not dict:
            raise ValueError("account data")
        parsed = validate_json_value(GoogleAdsCampaignPauseAccountBinding, raw)
        if canonical_bytes(raw) != canonical_bytes(
            parsed.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("account canonical shape")
    except Exception:
        raise _session_invalid() from None
    if type(parsed) is not GoogleAdsCampaignPauseAccountBinding:
        raise _session_invalid()
    return parsed


def _checked_observation_binding(value: object) -> GoogleAdsCampaignPauseObservationBinding:
    if type(value) is not GoogleAdsCampaignPauseObservationBinding:
        raise _session_invalid()
    try:
        raw = value.model_dump(mode="json", warnings="error")
        if type(raw) is not dict:
            raise ValueError("observation data")
        parsed = validate_json_value(GoogleAdsCampaignPauseObservationBinding, raw)
        if canonical_bytes(raw) != canonical_bytes(
            parsed.model_dump(mode="json", warnings="error")
        ):
            raise ValueError("observation canonical shape")
    except Exception:
        raise _session_invalid() from None
    if type(parsed) is not GoogleAdsCampaignPauseObservationBinding:
        raise _session_invalid()
    return parsed


def _checked_identity(value: object) -> GoogleAdsEditIdentity:
    if type(value) is not GoogleAdsEditIdentity:
        raise _observation_invalid()
    try:
        return GoogleAdsEditIdentity.model_validate(
            value.model_dump(mode="python", warnings="error")
        )
    except Exception:
        raise _observation_invalid() from None


def _checked_campaign_state(value: object) -> GoogleAdsCampaignState:
    if type(value) is not GoogleAdsCampaignState:
        raise _observation_invalid()
    try:
        return GoogleAdsCampaignState.model_validate(
            value.model_dump(mode="python", warnings="error")
        )
    except Exception:
        raise _observation_invalid() from None


def _headers_from_snapshot(
    snapshot: oauth_credentials._GoogleAdsPauseProtectedSnapshot,
    target: _GoogleAdsEditTarget,
) -> dict[str, str]:
    try:
        headers = {
            "authorization": f"Bearer {snapshot._bearer_token}",
            "developer-token": snapshot._developer_token,
        }
        if target.login_customer_client_ref is not None:
            headers["login-customer-id"] = target.login_customer_client_ref
        return headers
    except Exception:
        raise _pause_credential_changed() from None


def _check_planning_window(
    binding: GoogleAdsCampaignPauseExecutionBinding,
    now: datetime,
    *,
    reader_callback: bool,
) -> None:
    if now < binding.resolved_at:
        raise _clock_invalid()
    if now >= binding.expires_at:
        if reader_callback:
            raise _edit_token_expired()
        raise _pause_credential_changed()


def _reader_callback_error(error: AdhelmError) -> AdhelmError:
    """Translate only PAUSE drift to the frozen reader's safe vocabulary."""

    if error.code == "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED":
        return _edit_credential_changed()
    return error


def _same_exact(left: object, right: object) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return type(left) is str and type(right) is str and hmac.compare_digest(left, right)


def _checked_directory(value: object) -> Path:
    if not isinstance(value, Path):
        raise _session_invalid()
    try:
        return value.resolve()
    except Exception:
        raise _session_invalid() from None


def _checked_profile_id(value: object) -> str:
    if type(value) is not str or _SAFE_REF.fullmatch(value) is None:
        raise _session_invalid()
    return value


def _checked_campaign_id(value: object) -> str:
    if type(value) is not str or _CAMPAIGN_ID.fullmatch(value) is None or int(value) > _MAX_INT64:
        raise AdhelmError(
            "INVALID_INPUT",
            "Campaign ID must be a positive ASCII signed-64-bit decimal identifier.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return value


def _checked_expected_digest(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise _session_invalid()
    return value


def _checked_configuration(value: object) -> Configuration:
    try:
        return validate_configuration(value)
    except Exception:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation for Google Ads PAUSE planning.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _selected_profile(configuration: Configuration, profile_id: str) -> Profile:
    try:
        matches = [
            profile for profile in configuration.profiles if profile.profile_id == profile_id
        ]
        if len(matches) != 1 or type(matches[0]) is not Profile:
            raise ValueError("profile")
        return _checked_profile(matches[0])
    except AdhelmError:
        raise
    except Exception:
        raise _profile_required() from None


def _checked_profile(value: object) -> Profile:
    try:
        if type(value) is not Profile:
            raise ValueError("profile")
        return oauth_credentials._checked_google_ads_edit_profile(value)
    except AdhelmError:
        raise
    except Exception:
        raise _profile_required() from None


def _checked_target(value: object) -> _GoogleAdsEditTarget:
    if type(value) is not _GoogleAdsEditTarget:
        raise ValueError("target")
    return _GoogleAdsEditTarget.model_validate(value.model_dump(mode="python", warnings="error"))


def _checked_now() -> datetime:
    try:
        return _utc_time(_now())
    except AdhelmError:
        raise
    except Exception:
        raise _clock_invalid() from None


def _utc_time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time")
    return value.astimezone(UTC)


def _session_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_PLANNING_SESSION_INVALID",
        "Google Ads PAUSE planning session input is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _profile_required() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_EDIT_PROFILE_REQUIRED",
        "A configured Google Ads OAuth EDIT profile is required for PAUSE planning.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Configure the selected Google Ads EDIT profile locally.",
    )


def _configuration_changed() -> AdhelmError:
    return AdhelmError(
        "CONFIGURATION_CHANGED",
        "Local configuration changed during Google Ads PAUSE planning.",
        next_action="Review current configuration before another fixed planning observation.",
    )


def _pause_credential_changed() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED",
        "Google Ads PAUSE protected credentials changed or are no longer usable.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Prepare a fresh Google Ads PAUSE observation from current local credentials.",
    )


def _edit_credential_changed() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_EDIT_CREDENTIAL_CHANGED",
        "Local Google Ads EDIT credentials changed during the fixed observation.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reauthorize the configured Google Ads EDIT profile before another read.",
    )


def _edit_token_expired() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_EDIT_TOKEN_EXPIRED",
        "The local Google Ads EDIT OAuth token is no longer usable.",
        exit_code=ExitCode.AUTHORIZATION,
        next_action="Reauthorize the configured Google Ads EDIT profile before another read.",
    )


def _clock_invalid() -> AdhelmError:
    return AdhelmError(
        "CLOCK_INVALID",
        "A trusted local clock is unavailable for Google Ads PAUSE planning.",
        exit_code=ExitCode.AUTHORIZATION,
    )


def _observation_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_OBSERVATION_INVALID",
        "Google Ads PAUSE planning requires one fresh ENABLED campaign observation.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Read the configured Google Ads campaign again before a new plan.",
    )


def _retention_recheck_required() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_RETENTION_RECHECK_REQUIRED",
        "Google Ads PAUSE planning requires one immediate local retention recheck.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Restart the fixed Google Ads planning observation before retention.",
    )
