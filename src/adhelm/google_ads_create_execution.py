"""Google Ads paused Search hierarchy CREATE on the two provider-neutral registries.

This module is the whole of P04's registration surface.  It adds one
:class:`~adhelm.execution_planning_session.ConfiguredExecutionPlanner` and one
:class:`~adhelm.generic_execution_workflow.ConfiguredExecutionAdapterFactory` for
the exact pair ``("google_ads", CREATE)``, so ``plan execution create
--provider google_ads --operation CREATE`` and ``execute plan <digest>`` become
the route for this capability.  It adds no command, no approval kind, no
credential, no scope, and no live evidence: the installed approval bridge still
refuses ``COMPOSITE_EXECUTION_PLAN``, so every ordinary build refuses at the
bridge before the factory is opened and before any credential is read.

**Two records, one join key.**  The planner retains the provider-specific
``GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN``, which carries the closed action --
the recompiled candidate, the customer baseline, the EDIT association, the
bounded promotion checks, and the postcondition -- and then the
provider-neutral ``EXECUTION_PLAN_RETAINED`` record the generic route requires.
Both describe the byte-identical :class:`~adhelm.safety.CompositePlan`, so
``plan_digest`` is the join key: the adapter factory resolves the retained
action from verified local history by that digest and refuses unless the two
plans compare equal.  The provider record is not duplication for its own sake:
the provider-neutral record deliberately models no native request, and the
adapter cannot build one atomic ``googleAds:mutate`` body without the compiled
candidate it holds.
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
from adhelm.google_ads_create_planning_session import (
    GoogleAdsSearchCampaignCreateReader,
    quotable_campaign_name,
)
from adhelm.google_ads_create_readback_records import (
    google_ads_create_readback_record_kind_installed,
)
from adhelm.google_ads_pause_planning_session import (
    configured_google_ads_pause_planning_session,
)
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.providers.google_ads_compiler import compile_google_ads_search_campaign
from adhelm.providers.google_ads_create_adapter import (
    GoogleAdsSearchCampaignCreateGuardedAdapter,
    _configured_google_ads_search_campaign_create_adapter,
)
from adhelm.providers.google_ads_create_contract import (
    GOOGLE_ADS_CREATE_CAPABILITY_VERSION,
    GOOGLE_ADS_CREATE_PLAN_RECORD_KIND,
    GoogleAdsSearchCampaignCreateCandidateBinding,
    GoogleAdsSearchCampaignCreateObservationBinding,
    GoogleAdsSearchCampaignCreatePlannedAction,
    GoogleAdsSearchCampaignCreatePreconditions,
    GoogleAdsSearchCampaignCreateSelection,
    build_google_ads_search_campaign_create_plan,
    fingerprint_key_marker,
    google_ads_create_plan_record_kind_installed,
    google_ads_search_campaign_create_plan_record,
    projection_from_compiled_spec,
    revalidate_google_ads_search_campaign_create_plan_record,
)
from adhelm.safety import CompositePlan, GuardedAdapter, OperationKind
from adhelm.saved_candidates import compile_candidate, load_candidate

#: The registered pair.  ``RECOVER`` is never registrable: a recovery reuses
#: the adapter of the operation that started the attempt.
GOOGLE_ADS_CREATE_PROVIDER: Final = "google_ads"
GOOGLE_ADS_CREATE_OPERATION: Final = OperationKind.CREATE

_SCOPE: Final = "https://www.googleapis.com/auth/adwords"
_DIGEST: Final = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_CANDIDATE_RESOURCE: Final = re.compile(
    r"^google_ads\.candidate\.(?P<record>[a-f0-9]{64})$", re.ASCII
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
            "Google Ads CREATE execution planning requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _invalid_selection(detail: str) -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_CREATE_EXECUTION_SELECTION",
        detail,
        exit_code=ExitCode.INVALID_INPUT,
    )


def _facts_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_CREATE_FACTS_INVALID",
        "The configured Google Ads CREATE facts are incomplete or inconsistent.",
        exit_code=ExitCode.PRECONDITION,
    )


def _retention_unverified() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_CREATE_PLAN_RETENTION_UNVERIFIED",
        "The Google Ads read completed, but local plan retention is unverified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect retained Google Ads CREATE plan history before planning again.",
    )


class GoogleAdsSearchCampaignCreateExecutionPlanner:
    """Compile and retain one provider-neutral Google Ads Search CREATE plan.

    The planner owns exactly what the generic command cannot: the recompilation
    that binds a retained offline candidate, the configured OAuth EDIT profile
    selection, the sealed three-slot protected session, the bounded promotion
    reads the compiler contract requires, and the two retentions.  It performs
    no mutation and opens no credential outside that sealed session.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<GoogleAdsSearchCampaignCreateExecutionPlanner>"

    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> RetainedExecutionPlan:
        from adhelm.plan_cli import ExecutionPlanRequest

        if not isinstance(directory, Path) or type(request) is not ExecutionPlanRequest:
            raise _invalid_selection("A complete generic execution plan request is required.")
        if (
            request.provider != GOOGLE_ADS_CREATE_PROVIDER
            or request.operation != GOOGLE_ADS_CREATE_OPERATION.value
        ):
            raise _invalid_selection(
                "This planner compiles only google_ads CREATE execution plans."
            )
        selection = request.selection
        if type(selection) is not GoogleAdsSearchCampaignCreateSelection:
            raise _invalid_selection(
                "A google_ads CREATE request carries the retained candidate and its brief."
            )
        customer = _selected_customer(request.account_ref)
        record_digest = _selected_candidate_record(request.resource_ref)
        if record_digest != selection.candidate_record_digest:
            raise _invalid_selection(
                "resource_ref must name the same retained candidate the selection carries."
            )
        if selection.fresh.account_ref != request.account_ref:
            raise _invalid_selection(
                "The compiled candidate is outside the requested Google Ads customer."
            )
        # Two fail-closed build checks, before any configuration, credential, or
        # provider work: this build must be able to retain both records.
        _require_installed_record_kinds()
        if os.environ.get("CI", "").casefold() in {"1", "true"}:
            raise AdhelmError(
                "GOOGLE_ADS_CREATE_CI_UNSUPPORTED",
                "Google Ads CREATE planning requires the configured local protected-store runtime.",
                exit_code=ExitCode.AUTHORIZATION,
            )
        configuration = load_configuration(directory)
        captured = digest(configuration)
        if captured != expected_configuration_digest:
            raise AdhelmError(
                "CONFIGURATION_CHANGED",
                "Local configuration changed before Google Ads CREATE execution planning.",
                exit_code=ExitCode.PRECONDITION,
            )
        profile = _edit_profile(configuration, request.profile_id, customer)
        ledger = Ledger(directory)
        if ledger.unresolved_starts() or ledger.unresolved_runs():
            raise AdhelmError(
                "RECOVERY_REQUIRED",
                "Unresolved mutation history must be reconciled before CREATE planning.",
                next_action="Inspect verified history and reconcile the retained evidence.",
            )
        candidate = _bound_candidate(ledger, selection)
        name = quotable_campaign_name(candidate.specification.campaign_request.name)
        key = export_key()
        with configured_google_ads_pause_planning_session(
            directory, profile.profile_id, expected_configuration_digest=captured
        ) as session:
            action, planning_checked_at, read_started_at = _build_action_from_session(
                workspace_profile_ref=ledger.profile_ref,
                profile=profile,
                configuration_digest=captured,
                selection=selection,
                candidate=candidate,
                campaign_name=name,
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
                            "The Google Ads CREATE plan expired before retention.",
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


class GoogleAdsSearchCampaignCreateExecutionAdapterFactory:
    """Open the one configured guarded adapter for a retained Google CREATE plan.

    ``declared_capabilities`` is answered from this module's own compiled
    contract: the fixed route is a single ``googleAds:mutate`` request with
    temporary resource IDs, so it declares no journaled child egress.  It reads
    no configuration, opens no credential store, and contacts no provider,
    because the generic route asks for it before the approval bridge builds or
    consumes a receipt.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<GoogleAdsSearchCampaignCreateExecutionAdapterFactory>"

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
            checked.provider != GOOGLE_ADS_CREATE_PROVIDER
            or checked.operation is not GOOGLE_ADS_CREATE_OPERATION
            or checked.expected_child_egress_count != 1
        ):
            raise _invalid_selection(
                "This adapter executes only a single-request google_ads CREATE plan."
            )
        now = clock()
        if type(now) is not datetime or now.tzinfo is None or checked.expires_at <= now:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The retained Google Ads CREATE plan expired before its adapter opened.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Compile a new plan from a fresh bounded read; no credential was read "
                    "and no provider request was made."
                ),
            )
        action = _retained_action_for_plan(ledger, checked.plan)
        return _configured_google_ads_search_campaign_create_adapter(directory, action, ledger)


def _require_installed_record_kinds() -> None:
    for installed, kind in (
        (google_ads_create_plan_record_kind_installed(), GOOGLE_ADS_CREATE_PLAN_RECORD_KIND),
        (
            google_ads_create_readback_record_kind_installed(),
            "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_READBACK_OBSERVATION",
        ),
    ):
        if not installed:
            raise AdhelmError(
                "GOOGLE_ADS_CREATE_RECORD_KIND_NOT_INSTALLED",
                "This build's ledger does not register the Google Ads CREATE record kinds.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    f"Install a build whose ledger registers {kind}; no credential was read "
                    "and no provider request was made."
                ),
            )


def _selected_customer(account_ref: object) -> str:
    if type(account_ref) is not str:
        raise _invalid_selection("A google_ads CREATE request names one customer account.")
    matched = _CUSTOMER_ACCOUNT.fullmatch(account_ref)
    if matched is None:
        raise _invalid_selection("account_ref must be google_ads.customer.<customer-id>.")
    return matched.group("customer")


def _selected_candidate_record(resource_ref: object) -> str:
    """Return the retained candidate record digest the generic request names."""

    if type(resource_ref) is not str:
        raise _invalid_selection(
            "A google_ads CREATE request names one retained candidate reference."
        )
    matched = _CANDIDATE_RESOURCE.fullmatch(resource_ref)
    if matched is None:
        raise _invalid_selection(
            "resource_ref must be google_ads.candidate.<retained-record-digest>."
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
            "Google Ads CREATE planning requires the configured EDIT profile for this "
            "exact customer.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Configure the Google Ads EDIT profile for this customer; nothing was read, "
                "compiled, or retained."
            ),
        )
    return profiles[0]


def _bound_candidate(
    ledger: Ledger, selection: GoogleAdsSearchCampaignCreateSelection
) -> GoogleAdsSearchCampaignCreateCandidateBinding:
    """Recompile the supplied brief and bind it to the retained candidate.

    Neither the compiled specification nor the brief is asserted: the brief is
    recompiled here and the retained record's own review is required to be the
    byte-identical result.  A retained candidate that names a different spec,
    input, or provider is refused before any provider read.
    """

    retained, provenance = load_candidate(ledger, selection.candidate_record_digest)
    recompiled = compile_candidate(selection.fresh)
    if recompiled.provider != "google_ads":
        raise _invalid_selection("The retained candidate is not a Google Ads candidate.")
    if canonical_bytes(recompiled.model_dump(mode="json", warnings="error")) != canonical_bytes(
        retained.model_dump(mode="json", warnings="error")
    ):
        raise AdhelmError(
            "GOOGLE_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH",
            "The supplied brief does not recompile to the retained Google Ads candidate.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Supply the exact brief this candidate was compiled from, or compile a new "
                "candidate; nothing was read from the provider."
            ),
        )
    if recompiled.candidate_digest != provenance["candidate_digest"]:
        raise AdhelmError(
            "GOOGLE_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH",
            "The retained candidate record does not restate its own candidate digest.",
            exit_code=ExitCode.PRECONDITION,
        )
    specification = compile_google_ads_search_campaign(selection.fresh)
    # Rule 8 at the earliest possible point: a candidate holding any non-paused
    # entity is refused before configuration, credential, or provider work.
    try:
        projection_from_compiled_spec(specification)
    except AdhelmError:
        raise
    except Exception:
        raise AdhelmError(
            "GOOGLE_ADS_CREATE_CANDIDATE_NOT_PAUSED",
            "Every entity in a Google Ads Search create candidate must be PAUSED.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile the candidate again; advertising entities are created paused and "
                "activation is a separate authorization. Nothing was read or retained."
            ),
        ) from None
    try:
        return GoogleAdsSearchCampaignCreateCandidateBinding(
            candidate_record_digest=selection.candidate_record_digest,
            candidate_digest=recompiled.candidate_digest,
            spec_digest=specification.spec_digest,
            input_digest=specification.input_digest,
            specification=specification,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _facts_invalid() from None


def _build_action_from_session(
    *,
    workspace_profile_ref: str,
    profile: Profile,
    configuration_digest: str,
    selection: GoogleAdsSearchCampaignCreateSelection,
    candidate: GoogleAdsSearchCampaignCreateCandidateBinding,
    campaign_name: str,
    fingerprint_key: bytes,
    session: Any,
) -> tuple[GoogleAdsSearchCampaignCreatePlannedAction, datetime, datetime]:
    try:
        execution = session.execution_binding
        reader = GoogleAdsSearchCampaignCreateReader(session)
        read_started_at = _checked_now()
        observed = reader.observe_account(campaign_name)
        read_finished_at = _checked_now()
        identity = observed.identity
        if identity.status != "ENABLED":
            raise AdhelmError(
                "GOOGLE_ADS_ACCOUNT_NOT_ENABLED",
                "A Google Ads Search hierarchy is created only in an ENABLED customer.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured Google Ads customer; nothing was created.",
            )
        if observed.campaign_name_matches != 0:
            raise AdhelmError(
                "GOOGLE_ADS_CREATE_NAME_COLLISION",
                "This Google Ads customer already holds a campaign with the planned name.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Compile a candidate with a different campaign name; nothing was created."
                ),
            )
        if observed.campaign_name_near_matches != 0:
            raise AdhelmError(
                "GOOGLE_ADS_CREATE_NAME_COLLISION_NEAR_MATCH",
                "This Google Ads customer holds a campaign whose name differs from the "
                "planned name only by case, surrounding whitespace, or Unicode "
                "normalization form.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Compile a candidate with a clearly distinct campaign name; nothing was "
                    "created. Google does not document whether it compares campaign names "
                    "case-sensitively, so a near match is refused rather than assumed distinct."
                ),
            )
        account = _account_binding(identity, profile)
        specification = candidate.specification
        if (
            specification.account_ref != account.account_ref
            or specification.account_currency != account.currency
            or specification.account_timezone != account.timezone
        ):
            raise AdhelmError(
                "GOOGLE_ADS_CREATE_ACCOUNT_MISMATCH",
                "The compiled candidate's account calendar differs from the observed customer.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Compile a candidate for this customer's currency and timezone; "
                    "nothing was created."
                ),
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
        observation = GoogleAdsSearchCampaignCreateObservationBinding(
            campaign_name=campaign_name,
            preconditions=GoogleAdsSearchCampaignCreatePreconditions(
                probed_near_match_names=observed.probed_near_match_names
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
                "The requested Google Ads CREATE lifetime exceeds the usable credential window.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action="Prepare a fresh deliberate plan with a usable credential window.",
            )
        action = build_google_ads_search_campaign_create_plan(
            workspace_profile_ref=workspace_profile_ref,
            account_binding=account,
            execution_binding=execution,
            candidate_binding=candidate,
            observation_binding=observation,
            expires_at=expires_at,
            fingerprint_key=fingerprint_key,
        )
        return action, current, read_started_at
    except AdhelmError:
        raise
    except (AttributeError, OverflowError, RecursionError, TypeError, ValueError):
        raise _facts_invalid() from None


def _account_binding(identity: Any, profile: Profile) -> Any:
    from adhelm.providers.google_ads_pause_contract import GoogleAdsCampaignPauseAccountBinding

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
    action: GoogleAdsSearchCampaignCreatePlannedAction,
    *,
    before_durable_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Retain the closed CREATE action, without asserting a remote condition."""

    record = google_ads_search_campaign_create_plan_record(action)
    if ledger.profile_ref != record.action.plan.profile_ref:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_CREATE_PLAN_PROVENANCE",
            "The Google Ads CREATE plan belongs to a different local workspace.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return ledger.append(
        "changes",
        GOOGLE_ADS_CREATE_PLAN_RECORD_KIND,
        record.model_dump(mode="json", warnings="error"),
        provider="google_ads",
        account_ref=record.action.plan.subplans[0].account.account_ref,
        source="LOCAL_CLI",
        _before_durable_append=before_durable_append,
    )


