"""Google Ads campaign PAUSE on the two provider-neutral registries.

This module is the whole of P03's registration surface.  It adds one
:class:`~adhelm.execution_planning_session.ConfiguredExecutionPlanner` and one
:class:`~adhelm.generic_execution_workflow.ConfiguredExecutionAdapterFactory` for
the exact pair ``("google_ads", PAUSE)``, so ``plan execution create
--provider google_ads --operation PAUSE`` and ``execute plan <digest>`` become
the route for this capability.  It adds no command, no approval kind, no
credential, no scope, and no live evidence: the installed approval bridge still
refuses ``COMPOSITE_EXECUTION_PLAN``, so every ordinary build refuses at the
bridge before the factory is opened and before any credential is read.

**Two records, one join key.**  ``plan google-ads-campaign-pause create``
(F01/M55) is unchanged and still retains its provider-specific
``GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN``, which the F02 catalog, review, and artifact
surfaces read.  The planner here runs that same F01 flow and then retains the
provider-neutral ``EXECUTION_PLAN_RETAINED`` record the generic route requires.
Both records describe the byte-identical
:class:`~adhelm.safety.CompositePlan`, so ``plan_digest`` is the join key: the
adapter factory resolves the retained action from verified local history by
that digest and refuses unless the two plans compare equal.  Keeping the
provider record is not duplication for its own sake -- it carries the closed
M55 action (customer baseline, EDIT association, bounded observation,
postcondition) that the provider-neutral record deliberately does not model and
that the adapter needs to reach exactly one campaign.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
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
from adhelm.google_ads_pause_plan_records import revalidate_google_ads_campaign_pause_plan_record
from adhelm.ledger import Ledger
from adhelm.providers.google_ads_pause_adapter import (
    GoogleAdsCampaignPauseGuardedAdapter,
    _configured_google_ads_campaign_pause_adapter,
)
from adhelm.providers.google_ads_pause_contract import (
    GOOGLE_ADS_CAMPAIGN_PAUSE_CAPABILITY_VERSION,
    GoogleAdsCampaignPausePlannedAction,
)
from adhelm.safety import CompositePlan, GuardedAdapter, OperationKind

#: The registered pair.  ``RECOVER`` is never registrable: a recovery reuses
#: the adapter of the operation that started the attempt.
GOOGLE_ADS_PAUSE_PROVIDER: Final = "google_ads"
GOOGLE_ADS_PAUSE_OPERATION: Final = OperationKind.PAUSE

#: The retained provider-specific family the adapter factory joins against.
_PROVIDER_PLAN_KIND: Final = "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"

#: The generic request carries no lifetime, so the planner uses the F01
#: request's own documented default rather than inventing a second bound.
_DEFAULT_PLAN_LIFETIME_SECONDS: Final = 300

_CAMPAIGN_RESOURCE: Final = re.compile(
    r"^google_ads\.campaign\.(?P<customer>[0-9]{10})\.(?P<campaign>[1-9][0-9]{0,18})$", re.ASCII
)
_MAX_INT64: Final = 9_223_372_036_854_775_807


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
            "Google Ads PAUSE execution planning requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _invalid_selection(detail: str) -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_PAUSE_EXECUTION_SELECTION",
        detail,
        exit_code=ExitCode.INVALID_INPUT,
    )


class GoogleAdsCampaignPauseExecutionPlanner:
    """Compile and retain one provider-neutral Google Ads PAUSE plan.

    The planner owns nothing the generic command could own: it reuses the
    complete F01 workflow -- its configured OAuth EDIT profile selection, its
    three-slot protected continuity, its two fixed ``googleAds:search`` reads,
    its clock and expiry checks, and its retention recheck -- and then retains
    the provider-neutral record from the exact plan that flow produced.  It
    performs no provider request and opens no credential of its own.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<GoogleAdsCampaignPauseExecutionPlanner>"

    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> RetainedExecutionPlan:
        from adhelm.google_ads_pause_workflow import (
            GoogleAdsCampaignPausePlanRequest,
            plan_configured_google_ads_campaign_pause,
        )
        from adhelm.plan_cli import ExecutionPlanRequest

        if not isinstance(directory, Path) or type(request) is not ExecutionPlanRequest:
            raise _invalid_selection("A complete generic execution plan request is required.")
        if (
            request.provider != GOOGLE_ADS_PAUSE_PROVIDER
            or request.operation != GOOGLE_ADS_PAUSE_OPERATION.value
        ):
            raise _invalid_selection("This planner compiles only google_ads PAUSE execution plans.")
        customer, campaign = _selected_campaign(request.resource_ref)
        if request.account_ref != f"google_ads.customer.{customer}":
            raise _invalid_selection(
                "The requested campaign is outside the requested Google Ads customer."
            )
        # One local configuration read, before the F01 flow opens its protected
        # session, so a request that names another customer is refused without
        # a credential, a provider read, or a retained plan.
        configuration = load_configuration(directory)
        if digest(configuration) != expected_configuration_digest:
            raise AdhelmError(
                "CONFIGURATION_CHANGED",
                "Local configuration changed before Google Ads PAUSE execution planning.",
                exit_code=ExitCode.PRECONDITION,
            )
        selected = [
            profile
            for profile in configuration.profiles
            if profile.profile_id == request.profile_id
        ]
        if len(selected) != 1 or getattr(selected[0], "account_ref", None) != customer:
            raise AdhelmError(
                "GOOGLE_ADS_EDIT_PROFILE_REQUIRED",
                "Google Ads PAUSE planning requires the configured EDIT profile for "
                "this exact customer.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Configure the Google Ads EDIT profile for this customer; nothing was "
                    "read, compiled, or retained."
                ),
            )
        retained = plan_configured_google_ads_campaign_pause(
            directory,
            GoogleAdsCampaignPausePlanRequest(
                profile_id=request.profile_id,
                campaign_id=campaign,
                expires_in_seconds=_DEFAULT_PLAN_LIFETIME_SECONDS,
            ),
            expected_configuration_digest=expected_configuration_digest,
        )
        return _retain_provider_neutral_plan(
            Ledger(directory),
            retained.action,
            supersedes_record_digest=request.supersedes_record_digest,
        )


