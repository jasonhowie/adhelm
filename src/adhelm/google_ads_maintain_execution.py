"""Google Ads Search maintenance on the two provider-neutral registries.

This module is the whole of P05's maintenance registration surface.  It defines
one :class:`~adhelm.execution_planning_session.ConfiguredExecutionPlanner` and one
:class:`~adhelm.generic_execution_workflow.ConfiguredExecutionAdapterFactory` that
can serve **two** pairs -- ``("google_ads", UPDATE)`` and
``("google_ads", ARCHIVE)`` -- because the four documented maintenance
operations compile to those two operation kinds.  Each is still its own
single-request executable plan with ``expected_child_egress_count=1``; the
operation kind is derived from the request's own intent and must match the
operation the command names.

Only ``UPDATE`` is **registered** in a production build.  ``ARCHIVE`` is
delete-class and AGENTS rule 11 requires a read-derived ``PROVEN_UNREFERENCED``
proof that no cited read can supply here, so every archive request would be
refused at planning.  ``discover`` renders the registries, so registering the
pair would advertise an operation no request can compile.  The pair is
registered when a cited reference read exists; until then the fake-read tests
register it explicitly.

It adds no command, no approval kind, no credential, no scope, and no live
evidence: the installed approval bridge still refuses
``COMPOSITE_EXECUTION_PLAN``, so every ordinary build refuses at the bridge
before the factory is opened and before any credential is read.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_plan_records import ExecutionPlanRecord, revalidate_execution_plan_record
from adhelm.execution_planning_session import (
    BoundedExecutionRead,
    CompiledExecutionCandidate,
    ConfiguredExecutionPlanner,
    RetainedExecutionPlan,
    build_execution_plan_record,
    configured_execution_planner,
    register_execution_planner,
    retain_execution_plan,
)
from adhelm.generic_execution_workflow import (
    ConfiguredExecutionAdapterFactory,
    ExecutionAdapterCapabilities,
    configured_execution_adapter,
    register_execution_adapter,
)
from adhelm.google_ads_create_planning_session import quotable_campaign_name
from adhelm.google_ads_maintain_planning_session import (
    GoogleAdsLiveHierarchyReader,
    assert_maintainable,
    maintain_observation_binding,
)
from adhelm.google_ads_maintain_readback_records import (
    GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND,
    google_ads_maintain_readback_record_kind_installed,
)
from adhelm.google_ads_pause_planning_session import (
    configured_google_ads_pause_planning_session,
)
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.providers.google_ads_activate_contract import fingerprint_key_marker
from adhelm.providers.google_ads_maintain_adapter import (
    GoogleAdsSearchCampaignMaintainGuardedAdapter,
    _configured_google_ads_search_campaign_maintain_adapter,
)
from adhelm.providers.google_ads_maintain_contract import (
    GOOGLE_ADS_MAINTAIN_CAPABILITY_VERSION,
    GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND,
    MAINTENANCE_OPERATIONS,
    GoogleAdsSearchCampaignMaintainPlannedAction,
    GoogleAdsSearchCampaignMaintainSelection,
    build_google_ads_search_campaign_maintain_plan,
    google_ads_maintain_plan_record_kind_installed,
    google_ads_search_campaign_maintain_plan_record,
    redacted_review,
    revalidate_google_ads_search_campaign_maintain_plan_record,
)
from adhelm.providers.google_ads_pause_contract import GoogleAdsCampaignPauseAccountBinding
from adhelm.safety import CompositePlan, GuardedAdapter, OperationKind

#: The pairs this planner and factory can serve.  ``UPDATE`` covers the budget,
#: bid, and schedule operations; ``ARCHIVE`` covers the one irreversible one.
GOOGLE_ADS_MAINTAIN_PROVIDER: Final = "google_ads"
GOOGLE_ADS_MAINTAIN_OPERATIONS: Final = (OperationKind.UPDATE, OperationKind.ARCHIVE)

#: The pairs a production build *registers*, and therefore advertises through
#: ``discover``.  ``ARCHIVE`` is deliberately absent: see the module docstring.
GOOGLE_ADS_MAINTAIN_REGISTERED_OPERATIONS: Final = (OperationKind.UPDATE,)

_SCOPE: Final = "https://www.googleapis.com/auth/adwords"
_CUSTOMER_ACCOUNT: Final = re.compile(r"^google_ads\.customer\.(?P<customer>[0-9]{10})$", re.ASCII)

#: Domain separation for the request's own campaign-name reference.  The
#: generic envelope's ``resource_ref`` is a ``SafeRef``, and a customer campaign
#: name is neither bounded to that character set nor something this build puts
#: in an uninterpreted reference slot.  The request therefore names the campaign
#: by a keyless digest of it and carries the exact name in its own selection,
#: which the planner requires to agree.
_CAMPAIGN_REF_DOMAIN: Final = "adhelm.google-ads.search-campaign-maintain.campaign-ref.v1"


def campaign_name_ref(campaign_name: str) -> str:
    """Return the ``resource_ref`` a maintenance request names one campaign by."""

    if type(campaign_name) is not str or not 1 <= len(campaign_name.encode("utf-8")) <= 255:
        raise ValueError("a bounded Google Ads campaign name is required")
    return "google_ads.campaign_name." + digest(
        {"domain": _CAMPAIGN_REF_DOMAIN, "campaign_name": campaign_name}
    )


def _now() -> datetime:
    """Private UTC clock seam for deterministic fake-only tests."""

    return datetime.now(UTC)


def _checked_now() -> datetime:
    try:
        value = _now()
        if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock")
        return value.astimezone(UTC)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "CLOCK_INVALID",
            "Google Ads MAINTAIN execution planning requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _invalid_selection(detail: str) -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_MAINTAIN_EXECUTION_SELECTION",
        detail,
        exit_code=ExitCode.INVALID_INPUT,
    )


def _facts_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_MAINTAIN_FACTS_INVALID",
        "The configured Google Ads MAINTAIN facts are incomplete or inconsistent.",
        exit_code=ExitCode.PRECONDITION,
    )


def _retention_unverified() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_MAINTAIN_PLAN_RETENTION_UNVERIFIED",
        "The Google Ads read completed, but local plan retention is unverified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect retained Google Ads MAINTAIN plan history before planning again.",
    )


class GoogleAdsSearchCampaignMaintainExecutionPlanner:
    """Compile and retain one provider-neutral Google Ads maintenance plan."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<GoogleAdsSearchCampaignMaintainExecutionPlanner>"

    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> RetainedExecutionPlan:
        from adhelm.plan_cli import ExecutionPlanRequest

        if not isinstance(directory, Path) or type(request) is not ExecutionPlanRequest:
            raise _invalid_selection("A complete generic execution plan request is required.")
        if request.provider != GOOGLE_ADS_MAINTAIN_PROVIDER:
            raise _invalid_selection("This planner compiles only google_ads maintenance plans.")
        selection = request.maintain_selection
        if type(selection) is not GoogleAdsSearchCampaignMaintainSelection:
            raise _invalid_selection(
                "A google_ads maintenance request carries the campaign and its intent."
            )
        operation = MAINTENANCE_OPERATIONS[selection.intent.maintenance_kind]
        if request.operation != operation.value:
            raise _invalid_selection(
                "The requested operation is not the one this maintenance intent compiles to."
            )
        customer = _selected_customer(request.account_ref)
        if request.resource_ref != campaign_name_ref(selection.campaign_name):
            raise _invalid_selection(
                "resource_ref must be the campaign-name reference the selection carries."
            )
        _require_installed_record_kinds()
        if os.environ.get("CI", "").casefold() in {"1", "true"}:
            raise AdhelmError(
                "GOOGLE_ADS_MAINTAIN_CI_UNSUPPORTED",
                "Google Ads MAINTAIN planning requires the configured local "
                "protected-store runtime.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        configuration = load_configuration(directory)
        captured = digest(configuration)
        if captured != expected_configuration_digest:
            raise AdhelmError(
                "CONFIGURATION_CHANGED",
                "Local configuration changed before Google Ads MAINTAIN execution planning.",
                exit_code=ExitCode.PRECONDITION,
            )
        profile = _edit_profile(configuration, request.profile_id, customer)
        ledger = Ledger(directory)
        if ledger.unresolved_starts() or ledger.unresolved_runs():
            raise AdhelmError(
                "RECOVERY_REQUIRED",
                "Unresolved mutation history must be reconciled before MAINTAIN planning.",
                next_action="Inspect verified history and reconcile the retained evidence.",
            )
        key = export_key()
        with configured_google_ads_pause_planning_session(
            directory, profile.profile_id, expected_configuration_digest=captured
        ) as session:
            action, planning_checked_at, read_started_at = _build_action_from_session(
                workspace_profile_ref=ledger.profile_ref,
                profile=profile,
                configuration_digest=captured,
                selection=selection,
                fingerprint_key=key,
                session=session,
            )
            with session.retention_recheck() as recheck:

                def before_durable_append() -> None:
                    recheck()
                    current = _checked_now()
                    if current < planning_checked_at:
                        raise AdhelmError(
                            "CLOCK_INVALID",
                            "The Google Ads planning clock moved backwards.",
                            exit_code=ExitCode.PRECONDITION,
                        )
                    if current >= action.plan.expires_at:
                        raise AdhelmError(
                            "PLAN_EXPIRED",
                            "The Google Ads MAINTAIN plan expired before retention.",
                            exit_code=ExitCode.AUTHORIZATION,
                        )

                try:
                    _retain_provider_plan(
                        ledger, action, before_durable_append=before_durable_append
                    )
                except AdhelmError:
                    raise
                except Exception:
                    raise _retention_unverified() from None
        return _retain_provider_neutral_plan(
            ledger,
            action,
            read_started_at=read_started_at,
            supersedes_record_digest=request.supersedes_record_digest,
        )


class GoogleAdsSearchCampaignMaintainExecutionAdapterFactory:
    """Open the one configured guarded adapter for a retained maintenance plan."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<GoogleAdsSearchCampaignMaintainExecutionAdapterFactory>"

    def declared_capabilities(self) -> ExecutionAdapterCapabilities:
        return ExecutionAdapterCapabilities(child_egress=False)

    def open_configured_adapter(
        self,
        directory: Path,
        record: ExecutionPlanRecord,
        ledger: Ledger,
        *,
        clock: Callable[[], datetime],
    ) -> AbstractContextManager[GuardedAdapter]:
        if not isinstance(directory, Path) or type(ledger) is not Ledger or not callable(clock):
            raise _invalid_selection("A complete configured adapter request is required.")
        checked = revalidate_execution_plan_record(record)
        if (
            checked.provider != GOOGLE_ADS_MAINTAIN_PROVIDER
            or checked.operation not in GOOGLE_ADS_MAINTAIN_OPERATIONS
            or checked.expected_child_egress_count != 1
        ):
            raise _invalid_selection(
                "This adapter executes only a single-request google_ads maintenance plan."
            )
        now = clock()
        if type(now) is not datetime or now.tzinfo is None or checked.expires_at <= now:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The retained Google Ads MAINTAIN plan expired before its adapter opened.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Compile a new plan from a fresh bounded read; no credential was read "
                    "and no provider request was made."
                ),
            )
        action = _retained_action_for_plan(ledger, checked.plan)
        return _configured_google_ads_search_campaign_maintain_adapter(directory, action, ledger)


def _require_installed_record_kinds() -> None:
    for installed, kind in (
        (google_ads_maintain_plan_record_kind_installed(), GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND),
        (
            google_ads_maintain_readback_record_kind_installed(),
            GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND,
        ),
    ):
        if not installed:
            raise AdhelmError(
                "GOOGLE_ADS_MAINTAIN_RECORD_KIND_NOT_INSTALLED",
                "This build's ledger does not register the Google Ads MAINTAIN record kinds.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    f"Install a build whose ledger registers {kind}; no credential was read "
                    "and no provider request was made."
                ),
            )


def _selected_customer(account_ref: object) -> str:
    if type(account_ref) is not str:
        raise _invalid_selection("A google_ads maintenance request names one customer account.")
    matched = _CUSTOMER_ACCOUNT.fullmatch(account_ref)
    if matched is None:
        raise _invalid_selection("account_ref must be google_ads.customer.<customer-id>.")
    return matched.group("customer")


def _edit_profile(configuration: Configuration, profile_id: str, customer: str) -> Profile:
    profiles = [item for item in configuration.profiles if item.profile_id == profile_id]
    if (
        len(profiles) != 1
        or type(profiles[0]) is not Profile
        or profiles[0].provider != "google_ads"
        or profiles[0].purpose != "EDIT"
        or profiles[0].credential_source != "OS_OAUTH_GOOGLE"
        or profiles[0].scopes != [_SCOPE]
        or profiles[0].currency is None
        or profiles[0].account_ref != customer
    ):
        raise AdhelmError(
            "GOOGLE_ADS_EDIT_PROFILE_REQUIRED",
            "Google Ads MAINTAIN planning requires the configured EDIT profile for this "
            "exact customer.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Configure the Google Ads EDIT profile for this customer; nothing was read, "
                "compiled, or retained."
            ),
        )
    return profiles[0]


def _build_action_from_session(
    *,
    workspace_profile_ref: str,
    profile: Profile,
    configuration_digest: str,
    selection: GoogleAdsSearchCampaignMaintainSelection,
    fingerprint_key: bytes,
    session: Any,
) -> tuple[GoogleAdsSearchCampaignMaintainPlannedAction, datetime, datetime]:
    try:
        execution = session.execution_binding
        reader = GoogleAdsLiveHierarchyReader(session)
        name = quotable_campaign_name(selection.campaign_name)
        read_started_at = _checked_now()
        identity = reader.discover_identity()
        if identity.status != "ENABLED":
            raise AdhelmError(
                "GOOGLE_ADS_ACCOUNT_NOT_ENABLED",
                "A Google Ads Search hierarchy is maintained only in an ENABLED customer.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured Google Ads customer; nothing was changed.",
            )
        observed = reader.observe_live_hierarchy(name)
        read_finished_at = _checked_now()
        account = _account_binding(identity, profile)
        assert_maintainable(observed, selection.intent)
        if (
            execution.profile_ref != profile.profile_id
            or execution.client_application_ref != profile.client_ref
            or execution.principal_ref != profile.principal_ref
            or execution.required_scope != _SCOPE
            or execution.login_customer_client_ref != profile.login_customer_ref
            or execution.configuration_digest != configuration_digest
            or execution.profile_configuration_digest != digest(profile)
        ):
            raise ValueError("bound facts")
        observation = maintain_observation_binding(
            observed,
            selection.intent,
            campaign_name=name,
            fingerprint_key=fingerprint_key,
            read_started_at=read_started_at,
            read_finished_at=read_finished_at,
        )
        if observation.fingerprint_key_marker != fingerprint_key_marker(fingerprint_key):
            raise ValueError("fingerprint key")
        expires_at = read_finished_at + timedelta(seconds=selection.expires_in_seconds)
        current = _checked_now()
        if current < read_finished_at:
            raise AdhelmError(
                "CLOCK_INVALID",
                "The Google Ads planning clock moved backwards after the fixed reads.",
                exit_code=ExitCode.PRECONDITION,
            )
        if expires_at > execution.expires_at or current >= expires_at:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The requested Google Ads MAINTAIN lifetime exceeds the usable credential window.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Prepare a fresh deliberate plan with a usable credential window.",
            )
        action = build_google_ads_search_campaign_maintain_plan(
            workspace_profile_ref=workspace_profile_ref,
            account_binding=account,
            execution_binding=execution,
            intent=selection.intent,
            observation_binding=observation,
            expires_at=expires_at,
            fingerprint_key=fingerprint_key,
        )
        return action, current, read_started_at
    except AdhelmError:
        raise
    except (AttributeError, OverflowError, RecursionError, TypeError, ValueError):
        raise _facts_invalid() from None


def _account_binding(identity: Any, profile: Profile) -> GoogleAdsCampaignPauseAccountBinding:
    if (
        identity.customer_client_ref != profile.account_ref
        or identity.currency != profile.currency
        or identity.timezone != profile.timezone
    ):
        raise ValueError("account facts")
    return GoogleAdsCampaignPauseAccountBinding(
        account_ref=identity.customer_ref,
        account_client_ref=identity.customer_client_ref,
        currency=identity.currency,
        timezone=identity.timezone,
        account_status=identity.status,
    )


def _retain_provider_plan(
    ledger: Ledger,
    action: GoogleAdsSearchCampaignMaintainPlannedAction,
    *,
    before_durable_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Retain the closed MAINTAIN action, without asserting a remote condition."""

    record = google_ads_search_campaign_maintain_plan_record(action)
    if ledger.profile_ref != record.action.plan.profile_ref:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_MAINTAIN_PLAN_PROVENANCE",
            "The Google Ads MAINTAIN plan belongs to a different local workspace.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return ledger.append(
        "changes",
        GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND,
        record.model_dump(mode="json", warnings="error"),
        provider="google_ads",
        account_ref=record.action.plan.subplans[0].account.account_ref,
        source="LOCAL_CLI",
        _before_durable_append=before_durable_append,
    )