def _retain_provider_neutral_plan(
    ledger: Ledger,
    action: GoogleAdsSearchCampaignCreatePlannedAction,
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
        connector_version=GOOGLE_ADS_CREATE_CAPABILITY_VERSION,
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
        redacted_review=action.candidate_binding.specification.redacted_review,
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
) -> GoogleAdsSearchCampaignCreatePlannedAction:
    """Resolve the closed CREATE action for one provider-neutral plan digest.

    The join key is the composite plan digest both records already carry.  The
    scan reads a verified checkpoint, revalidates every candidate, and requires
    the retained action's own plan to compare byte for byte with the approved
    one, so a record that merely reuses a digest cannot substitute a different
    customer, candidate, or credential association.
    """

    expected = canonical_bytes(plan.model_dump(mode="json", warnings="error"))
    matching: list[GoogleAdsSearchCampaignCreatePlannedAction] = []
    for stored in ledger.checkpoint()["records_by_stream"]["changes"]:
        payload: Any = stored.get("payload")
        if (
            stored.get("record_kind") != GOOGLE_ADS_CREATE_PLAN_RECORD_KIND
            or type(payload) is not dict
            or payload.get("plan_digest") != plan.plan_digest
        ):
            continue
        action = revalidate_google_ads_search_campaign_create_plan_record(payload).action
        if canonical_bytes(action.plan.model_dump(mode="json", warnings="error")) == expected:
            matching.append(action)
    if not matching:
        raise AdhelmError(
            "GOOGLE_ADS_CREATE_PLAN_NOT_RETAINED",
            "The retained execution plan has no matching Google Ads CREATE action in history.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile the plan again with plan execution create --provider google_ads "
                "--operation CREATE; no credential was read and no provider request was made."
            ),
        )
    if len({canonical_bytes(item.to_data()) for item in matching}) != 1:
        raise AdhelmError(
            "GOOGLE_ADS_CREATE_PLAN_AMBIGUOUS",
            "Local history retains more than one different Google Ads CREATE action here.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify before requesting another execution.",
        )
    return matching[0]


