"""Meta Ads paused single-image hierarchy CREATE on the provider-neutral registries.

This module is the whole of P04-MA's registration surface.  It adds one
:class:`~adhelm.execution_planning_session.ConfiguredExecutionPlanner` and one
:class:`~adhelm.generic_execution_workflow.ConfiguredExecutionAdapterFactory` for
the exact pair ``("meta_ads", CREATE)``, so ``plan execution create --provider
meta_ads --operation CREATE`` and ``execute plan <digest>`` become the route for
this capability.  It adds no command, no approval kind, no credential, no scope,
and no live evidence: the installed approval bridge still refuses
``COMPOSITE_EXECUTION_PLAN``, so every ordinary build refuses at the bridge
before the factory is opened and before any credential is read.

**Two records, one join key.**  The planner retains the provider-specific
``META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_PLAN``, which carries the closed action
-- the recompiled candidate, the observed ad account, the inspected EDIT token
binding, the private-vault creative, the bounded promotion checks, and the
postcondition -- and then the provider-neutral ``EXECUTION_PLAN_RETAINED`` record
the generic route requires.  Both describe the byte-identical
:class:`~adhelm.safety.CompositePlan`, so ``plan_digest`` is the join key: the
adapter factory resolves the retained action from verified local history by that
digest and refuses unless the two plans compare equal.  The provider record is
not duplication for its own sake: the provider-neutral record deliberately models
no native request, and the adapter cannot build five ordered request bodies
without the compiled candidate it holds.

**The credential boundary is the Meta EDIT session, unchanged.**  The inspected
subject and inspector tokens, the configuration-then-credential transaction, and
the continuity marker are exactly the ones
``docs/implementation/meta-ads-token-edit-session.md`` describes; this module
reuses them rather than introducing a second credential path.  Both durable
appends happen inside one ``retention_recheck`` so the same continuity is proved
immediately before each of them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from adhelm.assets import read_image
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
from adhelm.creative import decode_image
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
from adhelm.ledger import Ledger
from adhelm.meta_ads_token_edit_session import (
    MetaAdsTokenEditSession,
    _checked_binding,
    configured_meta_ads_token_edit_session,
)
from adhelm.meta_create_readback_records import meta_create_readback_record_kind_installed
from adhelm.models import Configuration, MetaAccountTokenProfile, validate_configuration
from adhelm.privacy import validate_data
from adhelm.providers.meta_compiler import (
    MetaAdsCampaignBrief,
    MetaAdsCompiledCampaignSpec,
    compile_meta_ads_campaign,
)
from adhelm.providers.meta_create_adapter import (
    MetaAdsSingleImageHierarchyCreateAdapter,
    _configured_meta_ads_create_adapter,
    ad_request_form,
    adset_request_form,
    campaign_request_form,
    creative_request_form,
    meta_create_upload_transport_installed,
)
from adhelm.providers.meta_create_contract import (
    META_CREATE_CAPABILITY_VERSION,
    META_CREATE_CHILD_EGRESS_COUNT,
    META_CREATE_MAX_UPLOAD_BYTES,
    META_CREATE_MEDIA_TYPES,
    META_CREATE_PLAN_RECORD_KIND,
    META_CREATE_PLANNING_IMAGE_HASH,
    META_CREATE_SCOPE,
    MetaAdsCreateAccountBinding,
    MetaAdsCreateCandidateBinding,
    MetaAdsCreateExecutionBinding,
    MetaAdsCreateObservationBinding,
    MetaAdsCreatePreconditions,
    MetaAdsCreativeVaultBinding,
    MetaAdsSingleImageHierarchyCreatePlannedAction,
    MetaAdsSingleImageHierarchyCreateSelection,
    build_meta_ads_single_image_hierarchy_create_plan,
    create_child_plan,
    create_redacted_review,
    expected_child_egress_count,
    fingerprint_key_marker,
    form_value_expressible,
    meta_ads_create_plan_record,
    meta_ads_create_plan_record_kind_installed,
    revalidate_meta_ads_create_plan_record,
    unexpressible_form_values,
)
from adhelm.safety import CompositePlan, GuardedAdapter, OperationKind
from adhelm.saved_candidates import compile_candidate, load_candidate

#: The registered pair.  ``RECOVER`` is never registrable: a recovery reuses the
#: adapter of the operation that started the attempt.
META_CREATE_PROVIDER: Final = "meta_ads"
META_CREATE_OPERATION: Final = OperationKind.CREATE

_CANDIDATE_RESOURCE: Final = re.compile(
    r"^meta_ads\.candidate\.(?P<record>[a-f0-9]{64})$", re.ASCII
)
_ACCOUNT_REF: Final = re.compile(
    r"^meta_ads\.account\.act_(?P<account>[1-9][0-9]{0,39})$", re.ASCII
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
            "Meta Ads CREATE execution planning requires an aware local clock.",
            exit_code=ExitCode.PRECONDITION,
        ) from None


def _invalid_selection(detail: str) -> AdhelmError:
    return AdhelmError(
        "INVALID_META_ADS_CREATE_EXECUTION_SELECTION", detail, exit_code=ExitCode.INVALID_INPUT
    )


def _facts_invalid() -> AdhelmError:
    return AdhelmError(
        "META_ADS_CREATE_FACTS_INVALID",
        "The configured Meta Ads CREATE facts are incomplete or inconsistent.",
        exit_code=ExitCode.PRECONDITION,
    )


def _retention_unverified() -> AdhelmError:
    return AdhelmError(
        "META_ADS_CREATE_PLAN_RETENTION_UNVERIFIED",
        "The Meta Ads read completed, but local plan retention is unverified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action="Inspect retained Meta Ads CREATE plan history before planning again.",
    )


class MetaAdsSingleImageHierarchyCreateExecutionPlanner:
    """Compile and retain one provider-neutral Meta Ads single-image CREATE plan.

    The planner owns exactly what the generic command cannot: the recompilation
    that binds a retained offline candidate, the private-vault creative read, the
    configured Meta EDIT token session and its inspected binding, the bounded
    promotion reads the write contract requires, and the two retentions.  It
    performs no mutation and no upload.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<MetaAdsSingleImageHierarchyCreateExecutionPlanner>"

    def plan_configured_execution(
        self, directory: Path, request: object, *, expected_configuration_digest: str
    ) -> RetainedExecutionPlan:
        from adhelm.plan_cli import ExecutionPlanRequest

        if not isinstance(directory, Path) or type(request) is not ExecutionPlanRequest:
            raise _invalid_selection("A complete generic execution plan request is required.")
        if (
            request.provider != META_CREATE_PROVIDER
            or request.operation != META_CREATE_OPERATION.value
        ):
            raise _invalid_selection("This planner compiles only meta_ads CREATE plans.")
        selection = request.meta_selection
        if type(selection) is not MetaAdsSingleImageHierarchyCreateSelection:
            raise _invalid_selection(
                "A meta_ads CREATE request carries the retained candidate, its brief, "
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
                "The compiled candidate is outside the requested Meta ad account."
            )
        # The feature gate, refused before any configuration, credential, vault
        # or provider work.  Nothing readable proves account entitlement for
        # another objective, optimization goal, placement, special ad category
        # or country, so an unreviewed one is refused outright.
        _require_enabled_brief(selection.fresh)
        # Three fail-closed build checks, before any configuration, credential,
        # or provider work: this build must be able to retain both records and
        # to send the one documented multipart upload.
        _require_installed_build()
        configuration = load_configuration(directory)
        captured = digest(configuration)
        if captured != expected_configuration_digest:
            raise AdhelmError(
                "CONFIGURATION_CHANGED",
                "Local configuration changed before Meta Ads CREATE execution planning.",
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
        _require_expressible_request(candidate)
        creative = _vault_creative(directory, selection, candidate)
        key = export_key()
        return _plan_within_edit_session(
            directory=directory,
            ledger=ledger,
            profile=profile,
            configuration_digest=captured,
            selection=selection,
            candidate=candidate,
            creative=creative,
            fingerprint_key=key,
            supersedes_record_digest=request.supersedes_record_digest,
        )


class MetaAdsSingleImageHierarchyCreateExecutionAdapterFactory:
    """Open the one configured child-egress adapter for a retained Meta CREATE plan.

    ``declared_capabilities`` is answered from this module's own compiled
    contract: the route is one upload plus one request per created entity, so it
    declares journaled child egress.  It reads no configuration, opens no
    credential store, and contacts no provider, because the generic route asks
    for it before the approval bridge builds or consumes a receipt.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "<MetaAdsSingleImageHierarchyCreateExecutionAdapterFactory>"

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
            checked.provider != META_CREATE_PROVIDER
            or checked.operation is not META_CREATE_OPERATION
            or checked.expected_child_egress_count != META_CREATE_CHILD_EGRESS_COUNT
        ):
            raise _invalid_selection(
                "This adapter executes only a journaled five-request meta_ads CREATE plan."
            )
        now = clock()
        if type(now) is not datetime or now.tzinfo is None or checked.expires_at <= now:
            raise AdhelmError(
                "PLAN_EXPIRED",
                "The retained Meta Ads CREATE plan expired before its adapter opened.",
                exit_code=ExitCode.AUTHORIZATION,
                next_action=(
                    "Compile a new plan from a fresh bounded read; no credential was read and "
                    "no provider request was made."
                ),
            )
        action = _retained_action_for_plan(ledger, checked.plan)
        configuration = validate_configuration(load_configuration(directory))
        execution = action.execution_binding
        profile = _edit_profile(
            configuration,
            execution.profile_ref,
            action.account_binding.account_client_ref,
        )
        # Prove the local EDIT configuration binding before the session opens,
        # so a changed profile or configuration refuses here rather than inside
        # a credential transaction.
        if (
            digest(configuration) != execution.configuration_digest
            or digest(profile) != execution.profile_configuration_digest
            or profile.client_application_ref != execution.client_application_ref
            or profile.required_scope != execution.required_scope
        ):
            raise AdhelmError(
                "CONFIGURATION_DRIFT",
                "The current Meta EDIT configuration differs from the retained CREATE plan.",
                exit_code=ExitCode.PRECONDITION,
                next_action="Review local Meta configuration and compile a new exact plan.",
            )
        return _configured_meta_ads_create_adapter(directory, action, ledger, clock=clock)


def _require_installed_build() -> None:
    for installed, detail in (
        (
            meta_ads_create_plan_record_kind_installed(),
            f"a ledger that registers {META_CREATE_PLAN_RECORD_KIND}",
        ),
        (
            meta_create_readback_record_kind_installed(),
            "a ledger that registers META_ADS_SINGLE_IMAGE_HIERARCHY_CREATE_READBACK_OBSERVATION",
        ),
    ):
        if not installed:
            raise AdhelmError(
                "META_ADS_CREATE_RECORD_KIND_NOT_INSTALLED",
                "This build's ledger does not register the Meta Ads CREATE record kinds.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    f"Install a build with {detail}; no credential was read and no provider "
                    "request was made."
                ),
            )
    if not meta_create_upload_transport_installed():
        raise AdhelmError(
            "META_ADS_CREATE_UPLOAD_TRANSPORT_NOT_INSTALLED",
            "This build's transport installs no bounded multipart image upload.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Install a build whose SafeHttpClient exposes the reviewed bounded upload_image "
                "seam; no credential was read and no provider request was made."
            ),
        )


def _option_not_enabled(detail: str, next_action: str) -> AdhelmError:
    return AdhelmError(
        "META_ADS_CREATE_OPTION_NOT_ENABLED",
        f"NOT_ENABLED: {detail}",
        exit_code=ExitCode.PRECONDITION,
        next_action=next_action,
    )


def _require_enabled_brief(brief: MetaAdsCampaignBrief) -> None:
    """Refuse a feature-gated brief before any configuration or credential work.

    The compiler's own closed types already exclude every one of these, so a
    brief that reaches this refusal was constructed around its validators.  The
    gate is restated here rather than assumed: it names a *provider* limit this
    build cannot prove entitlement for, not a compiler limitation, and it runs
    before anything local or remote is read.
    """

    if getattr(brief, "special_ad_category", None) != "NONE":
        raise _option_not_enabled(
            "a special ad category changes eligibility, targeting and review, and no reviewed "
            "read establishes any of them.",
            "Compile a candidate with no special ad category; nothing was read, compiled, "
            "or retained.",
        )
    if tuple(getattr(brief, "country_codes", ())) != ("US",):
        raise _option_not_enabled(
            "only the reviewed US country target has a compiled and reviewed placement shape.",
            "Compile a candidate targeting only US; nothing was read, compiled, or retained.",
        )
    if getattr(brief, "account_currency", None) != "USD":
        raise _option_not_enabled(
            "only USD has a reviewed exact minor-unit spend bound in this build.",
            "Compile a USD candidate; nothing was read, compiled, or retained.",
        )


def _require_enabled_options(specification: MetaAdsCompiledCampaignSpec) -> None:
    """Restate the enabled compiled option set before any provider read.

    ``MetaAdsCreateCandidateBinding`` checks the objective and optimization goal
    again when the action is built.  This runs earlier, so a vendored or altered
    compiler cannot carry a feature-gated option as far as a bounded read.
    """

    adset = specification.adset_request
    checks = (
        (specification.campaign_request.objective, "OUTCOME_TRAFFIC", "campaign objective"),
        (specification.campaign_request.buying_type, "AUCTION", "campaign buying type"),
        (adset.optimization_goal, "LINK_CLICKS", "ad set optimization goal"),
        (adset.billing_event, "IMPRESSIONS", "ad set billing event"),
        (adset.bid_strategy, "LOWEST_COST_WITHOUT_CAP", "ad set bid strategy"),
    )
    for observed, expected, label in checks:
        if observed != expected:
            raise _option_not_enabled(
                f"the compiled {label} is outside this build's reviewed option set.",
                "Compile the reviewed paused traffic candidate; nothing was read from the "
                "provider.",
            )
    if (
        tuple(adset.targeting.publisher_platforms) != ("facebook",)
        or tuple(adset.targeting.facebook_positions) != ("feed",)
        or tuple(adset.targeting.geo_locations.countries) != ("US",)
    ):
        raise _option_not_enabled(
            "the compiled placement or country targeting is outside this build's reviewed set.",
            "Compile the reviewed US Facebook Feed candidate; nothing was read from the provider.",
        )


def _selected_account(account_ref: object) -> str:
    if type(account_ref) is not str:
        raise _invalid_selection("A meta_ads CREATE request names one ad account.")
    matched = _ACCOUNT_REF.fullmatch(account_ref)
    if matched is None:
        raise _invalid_selection("account_ref must be meta_ads.account.act_<ad-account-id>.")
    return matched.group("account")


def _selected_candidate_record(resource_ref: object) -> str:
    """Return the retained candidate record digest the generic request names."""

    if type(resource_ref) is not str:
        raise _invalid_selection(
            "A meta_ads CREATE request names one retained candidate reference."
        )
    matched = _CANDIDATE_RESOURCE.fullmatch(resource_ref)
    if matched is None:
        raise _invalid_selection(
            "resource_ref must be meta_ads.candidate.<retained-record-digest>."
        )
    return matched.group("record")


def _edit_profile(
    configuration: Configuration, profile_id: str, account_client_ref: str
) -> MetaAccountTokenProfile:
    native = account_client_ref.removeprefix("act_")
    profiles = [item for item in configuration.profiles if item.profile_id == profile_id]
    if (
        len(profiles) != 1
        or type(profiles[0]) is not MetaAccountTokenProfile
        or profiles[0].purpose != "EDIT"
        or profiles[0].required_scope != META_CREATE_SCOPE
        or profiles[0].credential_source != "OS_META_TOKEN"
        or profiles[0].account_ref != native
        or profiles[0].currency != "USD"
    ):
        raise AdhelmError(
            "META_TOKEN_EDIT_PROFILE_REQUIRED",
            "Meta Ads CREATE planning requires the configured OS-store Meta token EDIT "
            "profile for this exact ad account.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action=(
                "Configure the Meta Ads EDIT profile for this account; nothing was read, "
                "compiled, or retained."
            ),
        )
    return profiles[0]


def _bound_candidate(
    ledger: Ledger, selection: MetaAdsSingleImageHierarchyCreateSelection
) -> MetaAdsCreateCandidateBinding:
    """Recompile the supplied brief and bind it to the retained candidate.

    Neither the compiled specification nor the brief is asserted: the brief is
    recompiled here and the retained record's own review is required to be the
    byte-identical result.  A retained candidate that names a different spec,
    input, or provider is refused before any provider read.
    """

    retained, provenance = load_candidate(ledger, selection.candidate_record_digest)
    recompiled = compile_candidate(selection.fresh)
    if recompiled.provider != META_CREATE_PROVIDER:
        raise _invalid_selection("The retained candidate is not a Meta Ads candidate.")
    if canonical_bytes(recompiled.model_dump(mode="json", warnings="error")) != canonical_bytes(
        retained.model_dump(mode="json", warnings="error")
    ):
        raise AdhelmError(
            "META_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH",
            "The supplied brief does not recompile to the retained Meta Ads candidate.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Supply the exact brief this candidate was compiled from, or compile a new "
                "candidate; nothing was read from the provider."
            ),
        )
    if recompiled.candidate_digest != provenance["candidate_digest"]:
        raise AdhelmError(
            "META_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH",
            "The retained candidate record does not restate its own candidate digest.",
            exit_code=ExitCode.PRECONDITION,
        )
    specification = compile_meta_ads_campaign(selection.fresh)
    # The typed provider gate runs before the closed binding is constructed, so
    # a feature-gated compiled option is named as such rather than reported as
    # a generic invalid-facts refusal.
    _require_enabled_options(specification)
    try:
        return MetaAdsCreateCandidateBinding(
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


def _require_expressible_request(candidate: MetaAdsCreateCandidateBinding) -> None:
    """Refuse a compiled request the fixed Graph form body cannot carry.

    The shared transport's fixed mutation form accepts printable ASCII values
    only and bounds each value's length.  Nested objects are sent as
    ``ensure_ascii`` JSON, so they are ASCII but can still be too long.  Checking
    here means the refusal happens before any provider work rather than after a
    permit is consumed.

    The same pass screens the compiled creative through the ordinary privacy
    boundary, so a destination or copy value this product refuses to transmit
    fails at planning instead of at egress.
    """

    specification = candidate.specification
    unexpressible = unexpressible_form_values(specification)
    if not unexpressible:
        # The names fit, so check every whole form.  The creative form carries
        # the ad copy as ``ensure_ascii`` JSON, which expands non-ASCII text.
        # The image hash only exists after the upload.  The accepted hash
        # grammar is closed at exactly 32 escape-free characters, so a
        # placeholder from that grammar reserves the creative form's exact
        # length and no acknowledgement can grow it.  The adapter still checks
        # the real form before the creative child's durable start.
        unexpressible = tuple(
            label
            for label, form in (
                ("campaign request", campaign_request_form(specification)),
                ("ad set request", adset_request_form(specification, campaign_id="1")),
                (
                    "ad creative copy",
                    creative_request_form(
                        specification, image_hash=META_CREATE_PLANNING_IMAGE_HASH
                    ),
                ),
                ("ad request", ad_request_form(specification, adset_id="1", creative_id="1")),
            )
            if not all(form_value_expressible(value) for value in form.values())
        )
    if unexpressible:
        raise AdhelmError(
            "META_ADS_CREATE_REQUEST_NOT_EXPRESSIBLE",
            "A compiled Meta Ads value cannot be expressed in this build's fixed form body.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile a candidate whose "
                + ", ".join(unexpressible)
                + " fits this build's fixed Graph form body (printable ASCII, bounded length); "
                "nothing was read from the provider."
            ),
        )
    for payload in (
        specification.campaign_request.model_dump(mode="json", warnings="error"),
        specification.adset_request.model_dump(mode="json", warnings="error"),
        specification.creative_request.model_dump(mode="json", warnings="error"),
        specification.ad_request.model_dump(mode="json", warnings="error"),
    ):
        validate_data(payload)


def _vault_creative(
    directory: Path,
    selection: MetaAdsSingleImageHierarchyCreateSelection,
    candidate: MetaAdsCreateCandidateBinding,
) -> MetaAdsCreativeVaultBinding:
    """Resolve the one private-vault image the compiled ad declares.

    The vault is content-addressed, so the request names a digest and the object
    path is derived.  The bytes are read through the vault boundary -- which
    rehashes them -- and decoded again here, so an unsupported or mislabelled
    image fails before any provider work.
    """

    matched = _VAULT_REF.fullmatch(selection.creative_vault_ref)
    if matched is None:
        raise _invalid_selection("creative_vault_ref must be vault:sha256:<content-digest>.")
    content_digest = matched.group("digest")
    if candidate.specification.asset_content_digest != content_digest:
        raise AdhelmError(
            "META_ADS_CREATE_CREATIVE_NOT_DECLARED",
            "The requested vault image is not the one the compiled ad declares.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Name the creative asset the brief declares, and compile one hierarchy per "
                "image; nothing was read from the provider."
            ),
        )
    data = read_image(directory, content_digest)
    if not 1 <= len(data) <= META_CREATE_MAX_UPLOAD_BYTES:
        raise AdhelmError(
            "META_ADS_CREATE_CREATIVE_TOO_LARGE",
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
    if candidate.specification.asset_version_id != expected_asset_id:
        raise AdhelmError(
            "META_ADS_CREATE_CREATIVE_DRIFT",
            "The private vault object is not the image the compiled candidate declares.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Register the creative again and compile a new candidate from it; nothing was "
                "read from the provider."
            ),
        )
    if decoded.mime_type not in META_CREATE_MEDIA_TYPES:
        raise AdhelmError(
            "META_ADS_CREATE_CREATIVE_MEDIA_TYPE_UNSUPPORTED",
            "This route uploads only the reviewed PNG and JPEG single-image types.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Register a PNG or JPEG creative; Meta documents no media allowlist for the "
                "ad-image upload, so this bound is deliberately local and narrow."
            ),
        )
    media_type: Literal["image/png", "image/jpeg"] = (
        "image/png" if decoded.mime_type == "image/png" else "image/jpeg"
    )
    try:
        return MetaAdsCreativeVaultBinding(
            vault_ref=selection.creative_vault_ref,
            content_digest=content_digest,
            media_type=media_type,
            byte_length=len(data),
            width_px=decoded.width_px,
            height_px=decoded.height_px,
            upload_part_filename="creative.png" if media_type == "image/png" else "creative.jpg",
        )
    except AdhelmError:
        raise
    except Exception:
        raise _facts_invalid() from None


def _plan_within_edit_session(
    *,
    directory: Path,
    ledger: Ledger,
    profile: MetaAccountTokenProfile,
    configuration_digest: str,
    selection: MetaAdsSingleImageHierarchyCreateSelection,
    candidate: MetaAdsCreateCandidateBinding,
    creative: MetaAdsCreativeVaultBinding,
    fingerprint_key: bytes,
    supersedes_record_digest: str | None,
) -> RetainedExecutionPlan:
    """Take the one bounded EDIT read and retain both records under its recheck."""

    with configured_meta_ads_token_edit_session(
        directory, profile.profile_id, expected_configuration_digest=configuration_digest
    ) as session:
        read_started_at = _checked_now()
        action = _build_action(
            workspace_profile_ref=ledger.profile_ref,
            session=session,
            profile=profile,
            configuration_digest=configuration_digest,
            selection=selection,
            candidate=candidate,
            creative=creative,
            fingerprint_key=fingerprint_key,
            read_started_at=read_started_at,
        )
        # Both durable appends happen under one continuity recheck.  The
        # provider reads are already complete: the callback performs no network
        # or ledger I/O and only reproves the held configuration and credential
        # transaction immediately before each append.
        with session.retention_recheck() as recheck:

            def before_durable_append() -> None:
                recheck()
                if _checked_now() >= action.plan.expires_at:
                    raise AdhelmError(
                        "PLAN_EXPIRED",
                        "The Meta Ads CREATE plan expired before retention.",
                        exit_code=ExitCode.AUTHORIZATION,
                    )

            try:
                _retain_provider_plan(ledger, action, before_durable_append=before_durable_append)
            except AdhelmError:
                raise
            except Exception:
                raise _retention_unverified() from None
            return _retain_provider_neutral_plan(
                ledger,
                action,
                read_started_at=read_started_at,
                supersedes_record_digest=supersedes_record_digest,
                before_durable_append=before_durable_append,
            )


def _build_action(
    *,
    workspace_profile_ref: str,
    session: MetaAdsTokenEditSession,
    profile: MetaAccountTokenProfile,
    configuration_digest: str,
    selection: MetaAdsSingleImageHierarchyCreateSelection,
    candidate: MetaAdsCreateCandidateBinding,
    creative: MetaAdsCreativeVaultBinding,
    fingerprint_key: bytes,
    read_started_at: datetime,
) -> MetaAdsSingleImageHierarchyCreatePlannedAction:
    """Perform the bounded promotion reads and build the closed action."""

    identity = session.identity
    binding = _checked_binding(session.binding)
    if identity.status != "1":
        raise AdhelmError(
            "META_ADS_ACCOUNT_NOT_ACTIVE",
            "A Meta Ads hierarchy is created only in an account whose account_status is 1.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Review the configured Meta ad account; nothing was created.",
        )
    specification = candidate.specification
    if (
        identity.currency != specification.account_currency
        or identity.timezone != specification.account_timezone
        or identity.currency != profile.currency
        or identity.timezone != profile.timezone
    ):
        raise AdhelmError(
            "META_ADS_CREATE_ACCOUNT_MISMATCH",
            "The compiled candidate's account calendar differs from the observed account.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile a candidate for this account's currency and timezone; nothing was created."
            ),
        )
    if identity.account_ref != specification.account_ref:
        raise AdhelmError(
            "META_ADS_CREATE_ACCOUNT_MISMATCH",
            "The compiled candidate names a different Meta ad account.",
            exit_code=ExitCode.PRECONDITION,
        )
    name = specification.campaign_request.name
    probe = session._probe_fixed_create_campaign_name(name)
    if probe.exact_matches:
        raise AdhelmError(
            "META_ADS_CREATE_NAME_COLLISION",
            "This Meta ad account already holds a campaign with the planned name.",
            exit_code=ExitCode.PRECONDITION,
            next_action="Compile a candidate with a different campaign name; nothing was created.",
        )
    if probe.near_matches:
        raise AdhelmError(
            "META_ADS_CREATE_NAME_COLLISION_NEAR_MATCH",
            "This Meta ad account holds a campaign whose name differs from the planned name "
            "only by case, surrounding whitespace, or Unicode normalization form.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile a candidate with a clearly distinct campaign name; nothing was "
                "created. Meta does not document whether it compares campaign names "
                "case-sensitively, so a near match is refused rather than assumed distinct."
            ),
        )
    read_finished_at = _checked_now()
    try:
        account = MetaAdsCreateAccountBinding(
            account_ref=identity.account_ref,
            account_client_ref=identity.account_client_ref,
            timezone=identity.timezone,
            currency=identity.currency,
        )
        execution = MetaAdsCreateExecutionBinding(
            profile_ref=binding.profile_ref,
            client_application_ref=binding.client_application_ref,
            principal_ref=binding.principal_ref,
            credential_binding_ref=binding.credential_binding_ref,
            configuration_digest=binding.configuration_digest,
            profile_configuration_digest=binding.profile_configuration_digest,
            inspected_at=binding.observed_at,
            expires_at=binding.expires_at,
            data_access_expires_at=binding.data_access_expires_at,
        )
        observation = MetaAdsCreateObservationBinding(
            campaign_name=name,
            preconditions=MetaAdsCreatePreconditions(
                observed_account_campaign_names=probe.observed_names
            ),
            creative=creative,
            fingerprint_key_marker=fingerprint_key_marker(fingerprint_key),
            read_started_at=read_started_at,
            read_finished_at=read_finished_at,
        )
    except AdhelmError:
        raise
    except Exception:
        raise _facts_invalid() from None
    if (
        binding.configuration_digest != configuration_digest
        or binding.profile_configuration_digest != digest(profile)
        or binding.profile_ref != profile.profile_id
        or binding.client_application_ref != profile.client_application_ref
    ):
        raise AdhelmError(
            "CONFIGURATION_DRIFT",
            "The inspected Meta EDIT binding differs from the configuration this plan read.",
            exit_code=ExitCode.PRECONDITION,
        )
    expires_at = read_finished_at + timedelta(seconds=selection.expires_in_seconds)
    current = _checked_now()
    if current < read_finished_at:
        raise AdhelmError(
            "CLOCK_INVALID",
            "The Meta Ads planning clock moved backwards after the bounded read.",
            exit_code=ExitCode.PRECONDITION,
        )
    if current >= expires_at:
        raise AdhelmError(
            "PLAN_EXPIRED",
            "The requested Meta Ads CREATE lifetime elapsed during planning.",
            exit_code=ExitCode.AUTHORIZATION,
            next_action="Prepare a fresh deliberate plan.",
        )
    if expires_at > execution.expires_at or expires_at > execution.data_access_expires_at:
        raise AdhelmError(
            "META_TOKEN_WINDOW_TOO_SHORT",
            "The inspected Meta EDIT token expires before this plan's own lifetime.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Refresh the configured Meta EDIT token, or request a shorter plan lifetime; "
                "nothing was created."
            ),
        )
    return build_meta_ads_single_image_hierarchy_create_plan(
        workspace_profile_ref=workspace_profile_ref,
        account_binding=account,
        execution_binding=execution,
        candidate_binding=candidate,
        observation_binding=observation,
        expires_at=expires_at,
        fingerprint_key=fingerprint_key,
    )


def _retain_provider_plan(
    ledger: Ledger,
    action: MetaAdsSingleImageHierarchyCreatePlannedAction,
    *,
    before_durable_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Retain the closed CREATE action, without asserting a remote condition."""

    record = meta_ads_create_plan_record(action)
    if ledger.profile_ref != record.action.plan.profile_ref:
        raise AdhelmError(
            "INVALID_META_ADS_CREATE_PLAN_PROVENANCE",
            "The Meta Ads CREATE plan belongs to a different local workspace.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return ledger.append(
        "changes",
        META_CREATE_PLAN_RECORD_KIND,
        record.model_dump(mode="json", warnings="error"),
        provider=META_CREATE_PROVIDER,
        account_ref=record.action.plan.subplans[0].account.account_ref,
        source="LOCAL_CLI",
        _before_durable_append=before_durable_append,
    )


def _retain_provider_neutral_plan(
    ledger: Ledger,
    action: MetaAdsSingleImageHierarchyCreatePlannedAction,
    *,
    read_started_at: datetime,
    supersedes_record_digest: str | None,
    before_durable_append: Callable[[], None] | None = None,
) -> RetainedExecutionPlan:
    """Retain the provider-neutral record for the exact plan this flow compiled."""

    subplan = action.plan.subplans[0]
    observation = action.observation_binding
    read = BoundedExecutionRead(
        observed_state=subplan.before,
        read_started_at=read_started_at,
        read_finished_at=observation.read_finished_at,
        connector_version=META_CREATE_CAPABILITY_VERSION,
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
        before_durable_append=before_durable_append,
    )


def _retained_action_for_plan(
    ledger: Ledger, plan: CompositePlan
) -> MetaAdsSingleImageHierarchyCreatePlannedAction:
    """Resolve the closed CREATE action for one provider-neutral plan digest.

    The join key is the composite plan digest both records already carry.  The
    scan reads a verified checkpoint, revalidates every candidate, and requires
    the retained action's own plan to compare byte for byte with the approved
    one, so a record that merely reuses a digest cannot substitute a different
    account, candidate, creative, or credential association.
    """

    expected = canonical_bytes(plan.model_dump(mode="json", warnings="error"))
    matching: list[MetaAdsSingleImageHierarchyCreatePlannedAction] = []
    for stored in ledger.checkpoint()["records_by_stream"]["changes"]:
        payload: Any = stored.get("payload")
        if (
            stored.get("record_kind") != META_CREATE_PLAN_RECORD_KIND
            or type(payload) is not dict
            or payload.get("plan_digest") != plan.plan_digest
        ):
            continue
        action = revalidate_meta_ads_create_plan_record(payload).action
        if canonical_bytes(action.plan.model_dump(mode="json", warnings="error")) == expected:
            matching.append(action)
    if not matching:
        raise AdhelmError(
            "META_ADS_CREATE_PLAN_NOT_RETAINED",
            "The retained execution plan has no matching Meta Ads CREATE action in history.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Compile the plan again with plan execution create --provider meta_ads "
                "--operation CREATE; no credential was read and no provider request was made."
            ),
        )
    if len({canonical_bytes(item.to_data()) for item in matching}) != 1:
        raise AdhelmError(
            "META_ADS_CREATE_PLAN_AMBIGUOUS",
            "Local history retains more than one different Meta Ads CREATE action here.",
            exit_code=ExitCode.AMBIGUOUS,
            next_action="Inspect history verify before requesting another execution.",
        )
    return matching[0]