def _retain_provider_neutral_plan(
    ledger: Ledger,
    action: GoogleAdsSearchCampaignMaintainPlannedAction,
    *,
    read_started_at: datetime,
    supersedes_record_digest: str | None,
) -> RetainedExecutionPlan:
    """Retain the S3 record for the exact plan this flow already compiled."""

    subplan = action.plan.subplans[0]
    observation = action.observation_binding
    read = BoundedExecutionRead(
        observed_state=subplan.before,
        read_started_at=read_started_at,
        read_finished_at=observation.read_finished_at,
        connector_version=GOOGLE_ADS_MAINTAIN_CAPABILITY_VERSION,
    )
    candidate = CompiledExecutionCandidate(
        provider=subplan.provider,
        operation=subplan.operation,
        subplan_id=subplan.subplan_id,
        account=subplan.account,
        provider_api_version=subplan.provider_api_version,
        capability_version=subplan.capability_version,
        after=subplan.after,
        max_spend=subplan.max_spend,
        egress=subplan.egress,
        recovery_actions=subplan.recovery_actions,
        field_changes=subplan.field_changes,
        redacted_review=redacted_review(observation, action.intent),
        capability_status=action.status,
    )
    return retain_execution_plan(
        ledger,
        build_execution_plan_record(
            action.plan,
            candidate=candidate,
            read=read,
            supersedes_record_digest=supersedes_record_digest,
        ),
        now=_checked_now(),
    )