class GoogleAdsCampaignPauseExecutionAdapterFactory:
    """Open the one configured guarded adapter for a retained Google PAUSE plan.

    ``declared_capabilities`` is answered from this module's own compiled
    contract: the fixed route is a single ``campaigns:mutate`` request, so it
    declares no journaled child egress.  It reads no configuration, opens no
    credential store, and contacts no provider, because the generic route asks
    for it before the approval bridge builds or consumes a receipt.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<GoogleAdsCampaignPauseExecutionAdapterFactory>"

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
            checked.provider != GOOGLE_ADS_PAUSE_PROVIDER
            or checked.operation is not GOOGLE_ADS_PAUSE_OPERATION
            or checked.expected_child_egress_count != 1
        ):
            raise _invalid_selection(
                "This adapter executes only a single-request google_ads PAUSE plan."
            )
        # The supplied clock is the route's own checked one.  Refusing an
        # expired plan here keeps the credential store closed for a plan the
        # kernel would refuse a moment later anyway.
        now = clock()
        if type(now) is not datetime or now.tzinfo is None or checked.expires_at <= now:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The retained Google Ads PAUSE plan expired before its adapter opened.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Compile a new plan from a fresh bounded read; no credential was read "
                    "and no provider request was made."
                ),
            )
        action = _retained_action_for_plan(ledger, checked.plan)
        return _configured_google_ads_campaign_pause_adapter(directory, action, ledger)


def _selected_campaign(resource_ref: object) -> tuple[str, str]:
    """Return the exact customer and campaign the generic request names."""

    if type(resource_ref) is not str:
        raise _invalid_selection(
            "A google_ads PAUSE request names one campaign resource reference."
        )
    matched = _CAMPAIGN_RESOURCE.fullmatch(resource_ref)
    if matched is None or int(matched.group("campaign")) > _MAX_INT64:
        raise _invalid_selection(
            "resource_ref must be google_ads.campaign.<customer-id>.<campaign-id>."
        )
    return matched.group("customer"), matched.group("campaign")


def _retain_provider_neutral_plan(
    ledger: Ledger,
    action: GoogleAdsCampaignPausePlannedAction,
    *,
    supersedes_record_digest: str | None,
) -> RetainedExecutionPlan:
    """Retain the S3 record for the exact plan the F01 flow already compiled.

    Nothing is recompiled or predicted here: the composite plan, its observed
    ``before`` fingerprint, its predicted ``after``, its expiry, and its
    bounded read interval are the retained action's own, so both records name
    one ``plan_digest``.
    """

    subplan = action.plan.subplans[0]
    observation = action.observation_binding
    read = BoundedExecutionRead(
        observed_state=subplan.before,
        read_started_at=observation.read_started_at,
        read_finished_at=observation.read_finished_at,
        connector_version=GOOGLE_ADS_CAMPAIGN_PAUSE_CAPABILITY_VERSION,
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
) -> GoogleAdsCampaignPausePlannedAction:
    """Resolve the closed M55 action for one provider-neutral plan digest.

    The join key is the composite plan digest both records already carry.  The
    scan reads a verified checkpoint, revalidates every candidate, and requires
    the retained action's own plan to compare byte for byte with the approved
    one, so a record that merely reuses a digest cannot substitute a different
    customer, campaign, or credential association.
    """

    expected = canonical_bytes(plan.model_dump(mode="json", warnings="error"))
    matching: list[GoogleAdsCampaignPausePlannedAction] = []
    for stored in ledger.checkpoint()["records_by_stream"]["changes"]:
        payload: Any = stored.get("payload")
        if (
            stored.get("record_kind") != _PROVIDER_PLAN_KIND
            or type(payload) is not dict
            or payload.get("plan_digest") != plan.plan_digest
        ):
            continue
        action = revalidate_google_ads_campaign_pause_plan_record(payload).action
        if canonical_bytes(action.plan.model_dump(mode="json", warnings="error")) == expected:
            matching.append(action)
    if not matching:
        raise AdhelmError(
            "GOOGLE_ADS_PAUSE_PLAN_NOT_RETAINED",
            "The retained execution plan has no matching Google Ads PAUSE action in history.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile the plan again with plan execution create --provider google_ads "
                "--operation PAUSE; no credential was read and no provider request was made."
            ),
        )
    if len({canonical_bytes(item.to_data()) for item in matching}) != 1:
        raise AdhelmError(
            "GOOGLE_ADS_PAUSE_PLAN_AMBIGUOUS",
            "Local history retains more than one different Google Ads PAUSE action here.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify before requesting another execution.",
        )
    return matching[0]


#: The two singletons the registries hold.  A module-level instance keeps
#: registration idempotent across a reimport without letting a *different*
#: object silently replace an installed one.
GOOGLE_ADS_PAUSE_EXECUTION_PLANNER: Final = GoogleAdsCampaignPauseExecutionPlanner()
GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY: Final = GoogleAdsCampaignPauseExecutionAdapterFactory()


def register_google_ads_pause_execution() -> None:
    """Register the Google Ads PAUSE planner and adapter factory exactly once.

    Called at import of :mod:`adhelm.google_ads_pause_workflow`, which ``cli.py``
    already imports through ``google_ads_pause_cli``, so ``discover`` renders
    the pair from the registries themselves rather than from a restated list.
    A second registration of a *different* factory is still refused by the
    registries; only a repeat of these exact singletons is a no-op.
    """

    if (
        configured_execution_planner(GOOGLE_ADS_PAUSE_PROVIDER, GOOGLE_ADS_PAUSE_OPERATION)
        is not GOOGLE_ADS_PAUSE_EXECUTION_PLANNER
    ):
        register_execution_planner(
            GOOGLE_ADS_PAUSE_PROVIDER,
            GOOGLE_ADS_PAUSE_OPERATION,
            GOOGLE_ADS_PAUSE_EXECUTION_PLANNER,
        )
    if (
        configured_execution_adapter(GOOGLE_ADS_PAUSE_PROVIDER, GOOGLE_ADS_PAUSE_OPERATION)
        is not GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY
    ):
        register_execution_adapter(
            GOOGLE_ADS_PAUSE_PROVIDER,
            GOOGLE_ADS_PAUSE_OPERATION,
            GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY,
        )


def _assert_registration_seams() -> None:
    """Fail closed at import if either singleton stops matching its protocol."""

    factory: object = GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY
    if not isinstance(GOOGLE_ADS_PAUSE_EXECUTION_PLANNER, ConfiguredExecutionPlanner) or (
        not isinstance(factory, ConfiguredExecutionAdapterFactory)
    ):  # pragma: no cover - a structural regression, not a runtime state
        raise AdhelmError(
            "INVALID_EXECUTION_ADAPTER_REGISTRATION",
            "The Google Ads PAUSE registration singletons do not match their seams.",
            exit_code=ExitCode.INVALID_INPUT,
        )


_assert_registration_seams()


__all__ = [
    "GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY",
    "GOOGLE_ADS_PAUSE_EXECUTION_PLANNER",
    "GOOGLE_ADS_PAUSE_OPERATION",
    "GOOGLE_ADS_PAUSE_PROVIDER",
    "GoogleAdsCampaignPauseExecutionAdapterFactory",
    "GoogleAdsCampaignPauseExecutionPlanner",
    "GoogleAdsCampaignPauseGuardedAdapter",
    "register_google_ads_pause_execution",
]
