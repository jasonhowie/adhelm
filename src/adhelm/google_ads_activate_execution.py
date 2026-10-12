"""Google Ads Search hierarchy ACTIVATE on the two provider-neutral registries.

This module is the whole of P05's activation registration surface.  It adds one
:class:`~adhelm.execution_planning_session.ConfiguredExecutionPlanner` and one
:class:`~adhelm.generic_execution_workflow.ConfiguredExecutionAdapterFactory` for
the exact pair ``("google_ads", ACTIVATE)``, so ``plan execution create
--provider google_ads --operation ACTIVATE`` and ``execute plan <digest>``
become the route for this capability.  It adds no command, no approval kind, no
credential, no scope, and no live evidence: the installed approval bridge still
refuses ``COMPOSITE_EXECUTION_PLAN``, so every ordinary build refuses at the
bridge before the factory is opened and before any credential is read.

**Activation is a separate, spend-explicit authorization.**  Rule 8 and
``docs/authorization-policy.md`` both say so, and this route is what makes it
true in the code: nothing in P04's CREATE plan enables anything, and this plan
carries the campaign-lifetime budget total as the subplan's ``max_spend`` so the
``COMPOSITE_EXECUTION_PLAN`` subject a reviewer signs states the exposure.

**Two records, one join key**, exactly as P04: the planner retains the
provider-specific ``GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN`` and then the
provider-neutral ``EXECUTION_PLAN_RETAINED`` record the generic route requires.
Both describe the byte-identical :class:`~adhelm.safety.CompositePlan`, so
``plan_digest`` is the join key.
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
from adhelm.google_ads_activate_planning_session import (
    ACTIVATABLE_APPROVAL_STATUSES,
    NOT_READ_APPROVAL_STATUS,
    GoogleAdsLiveHierarchyObservation,
    GoogleAdsLiveHierarchyReader,
)
from adhelm.google_ads_activate_readback_records import (
    google_ads_activate_readback_record_kind_installed,
)
from adhelm.google_ads_create_planning_session import quotable_campaign_name
from adhelm.google_ads_pause_planning_session import (
    configured_google_ads_pause_planning_session,
)
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.providers.google_ads_activate_adapter import (
    GoogleAdsSearchCampaignActivateGuardedAdapter,
    _configured_google_ads_search_campaign_activate_adapter,
)
from adhelm.providers.google_ads_activate_contract import (
    ACTIVATED_STATUS,
    GOOGLE_ADS_ACTIVATE_CAPABILITY_VERSION,
    GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND,
    PAUSED_STATUS,
    REMOVED_STATUS,
    GoogleAdsActivateObservationBinding,
    GoogleAdsActivatePreconditions,
    GoogleAdsActivateRecoveryDisposition,
    GoogleAdsActivateSourceBinding,
    GoogleAdsSearchCampaignActivatePlannedAction,
    GoogleAdsSearchCampaignActivateSelection,
    build_google_ads_search_campaign_activate_plan,
    campaign_end_date_time_in_future,
    fingerprint_key_marker,
    google_ads_activate_plan_record_kind_installed,
    google_ads_search_campaign_activate_plan_record,
    redacted_review,
    revalidate_google_ads_search_campaign_activate_plan_record,
)
from adhelm.providers.google_ads_create_contract import (
    GOOGLE_ADS_CREATE_PLAN_RECORD_KIND,
    GoogleAdsSearchCampaignCreatePlannedAction,
    revalidate_google_ads_search_campaign_create_plan_record,
)
from adhelm.providers.google_ads_pause_contract import (
    GoogleAdsCampaignPauseAccountBinding,
    GoogleAdsCampaignPauseCampaignBaseline,
    GoogleAdsCampaignPauseObservationBinding,
    build_google_ads_campaign_pause_plan,
)
from adhelm.safety import CompositePlan, GuardedAdapter, OperationKind

#: The registered pair.
GOOGLE_ADS_ACTIVATE_PROVIDER: Final = "google_ads"
GOOGLE_ADS_ACTIVATE_OPERATION: Final = OperationKind.ACTIVATE

#: The one observed ``Campaign.bidding_strategy_type`` this route activates.
#: It is the non-conversion click strategy the P04 compiler emits.
_TARGET_SPEND_STRATEGY: Final = "TARGET_SPEND"

_SCOPE: Final = "https://www.googleapis.com/auth/adwords"
_CREATE_PLAN_RESOURCE: Final = re.compile(
    r"^google_ads\.create_plan\.(?P<record>[a-f0-9]{64})$", re.ASCII
)
_CUSTOMER_ACCOUNT: Final = re.compile(r"^google_ads\.customer\.(?P<customer>[0-9]{10})$", re.ASCII)


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
            "Google Ads ACTIVATE execution planning requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _invalid_selection(detail: str) -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_ACTIVATE_EXECUTION_SELECTION",
        detail,
        exit_code=ExitCode.INVALID_INPUT,
    )


def _facts_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_ACTIVATE_FACTS_INVALID",
        "The configured Google Ads ACTIVATE facts are incomplete or inconsistent.",
        exit_code=ExitCode.PRECONDITION,
    )


def _retention_unverified() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_ACTIVATE_PLAN_RETENTION_UNVERIFIED",
        "The Google Ads read completed, but local plan retention is unverified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect retained Google Ads ACTIVATE plan history before planning again.",
    )


class GoogleAdsSearchCampaignActivateExecutionPlanner:
    """Compile and retain one provider-neutral Google Ads ACTIVATE plan."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<GoogleAdsSearchCampaignActivateExecutionPlanner>"

    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> RetainedExecutionPlan:
        from adhelm.plan_cli import ExecutionPlanRequest

        if not isinstance(directory, Path) or type(request) is not ExecutionPlanRequest:
            raise _invalid_selection("A complete generic execution plan request is required.")
        if (
            request.provider != GOOGLE_ADS_ACTIVATE_PROVIDER
            or request.operation != GOOGLE_ADS_ACTIVATE_OPERATION.value
        ):
            raise _invalid_selection(
                "This planner compiles only google_ads ACTIVATE execution plans."
            )
        selection = request.activate_selection
        if type(selection) is not GoogleAdsSearchCampaignActivateSelection:
            raise _invalid_selection(
                "A google_ads ACTIVATE request carries the retained CREATE plan selection."
            )
        customer = _selected_customer(request.account_ref)
        record_digest = _selected_create_plan_record(request.resource_ref)
        if record_digest != selection.create_plan_record_digest:
            raise _invalid_selection(
                "resource_ref must name the same retained CREATE plan the selection carries."
            )
        # Two fail-closed build checks, before any configuration, credential, or
        # provider work: this build must be able to retain both records.
        _require_installed_record_kinds()
        if os.environ.get("CI", "").casefold() in {"1", "true"}:
            raise AdhelmError(
                "GOOGLE_ADS_ACTIVATE_CI_UNSUPPORTED",
                "Google Ads ACTIVATE planning requires the configured local "
                "protected-store runtime.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        configuration = load_configuration(directory)
        captured = digest(configuration)
        if captured != expected_configuration_digest:
            raise AdhelmError(
                "CONFIGURATION_CHANGED",
                "Local configuration changed before Google Ads ACTIVATE execution planning.",
                exit_code=ExitCode.PRECONDITION,
            )
        profile = _edit_profile(configuration, request.profile_id, customer)
        ledger = Ledger(directory)
        if ledger.unresolved_starts() or ledger.unresolved_runs():
            raise AdhelmError(
                "RECOVERY_REQUIRED",
                "Unresolved mutation history must be reconciled before ACTIVATE planning.",
                next_action="Inspect verified history and reconcile the retained evidence.",
            )
        source = _bound_source(ledger, selection, customer=customer)
        key = export_key()
        with configured_google_ads_pause_planning_session(
            directory, profile.profile_id, expected_configuration_digest=captured
        ) as session:
            action, planning_checked_at, read_started_at = _build_action_from_session(
                workspace_profile_ref=ledger.profile_ref,
                profile=profile,
                configuration_digest=captured,
                selection=selection,
                source=source,
                fingerprint_key=key,
                session=session,
            )
            with session.retention_recheck() as recheck:

                def before_durable_append() -> None:
                    # The ledger already owns its stream lock.  Recheck held
                    # local facts and the clock only; never read the ledger or
                    # the provider from here.
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
                            "The Google Ads ACTIVATE plan expired before retention.",
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


class GoogleAdsSearchCampaignActivateExecutionAdapterFactory:
    """Open the one configured guarded adapter for a retained Google ACTIVATE plan."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "<GoogleAdsSearchCampaignActivateExecutionAdapterFactory>"

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
            checked.provider != GOOGLE_ADS_ACTIVATE_PROVIDER
            or checked.operation is not GOOGLE_ADS_ACTIVATE_OPERATION
            or checked.expected_child_egress_count != 1
        ):
            raise _invalid_selection(
                "This adapter executes only a single-request google_ads ACTIVATE plan."
            )
        now = clock()
        if type(now) is not datetime or now.tzinfo is None or checked.expires_at <= now:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The retained Google Ads ACTIVATE plan expired before its adapter opened.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Compile a new plan from a fresh bounded read; no credential was read "
                    "and no provider request was made."
                ),
            )
        action = _retained_action_for_plan(ledger, checked.plan)
        return _configured_google_ads_search_campaign_activate_adapter(directory, action, ledger)


def _require_installed_record_kinds() -> None:
    from adhelm.google_ads_activate_readback_records import (
        GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND,
    )

    for installed, kind in (
        (google_ads_activate_plan_record_kind_installed(), GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND),
        (
            google_ads_activate_readback_record_kind_installed(),
            GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND,
        ),
    ):
        if not installed:
            raise AdhelmError(
                "GOOGLE_ADS_ACTIVATE_RECORD_KIND_NOT_INSTALLED",
                "This build's ledger does not register the Google Ads ACTIVATE record kinds.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    f"Install a build whose ledger registers {kind}; no credential was read "
                    "and no provider request was made."
                ),
            )


def _selected_customer(account_ref: object) -> str:
    if type(account_ref) is not str:
        raise _invalid_selection("A google_ads ACTIVATE request names one customer account.")
    matched = _CUSTOMER_ACCOUNT.fullmatch(account_ref)
    if matched is None:
        raise _invalid_selection("account_ref must be google_ads.customer.<customer-id>.")
    return matched.group("customer")


def _selected_create_plan_record(resource_ref: object) -> str:
    """Return the retained CREATE plan record digest the generic request names."""

    if type(resource_ref) is not str:
        raise _invalid_selection(
            "A google_ads ACTIVATE request names one retained CREATE plan reference."
        )
    matched = _CREATE_PLAN_RESOURCE.fullmatch(resource_ref)
    if matched is None:
        raise _invalid_selection(
            "resource_ref must be google_ads.create_plan.<retained-record-digest>."
        )
    return matched.group("record")


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
            "Google Ads ACTIVATE planning requires the configured EDIT profile for this "
            "exact customer.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Configure the Google Ads EDIT profile for this customer; nothing was read, "
                "compiled, or retained."
            ),
        )
    return profiles[0]


def _retained_create_action(
    ledger: Ledger, record_digest: str
) -> GoogleAdsSearchCampaignCreatePlannedAction:
    """Resolve one retained CREATE action from a verified local checkpoint."""

    for stored in ledger.checkpoint()["records_by_stream"]["changes"]:
        if (
            stored.get("record_kind") != GOOGLE_ADS_CREATE_PLAN_RECORD_KIND
            or stored.get("record_digest") != record_digest
        ):
            continue
        payload: Any = stored.get("payload")
        if type(payload) is not dict:
            break
        return revalidate_google_ads_search_campaign_create_plan_record(payload).action
    raise AdhelmError(
        "GOOGLE_ADS_ACTIVATE_CREATE_PLAN_NOT_RETAINED",
        "Local history holds no retained Google Ads CREATE plan with this record digest.",
        exit_code=ExitCode.PRECONDITION,
        next_action=(
            "Compile and execute the CREATE plan first; activation promotes a hierarchy this "
            "workspace planned. No credential was read and no provider request was made."
        ),
    )


def _bound_source(
    ledger: Ledger, selection: GoogleAdsSearchCampaignActivateSelection, *, customer: str
) -> GoogleAdsActivateSourceBinding:
    """Bind the retained CREATE plan and restate its authorized baseline."""

    action = _retained_create_action(ledger, selection.create_plan_record_digest)
    if action.account_binding.account_client_ref != customer:
        raise _invalid_selection(
            "The retained CREATE plan belongs to a different Google Ads customer."
        )
    if action.observation_binding.campaign_name != selection.campaign_name:
        raise _invalid_selection("The retained CREATE plan configured a different campaign name.")
    specification = action.candidate_binding.specification
    try:
        return GoogleAdsActivateSourceBinding(
            create_plan_record_digest=selection.create_plan_record_digest,
            create_action_id=action.action_id,
            create_plan_digest=action.plan.plan_digest,
            campaign_name=action.observation_binding.campaign_name,
            authorized_budget_total_amount_micros=specification.budget.request.total_amount_micros,
            authorized_start_date_time=specification.campaign_request.start_date_time,
            authorized_end_date_time=specification.campaign_request.end_date_time,
            authorized_member_count=action.postcondition.member_count,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _facts_invalid() from None


def _assert_activatable(
    observation: GoogleAdsLiveHierarchyObservation,
    source: GoogleAdsActivateSourceBinding,
    *,
    timezone: str,
    now: datetime,
) -> str:
    """Run every activation precondition and return the disclosed policy status."""

    statuses = set(observation.projection.statuses) | {observation.campaign_status}
    if REMOVED_STATUS in statuses:
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_MEMBER_REMOVED",
            "A removed Google Ads entity is never activated.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile a new hierarchy; nothing was activated and nothing was removed here."
            ),
        )
    if ACTIVATED_STATUS in statuses:
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_ALREADY_ENABLED",
            "This Google Ads hierarchy already holds an ENABLED entity, so there is nothing "
            "to activate.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Review the current campaign state; a no-operation is not a plan and nothing "
                "was changed."
            ),
        )
    if statuses != {PAUSED_STATUS}:
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_HIERARCHY_NOT_PAUSED",
            "An activation plan requires an entirely paused Google Ads hierarchy.",
            exit_code=ExitCode.PRECONDITION,
        )
    campaign = observation.projection.configured("CAMPAIGN")
    if campaign.get("advertising_channel_type") != "SEARCH":
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_CAMPAIGN_TYPE_UNSUPPORTED",
            "This route activates a Search campaign only.",
            exit_code=ExitCode.PRECONDITION,
        )
    # The plan states ``TARGET_SPEND_NON_CONVERSION_OBJECTIVE`` in its review
    # lines.  That has to be a check, not a restatement of the compiler's own
    # literal: a campaign whose observed strategy is a conversion one would
    # make the plan's own conversion-dependency disclosure false, and
    # conversion-dependent activation is a separate C02 dependency.
    if campaign.get("bidding_strategy") != _TARGET_SPEND_STRATEGY:
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_BIDDING_STRATEGY_UNSUPPORTED",
            "This route activates the standard target_spend click strategy only.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Conversion-dependent activation is a separate capability; nothing was activated."
            ),
        )
    if (
        observation.budget_total_amount_micros != source.authorized_budget_total_amount_micros
        or observation.start_date_time != source.authorized_start_date_time
        or observation.end_date_time != source.authorized_end_date_time
    ):
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_BASELINE_DRIFT",
            "The campaign budget or its dates changed after the CREATE plan was authorized.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Reconcile the out-of-band change and compile a new exact plan; nothing was "
                "activated."
            ),
        )
    if not campaign_end_date_time_in_future(observation.end_date_time, timezone=timezone, now=now):
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_CAMPAIGN_ENDED",
            "This Google Ads campaign's end date-time has already passed.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Compile a campaign whose flight is still ahead; nothing was activated.",
        )
    if observation.ad_final_url_count < 1:
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_DESTINATION_MISSING",
            "An ad with no final URL has no destination and is never activated.",
            exit_code=ExitCode.PRECONDITION,
        )
    status = observation.policy_approval_status
    if status == "DISAPPROVED":
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_AD_DISAPPROVED",
            "Google Ads reports this ad as DISAPPROVED, so it is not activated.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Resolve the policy finding in Google Ads and compile a new exact plan; "
                "nothing was activated."
            ),
        )
    if status != NOT_READ_APPROVAL_STATUS and status not in ACTIVATABLE_APPROVAL_STATUSES:
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_POLICY_STATUS_UNSUPPORTED",
            "Google Ads reported an ad policy approval status this build does not interpret.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Review the ad in Google Ads; nothing was activated.",
        )
    return status


def _build_action_from_session(
    *,
    workspace_profile_ref: str,
    profile: Profile,
    configuration_digest: str,
    selection: GoogleAdsSearchCampaignActivateSelection,
    source: GoogleAdsActivateSourceBinding,
    fingerprint_key: bytes,
    session: Any,
) -> tuple[GoogleAdsSearchCampaignActivatePlannedAction, datetime, datetime]:
    try:
        execution = session.execution_binding
        reader = GoogleAdsLiveHierarchyReader(session)
        name = quotable_campaign_name(selection.campaign_name)
        read_started_at = _checked_now()
        identity = reader.discover_identity()
        if identity.status != "ENABLED":
            raise AdhelmError(
                "GOOGLE_ADS_ACCOUNT_NOT_ENABLED",
                "A Google Ads Search hierarchy is activated only in an ENABLED customer.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured Google Ads customer; nothing was activated.",
            )
        observed = reader.observe_live_hierarchy(name)
        read_finished_at = _checked_now()
        account = _account_binding(identity, profile)
        policy_status = _assert_activatable(
            observed, source, timezone=account.timezone, now=read_finished_at
        )
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
        observation = GoogleAdsActivateObservationBinding(
            campaign_name=name,
            projection=observed.projection,
            observed_budget_total_amount_micros=observed.budget_total_amount_micros,
            observed_start_date_time=observed.start_date_time,
            observed_end_date_time=observed.end_date_time,
            observed_ad_final_url_count=observed.ad_final_url_count,
            preconditions=GoogleAdsActivatePreconditions(
                policy_approval_status=policy_status  # type: ignore[arg-type]
            ),
            fingerprint_key_marker=fingerprint_key_marker(fingerprint_key),
            read_started_at=read_started_at,
            read_finished_at=read_finished_at,
        )
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
                "The requested Google Ads ACTIVATE lifetime exceeds the usable credential window.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Prepare a fresh deliberate plan with a usable credential window.",
            )
        recovery = _precomputed_pause_proposal(
            workspace_profile_ref=workspace_profile_ref,
            account=account,
            execution=execution,
            campaign_client_ref=observed.campaign_client_ref,
            read_started_at=read_started_at,
            read_finished_at=read_finished_at,
            expires_at=expires_at,
        )
        action = build_google_ads_search_campaign_activate_plan(
            workspace_profile_ref=workspace_profile_ref,
            account_binding=account,
            execution_binding=execution,
            source_binding=source,
            observation_binding=observation,
            recovery_disposition=recovery,
            expires_at=expires_at,
            fingerprint_key=fingerprint_key,
        )
        return action, current, read_started_at
    except AdhelmError:
        raise
    except (AttributeError, OverflowError, RecursionError, TypeError, ValueError):
        raise _facts_invalid() from None


def _precomputed_pause_proposal(
    *,
    workspace_profile_ref: str,
    account: GoogleAdsCampaignPauseAccountBinding,
    execution: Any,
    campaign_client_ref: str,
    read_started_at: datetime,
    read_finished_at: datetime,
    expires_at: datetime,
) -> GoogleAdsActivateRecoveryDisposition:
    """Precompute the exact pause plan that would undo this activation.

    The M55 PAUSE contract is reused verbatim: its campaign baseline is
    ``ENABLED``, which is precisely the state this activation produces.  Only
    the resulting plan digest is retained.  The plan itself is *not* retained,
    *not* authorized, and *not* executed by this route -- pausing is its own
    operation with its own exact grant.
    """

    proposal = build_google_ads_campaign_pause_plan(
        workspace_profile_ref=workspace_profile_ref,
        account_binding=account,
        execution_binding=execution,
        observation_binding=GoogleAdsCampaignPauseObservationBinding(
            campaign=GoogleAdsCampaignPauseCampaignBaseline(
                campaign_ref=(
                    f"google_ads.campaign.{account.account_client_ref}.{campaign_client_ref}"
                ),
                campaign_client_ref=campaign_client_ref,
                account_ref=account.account_ref,
                account_client_ref=account.account_client_ref,
            ),
            read_started_at=read_started_at,
            read_finished_at=read_finished_at,
        ),
        expires_at=expires_at,
    )
    return GoogleAdsActivateRecoveryDisposition(
        recovery_pause_plan_digest=proposal.plan.plan_digest
    )


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
    action: GoogleAdsSearchCampaignActivatePlannedAction,
    *,
    before_durable_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Retain the closed ACTIVATE action, without asserting a remote condition."""

    record = google_ads_search_campaign_activate_plan_record(action)
    if ledger.profile_ref != record.action.plan.profile_ref:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_ACTIVATE_PLAN_PROVENANCE",
            "The Google Ads ACTIVATE plan belongs to a different local workspace.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return ledger.append(
        "changes",
        GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND,
        record.model_dump(mode="json", warnings="error"),
        provider="google_ads",
        account_ref=record.action.plan.subplans[0].account.account_ref,
        source="LOCAL_CLI",
        _before_durable_append=before_durable_append,
    )


