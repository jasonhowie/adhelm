"""Private fixed Meta Ads READ facade over the shared token-session guard.

The shared guard owns configuration/bundle continuity, finite expiry, account
identity, and retention checks. This facade owns only the already reviewed
fixed inventory and aggregate-report reads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Never

from adhelm import _meta_ads_token_session as _shared
from adhelm.errors import AdhelmError, ExitCode
from adhelm.providers.meta_ads import (
    MetaAdsAd,
    MetaAdsAdSet,
    MetaAdsCampaign,
    MetaAdsClient,
    MetaAdsIdentity,
    MetaAdsInventoryQuery,
    MetaAdsReportQuery,
)
from adhelm.reporting import NormalizedReport

_SESSION_ISSUER = object()


class MetaAdsTokenReadSession:
    """One private fixed-read session with no raw credential surface."""

    __slots__ = ("_adapter", "_core", "_issuer")

    def __init__(
        self,
        *,
        core: _shared._ConfiguredMetaAdsTokenSession,
        adapter: MetaAdsClient,
        issuer: object,
    ) -> None:
        self._core = core
        self._adapter = adapter
        self._issuer = issuer

    def __repr__(self) -> str:
        return "<MetaAdsTokenReadSession redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta Ads read sessions cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta Ads read sessions cannot be serialized")

    @property
    def identity(self) -> MetaAdsIdentity:
        """Return a detached, validated account identity with no credential data."""
        return self._checked_core()._checked_identity()

    def list_inventory(
        self, maximum_records: int
    ) -> tuple[list[MetaAdsCampaign], list[MetaAdsAdSet], list[MetaAdsAd]]:
        """Read all three fixed inventory edges with an independent per-edge bound."""
        if type(maximum_records) is not int or not 1 <= maximum_records <= 500:
            raise AdhelmError(
                "RESULT_LIMIT_REQUIRED",
                "Meta Ads inventory maximum records must be between one and 500.",
                exit_code=ExitCode.INVALID_INPUT,
            )
        with self._network_lock() as adapter:
            identity = self._checked_core()._checked_identity()
            query = MetaAdsInventoryQuery(
                account_ref=identity.account_ref,
                maximum_records=maximum_records,
            )
            # The bound applies to each provider edge. Do not silently replace
            # it with a combined maximum that could truncate one hierarchy.
            return (
                adapter.list_campaigns(query, identity=identity),
                adapter.list_adsets(query, identity=identity),
                adapter.list_ads(query, identity=identity),
            )

    def run_report(self, query: MetaAdsReportQuery) -> NormalizedReport:
        """Run one fixed aggregate report after binding native account facts."""
        checked = _checked_report_query(query)
        with self._network_lock() as adapter:
            core = self._checked_core()
            profile = core._context._profile
            identity = core._checked_identity()
            if checked.account_ref != profile.account_ref:
                raise AdhelmError(
                    "IDENTITY_MISMATCH",
                    "Meta Ads report account differs from the configured account.",
                    exit_code=ExitCode.INVALID_INPUT,
                )
            if checked.source_timezone != profile.timezone or (
                checked.currency is not None and checked.currency != profile.currency
            ):
                raise AdhelmError(
                    "IDENTITY_MISMATCH",
                    "Meta Ads report settings differ from the configured account declaration.",
                    exit_code=ExitCode.INVALID_INPUT,
                )
            rebound = MetaAdsReportQuery.model_validate(
                {
                    **checked.model_dump(mode="python", warnings="error"),
                    "account_ref": identity.account_ref,
                    "currency": profile.currency,
                }
            )
            return (
                adapter.run_campaign_report(rebound, identity=identity)
                if rebound.level == "CAMPAIGN"
                else adapter.run_ad_report(rebound, identity=identity)
            )

    @contextmanager
    def retention_recheck(self) -> Iterator[Callable[[], None]]:
        """Hold local state for one immediate pre-append continuity check."""
        with self._checked_core()._retention_recheck() as recheck:
            yield recheck

    @contextmanager
    def _network_lock(self) -> Iterator[MetaAdsClient]:
        core = self._checked_core()
        if type(self._adapter) is not MetaAdsClient:
            raise _shared._session_invalid(_shared._READ_POLICY)
        with core._network_operation():
            yield self._adapter

    def _checked_core(self) -> _shared._ConfiguredMetaAdsTokenSession:
        try:
            issuer = self._issuer
            core = self._core
        except Exception:
            raise _shared._session_invalid(_shared._READ_POLICY) from None
        if (
            issuer is not _SESSION_ISSUER
            or type(core) is not _shared._ConfiguredMetaAdsTokenSession
        ):
            raise _shared._session_invalid(_shared._READ_POLICY)
        try:
            policy = core._context._policy
        except Exception:
            raise _shared._session_invalid(_shared._READ_POLICY) from None
        if policy is not _shared._READ_POLICY:
            raise _shared._session_invalid(_shared._READ_POLICY)
        return core


@contextmanager
def configured_meta_ads_token_read_session(
    directory: Path,
    profile_id: str,
    *,
    expected_configuration_digest: str,
) -> Iterator[MetaAdsTokenReadSession]:
    """Open one configured fixed Meta READ session."""
    with _shared._configured_meta_ads_token_read_state(
        directory,
        profile_id,
        expected_configuration_digest=expected_configuration_digest,
    ) as core:
        adapter = MetaAdsClient(
            core._transport,
            core._subject_token_for_provider_read,
            now=_shared._now,
        )
        yield MetaAdsTokenReadSession(core=core, adapter=adapter, issuer=_SESSION_ISSUER)


def _checked_report_query(value: object) -> MetaAdsReportQuery:
    if type(value) is not MetaAdsReportQuery:
        raise AdhelmError(
            "META_TOKEN_READ_QUERY_INVALID",
            "Meta Ads read session requires one bounded fixed report query.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    try:
        return MetaAdsReportQuery.model_validate(value.model_dump(mode="python", warnings="error"))
    except Exception:
        raise AdhelmError(
            "META_TOKEN_READ_QUERY_INVALID",
            "Meta Ads read session requires one bounded fixed report query.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None