#: The two singletons the registries hold.  A module-level instance keeps
#: registration idempotent across a reimport without letting a *different*
#: object silently replace an installed one.
GOOGLE_ADS_CREATE_EXECUTION_PLANNER: Final = GoogleAdsSearchCampaignCreateExecutionPlanner()
GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY: Final = (
    GoogleAdsSearchCampaignCreateExecutionAdapterFactory()
)


def register_google_ads_create_execution() -> None:
    """Register the Google Ads CREATE planner and adapter factory exactly once."""

    if (
        configured_execution_planner(GOOGLE_ADS_CREATE_PROVIDER, GOOGLE_ADS_CREATE_OPERATION)
        is not GOOGLE_ADS_CREATE_EXECUTION_PLANNER
    ):
        register_execution_planner(
            GOOGLE_ADS_CREATE_PROVIDER,
            GOOGLE_ADS_CREATE_OPERATION,
            GOOGLE_ADS_CREATE_EXECUTION_PLANNER,
        )
    if (
        configured_execution_adapter(GOOGLE_ADS_CREATE_PROVIDER, GOOGLE_ADS_CREATE_OPERATION)
        is not GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY
    ):
        register_execution_adapter(
            GOOGLE_ADS_CREATE_PROVIDER,
            GOOGLE_ADS_CREATE_OPERATION,
            GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY,
        )


def _assert_registration_seams() -> None:
    """Fail closed at import if either singleton stops matching its protocol."""

    factory: object = GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY
    if not isinstance(GOOGLE_ADS_CREATE_EXECUTION_PLANNER, ConfiguredExecutionPlanner) or (
        not isinstance(factory, ConfiguredExecutionAdapterFactory)
    ):  # pragma: no cover - a structural regression, not a runtime state
        raise AdhelmError(
            "INVALID_EXECUTION_ADAPTER_REGISTRATION",
            "The Google Ads CREATE registration singletons do not match their seams.",
            exit_code=ExitCode.INVALID_INPUT,
        )


_assert_registration_seams()


__all__ = [
    "GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY",
    "GOOGLE_ADS_CREATE_EXECUTION_PLANNER",
    "GOOGLE_ADS_CREATE_OPERATION",
    "GOOGLE_ADS_CREATE_PROVIDER",
    "GoogleAdsSearchCampaignCreateExecutionAdapterFactory",
    "GoogleAdsSearchCampaignCreateExecutionPlanner",
    "GoogleAdsSearchCampaignCreateGuardedAdapter",
    "register_google_ads_create_execution",
]