#: The two singletons the registries hold.  A module-level instance keeps
#: registration idempotent across a reimport without letting a *different*
#: object silently replace an installed one.
META_CREATE_EXECUTION_PLANNER: Final = MetaAdsSingleImageHierarchyCreateExecutionPlanner()
META_CREATE_EXECUTION_ADAPTER_FACTORY: Final = (
    MetaAdsSingleImageHierarchyCreateExecutionAdapterFactory()
)


def register_meta_create_execution() -> None:
    """Register the Meta Ads CREATE planner and adapter factory exactly once."""

    if (
        configured_execution_planner(META_CREATE_PROVIDER, META_CREATE_OPERATION)
        is not META_CREATE_EXECUTION_PLANNER
    ):
        register_execution_planner(
            META_CREATE_PROVIDER, META_CREATE_OPERATION, META_CREATE_EXECUTION_PLANNER
        )
    if (
        configured_execution_adapter(META_CREATE_PROVIDER, META_CREATE_OPERATION)
        is not META_CREATE_EXECUTION_ADAPTER_FACTORY
    ):
        register_execution_adapter(
            META_CREATE_PROVIDER,
            META_CREATE_OPERATION,
            META_CREATE_EXECUTION_ADAPTER_FACTORY,
        )


def _assert_registration_seams() -> None:
    """Fail closed at import if either singleton stops matching its protocol."""

    factory: object = META_CREATE_EXECUTION_ADAPTER_FACTORY
    if not isinstance(META_CREATE_EXECUTION_PLANNER, ConfiguredExecutionPlanner) or (
        not isinstance(factory, ConfiguredExecutionAdapterFactory)
    ):  # pragma: no cover - a structural regression, not a runtime state
        raise AdhelmError(
            "INVALID_EXECUTION_ADAPTER_REGISTRATION",
            "The Meta Ads CREATE registration singletons do not match their seams.",
            exit_code=ExitCode.INVALID_INPUT,
        )


_assert_registration_seams()


__all__ = [
    "META_CREATE_EXECUTION_ADAPTER_FACTORY",
    "META_CREATE_EXECUTION_PLANNER",
    "META_CREATE_OPERATION",
    "META_CREATE_PROVIDER",
    "MetaAdsSingleImageHierarchyCreateAdapter",
    "MetaAdsSingleImageHierarchyCreateExecutionAdapterFactory",
    "MetaAdsSingleImageHierarchyCreateExecutionPlanner",
    "register_meta_create_execution",
]