def _retain_provider_neutral_plan(
    ledger: Ledger,
    action: GoogleAdsSearchCampaignActivatePlannedAction,
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
        connector_version=GOOGLE_ADS_ACTIVATE_CAPABILITY_VERSION,
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
        # The spend-explicit review the approval host renders verbatim.
        redacted_review=redacted_review(observation),
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
) -> GoogleAdsSearchCampaignActivatePlannedAction:
    """Resolve the closed ACTIVATE action for one provider-neutral plan digest."""

    expected = canonical_bytes(plan.model_dump(mode="json", warnings="error"))
    matching: list[GoogleAdsSearchCampaignActivatePlannedAction] = []
    for stored in ledger.checkpoint()["records_by_stream"]["changes"]:
        payload: Any = stored.get("payload")
        if (
            stored.get("record_kind") != GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND
            or type(payload) is not dict
            or payload.get("plan_digest") != plan.plan_digest
        ):
            continue
        action = revalidate_google_ads_search_campaign_activate_plan_record(payload).action
        if canonical_bytes(action.plan.model_dump(mode="json", warnings="error")) == expected:
            matching.append(action)
    if not matching:
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_PLAN_NOT_RETAINED",
            "The retained execution plan has no matching Google Ads ACTIVATE action in history.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile the plan again with plan execution create --provider google_ads "
                "--operation ACTIVATE; no credential was read and no provider request was made."
            ),
        )
    if len({canonical_bytes(item.to_data()) for item in matching}) != 1:
        raise AdhelmError(
            "GOOGLE_ADS_ACTIVATE_PLAN_AMBIGUOUS",
            "Local history retains more than one different Google Ads ACTIVATE action here.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify before requesting another execution.",
        )
    return matching[0]


