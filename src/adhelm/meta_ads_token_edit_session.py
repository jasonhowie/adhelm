"""Private fixed Meta Ads EDIT observation session.

This preparation boundary reads one exact campaign state through an inspected,
continuity-guarded EDIT token bundle. It creates no plan, grant, ledger entry,
provider mutation, or CLI surface.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import httpx

from adhelm import _meta_ads_token_session as _shared
from adhelm.egress_permit import EgressPermit
from adhelm.egress_permit import _required as _permit_required
from adhelm.errors import AdhelmError
from adhelm.meta_create_planning_session import (
    MetaAdsCreateHierarchyObservation,
    MetaAdsCreateNameProbe,
    MetaAdsCreateReader,
)
from adhelm.providers.meta_ads import MetaAdsIdentity
from adhelm.providers.meta_campaign_state import MetaCampaignState, MetaCampaignStateReader
from adhelm.providers.meta_create_contract import META_CREATE_API_VERSION, META_CREATE_EDGES

_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_NATIVE_ID = re.compile(r"^[1-9][0-9]{0,39}$", re.ASCII)
_SAFE_REF = re.compile(r"^[a-zA-Z0-9_.:/-]{1,160}$", re.ASCII)
_PRINCIPAL_REF = re.compile(r"^meta_token_principal_v1\.[a-f0-9]{64}$", re.ASCII)
_CREDENTIAL_BINDING_REF = re.compile(r"^credential-binding:sha256:[a-f0-9]{64}$", re.ASCII)
_SESSION_ISSUER = object()
_BINDING_ISSUER = object()
_PAUSE_POST_ISSUER = object()
_CREATE_EGRESS_ISSUER = object()
_ACCOUNT_ID = re.compile(r"^act_[1-9][0-9]{0,39}$", re.ASCII)

#: The exact form-field set each fixed create edge accepts.  A body that is not
#: exactly one of these never reaches the shared transport, so this capability
#: cannot become a generic Graph mutation surface.
_CREATE_FORM_FIELDS: dict[str, frozenset[str]] = {
    "/campaigns": frozenset(
        {
            "name",
            "objective",
            "buying_type",
            "special_ad_categories",
            "lifetime_budget",
            "start_time",
            "stop_time",
            "status",
        }
    ),
    "/adsets": frozenset(
        {
            "campaign_id",
            "name",
            "status",
            "billing_event",
            "optimization_goal",
            "bid_strategy",
            "targeting",
        }
    ),
    "/adcreatives": frozenset({"name", "object_story_spec"}),
    "/ads": frozenset({"name", "adset_id", "creative", "status"}),
}


class _FixedMetaCampaignPausePostDispatchUnverified(Exception):
    """Internal marker for an error after a kernel permit was consumed."""


class _FixedMetaCreateDispatchUnverified(Exception):
    """Internal marker for a child failure after its kernel permit was consumed."""


@dataclass(frozen=True, slots=True, repr=False)
class MetaAdsTokenEditBinding:
    """Closed, nonsecret facts retained from one validated EDIT inspection."""

    profile_ref: str
    client_application_ref: str
    principal_ref: str
    required_scope: str
    credential_binding_ref: str
    configuration_digest: str
    profile_configuration_digest: str
    observed_at: datetime
    expires_at: datetime
    data_access_expires_at: datetime
    _issuer: object

    def __repr__(self) -> str:
        return "<MetaAdsTokenEditBinding redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta Ads edit bindings cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta Ads edit bindings cannot be serialized")


class MetaAdsTokenEditSession:
    """One private fixed campaign-state session with no mutation authority."""

    __slots__ = ("_core", "_create_reader", "_issuer", "_reader")

    def __init__(
        self,
        *,
        core: _shared._ConfiguredMetaAdsTokenSession,
        reader: MetaCampaignStateReader,
        create_reader: MetaAdsCreateReader,
        issuer: object,
    ) -> None:
        self._core = core
        self._reader = reader
        self._create_reader = create_reader
        self._issuer = issuer

    def __repr__(self) -> str:
        return "<MetaAdsTokenEditSession redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta Ads edit sessions cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta Ads edit sessions cannot be serialized")

    @property
    def identity(self) -> MetaAdsIdentity:
        """Return one detached account identity proved before the session yielded."""
        return self._checked_core()._checked_identity()

    @property
    def binding(self) -> MetaAdsTokenEditBinding:
        """Return detached closed inspection facts, never tokens or unknown scopes."""
        return _new_binding(*self._checked_core()._binding_fields())

    def read_campaign_state(self, campaign_id: str) -> MetaCampaignState:
        """Perform one fixed campaign-node GET after local continuity checks."""
        core = self._checked_core()
        reader = self._checked_reader()
        with core._network_operation():
            return reader.read_campaign_state(core._checked_identity(), campaign_id)

    def _refresh_identity_for_fixed_pause(self) -> MetaAdsIdentity:
        """Re-read the configured account for the private PAUSED adapter only."""

        return self._checked_core()._refresh_identity()

    def _refresh_identity_for_fixed_create(self) -> MetaAdsIdentity:
        """Re-read the configured account for the private CREATE adapter only."""

        return self._checked_core()._refresh_identity()

    def _probe_fixed_create_campaign_name(self, campaign_name: str) -> MetaAdsCreateNameProbe:
        """Compare one planned campaign name with every name this account holds."""

        core = self._checked_core()
        reader = self._checked_create_reader()
        with core._network_operation():
            return reader.campaign_name_probe(core._checked_identity(), campaign_name)

    def _observe_fixed_create_hierarchy(
        self,
        *,
        campaign_client_ref: str,
        adset_client_ref: str,
        creative_client_ref: str,
        ad_client_ref: str,
        uploaded_image_hash: str,
        creative_content_digest: str,
    ) -> MetaAdsCreateHierarchyObservation:
        """Read back whatever the private CREATE adapter actually created."""

        core = self._checked_core()
        reader = self._checked_create_reader()
        with core._network_operation():
            return reader.observe_hierarchy(
                core._checked_identity(),
                campaign_client_ref=campaign_client_ref,
                adset_client_ref=adset_client_ref,
                creative_client_ref=creative_client_ref,
                ad_client_ref=ad_client_ref,
                uploaded_image_hash=uploaded_image_hash,
                creative_content_digest=creative_content_digest,
            )

    @contextmanager
    def retention_recheck(self) -> Iterator[Callable[[], None]]:
        """Hold local facts for an owner-only immediate pre-append recheck."""
        with self._checked_core()._retention_recheck() as recheck:
            yield recheck

    def _checked_core(self) -> _shared._ConfiguredMetaAdsTokenSession:
        try:
            issuer = self._issuer
            core = self._core
        except Exception:
            raise _session_invalid() from None
        if (
            issuer is not _SESSION_ISSUER
            or type(core) is not _shared._ConfiguredMetaAdsTokenSession
        ):
            raise _session_invalid()
        try:
            policy = core._context._policy
        except Exception:
            raise _session_invalid() from None
        if policy is not _shared._EDIT_POLICY:
            raise _session_invalid()
        return core

    def _checked_reader(self) -> MetaCampaignStateReader:
        try:
            reader = self._reader
        except Exception:
            raise _session_invalid() from None
        if type(reader) is not MetaCampaignStateReader:
            raise _session_invalid()
        return reader

    def _checked_create_reader(self) -> MetaAdsCreateReader:
        try:
            reader = self._create_reader
        except Exception:
            raise _session_invalid() from None
        if type(reader) is not MetaAdsCreateReader:
            raise _session_invalid()
        return reader


class _FixedMetaCampaignPausePost:
    """Factory-issued, one-route EDIT capability with no token or URL surface."""

    __slots__ = ("_campaign_id", "_core", "_issuer", "_plan_digest", "_subplan_digest")

    def __init__(
        self,
        *,
        core: _shared._ConfiguredMetaAdsTokenSession,
        campaign_id: str,
        plan_digest: str,
        subplan_digest: str,
        issuer: object,
    ) -> None:
        self._core = core
        self._campaign_id = campaign_id
        self._plan_digest = plan_digest
        self._subplan_digest = subplan_digest
        self._issuer = issuer

    def __repr__(self) -> str:
        return "<_FixedMetaCampaignPausePost redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta campaign pause capabilities cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta campaign pause capabilities cannot be serialized")

    def _preflight(self) -> None:
        core, campaign, _plan_digest, _subplan_digest = self._checked_members()
        core._preflight_fixed_meta_campaign_pause(campaign)

    def _post(
        self,
        *,
        permit: EgressPermit,
        attempt_ref: str,
    ) -> httpx.Response:
        """Consume one active kernel permit at the fixed POST handoff."""

        core, campaign, plan_digest, subplan_digest = self._checked_members()
        if type(permit) is not EgressPermit:
            raise _permit_required()
        permit.consume(
            plan_digest=plan_digest,
            subplan_digest=subplan_digest,
            attempt_ref=attempt_ref,
        )
        try:
            return core._post_fixed_meta_campaign_pause(campaign)
        except Exception:
            # The permit has already been consumed.  The caller must not
            # classify a continuity, transport, or response-path failure as a
            # safe precondition because the fixed POST handoff was reached.
            raise _FixedMetaCampaignPausePostDispatchUnverified() from None

    def _checked_members(
        self,
    ) -> tuple[_shared._ConfiguredMetaAdsTokenSession, str, str, str]:
        try:
            issuer = self._issuer
            core = self._core
            campaign = self._campaign_id
            plan_digest = self._plan_digest
            subplan_digest = self._subplan_digest
        except Exception:
            raise _session_invalid() from None
        if (
            issuer is not _PAUSE_POST_ISSUER
            or type(core) is not _shared._ConfiguredMetaAdsTokenSession
            or type(campaign) is not str
            or _NATIVE_ID.fullmatch(campaign) is None
            or type(plan_digest) is not str
            or _DIGEST.fullmatch(plan_digest) is None
            or type(subplan_digest) is not str
            or not 1 <= len(subplan_digest) <= 256
        ):
            raise _session_invalid()
        try:
            policy = core._context._policy
        except Exception:
            raise _session_invalid() from None
        if policy is not _shared._EDIT_POLICY:
            raise _session_invalid()
        return core, campaign, plan_digest, subplan_digest


def _issue_fixed_meta_campaign_pause_post(
    session: object, *, campaign_id: object, plan_digest: object, subplan_digest: object
) -> _FixedMetaCampaignPausePost:
    """Issue the sole fixed PAUSED writer from one active EDIT session.

    This is package-private composition, not a generic credential or mutation
    factory.  The adapter supplies the campaign only after reconstructing its
    closed planned action.
    """

    if (
        type(session) is not MetaAdsTokenEditSession
        or type(campaign_id) is not str
        or _NATIVE_ID.fullmatch(campaign_id) is None
        or type(plan_digest) is not str
        or _DIGEST.fullmatch(plan_digest) is None
        or type(subplan_digest) is not str
        or not 1 <= len(subplan_digest) <= 256
    ):
        raise _session_invalid()
    return _FixedMetaCampaignPausePost(
        core=session._checked_core(),
        campaign_id=campaign_id,
        plan_digest=plan_digest,
        subplan_digest=subplan_digest,
        issuer=_PAUSE_POST_ISSUER,
    )


def _checked_fixed_meta_campaign_pause_post(value: object) -> _FixedMetaCampaignPausePost:
    """Revalidate an untrusted/private capability before adapter use."""

    if type(value) is not _FixedMetaCampaignPausePost:
        raise _session_invalid()
    value._checked_members()
    return value


@contextmanager
def configured_meta_ads_token_edit_session(
    directory: Path,
    profile_id: str,
    *,
    expected_configuration_digest: str,
) -> Iterator[MetaAdsTokenEditSession]:
    """Open one exact configured Meta EDIT campaign-observation session."""
    with _shared._configured_meta_ads_token_edit_state(
        directory,
        profile_id,
        expected_configuration_digest=expected_configuration_digest,
    ) as core:
        reader = MetaCampaignStateReader(
            core._new_campaign_state_transport(),
            core._subject_token_for_provider_read,
        )
        create_reader = MetaAdsCreateReader(
            core._new_campaign_state_transport(),
            core._subject_token_for_provider_read,
        )
        yield MetaAdsTokenEditSession(
            core=core, reader=reader, create_reader=create_reader, issuer=_SESSION_ISSUER
        )


class _FixedMetaAdsHierarchyCreateEgress:
    """Factory-issued, five-route EDIT capability with no token or URL surface.

    The pause family's ``_FixedMetaCampaignPausePost`` needs no body: its one
    route is ``status=PAUSED``.  A create does need one, so the closed grammar
    lives here instead: the ad account is fixed at issue time, the edge must be
    one of the five documented create edges in this build's own child plan, and
    the form-field set must equal that edge's exact allowlist.  Nothing here
    accepts a URL, method, header, token, HTTP client, or retry.
    """

    __slots__ = ("_account", "_core", "_issuer", "_plan_digest", "_subplan_digest")

    def __init__(
        self,
        *,
        core: _shared._ConfiguredMetaAdsTokenSession,
        account_client_ref: str,
        plan_digest: str,
        subplan_digest: str,
        issuer: object,
    ) -> None:
        self._core = core
        self._account = account_client_ref
        self._plan_digest = plan_digest
        self._subplan_digest = subplan_digest
        self._issuer = issuer

    def __repr__(self) -> str:
        return "<_FixedMetaAdsHierarchyCreateEgress redacted>"

    def __reduce__(self) -> Never:
        raise TypeError("Meta Ads create capabilities cannot be serialized")

    def __getstate__(self) -> Never:
        raise TypeError("Meta Ads create capabilities cannot be serialized")

    @property
    def account_client_ref(self) -> str:
        return self._checked_members()[1]

    def _preflight(self) -> None:
        core, _account, _plan_digest, _subplan_digest = self._checked_members()
        core._preflight_fixed_meta_create()

    def _url_path(self, edge: str) -> str:
        _core, account, _plan_digest, _subplan_digest = self._checked_members()
        if type(edge) is not str or edge not in META_CREATE_EDGES:
            raise _session_invalid()
        return f"/{META_CREATE_API_VERSION}/{account}{edge}"

    def _upload_image(
        self,
        *,
        permit: EgressPermit,
        attempt_ref: str,
        child_index: int,
        filename: str,
        media_type: str,
        content: bytes,
    ) -> httpx.Response:
        """Consume one active kernel child permit at the fixed upload handoff."""

        core, _account, plan_digest, subplan_digest = self._checked_members()
        path = self._url_path("/adimages")
        self._consume(
            permit,
            plan_digest=plan_digest,
            subplan_digest=subplan_digest,
            attempt_ref=attempt_ref,
            child_index=child_index,
        )
        try:
            return core._upload_fixed_meta_create_image(path, filename, media_type, content)
        except Exception:
            raise _FixedMetaCreateDispatchUnverified() from None

    def _create(
        self,
        *,
        permit: EgressPermit,
        attempt_ref: str,
        child_index: int,
        edge: str,
        form: dict[str, str],
    ) -> httpx.Response:
        """Consume one active kernel child permit at one fixed create handoff."""

        core, _account, plan_digest, subplan_digest = self._checked_members()
        allowed = _CREATE_FORM_FIELDS.get(edge)
        if allowed is None or type(form) is not dict or set(form) != allowed:
            raise _session_invalid()
        path = self._url_path(edge)
        body = dict(form)
        # Bound every value before the permit is consumed.  The adapter has
        # already checked its own built form before the child's durable start,
        # so this is defense in depth for any other caller of the capability.
        # The session's private POST repeats the check, but by then the permit
        # is consumed and a refusal would read as dispatch-unverified for a
        # request that was never sent.
        _shared._fixed_meta_create_form(body, _shared._EDIT_POLICY)
        self._consume(
            permit,
            plan_digest=plan_digest,
            subplan_digest=subplan_digest,
            attempt_ref=attempt_ref,
            child_index=child_index,
        )
        try:
            return core._post_fixed_meta_create(path, body)
        except Exception:
            # The permit has already been consumed.  The caller must not
            # classify a continuity, transport, or response-path failure as a
            # safe precondition because the fixed handoff was reached.
            raise _FixedMetaCreateDispatchUnverified() from None

    @staticmethod
    def _consume(
        permit: object,
        *,
        plan_digest: str,
        subplan_digest: str,
        attempt_ref: str,
        child_index: int,
    ) -> None:
        if type(permit) is not EgressPermit:
            raise _permit_required()
        permit.consume(
            plan_digest=plan_digest,
            subplan_digest=subplan_digest,
            attempt_ref=attempt_ref,
            child_index=child_index,
        )

    def _checked_members(
        self,
    ) -> tuple[_shared._ConfiguredMetaAdsTokenSession, str, str, str]:
        try:
            issuer = self._issuer
            core = self._core
            account = self._account
            plan_digest = self._plan_digest
            subplan_digest = self._subplan_digest
        except Exception:
            raise _session_invalid() from None
        if (
            issuer is not _CREATE_EGRESS_ISSUER
            or type(core) is not _shared._ConfiguredMetaAdsTokenSession
            or type(account) is not str
            or _ACCOUNT_ID.fullmatch(account) is None
            or type(plan_digest) is not str
            or _DIGEST.fullmatch(plan_digest) is None
            or type(subplan_digest) is not str
            or not 1 <= len(subplan_digest) <= 256
        ):
            raise _session_invalid()
        try:
            policy = core._context._policy
        except Exception:
            raise _session_invalid() from None
        if policy is not _shared._EDIT_POLICY:
            raise _session_invalid()
        return core, account, plan_digest, subplan_digest


def _issue_fixed_meta_ads_hierarchy_create_egress(
    session: object,
    *,
    account_client_ref: object,
    plan_digest: object,
    subplan_digest: object,
) -> _FixedMetaAdsHierarchyCreateEgress:
    """Issue the sole fixed hierarchy-create writer from one active EDIT session.

    This is package-private composition, not a generic credential or mutation
    factory.  The adapter supplies the ad account only after reconstructing its
    closed planned action.
    """

    if (
        type(session) is not MetaAdsTokenEditSession
        or type(account_client_ref) is not str
        or _ACCOUNT_ID.fullmatch(account_client_ref) is None
        or type(plan_digest) is not str
        or _DIGEST.fullmatch(plan_digest) is None
        or type(subplan_digest) is not str
        or not 1 <= len(subplan_digest) <= 256
    ):
        raise _session_invalid()
    return _FixedMetaAdsHierarchyCreateEgress(
        core=session._checked_core(),
        account_client_ref=account_client_ref,
        plan_digest=plan_digest,
        subplan_digest=subplan_digest,
        issuer=_CREATE_EGRESS_ISSUER,
    )


def _checked_fixed_meta_ads_hierarchy_create_egress(
    value: object,
) -> _FixedMetaAdsHierarchyCreateEgress:
    """Revalidate an untrusted/private capability before adapter use."""

    if type(value) is not _FixedMetaAdsHierarchyCreateEgress:
        raise _session_invalid()
    value._checked_members()
    return value


def _new_binding(
    profile_ref: object,
    client_application_ref: object,
    principal_ref: object,
    required_scope: object,
    credential_binding_ref: object,
    configuration_digest: object,
    profile_configuration_digest: object,
    observed_at: object,
    expires_at: object,
    data_access_expires_at: object,
) -> MetaAdsTokenEditBinding:
    """Validate and detach the exact safe binding facts from shared state."""
    if (
        type(profile_ref) is not str
        or _SAFE_REF.fullmatch(profile_ref) is None
        or type(client_application_ref) is not str
        or _NATIVE_ID.fullmatch(client_application_ref) is None
        or type(principal_ref) is not str
        or _PRINCIPAL_REF.fullmatch(principal_ref) is None
        or type(required_scope) is not str
        or required_scope != "ads_management"
        or type(credential_binding_ref) is not str
        or _CREDENTIAL_BINDING_REF.fullmatch(credential_binding_ref) is None
        or type(configuration_digest) is not str
        or _DIGEST.fullmatch(configuration_digest) is None
        or type(profile_configuration_digest) is not str
        or _DIGEST.fullmatch(profile_configuration_digest) is None
    ):
        raise _session_invalid()
    checked_observed_at = _time(observed_at)
    checked_expires_at = _time(expires_at)
    checked_data_access_expires_at = _time(data_access_expires_at)
    if (
        checked_observed_at >= checked_expires_at
        or checked_observed_at >= checked_data_access_expires_at
    ):
        raise _session_invalid()
    return MetaAdsTokenEditBinding(
        profile_ref=profile_ref,
        client_application_ref=client_application_ref,
        principal_ref=principal_ref,
        required_scope="ads_management",
        credential_binding_ref=credential_binding_ref,
        configuration_digest=configuration_digest,
        profile_configuration_digest=profile_configuration_digest,
        observed_at=checked_observed_at,
        expires_at=checked_expires_at,
        data_access_expires_at=checked_data_access_expires_at,
        _issuer=_BINDING_ISSUER,
    )


def _checked_binding(value: object) -> MetaAdsTokenEditBinding:
    if type(value) is not MetaAdsTokenEditBinding:
        raise _session_invalid()
    try:
        issuer = value._issuer
        fields = (
            value.profile_ref,
            value.client_application_ref,
            value.principal_ref,
            value.required_scope,
            value.credential_binding_ref,
            value.configuration_digest,
            value.profile_configuration_digest,
            value.observed_at,
            value.expires_at,
            value.data_access_expires_at,
        )
    except Exception:
        raise _session_invalid() from None
    if issuer is not _BINDING_ISSUER:
        raise _session_invalid()
    return _new_binding(*fields)


def _time(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise _session_invalid()
    return value.astimezone(UTC)


def _session_invalid() -> AdhelmError:
    return AdhelmError(
        "META_TOKEN_EDIT_SESSION_INVALID",
        "Meta Ads token edit session state is invalid.",
        exit_code=2,
        next_action="Start a new fixed Meta edit observation from current local configuration.",
    )