def _retained_action_for_plan(
    ledger: Ledger, plan: CompositePlan
) -> GoogleAdsSearchCampaignMaintainPlannedAction:
    """Resolve the closed MAINTAIN action for one provider-neutral plan digest."""

    expected = canonical_bytes(plan.model_dump(mode="json", warnings="error"))
    matching: list[GoogleAdsSearchCampaignMaintainPlannedAction] = []
    for stored in ledger.checkpoint()["records_by_stream"]["changes"]:
        payload: Any = stored.get("payload")
        if (
            stored.get("record_kind") != GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND
            or type(payload) is not dict
            or payload.get("plan_digest") != plan.plan_digest
        ):
            continue
        action = revalidate_google_ads_search_campaign_maintain_plan_record(payload).action
        if canonical_bytes(action.plan.model_dump(mode="json", warnings="error")) == expected:
            matching.append(action)
    if not matching:
        raise AdhelmError(
            "GOOGLE_ADS_MAINTAIN_PLAN_NOT_RETAINED",
            "The retained execution plan has no matching Google Ads MAINTAIN action in history.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile the plan again with plan execution create --provider google_ads; "
                "no credential was read and no provider request was made."
            ),
        )
    if len({canonical_bytes(item.to_data()) for item in matching}) != 1:
        raise AdhelmError(
            "GOOGLE_ADS_MAINTAIN_PLAN_AMBIGUOUS",
            "Local history retains more than one different Google Ads MAINTAIN action here.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify before requesting another execution.",
        )
    return matching[0]


