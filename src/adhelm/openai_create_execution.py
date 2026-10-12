"""OpenAI Ads paused chat-card hierarchy CREATE on the two provider-neutral registries.

This module is the whole of P04-OA's registration surface.  It adds one
:class:`~adhelm.execution_planning_session.ConfiguredExecutionPlanner` and one
:class:`~adhelm.generic_execution_workflow.ConfiguredExecutionAdapterFactory` for
the exact pair ``("openai_ads", CREATE)``, so ``plan execution create --provider
openai_ads --operation CREATE`` and ``execute plan <digest>`` become the route
for this capability.  It adds no command, no approval kind, no credential, no
scope, and no live evidence: the installed approval bridge still refuses
``COMPOSITE_EXECUTION_PLAN``, so every ordinary build refuses at the bridge
before the factory is opened and before any credential is read.

**Two records, one join key.**  The planner retains the provider-specific
``OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_PLAN``, which carries the closed action
-- the recompiled candidate, the observed ad account, the account-key binding,
the private-vault creative, the bounded promotion checks, and the postcondition
-- and then the provider-neutral ``EXECUTION_PLAN_RETAINED`` record the generic
route requires.  Both describe the byte-identical
:class:`~adhelm.safety.CompositePlan`, so ``plan_digest`` is the join key: the
adapter factory resolves the retained action from verified local history by that
digest and refuses unless the two plans compare equal.  The provider record is
not duplication for its own sake: the provider-neutral record deliberately
models no native request, and the adapter cannot build four ordered request
bodies without the compiled candidate it holds.

**The account-key continuity is the pause family's, unchanged.**  The credential
binding, the configuration transaction, and the OS-store account-key EDIT
profile selection are exactly the ones
``docs/implementation/openai-pause-installed-execution.md`` describes; this
module reuses them rather than introducing a second credential path.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from adhelm.assets import read_image
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import configuration_transaction, load_configuration
from adhelm.creative import decode_image
from adhelm.credentials import credential, export_key, openai_pause_binding_key
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
from adhelm.ledger import Ledger
from adhelm.models import Configuration, OpenAIAccountKeyProfile, validate_configuration
from adhelm.openai_create_planning_session import OpenAIAdsCreateReader
from adhelm.openai_create_readback_records import openai_create_readback_record_kind_installed
from adhelm.providers.openai_compiler import compile_openai_ads_campaign
from adhelm.providers.openai_create_adapter import (
    OpenAIAdsChatCardHierarchyCreateAdapter,
    _configured_openai_ads_create_adapter,
    _openai_create_http_client,
    openai_create_upload_transport_installed,
)
from adhelm.providers.openai_create_contract import (
    OPENAI_CREATE_CAPABILITY_VERSION,
    OPENAI_CREATE_HOST,
    OPENAI_CREATE_MAX_UPLOAD_BYTES,
    OPENAI_CREATE_MEDIA_TYPES,
    OPENAI_CREATE_PLAN_RECORD_KIND,
    OpenAIAdsChatCardHierarchyCreatePlannedAction,
    OpenAIAdsChatCardHierarchyCreateSelection,
    OpenAIAdsCreateAccountBinding,
    OpenAIAdsCreateCandidateBinding,
    OpenAIAdsCreateObservationBinding,
    OpenAIAdsCreatePreconditions,
    OpenAIAdsCreativeVaultBinding,
    build_openai_ads_chat_card_hierarchy_create_plan,
    create_child_plan,
    create_redacted_review,
    expected_child_egress_count,
    fingerprint_key_marker,
    openai_ads_create_plan_record,
    openai_ads_create_plan_record_kind_installed,
    revalidate_openai_ads_create_plan_record,
)
from adhelm.providers.openai_pause import OpenAIPauseCredentials, _credential_binding
from adhelm.safety import CompositePlan, GuardedAdapter, OperationKind
from adhelm.saved_candidates import compile_candidate, load_candidate
from adhelm.transport import SafeHttpClient

#: The registered pair.  ``RECOVER`` is never registrable: a recovery reuses the
#: adapter of the operation that started the attempt.
OPENAI_CREATE_PROVIDER: Final = "openai_ads"
OPENAI_CREATE_OPERATION: Final = OperationKind.CREATE

_CANDIDATE_RESOURCE: Final = re.compile(
    r"^openai_ads\.candidate\.(?P<record>[a-f0-9]{64})$", re.ASCII
)
_ACCOUNT_REF: Final = re.compile(
    r"^openai_ads\.account\.(?P<account>[A-Za-z0-9_-]{1,120})$", re.ASCII
)
_VAULT_REF: Final = re.compile(r"^vault:sha256:(?P<digest>[a-f0-9]{64})$", re.ASCII)


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
            "OpenAI Ads CREATE execution planning requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _invalid_selection(detail: str) -> AdhelmError:
    return AdhelmError(
        "INVALID_OPENAI_ADS_CREATE_EXECUTION_SELECTION", detail, exit_code=ExitCode.INVALID_INPUT
    )


def _facts_invalid() -> AdhelmError:
    return AdhelmError(
        "OPENAI_ADS_CREATE_FACTS_INVALID",
        "The configured OpenAI Ads CREATE facts are incomplete or inconsistent.",
        exit_code=ExitCode.PRECONDITION,
    )


def _retention_unverified() -> AdhelmError:
    return AdhelmError(
        "OPENAI_ADS_CREATE_PLAN_RETENTION_UNVERIFIED",
        "The OpenAI Ads read completed, but local plan retention is unverified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect retained OpenAI Ads CREATE plan history before planning again.",
    )


class OpenAIAdsChatCardHierarchyCreateExecutionPlanner:
    """Compile and retain one provider-neutral OpenAI Ads chat-card CREATE plan.

    The planner owns exactly what the generic command cannot: the recompilation
    that binds a retained offline candidate, the private-vault creative read, the
    configured OS-store account-key EDIT profile selection and its credential
    binding, the bounded promotion reads the write contract requires, and the two
    retentions.  It performs no mutation and no upload.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<OpenAIAdsChatCardHierarchyCreateExecutionPlanner>"

    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> RetainedExecutionPlan:
        from adhelm.plan_cli import ExecutionPlanRequest

        if not isinstance(directory, Path) or type(request) is not ExecutionPlanRequest:
            raise _invalid_selection("A complete generic execution plan request is required.")
        if (
            request.provider != OPENAI_CREATE_PROVIDER
            or request.operation != OPENAI_CREATE_OPERATION.value
        ):
            raise _invalid_selection("This planner compiles only openai_ads CREATE plans.")
        selection = request.openai_selection
        if type(selection) is not OpenAIAdsChatCardHierarchyCreateSelection:
            raise _invalid_selection(
                "An openai_ads CREATE request carries the retained candidate, its brief, "
                "and the private-vault creative."
            )
        account_client_ref = _selected_account(request.account_ref)
        record_digest = _selected_candidate_record(request.resource_ref)
        if record_digest != selection.candidate_record_digest:
            raise _invalid_selection(
                "resource_ref must name the same retained candidate the selection carries."
            )
        if selection.fresh.account_ref != request.account_ref:
            raise _invalid_selection(
                "The compiled candidate is outside the requested OpenAI ad account."
            )
        # The one feature-gated option, refused before any configuration,
        # credential, vault or provider work.  The compiler refuses it too; this
        # names the provider gate rather than a compiler limitation.
        if selection.fresh.bidding_type == "conversions":
            raise AdhelmError(
                "OPENAI_ADS_CREATE_OPTION_NOT_ENABLED",
                "NOT_ENABLED: conversion bidding needs an enabled account and exactly one "
                "active standard conversion event, and no reviewed read establishes either.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Compile a candidate with impressions or clicks bidding; nothing was read, "
                    "compiled, or retained."
                ),
            )
        # Three fail-closed build checks, before any configuration, credential,
        # or provider work: this build must be able to retain both records and to
        # send the one documented binary upload.
        _require_installed_build()
        configuration = load_configuration(directory)
        captured = digest(configuration)
        if captured != expected_configuration_digest:
            raise AdhelmError(
                "CONFIGURATION_CHANGED",
                "Local configuration changed before OpenAI Ads CREATE execution planning.",
                exit_code=ExitCode.PRECONDITION,
            )
        profile = _edit_profile(configuration, request.profile_id, account_client_ref)
        ledger = Ledger(directory)
        if ledger.unresolved_starts() or ledger.unresolved_runs():
            raise AdhelmError(
                "RECOVERY_REQUIRED",
                "Unresolved mutation history must be reconciled before CREATE planning.",
                next_action="Inspect verified history and reconcile the retained evidence.",
            )
        candidate = _bound_candidate(ledger, selection)
        expected_child_egress_count(candidate.specification)
        creative = _vault_creative(directory, selection, candidate)
        key = export_key()
        credentials = _current_credentials(
            directory, profile, expected_configuration_digest=captured
        )
        action, planning_checked_at, read_started_at = _build_action(
            workspace_profile_ref=ledger.profile_ref,
            profile=profile,
            credentials=credentials,
            selection=selection,
            candidate=candidate,
            creative=creative,
            fingerprint_key=key,
        )
        try:
            _retain_provider_plan(
                ledger, action, before_durable_append=_recheck(action, planning_checked_at)
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


class OpenAIAdsChatCardHierarchyCreateExecutionAdapterFactory:
    """Open the one configured child-egress adapter for a retained OpenAI CREATE plan.

    ``declared_capabilities`` is answered from this module's own compiled
    contract: the route is one upload plus one request per created entity, so it
    declares journaled child egress.  It reads no configuration, opens no
    credential store, and contacts no provider, because the generic route asks
    for it before the approval bridge builds or consumes a receipt.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<OpenAIAdsChatCardHierarchyCreateExecutionAdapterFactory>"

    def declared_capabilities(self) -> ExecutionAdapterCapabilities:
        return ExecutionAdapterCapabilities(child_egress=True)

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
            checked.provider != OPENAI_CREATE_PROVIDER
            or checked.operation is not OPENAI_CREATE_OPERATION
            or checked.expected_child_egress_count < 4
        ):
            raise _invalid_selection(
                "This adapter executes only a journaled multi-request openai_ads CREATE plan."
            )
        now = clock()
        if type(now) is not datetime or now.tzinfo is None or checked.expires_at <= now:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The retained OpenAI Ads CREATE plan expired before its adapter opened.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Compile a new plan from a fresh bounded read; no credential was read and "
                    "no provider request was made."
                ),
            )
        action = _retained_action_for_plan(ledger, checked.plan)
        configuration = load_configuration(directory)
        captured = digest(configuration)
        profile = _edit_profile(
            configuration,
            action.account_binding.profile_ref,
            action.account_binding.account_client_ref,
        )
        # Prove the current credential binding before opening the adapter, so a
        # rotated account key refuses here rather than inside a child scope.
        _current_credentials(
            directory, profile, expected_configuration_digest=captured, action=action
        )
        return _configured_openai_ads_create_adapter(
            directory,
            action,
            ledger,
            lambda: _current_credentials(
                directory, profile, expected_configuration_digest=captured, action=action
            ),
            clock=clock,
        )


def _require_installed_build() -> None:
    for installed, detail in (
        (
            openai_ads_create_plan_record_kind_installed(),
            f"a ledger that registers {OPENAI_CREATE_PLAN_RECORD_KIND}",
        ),
        (
            openai_create_readback_record_kind_installed(),
            "a ledger that registers OPENAI_ADS_CHAT_CARD_HIERARCHY_CREATE_READBACK_OBSERVATION",
        ),
    ):
        if not installed:
            raise AdhelmError(
                "OPENAI_ADS_CREATE_RECORD_KIND_NOT_INSTALLED",
                "This build's ledger does not register the OpenAI Ads CREATE record kinds.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    f"Install a build with {detail}; no credential was read and no provider "
                    "request was made."
                ),
            )
    if not openai_create_upload_transport_installed():
        raise AdhelmError(
            "OPENAI_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED",
            "This build's transport installs no bounded multipart image upload.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a build whose SafeHttpClient exposes the reviewed bounded upload_image "
                "seam; no credential was read and no provider request was made."
            ),
        )


def _selected_account(account_ref: object) -> str:
    if type(account_ref) is not str:
        raise _invalid_selection("An openai_ads CREATE request names one ad account.")
    matched = _ACCOUNT_REF.fullmatch(account_ref)
    if matched is None:
        raise _invalid_selection("account_ref must be openai_ads.account.<ad-account-id>.")
    return matched.group("account")


def _selected_candidate_record(resource_ref: object) -> str:
    """Return the retained candidate record digest the generic request names."""

    if type(resource_ref) is not str:
        raise _invalid_selection(
            "An openai_ads CREATE request names one retained candidate reference."
        )
    matched = _CANDIDATE_RESOURCE.fullmatch(resource_ref)
    if matched is None:
        raise _invalid_selection(
            "resource_ref must be openai_ads.candidate.<retained-record-digest>."
        )
    return matched.group("record")


def _edit_profile(
    configuration: Configuration, profile_id: str, account_client_ref: str
) -> OpenAIAccountKeyProfile:
    profiles = [item for item in configuration.profiles if item.profile_id == profile_id]
    if (
        len(profiles) != 1
        or type(profiles[0]) is not OpenAIAccountKeyProfile
        or profiles[0].purpose != "EDIT"
        or profiles[0].credential_source != "OS_STORE"
        or profiles[0].account_ref != account_client_ref
        or profiles[0].currency != "USD"
    ):
        raise AdhelmError(
            "OPENAI_ACCOUNT_KEY_EDIT_PROFILE_REQUIRED",
            "OpenAI Ads CREATE planning requires the configured OS-store account-key EDIT "
            "profile for this exact ad account.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Configure the OpenAI Ads EDIT profile for this account; nothing was read, "
                "compiled, or retained."
            ),
        )
    return profiles[0]


def _current_credentials(
    directory: Path,
    profile: OpenAIAccountKeyProfile,
    *,
    expected_configuration_digest: str,
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction | None = None,
) -> OpenAIPauseCredentials:
    """Reobserve the configuration and the account key together, on every use.

    This is the pause family's transaction, unchanged: the configuration lock
    opens first, the private credential slots are read inside it, and the
    transaction closes before any provider request.
    """

    with configuration_transaction(directory) as transaction:
        configuration = validate_configuration(
            transaction.require_unchanged(expected_configuration_digest)
        )
        current = _edit_profile(configuration, profile.profile_id, profile.account_ref)
        if digest(current) != digest(profile):
            raise AdhelmError(
                "CONFIGURATION_DRIFT",
                "The current OpenAI Ads profile differs from the one this plan was built under.",
                exit_code=ExitCode.PRECONDITION,
            )
        value = OpenAIPauseCredentials(
            profile_ref=current.profile_id,
            secret=credential(current),
            binding_key=openai_pause_binding_key(current),
        )
        if action is not None and not hmac.compare_digest(
            _credential_binding(value), action.account_binding.credential_binding_ref
        ):
            raise AdhelmError(
                "CREDENTIAL_BINDING_MISMATCH",
                "Current OpenAI Ads credentials differ from the retained CREATE plan.",
                exit_code=ExitCode.PRECONDITION,
            )
        transaction.require_unchanged(expected_configuration_digest)
        return value


def _bound_candidate(
    ledger: Ledger, selection: OpenAIAdsChatCardHierarchyCreateSelection
) -> OpenAIAdsCreateCandidateBinding:
    """Recompile the supplied brief and bind it to the retained candidate.

    Neither the compiled specification nor the brief is asserted: the brief is
    recompiled here and the retained record's own review is required to be the
    byte-identical result.  A retained candidate that names a different spec,
    input, or provider is refused before any provider read.
    """

    retained, provenance = load_candidate(ledger, selection.candidate_record_digest)
    recompiled = compile_candidate(selection.fresh)
    if recompiled.provider != OPENAI_CREATE_PROVIDER:
        raise _invalid_selection("The retained candidate is not an OpenAI Ads candidate.")
    if canonical_bytes(recompiled.model_dump(mode="json", warnings="error")) != canonical_bytes(
        retained.model_dump(mode="json", warnings="error")
    ):
        raise AdhelmError(
            "OPENAI_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH",
            "The supplied brief does not recompile to the retained OpenAI Ads candidate.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Supply the exact brief this candidate was compiled from, or compile a new "
                "candidate; nothing was read from the provider."
            ),
        )
    if recompiled.candidate_digest != provenance["candidate_digest"]:
        raise AdhelmError(
            "OPENAI_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH",
            "The retained candidate record does not restate its own candidate digest.",
            exit_code=ExitCode.PRECONDITION,
        )
    specification = compile_openai_ads_campaign(selection.fresh)
    try:
        return OpenAIAdsCreateCandidateBinding(
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


def _vault_creative(
    directory: Path,
    selection: OpenAIAdsChatCardHierarchyCreateSelection,
    candidate: OpenAIAdsCreateCandidateBinding,
) -> OpenAIAdsCreativeVaultBinding:
    """Resolve the one private-vault image every compiled ad declares.

    The vault is content-addressed, so the request names a digest and the object
    path is derived.  The bytes are read through the vault boundary -- which
    rehashes them -- and decoded again here, so an unsupported or mislabelled
    image fails before any provider work.
    """

    matched = _VAULT_REF.fullmatch(selection.creative_vault_ref)
    if matched is None:
        raise _invalid_selection("creative_vault_ref must be vault:sha256:<content-digest>.")
    content_digest = matched.group("digest")
    declared = {
        ad.asset_content_digest for group in candidate.specification.ad_groups for ad in group.ads
    }
    if declared != {content_digest}:
        raise AdhelmError(
            "OPENAI_ADS_CREATE_CREATIVE_NOT_DECLARED",
            "The requested vault image is not the one every compiled ad declares.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Name the creative asset the brief declares, and compile one hierarchy per "
                "image; nothing was read from the provider."
            ),
        )
    data = read_image(directory, content_digest)
    if not 1 <= len(data) <= OPENAI_CREATE_MAX_UPLOAD_BYTES:
        raise AdhelmError(
            "OPENAI_ADS_CREATE_CREATIVE_TOO_LARGE",
            "The declared creative exceeds this route's local upload bound.",
            exit_code=ExitCode.PRECONDITION,
        )
    decoded = decode_image(data)
    expected_asset_id = "asset:sha256:" + digest(
        {
            "content_digest": content_digest,
            "mime_type": decoded.mime_type,
            "width_px": decoded.width_px,
            "height_px": decoded.height_px,
        }
    )
    declared_assets = {
        ad.asset_version_id for group in candidate.specification.ad_groups for ad in group.ads
    }
    if declared_assets != {expected_asset_id}:
        raise AdhelmError(
            "OPENAI_ADS_CREATE_CREATIVE_DRIFT",
            "The private vault object is not the image the compiled candidate declares.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Register the creative again and compile a new candidate from it; nothing was "
                "read from the provider."
            ),
        )
    if decoded.mime_type not in OPENAI_CREATE_MEDIA_TYPES:
        raise AdhelmError(
            "OPENAI_ADS_CREATE_CREATIVE_MEDIA_TYPE_UNSUPPORTED",
            "This route uploads only the reviewed PNG and JPEG chat-card image types.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Register a PNG or JPEG creative; the Ads Files reference documents no media "
                "allowlist, so this bound is deliberately local and narrow."
            ),
        )
    media_type: Literal["image/png", "image/jpeg"] = (
        "image/png" if decoded.mime_type == "image/png" else "image/jpeg"
    )
    try:
        return OpenAIAdsCreativeVaultBinding(
            vault_ref=selection.creative_vault_ref,
            content_digest=content_digest,
            media_type=media_type,
            byte_length=len(data),
            width_px=decoded.width_px,
            height_px=decoded.height_px,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _facts_invalid() from None


def _build_action(
    *,
    workspace_profile_ref: str,
    profile: OpenAIAccountKeyProfile,
    credentials: OpenAIPauseCredentials,
    selection: OpenAIAdsChatCardHierarchyCreateSelection,
    candidate: OpenAIAdsCreateCandidateBinding,
    creative: OpenAIAdsCreativeVaultBinding,
    fingerprint_key: bytes,
) -> tuple[OpenAIAdsChatCardHierarchyCreatePlannedAction, datetime, datetime]:
    """Take the one bounded read and build the closed non-authorizing action."""

    client = _openai_create_http_client()
    try:
        reader = OpenAIAdsCreateReader(
            SafeHttpClient(client, {OPENAI_CREATE_HOST}), lambda: credentials.secret
        )
        read_started_at = _checked_now()
        identity = reader.discover_identity(expected_account_client_ref=profile.account_ref)
        if identity.status.casefold() != "active":
            raise AdhelmError(
                "OPENAI_ADS_ACCOUNT_NOT_ACTIVE",
                "An OpenAI Ads hierarchy is created only in an active ad account.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review the configured OpenAI ad account; nothing was created.",
            )
        specification = candidate.specification
        if (
            identity.currency != specification.account_currency
            or identity.timezone != specification.account_timezone
            or identity.currency != profile.currency
            or identity.timezone != profile.timezone
        ):
            raise AdhelmError(
                "OPENAI_ADS_CREATE_ACCOUNT_MISMATCH",
                "The compiled candidate's account calendar differs from the observed account.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Compile a candidate for this account's currency and timezone; nothing "
                    "was created."
                ),
            )
        if identity.account_ref != specification.account_ref:
            raise AdhelmError(
                "OPENAI_ADS_CREATE_ACCOUNT_MISMATCH",
                "The compiled candidate names a different OpenAI ad account.",
                exit_code=ExitCode.PRECONDITION,
            )
        name = specification.campaign_request.name
        if reader.campaigns_named(identity, name):
            raise AdhelmError(
                "OPENAI_ADS_CREATE_NAME_COLLISION",
                "This OpenAI ad account already holds a campaign with the planned name.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Compile a candidate with a different campaign name; nothing was created."
                ),
            )
        read_finished_at = _checked_now()
    finally:
        client.close()
    try:
        account = OpenAIAdsCreateAccountBinding(
            account_ref=identity.account_ref,
            account_client_ref=identity.account_client_ref,
            profile_ref=credentials.profile_ref,
            credential_binding_ref=_credential_binding(credentials),
            account_currency=identity.currency,
            account_timezone=identity.timezone,
        )
        observation = OpenAIAdsCreateObservationBinding(
            campaign_name=name,
            preconditions=OpenAIAdsCreatePreconditions(),
            creative=creative,
            fingerprint_key_marker=fingerprint_key_marker(fingerprint_key),
            read_started_at=read_started_at,
            read_finished_at=read_finished_at,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _facts_invalid() from None
    expires_at = read_finished_at + timedelta(seconds=selection.expires_in_seconds)
    current = _checked_now()
    if current < read_finished_at:
        raise AdhelmError(
            "CLOCK_INVALID",
            "The OpenAI Ads planning clock moved backwards after the bounded read.",
            exit_code=ExitCode.PRECONDITION,
        )
    if current >= expires_at:
        raise AdhelmError(
            "PLAN_EXPIRED",
            "The requested OpenAI Ads CREATE lifetime elapsed during planning.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Prepare a fresh deliberate plan.",
        )
    action = build_openai_ads_chat_card_hierarchy_create_plan(
        workspace_profile_ref=workspace_profile_ref,
        account_binding=account,
        candidate_binding=candidate,
        observation_binding=observation,
        expires_at=expires_at,
        fingerprint_key=fingerprint_key,
    )
    return action, current, read_started_at


def _recheck(
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction, planning_checked_at: datetime
) -> Callable[[], None]:
    """Recheck held local facts and the clock immediately before the append."""

    def before_durable_append() -> None:
        current = _checked_now()
        if current < planning_checked_at:
            raise AdhelmError(
                "CLOCK_INVALID",
                "The OpenAI Ads planning clock moved backwards.",
                exit_code=ExitCode.PRECONDITION,
            )
        if current >= action.plan.expires_at:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The OpenAI Ads CREATE plan expired before retention.",
                exit_code=ExitCode.AUTHORIZATION,
            )

    return before_durable_append


def _retain_provider_plan(
    ledger: Ledger,
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
    *,
    before_durable_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Retain the closed CREATE action, without asserting a remote condition."""

    record = openai_ads_create_plan_record(action)
    if ledger.profile_ref != record.action.plan.profile_ref:
        raise AdhelmError(
            "INVALID_OPENAI_ADS_CREATE_PLAN_PROVENANCE",
            "The OpenAI Ads CREATE plan belongs to a different local workspace.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return ledger.append(
        "changes",
        OPENAI_CREATE_PLAN_RECORD_KIND,
        record.model_dump(mode="json", warnings="error"),
        provider=OPENAI_CREATE_PROVIDER,
        account_ref=record.action.plan.subplans[0].account.account_ref,
        source="LOCAL_CLI",
        _before_durable_append=before_durable_append,
    )


def _retain_provider_neutral_plan(
    ledger: Ledger,
    action: OpenAIAdsChatCardHierarchyCreatePlannedAction,
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
        connector_version=OPENAI_CREATE_CAPABILITY_VERSION,
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
        expected_child_egress_count=subplan.expected_child_egress_count,
        redacted_review=create_redacted_review(
            action.candidate_binding.specification,
            create_child_plan(action.candidate_binding.specification),
        ),
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
) -> OpenAIAdsChatCardHierarchyCreatePlannedAction:
    """Resolve the closed CREATE action for one provider-neutral plan digest.

    The join key is the composite plan digest both records already carry.  The
    scan reads a verified checkpoint, revalidates every candidate, and requires
    the retained action's own plan to compare byte for byte with the approved
    one, so a record that merely reuses a digest cannot substitute a different
    account, candidate, creative, or credential association.
    """

    expected = canonical_bytes(plan.model_dump(mode="json", warnings="error"))
    matching: list[OpenAIAdsChatCardHierarchyCreatePlannedAction] = []
    for stored in ledger.checkpoint()["records_by_stream"]["changes"]:
        payload: Any = stored.get("payload")
        if (
            stored.get("record_kind") != OPENAI_CREATE_PLAN_RECORD_KIND
            or type(payload) is not dict
            or payload.get("plan_digest") != plan.plan_digest
        ):
            continue
        action = revalidate_openai_ads_create_plan_record(payload).action
        if canonical_bytes(action.plan.model_dump(mode="json", warnings="error")) == expected:
            matching.append(action)
    if not matching:
        raise AdhelmError(
            "OPENAI_ADS_CREATE_PLAN_NOT_RETAINED",
            "The retained execution plan has no matching OpenAI Ads CREATE action in history.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile the plan again with plan execution create --provider openai_ads "
                "--operation CREATE; no credential was read and no provider request was made."
            ),
        )
    if len({canonical_bytes(item.to_data()) for item in matching}) != 1:
        raise AdhelmError(
            "OPENAI_ADS_CREATE_PLAN_AMBIGUOUS",
            "Local history retains more than one different OpenAI Ads CREATE action here.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify before requesting another execution.",
        )
    return matching[0]