#: The two singletons the registries hold.
GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER: Final = GoogleAdsSearchCampaignActivateExecutionPlanner()
GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY: Final = (
    GoogleAdsSearchCampaignActivateExecutionAdapterFactory()
)


def register_google_ads_activate_execution() -> None:
    """Register the Google Ads ACTIVATE planner and adapter factory exactly once."""

    if (
        configured_execution_planner(GOOGLE_ADS_ACTIVATE_PROVIDER, GOOGLE_ADS_ACTIVATE_OPERATION)
        is not GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER
    ):
        register_execution_planner(
            GOOGLE_ADS_ACTIVATE_PROVIDER,
            GOOGLE_ADS_ACTIVATE_OPERATION,
            GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER,
        )
    if (
        configured_execution_adapter(GOOGLE_ADS_ACTIVATE_PROVIDER, GOOGLE_ADS_ACTIVATE_OPERATION)
        is not GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY
    ):
        register_execution_adapter(
            GOOGLE_ADS_ACTIVATE_PROVIDER,
            GOOGLE_ADS_ACTIVATE_OPERATION,
            GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY,
        )


def _assert_registration_seams() -> None:
    """Fail closed at import if either singleton stops matching its protocol."""

    factory: object = GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY
    if not isinstance(GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER, ConfiguredExecutionPlanner) or (
        not isinstance(factory, ConfiguredExecutionAdapterFactory)
    ):  # pragma: no cover - a structural regression, not a runtime state
        raise AdhelmError(
            "INVALID_EXECUTION_ADAPTER_REGISTRATION",
            "The Google Ads ACTIVATE registration singletons do not match their seams.",
            exit_code=ExitCode.INVALID_INPUT,
        )


_assert_registration_seams()


__all__ = [
    "GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY",
    "GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER",
    "GOOGLE_ADS_ACTIVATE_OPERATION",
    "GOOGLE_ADS_ACTIVATE_PROVIDER",
    "GoogleAdsSearchCampaignActivateExecutionAdapterFactory",
    "GoogleAdsSearchCampaignActivateExecutionPlanner",
    "GoogleAdsSearchCampaignActivateGuardedAdapter",
    "register_google_ads_activate_execution",
]