#: The two singletons the registries hold, registered under the pairs in
#: :data:`GOOGLE_ADS_MAINTAIN_REGISTERED_OPERATIONS`.
GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER: Final = GoogleAdsSearchCampaignMaintainExecutionPlanner()
GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY: Final = (
    GoogleAdsSearchCampaignMaintainExecutionAdapterFactory()
)


def register_google_ads_maintain_execution() -> None:
    """Register the Google Ads maintenance planner and factory exactly once.

    Only the pairs in :data:`GOOGLE_ADS_MAINTAIN_REGISTERED_OPERATIONS` are
    registered; ``ARCHIVE`` waits for a cited reference read (rule 11).
    """

    for operation in GOOGLE_ADS_MAINTAIN_REGISTERED_OPERATIONS:
        if (
            configured_execution_planner(GOOGLE_ADS_MAINTAIN_PROVIDER, operation)
            is not GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER
        ):
            register_execution_planner(
                GOOGLE_ADS_MAINTAIN_PROVIDER, operation, GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER
            )
        if (
            configured_execution_adapter(GOOGLE_ADS_MAINTAIN_PROVIDER, operation)
            is not GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY
        ):
            register_execution_adapter(
                GOOGLE_ADS_MAINTAIN_PROVIDER,
                operation,
                GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY,
            )


def _assert_registration_seams() -> None:
    """Fail closed at import if either singleton stops matching its protocol."""

    factory: object = GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY
    if not isinstance(GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER, ConfiguredExecutionPlanner) or (
        not isinstance(factory, ConfiguredExecutionAdapterFactory)
    ):  # pragma: no cover - a structural regression, not a runtime state
        raise AdhelmError(
            "INVALID_EXECUTION_ADAPTER_REGISTRATION",
            "The Google Ads MAINTAIN registration singletons do not match their seams.",
            exit_code=ExitCode.INVALID_INPUT,
        )


_assert_registration_seams()


__all__ = [
    "GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY",
    "GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER",
    "GOOGLE_ADS_MAINTAIN_OPERATIONS",
    "GOOGLE_ADS_MAINTAIN_PROVIDER",
    "GOOGLE_ADS_MAINTAIN_REGISTERED_OPERATIONS",
    "GoogleAdsSearchCampaignMaintainExecutionAdapterFactory",
    "GoogleAdsSearchCampaignMaintainExecutionPlanner",
    "GoogleAdsSearchCampaignMaintainGuardedAdapter",
    "campaign_name_ref",
    "register_google_ads_maintain_execution",
]