#: The two singletons the registries hold.  A module-level instance keeps
#: registration idempotent across a reimport without letting a *different*
#: object silently replace an installed one.
OPENAI_CREATE_EXECUTION_PLANNER: Final = OpenAIAdsChatCardHierarchyCreateExecutionPlanner()
OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY: Final = (
    OpenAIAdsChatCardHierarchyCreateExecutionAdapterFactory()
)


def register_openai_create_execution() -> None:
    """Register the OpenAI Ads CREATE planner and adapter factory exactly once."""

    if (
        configured_execution_planner(OPENAI_CREATE_PROVIDER, OPENAI_CREATE_OPERATION)
        is not OPENAI_CREATE_EXECUTION_PLANNER
    ):
        register_execution_planner(
            OPENAI_CREATE_PROVIDER, OPENAI_CREATE_OPERATION, OPENAI_CREATE_EXECUTION_PLANNER
        )
    if (
        configured_execution_adapter(OPENAI_CREATE_PROVIDER, OPENAI_CREATE_OPERATION)
        is not OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY
    ):
        register_execution_adapter(
            OPENAI_CREATE_PROVIDER,
            OPENAI_CREATE_OPERATION,
            OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY,
        )


def _assert_registration_seams() -> None:
    """Fail closed at import if either singleton stops matching its protocol."""

    factory: object = OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY
    if not isinstance(OPENAI_CREATE_EXECUTION_PLANNER, ConfiguredExecutionPlanner) or (
        not isinstance(factory, ConfiguredExecutionAdapterFactory)
    ):  # pragma: no cover - a structural regression, not a runtime state
        raise AdhelmError(
            "INVALID_EXECUTION_ADAPTER_REGISTRATION",
            "The OpenAI Ads CREATE registration singletons do not match their seams.",
            exit_code=ExitCode.INVALID_INPUT,
        )


_assert_registration_seams()


__all__ = [
    "OPENAI_CREATE_EXECUTION_ADAPTER_FACTORY",
    "OPENAI_CREATE_EXECUTION_PLANNER",
    "OPENAI_CREATE_OPERATION",
    "OPENAI_CREATE_PROVIDER",
    "OpenAIAdsChatCardHierarchyCreateAdapter",
    "OpenAIAdsChatCardHierarchyCreateExecutionAdapterFactory",
    "OpenAIAdsChatCardHierarchyCreateExecutionPlanner",
    "register_openai_create_execution",
]
