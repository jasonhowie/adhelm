"""Bounded, noninteractive machine interface. No implicit remote activity."""

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import BaseModel, ValidationError
from typer import _click as click

from adhelm import RELEASE_STATE, __version__
from adhelm._installed_approval_execution_status import (
    ATTEMPT_RECOVERY_COMMAND_STATUS,
    GENERIC_EXECUTION_COMMAND_STATUS,
    GOOGLE_ADS_PAUSE_EXECUTION_COMMAND_STATUS,
    INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
)
from adhelm.additional_compile_cli import register_commands as register_additional_compilers
from adhelm.artifact_cli import (
    ArtifactPolicyQuery,
    ArtifactRenderRequest,
    Ga4KeyEventCreatePlanArtifactRequest,
    PausePlanArtifactRequest,
)
from adhelm.artifact_cli import app as artifacts_app
from adhelm.audit import AuditPolicy
from adhelm.audits import _RULE_MODULES, ProviderAuditEvidence, ProviderAuditResult
from adhelm.campaign_joins import CampaignJoinContract, CampaignJoinMap
from adhelm.candidate_cli import app as candidate_app
from adhelm.candidate_records import CandidateCompilationRecord
from adhelm.canonical import canonical_bytes
from adhelm.compile_cli import app as compile_app
from adhelm.config import (
    default_data_dir,
    load_configuration,
    save_configuration,
    update_configuration,
)
from adhelm.conversion_cli import app as conversion_app
from adhelm.conversion_contracts import ConversionContract, ConversionContractInput
from adhelm.creative import CreativeVersion
from adhelm.creative_registration import (
    CreativeRegistrationRequest,
    analyze_local_image,
    creative_catalog_entries,
    creative_image_analysis_receipt_preview,
    creative_registration_receipt_preview,
    creative_show_data,
    ephemeral_local_image_analysis,
    register_creative_request,
    retain_local_image_analysis,
)
from adhelm.credential_cli import app as auth_app
from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_cli import app as execution_app
from adhelm.execution_planning_session import compilable_execution_operations
from adhelm.ga4_admin_access_workflow import Ga4AdminAccessRequest
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_admin_edit_workflow import Ga4AdminEditInventoryRequest
from adhelm.ga4_create_cli import app as ga4_create_app
from adhelm.ga4_create_execution_workflow import Ga4KeyEventCreateExecutionRequest
from adhelm.ga4_create_plan_artifacts import Ga4CreatePlanArtifactDocument
from adhelm.ga4_create_plan_records import Ga4KeyEventCreatePlanRecord
from adhelm.ga4_create_workflow import Ga4KeyEventCreatePlanRequest
from adhelm.ga4_key_event_candidate_cli import app as ga4_key_event_candidate_app
from adhelm.ga4_key_event_candidates import Ga4KeyEventCandidateInput, Ga4KeyEventCandidateRecord
from adhelm.ga4_measurement_workflow import Ga4AdminReadRequest
from adhelm.generic_execution_workflow import (
    AttemptRecoveryRequest,
    ExecutionPlanExecutionRequest,
    executable_execution_operations,
    first_execution_refusal_code,
    installed_execution_refusal_gates,
)
from adhelm.google_ads_pause_artifact_cli import (
    GoogleAdsCampaignPausePlanArtifactRequest,
)
from adhelm.google_ads_pause_artifact_cli import (
    register_commands as register_google_ads_pause_artifact_commands,
)
from adhelm.google_ads_pause_cli import app as google_ads_pause_app
from adhelm.google_ads_pause_plan_artifacts import GoogleAdsCampaignPausePlanArtifactDocument
from adhelm.google_ads_pause_workflow import GoogleAdsCampaignPausePlanRequest
from adhelm.google_oauth_cli import app as google_oauth_app
from adhelm.google_oauth_setup import GoogleOAuthProfileDraft
from adhelm.gtm_measurement_workflow import GtmReadRequest
from adhelm.guidance_cli import app as guidance_app
from adhelm.input_files import read_json_input_file, read_regular_input_file, validate_json_file
from adhelm.join_cli import register_commands as register_join_commands
from adhelm.linkedin_token_cli import register_commands as register_linkedin_token_commands
from adhelm.measurement import ContainerInventory, EventContract
from adhelm.measurement_cli import _fits
from adhelm.measurement_cli import app as measurement_app
from adhelm.measurement_comparison import MeasurementComparison, MeasurementComparisonRequest
from adhelm.measurement_comparison_artifacts import (
    MeasurementComparisonArtifactDocument,
    MeasurementComparisonArtifactRequest,
)
from adhelm.measurement_records import MeasurementObservation
from adhelm.meta_pause_artifact_cli import (
    MetaCampaignPausePlanArtifactRequest,
)
from adhelm.meta_pause_artifact_cli import (
    register_commands as register_meta_pause_artifact_commands,
)
from adhelm.meta_pause_cli import app as meta_pause_app
from adhelm.meta_pause_execution_workflow import MetaCampaignPauseExecutionRequest
from adhelm.meta_pause_plan_artifacts import MetaCampaignPausePlanArtifactDocument
from adhelm.meta_pause_workflow import MetaCampaignPausePlanRequest
from adhelm.meta_token_cli import app as meta_token_app
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.models import (
    PROVIDERS,
    Configuration,
    Diagnostic,
    Money,
    OpenAIAccountKeyProfile,
    OutputPreferences,
    Profile,
    ResultEnvelope,
)
from adhelm.openai_account_key_cli import register_commands as register_openai_account_key_commands
from adhelm.openai_measurement_workflow import OpenAIConversionsReadRequest
from adhelm.openai_pause_execution_workflow import OpenAIPauseExecutionRequest
from adhelm.pause_workflow import OpenAIPauseRequest
from adhelm.pilot_cli import app as pilot_app
from adhelm.pilot_records import (
    PILOT_WINDOW_DAYS,
    RELEASE_PROVIDERS,
    PilotRecord,
    pilot_steps,
)
from adhelm.pilot_workflow import UI_COMPARISONS, pilot_options
from adhelm.plan_cli import ExecutionPlanRequest
from adhelm.plan_cli import app as plan_app
from adhelm.plan_cli import register_commands as register_generic_plan_commands
from adhelm.providers.ga4 import Ga4AggregateQuery, Ga4ReportQuery
from adhelm.providers.google_ads import GoogleAdsReportQuery
from adhelm.providers.google_ads_compiler import GoogleAdsSearchCampaignBrief
from adhelm.providers.linkedin_ads import LinkedInAdsReportQuery
from adhelm.providers.linkedin_compiler import LinkedInAdsCampaignBrief
from adhelm.providers.meta_ads import MetaAdsReportQuery
from adhelm.providers.meta_compiler import MetaAdsCampaignBrief
from adhelm.providers.openai_ads import OpenAIAdsReportQuery
from adhelm.providers.openai_compiler import OpenAIAdsCampaignBrief
from adhelm.providers.reddit_ads import RedditAdsReportQuery
from adhelm.providers.reddit_compiler import RedditAdsCampaignBrief
from adhelm.providers.tiktok_ads import TikTokAdsReportQuery
from adhelm.providers.tiktok_compiler import TikTokAdsCampaignBrief
from adhelm.recovery_cli import app as recovery_app
from adhelm.reddit_token_cli import register_commands as register_reddit_token_commands
from adhelm.release_labels import (
    LABEL_MEANINGS,
    is_released_provider,
    pilot_record_inventory,
    release_label,
)
from adhelm.release_policy import (
    UNRELEASED_STATUS,
    WRITES_NOT_RELEASED,
    unreleased_commands,
)
from adhelm.schema_views import installed_schema_view
from adhelm.storage_cli import app as storage_app
from adhelm.strategy import StrategyBrief
from adhelm.strategy_cli import register_commands as register_strategy_compiler
from adhelm.strategy_history_cli import app as strategy_history_app
from adhelm.strategy_records import StrategyCandidateCompilationRecord
from adhelm.tiktok_token_cli import register_commands as register_tiktok_token_commands

app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="AdHelm: inspect the installed contract with discover.",
)
history = typer.Typer(no_args_is_help=True)
config_app = typer.Typer(no_args_is_help=True)
reports_app = typer.Typer(no_args_is_help=True)
creative_app = typer.Typer(no_args_is_help=True)
register_additional_compilers(compile_app)
register_strategy_compiler(compile_app)
app.add_typer(history, name="history")
app.add_typer(config_app, name="config")
app.add_typer(reports_app, name="report")
app.add_typer(creative_app, name="creative")
app.add_typer(measurement_app, name="measurement")
measurement_app.add_typer(ga4_key_event_candidate_app, name="key-event-candidate")
register_join_commands(measurement_app)
app.add_typer(artifacts_app, name="artifacts")
register_meta_pause_artifact_commands(artifacts_app)
register_google_ads_pause_artifact_commands(artifacts_app)
app.add_typer(compile_app, name="compile")
app.add_typer(auth_app, name="auth")
auth_app.add_typer(google_oauth_app, name="google-oauth")
auth_app.add_typer(meta_token_app, name="meta-token")
register_linkedin_token_commands(auth_app)
register_reddit_token_commands(auth_app)
register_openai_account_key_commands(auth_app)
register_tiktok_token_commands(auth_app)
app.add_typer(candidate_app, name="candidate")
app.add_typer(strategy_history_app, name="strategy")
app.add_typer(storage_app, name="storage")
app.add_typer(pilot_app, name="pilot")
app.add_typer(guidance_app, name="guidance")
app.add_typer(conversion_app, name="conversion")
app.add_typer(plan_app, name="plan")
plan_app.add_typer(ga4_create_app, name="ga4-key-event")
plan_app.add_typer(meta_pause_app, name="meta-campaign-pause")
plan_app.add_typer(google_ads_pause_app, name="google-ads-campaign-pause")
register_generic_plan_commands(plan_app)
app.add_typer(execution_app, name="execute")
app.add_typer(recovery_app, name="recover")

_SCHEMAS: dict[str, type[BaseModel]] = {
    "config": Configuration,
    "profile": Profile,
    "google-oauth-profile-draft": GoogleOAuthProfileDraft,
    "meta-token-profile-draft": MetaAccountTokenProfileDraft,
    "openai-account-key-profile": OpenAIAccountKeyProfile,
    "output": OutputPreferences,
    "artifact-policy": ArtifactPolicyQuery,
    "artifact-render": ArtifactRenderRequest,
    "pause-plan-artifact-render": PausePlanArtifactRequest,
    "ga4-key-event-create-plan-artifact-render": Ga4KeyEventCreatePlanArtifactRequest,
    "ga4-key-event-create-plan-artifact-document": Ga4CreatePlanArtifactDocument,
    "meta-campaign-pause-plan-artifact-render": MetaCampaignPausePlanArtifactRequest,
    "meta-campaign-pause-plan-artifact-document": MetaCampaignPausePlanArtifactDocument,
    "google-ads-campaign-pause-plan-artifact-render": GoogleAdsCampaignPausePlanArtifactRequest,
    "google-ads-campaign-pause-plan-artifact-document": (
        GoogleAdsCampaignPausePlanArtifactDocument
    ),
    "money": Money,
    "result": ResultEnvelope,
    "audit-policy": AuditPolicy,
    "provider-audit-evidence": ProviderAuditEvidence,
    "provider-audit-result": ProviderAuditResult,
    "ga4-report": Ga4ReportQuery,
    "ga4-aggregate-report": Ga4AggregateQuery,
    "openai-report": OpenAIAdsReportQuery,
    "openai-campaign-brief": OpenAIAdsCampaignBrief,
    "meta-campaign-brief": MetaAdsCampaignBrief,
    "google-ads-search-brief": GoogleAdsSearchCampaignBrief,
    "linkedin-campaign-brief": LinkedInAdsCampaignBrief,
    "reddit-campaign-brief": RedditAdsCampaignBrief,
    "tiktok-campaign-brief": TikTokAdsCampaignBrief,
    "strategy-brief": StrategyBrief,
    "strategy-candidate-record": StrategyCandidateCompilationRecord,
    "candidate-record": CandidateCompilationRecord,
    "google-ads-report": GoogleAdsReportQuery,
    "meta-ads-report": MetaAdsReportQuery,
    "linkedin-ads-report": LinkedInAdsReportQuery,
    "tiktok-ads-report": TikTokAdsReportQuery,
    "reddit-ads-report": RedditAdsReportQuery,
    "pilot-record": PilotRecord,
    "creative-version": CreativeVersion,
    "creative-registration-request": CreativeRegistrationRequest,
    "event-contract": EventContract,
    "gtm-inventory": ContainerInventory,
    "measurement-join-contract": CampaignJoinContract,
    "measurement-join-map": CampaignJoinMap,
    "measurement-observation": MeasurementObservation,
    "measurement-comparison": MeasurementComparison,
    "measurement-comparison-request": MeasurementComparisonRequest,
    "measurement-comparison-artifact-render": MeasurementComparisonArtifactRequest,
    "measurement-comparison-artifact-document": MeasurementComparisonArtifactDocument,
    "measurement-ga4-key-event-candidate-input": Ga4KeyEventCandidateInput,
    "measurement-ga4-key-event-candidate-record": Ga4KeyEventCandidateRecord,
    "measurement-ga4-read": Ga4AdminReadRequest,
    "measurement-ga4-access": Ga4AdminAccessRequest,
    "measurement-ga4-edit-read": Ga4AdminEditInventoryRequest,
    "measurement-ga4-edit-observation": Ga4AdminEditInventoryObservation,
    "measurement-openai-read": OpenAIConversionsReadRequest,
    "measurement-gtm-read": GtmReadRequest,
    "conversion-definition": ConversionContractInput,
    "conversion-contract": ConversionContract,
    "openai-pause-request": OpenAIPauseRequest,
    "openai-pause-installed-approval-execution-request": OpenAIPauseExecutionRequest,
    "meta-campaign-pause-installed-approval-execution-request": MetaCampaignPauseExecutionRequest,
    "ga4-key-event-create-plan-request": Ga4KeyEventCreatePlanRequest,
    "meta-campaign-pause-plan-request": MetaCampaignPausePlanRequest,
    "google-ads-campaign-pause-plan-request": GoogleAdsCampaignPausePlanRequest,
    "ga4-key-event-create-plan-record": Ga4KeyEventCreatePlanRecord,
    "ga4-key-event-create-installed-approval-execution-request": Ga4KeyEventCreateExecutionRequest,
    "execution-plan-request": ExecutionPlanRequest,
    "execution-plan-installed-approval-execution-request": ExecutionPlanExecutionRequest,
    "attempt-recovery-installed-approval-request": AttemptRecoveryRequest,
}
_COMMANDS = [
    "discover",
    "schema",
    "version",
    "init",
    "doctor",
    "config show",
    "history status",
    "history verify",
    "history tail",
    "history index",
    "history export",
    "config add-profile",
    "artifacts configure",
    "artifacts policy",
    "artifacts report",
    "artifacts plan",
    "artifacts ga4-key-event-plan",
    "artifacts meta-campaign-pause",
    "artifacts google-ads-campaign-pause",
    "artifacts measurement-comparison",
    "compile openai-ads",
    "compile meta-ads",
    "compile google-ads",
    "compile linkedin-ads",
    "compile reddit-ads",
    "compile tiktok-ads",
    "compile strategy",
    "candidate catalog",
    "candidate show",
    "strategy catalog",
    "strategy show",
    "storage usage",
    "pilot run",
    "guidance catalog",
    "guidance show",
    "auth status",
    "auth store",
    "auth rotate",
    "auth remove-local",
    "auth google-oauth enroll",
    "auth google-oauth reconnect",
    "auth google-oauth status",
    "auth meta-token enroll",
    "auth meta-token rotate",
    "auth meta-token status",
    "auth meta-token inspect",
    "auth meta-token remove-local",
    "auth linkedin-token enroll",
    "auth linkedin-token refresh",
    "auth linkedin-token status",
    "auth linkedin-token remove-local",
    "auth tiktok-token enroll",
    "auth tiktok-token refresh",
    "auth tiktok-token status",
    "auth tiktok-token remove-local",
    "auth reddit-token enroll",
    "auth reddit-token refresh",
    "auth reddit-token status",
    "auth reddit-token remove-local",
    "auth openai-key status",
    "auth openai-key rotate",
    "report ga4",
    "report openai-ads",
    "report google-ads",
    "report meta-ads",
    "report linkedin-ads",
    "report tiktok-ads",
    "report reddit-ads",
    "inventory",
    "report catalog",
    "report show",
    "audit",
    "audit --provider-rules",
    "creative analyze",
    "creative register",
    "creative catalog",
    "creative show",
    "measurement validate-event",
    "measurement check-stage",
    "measurement ga4-admin",
    "measurement ga4-admin-access",
    "measurement ga4-admin-edit read",
    "measurement ga4-admin-edit catalog",
    "measurement ga4-admin-edit show",
    "measurement openai-conversions",
    "measurement gtm",
    "measurement gtm-graph",
    "measurement join create",
    "measurement join catalog",
    "measurement join show",
    "measurement catalog",
    "measurement show",
    "measurement compare",
    "measurement key-event-candidate compile",
    "measurement key-event-candidate catalog",
    "measurement key-event-candidate show",
    "conversion register",
    "conversion catalog",
    "conversion show",
    "plan openai-pause",
    "plan catalog",
    "plan show",
    "plan ga4-key-event create",
    "plan ga4-key-event catalog",
    "plan ga4-key-event show",
    "plan meta-campaign-pause create",
    "plan meta-campaign-pause catalog",
    "plan meta-campaign-pause show",
    "plan google-ads-campaign-pause create",
    "plan google-ads-campaign-pause catalog",
    "plan google-ads-campaign-pause show",
    "plan execution create",
    "plan execution catalog",
    "plan execution show",
    "execute openai-pause",
    "execute ga4-key-event-create",
    "execute meta-campaign-pause",
    "execute plan",
    "recover attempt",
    "recover catalog",
]
#: The commands that compile, plan or preview a change without any remote
#: effect. They stay available; only their execution is unreleased. Some of them
#: read provider state (and so use credentials and the network) to build the plan;
#: what they share is that none of them changes a provider account.
_PLANNING_WITHOUT_REMOTE_EFFECT = (
    "compile ",
    "plan ",
    "artifacts plan",
    "artifacts ga4-key-event-plan",
    "artifacts meta-campaign-pause",
    "artifacts google-ads-campaign-pause",
)


def emit(
    command: str,
    data: dict[str, Any],
    *,
    outcome: str = "OK",
    maximum_bytes: int = 65_536,
    next_cursor: str | None = None,
    total_count: int | None = None,
    artifact_refs: list[str] | None = None,
) -> None:
    envelope = ResultEnvelope(
        tool_version=__version__,
        command=command,
        outcome=outcome,
        data=data,
        next_cursor=next_cursor,
        total_count=total_count,
        artifact_refs=artifact_refs or [],
    )
    raw = canonical_bytes(envelope)
    if len(raw) + 1 > maximum_bytes:
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "Result exceeds output limit; request a smaller page.",
            next_action="Use filters or a smaller --limit; no rows were silently omitted.",
        )
    typer.echo(raw.decode())


def fail(command: str, error: AdhelmError) -> None:
    envelope = ResultEnvelope(
        tool_version=__version__,
        command=command,
        outcome="FAILED_CLOSED",
        diagnostics=[
            Diagnostic(
                code=error.code,
                message=error.message,
                next_action=error.next_action,
                http_status=error.status_code,
                provider_code=error.provider_code,
                provider_request_id=error.provider_request_id,
            )
        ],
    )
    typer.echo(canonical_bytes(envelope).decode())
    raise typer.Exit(error.exit_code)


def _release_label_name(provider: str) -> str | None:
    label = release_label(provider)
    return label.label if label is not None else None


def guarded(command: str, work: Callable[[], None]) -> None:
    try:
        work()
    except AdhelmError as error:
        fail(command, error)
    except ValidationError:
        fail(
            command,
            AdhelmError(
                "SCHEMA_VALIDATION_FAILED",
                "Input does not match the installed schema.",
                exit_code=2,
                next_action="Inspect `adhelm schema`; input was not stored.",
            ),
        )
    except OSError:
        fail(
            command,
            AdhelmError(
                "LOCAL_IO_FAILED",
                "Local data could not be read or written.",
                next_action="Check the selected data directory and permissions.",
            ),
        )


def root(ctx: typer.Context) -> Path:
    """Return the data directory; the platform default is resolved here, not at startup.

    Only commands that touch local data call this, so `version`, `discover` and `schema` still
    work when the operating system cannot supply a user directory.
    """
    state = ctx.find_root().obj
    if state["data_dir"] is None:
        # Raised, not reported here: every caller runs inside `guarded(<full command name>, ...)`,
        # which owns the envelope so the refusal names the command that ran.
        state["data_dir"] = default_data_dir().resolve()
    return Path(state["data_dir"])


@app.callback()
def global_options(
    ctx: typer.Context,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Machine JSON is the default; secret entry requires explicit --interactive handoff."""
    ctx.obj = {"data_dir": data_dir.resolve() if data_dir is not None else None}


@app.command()
def version() -> None:
    emit("version", {"version": __version__, "release_state": RELEASE_STATE})


@app.command()
def discover() -> None:
    """Discover implemented commands and provider evidence without network access."""
    emit(
        "discover",
        {
            "commands": _COMMANDS,
            "schemas": sorted(_SCHEMAS),
            "release_policy": {
                "remote_writes": UNRELEASED_STATUS,
                "refusal_code": WRITES_NOT_RELEASED,
                "exit_code": int(ExitCode.PRECONDITION),
                # Declared by each command with `remote_mutation_leaf`, so a
                # leaf cannot be refused without being listed, or listed
                # without being refused.
                "unreleased_commands": [
                    {"command": command, "status": UNRELEASED_STATUS}
                    for command in unreleased_commands()
                ],
                "refused_before": [
                    "CONFIGURATION_AND_INPUT_READ",
                    "CREDENTIAL_STORE_ACCESS",
                    "LEDGER_APPLY_STARTED",
                    "NETWORK_EGRESS",
                ],
                "planning_without_remote_effect": {
                    "status": "AVAILABLE",
                    "execution": UNRELEASED_STATUS,
                    "remote_reads": "SOME_COMMANDS_READ_PROVIDER_STATE",
                    "commands": [
                        command
                        for command in _COMMANDS
                        if command.startswith(_PLANNING_WITHOUT_REMOTE_EFFECT)
                    ],
                },
                "enabled_by": "NOTHING",
                "policy": "docs/release-0.1.0.md",
            },
            "providers": [
                {
                    "provider": p,
                    "release_required": is_released_provider(p),
                    "implementation": "FIXTURE_VERIFIED_READ_SUBSET"
                    if p
                    in {
                        "ga4",
                        "ga4_admin",
                        "gtm",
                        "openai_ads",
                        "google_ads",
                        "meta_ads",
                        "linkedin_ads",
                        "tiktok_ads",
                        "reddit_ads",
                    }
                    else "NOT_IMPLEMENTED",
                    "live_verified": False,
                    "release_label": _release_label_name(p),
                }
                for p in PROVIDERS
            ],
            "release_labels": {
                "meanings": dict(LABEL_MEANINGS),
                "source": "COMMITTED_PILOT_RECORDS",
                "live_piloted_requires": (
                    "A schema-valid, digest-consistent pilot record for the provider whose "
                    "cli_version equals this build."
                ),
                "grants_capability": False,
                "pilot_command": "pilot run",
                "policy": "docs/release-0.1.0.md",
            },
            "pilot": {
                "command": "pilot run",
                "record_schema": "pilot-record",
                "record_directory": "docs/pilots",
                "report_window_days": PILOT_WINDOW_DAYS,
                "ui_comparison": list(UI_COMPARISONS),
                "always_required_options": [
                    "--provider",
                    "--profile",
                    "--ui-comparison",
                    "--source-commit",
                    "--output",
                ],
                "providers": {
                    provider: {
                        "options": [
                            "--" + option.replace("_", "-") for option in pilot_options(provider)
                        ],
                        "steps": [
                            {"step": step, "command": command}
                            for step, command in pilot_steps(provider)
                        ],
                    }
                    for provider in RELEASE_PROVIDERS
                },
                "record_written": "ONLY_FOR_A_COMPLETE_PILOT",
                "record_contents": "PSEUDONYM_COMMAND_OUTCOME_AND_DIGESTS_ONLY",
                "remote_mutations": False,
                "run_by": "THE_OWNER_ON_AN_ACCOUNT_THEY_ADMINISTER",
            },
            "exit_codes": {
                "0": "success",
                "2": "invalid input",
                "3": "failed precondition",
                "4": "authorization required",
                "5": "partial",
                "6": "ambiguous",
                "7": "pending",
                "10": "internal failure",
            },
            "input_files": {
                "json_maximum_bytes": 1_048_576,
                "local_image_maximum_bytes": 20 * 1024 * 1024,
                "required_type": "REGULAR_FILE",
                "final_symlinks": "REJECTED",
                "pipes_devices_sockets": "REJECTED",
                "observed_read_changes": "REJECTED",
                "unsupported_platform": "FAIL_CLOSED",
                "filesystem_latency_guaranteed": False,
            },
            "provider_audit": {
                "command": "audit --provider-rules",
                # Derived from the seam's own registry so the catalogue an
                # agent discovers cannot drift from the modules that exist.
                "rule_modules": sorted(_RULE_MODULES),
                "evidence": "RETAINED_LOCAL_RECORDS_ONLY",
                "live_provider_reads": "NONE",
                "ledger_appends": "NONE",
                "coverage_statuses": ["COVERED", "PARTIAL", "NOT_READ", "NOT_APPLICABLE"],
                "coverage_semantics": (
                    "A dimension the reviewed fixed read does not return is NOT_READ, never "
                    "healthy. Browser measurement is NOT_APPLICABLE to a local audit."
                ),
                "analytics_completeness": "BOUNDED_NON_EXHAUSTIVE",
                "read_boundary": "INVENTORY_READ_COMPLETED_MARKER_REQUIRED",
                "read_boundary_semantics": (
                    "A retained inventory read is selected from its recorded completion "
                    "marker. History with no marker is reported as an unproven boundary and "
                    "its absences are never reported as out-of-band changes."
                ),
                "analytics_semantics": (
                    "The reviewed LinkedIn analytics finder documents no pagination, so a "
                    "retained report is never exhaustive and absence of a row is not zero."
                ),
                "recommendations": "NOT_EXECUTED_NOT_A_PLAN_NO_AUTHORIZATION_DIGEST",
                "evidence_level": "FIXTURE_VERIFIED",
                "provider_ui_reconciled": False,
                "live_verified": False,
            },
            "output_policy": OutputPreferences().model_dump(mode="json"),
            "sharing": "Agent-visible output can be forwarded to the customer's selected model.",
            "credential_handoff": {
                "default": "NONINTERACTIVE",
                "secret_entry": "EXPLICIT_LOCAL_TERMINAL_ONLY",
                "provider_authentication_verified": False,
                "google_oauth": (
                    "EXPLICIT_LOCAL_BROWSER_READ_AND_GA4_ADMIN_GOOGLE_ADS_EDIT_OFFLINE_TESTED"
                ),
                "meta_token": (
                    "EXPLICIT_LOCAL_TERMINAL_SETUP_AND_FIXED_INSPECTION_DEVELOPMENT_TESTED"
                ),
            },
            "installed_approval_execution": {
                "commands": [
                    "execute openai-pause",
                    "execute ga4-key-event-create",
                    "execute meta-campaign-pause",
                ],
                "status": INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
                "native_setup_prerequisite": "REQUIRED",
                "live_provider_execution_verified": False,
                "automatic_execution_from_plan_or_artifact": False,
            },
            "meta_campaign_pause": {
                "planning": "RETAINED_DEVELOPMENT_FIXTURE_ONLY",
                "historical_review": "REDACTED_LOCAL_HISTORY_ONLY",
                "artifact_generation": "STATIC_REDACTED_LOCAL_HISTORY_ONLY",
                "execution": INSTALLED_APPROVAL_EXECUTION_COMMAND_STATUS,
                "approval_bridge": "SEPARATE_META_CAMPAIGN_PAUSE_V2_REQUIRED",
                "default_runtime_provider_mutations": False,
                "live_provider_execution_verified": False,
            },
            "google_ads_campaign_pause": {
                "planning": "RETAINED_DEVELOPMENT_FIXTURE_ONLY",
                "historical_review": "REDACTED_LOCAL_HISTORY_ONLY",
                "artifact_generation": "STATIC_REDACTED_LOCAL_HISTORY_ONLY",
                "execution": GOOGLE_ADS_PAUSE_EXECUTION_COMMAND_STATUS,
                "default_runtime_provider_mutations": False,
                "live_provider_execution_verified": False,
            },
            "execution_plan": {
                "planning": "GENERIC_RECORD_INSTALLED_NO_PROVIDER_COMPILER",
                # Rendered from the registry itself, never a hard-coded list, so
                # this cannot advertise an operation no planner compiles or omit
                # one a provider ticket registered.
                "compilable_operations": [dict(item) for item in compilable_execution_operations()],
                "refusal_code": "EXECUTION_PLAN_NOT_COMPILABLE",
                "historical_review": "REDACTED_LOCAL_HISTORY_ONLY",
                "artifact_generation": "NOT_IMPLEMENTED_USE_PER_PROVIDER_ARTIFACT_COMMANDS",
                "execution": GENERIC_EXECUTION_COMMAND_STATUS,
                "execution_command": "execute plan",
                "execution_selection": "POSITIONAL_RECORD_DIGEST_ONLY_NO_INPUT_FILE",
                # The same call-time derivation as the planner registry above,
                # from the separate execution-adapter registry: no adapter is
                # registered, so no retained plan is executable today.
                "executable_operations": [dict(item) for item in executable_execution_operations()],
                # The ordered gates an explicit `execute plan` passes, and the
                # code the first unsatisfied one produces.  Both are derived, so
                # this cannot name a refusal the caller never reaches: readiness
                # runs before the adapter lookup, not after it.
                "execution_refusal_gates": [
                    dict(item) for item in installed_execution_refusal_gates()
                ],
                "execution_first_refusal_code": first_execution_refusal_code(),
                "native_setup_prerequisite": "REQUIRED",
                "automatic_execution_from_plan_or_artifact": False,
                "default_runtime_provider_mutations": False,
                "live_provider_execution_verified": False,
            },
            "attempt_recovery": {
                "commands": ["recover attempt", "recover catalog"],
                "status": ATTEMPT_RECOVERY_COMMAND_STATUS,
                "selection": "POSITIONAL_RECORD_DIGEST_ONLY_NO_INPUT_FILE",
                "native_setup_prerequisite": "REQUIRED",
                "recorded_kind": "ATTEMPT_RECOVERY_OBSERVED",
                # A recovery reads through the same configured adapter an
                # execution would use, so it is refused for exactly the pairs
                # `execute plan` is refused for.
                "executable_operations": [dict(item) for item in executable_execution_operations()],
                "refusal_gates": [dict(item) for item in installed_execution_refusal_gates()],
                "first_refusal_code": first_execution_refusal_code(),
                "attribution": "NO_ACTOR_ATTRIBUTED",
                "barrier_scope": "GLOBAL_ACROSS_PROVIDERS_AND_ACCOUNTS",
                "settles_attempt": False,
                "authorizes_retry": False,
                "provider_mutations": False,
                "live_provider_execution_verified": False,
            },
            "release_state": RELEASE_STATE,
        },
    )


@app.command()
def schema(
    name: str,
    root_only: Annotated[bool, typer.Option("--root-only")] = False,
    definitions: Annotated[bool, typer.Option("--definitions")] = False,
    definition: Annotated[str | None, typer.Option("--definition")] = None,
) -> None:
    """Read a complete installed schema or digest-bound root/definition fragments."""

    def work() -> None:
        if name not in _SCHEMAS:
            raise AdhelmError("UNKNOWN_SCHEMA", "Schema is not installed.", exit_code=2)
        data = installed_schema_view(
            _SCHEMAS[name],
            name,
            root_only=root_only,
            definitions=definitions,
            definition=definition,
        )
        try:
            emit("schema", data)
        except AdhelmError as error:
            if error.code != "OUTPUT_LIMIT_EXCEEDED":
                raise
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Installed schema view exceeds the bounded discovery output.",
                next_action=(
                    "Use --root-only and --definitions, then request --definition NAME; "
                    "combine fragments only when their schema_digest matches."
                ),
            ) from error

    guarded("schema", work)


@app.command()
def init(ctx: typer.Context) -> None:
    """Create safe local defaults without credentials, delegation, or remote activity."""

    def work() -> None:
        save_configuration(root(ctx), Configuration())
        emit("init", {"configured": True, "provider_profiles": 0, "mutation_delegation": False})

    guarded("init", work)


@app.command()
def doctor(ctx: typer.Context, include_optional: bool = False) -> None:
    """Read-only local readiness; does not probe providers or credential stores."""

    def work() -> None:
        from adhelm.ledger import Ledger

        configuration = load_configuration(root(ctx))
        ledger = Ledger(root(ctx))
        verification = ledger.verify()
        unresolved = len(ledger.unresolved_starts())
        unresolved_runs = len(ledger.unresolved_runs())
        configured = {p.provider for p in configuration.profiles}
        checks = [
            {
                "provider": "ga4",
                "status": "CONFIGURED_UNVERIFIED"
                if "ga4" in configured
                else "NOT_CONFIGURED_REQUIRED",
                "release_label": _release_label_name("ga4"),
            }
        ]
        for provider in PROVIDERS:
            if provider == "ga4":
                continue
            if provider in configured or include_optional:
                checks.append(
                    {
                        "provider": provider,
                        "status": "CONFIGURED_UNVERIFIED"
                        if provider in configured
                        else "NOT_CONFIGURED_OPTIONAL",
                        "release_label": _release_label_name(provider),
                    }
                )
        emit(
            "doctor",
            {
                "local_history": verification,
                "providers": checks,
                # A record that is present but unusable leaves its provider
                # EXPERIMENTAL; it is reported here, not dropped silently.
                "pilot_records": [
                    {"file": status.file, "state": status.state, "provider": status.provider}
                    for status in pilot_record_inventory().statuses
                ],
                "remote_checks_performed": False,
                "unresolved_apply_starts": unresolved,
                "unresolved_composites": unresolved_runs,
                "mutation_readiness": "RECOVERY_REQUIRED"
                if unresolved or unresolved_runs
                else "NOT_EVALUATED",
                "release_state": RELEASE_STATE,
            },
            outcome="ATTENTION_REQUIRED"
            if unresolved or unresolved_runs or "ga4" not in configured
            else "LOCAL_ONLY",
        )
        if unresolved or unresolved_runs or "ga4" not in configured:
            raise typer.Exit(3)

    guarded("doctor", work)


@config_app.command("show")
def config_show(ctx: typer.Context) -> None:
    def work() -> None:
        configuration = load_configuration(root(ctx))
        emit(
            "config show",
            {
                "output": configuration.output.model_dump(mode="json"),
                "retention": configuration.retention.model_dump(mode="json"),
                "profiles": [
                    {"provider": p.provider, "purpose": p.purpose} for p in configuration.profiles
                ],
            },
        )

    guarded("config show", work)


@config_app.command("add-profile")
def add_profile(ctx: typer.Context, input_file: Annotated[Path, typer.Option("--input")]) -> None:
    """Add a nonsecret profile; never accepts credentials or changes delegation."""

    def work() -> None:
        from adhelm.models import validate_configured_profile_json

        profile = validate_configured_profile_json(read_json_input_file(input_file))
        update_configuration(
            root(ctx),
            lambda config: Configuration.model_validate(
                {**config.model_dump(), "profiles": [*config.profiles, profile]}
            ),
        )
        emit(
            "config add-profile",
            {"provider": profile.provider, "purpose": profile.purpose, "credential_stored": False},
        )

    guarded("config add-profile", work)


@history.command("verify")
def history_verify(ctx: typer.Context) -> None:
    def work() -> None:
        from adhelm.ledger import Ledger

        ledger = Ledger(root(ctx))
        verification = ledger.verify()
        unresolved = len(ledger.unresolved_starts())
        unresolved_runs = len(ledger.unresolved_runs())
        emit(
            "history verify",
            {
                **verification,
                "unresolved_apply_starts": unresolved,
                "unresolved_composites": unresolved_runs,
                "mutation_readiness": "RECOVERY_REQUIRED"
                if unresolved or unresolved_runs
                else "NOT_EVALUATED",
            },
            outcome="RECOVERY_REQUIRED" if unresolved or unresolved_runs else "OK",
        )
        if unresolved or unresolved_runs:
            raise typer.Exit(3)

    guarded("history verify", work)


@history.command("status")
def history_status(ctx: typer.Context) -> None:
    def work() -> None:
        from adhelm.ledger import Ledger

        ledger = Ledger(root(ctx))
        unresolved = len(ledger.unresolved_starts())
        unresolved_runs = len(ledger.unresolved_runs())
        emit(
            "history status",
            {
                "verification": ledger.verify(),
                "unresolved_starts": unresolved,
                "unresolved_composites": unresolved_runs,
                "mutation_readiness": "RECOVERY_REQUIRED"
                if unresolved or unresolved_runs
                else "NOT_EVALUATED",
            },
            outcome="RECOVERY_REQUIRED" if unresolved or unresolved_runs else "OK",
        )
        if unresolved or unresolved_runs:
            raise typer.Exit(3)

    guarded("history status", work)


@history.command("tail")
def history_tail(
    ctx: typer.Context, stream: str = "changes", limit: int = 50, cursor: int = 0
) -> None:
    def work() -> None:
        from adhelm.ledger import Ledger

        if (
            stream not in {"changes", "creative", "performance"}
            or not 1 <= limit <= 500
            or cursor < 0
        ):
            raise AdhelmError(
                "INVALID_PAGE",
                "Expected valid stream, nonnegative cursor, limit 1–500.",
                exit_code=2,
            )
        records = Ledger(root(ctx)).records(stream)
        page = records[cursor : cursor + limit]
        # Detail lookups are deliberately separate from safe summary output.
        selected = [
            {
                key: record[key]
                for key in ("record_kind", "stream_sequence", "record_digest", "recorded_at")
            }
            for record in page
        ]
        next_cursor = str(cursor + limit) if cursor + limit < len(records) else None
        emit(
            "history tail", {"records": selected}, next_cursor=next_cursor, total_count=len(records)
        )

    guarded("history tail", work)


@history.command("index")
def history_index(ctx: typer.Context) -> None:
    def work() -> None:
        from adhelm.ledger import Ledger

        emit("history index", Ledger(root(ctx)).rebuild_index())

    guarded("history index", work)


@history.command("export")
def history_export(ctx: typer.Context, output: Annotated[Path, typer.Option("--output")]) -> None:
    """Create a deterministic redacted JSONL/CSV bundle in a new local directory."""

    def work() -> None:
        from adhelm.credentials import export_key
        from adhelm.ledger import Ledger
        from adhelm.projections import export_verified

        manifest = export_verified(Ledger(root(ctx)), output, opaque_key=export_key())
        emit(
            "history export",
            {
                "manifest_digest": manifest["manifest_digest"],
                "file_count": len(manifest["files"]),
                "output": str(output.resolve()),
            },
        )

    guarded("history export", work)


def _report(
    ctx: typer.Context,
    provider: str,
    profile: str,
    input_file: Path,
    stream_id: str | None,
    ephemeral: bool,
    ci: bool,
    limit: int | None,
) -> None:
    from typing import cast

    from adhelm.workflows import ReportProvider, run_report

    def work() -> None:
        preferences = load_configuration(root(ctx)).output
        selected_limit = preferences.max_rows if limit is None else limit
        if not 1 <= selected_limit <= 500:
            raise AdhelmError(
                "INVALID_PAGE", "Report output limit must be between 1 and 500.", exit_code=2
            )
        report = run_report(
            root(ctx),
            cast(ReportProvider, provider),
            profile,
            input_file,
            stream_id=stream_id,
            ephemeral=ephemeral,
            ci_enabled=ci,
        )
        counts = {key: len(report[key]) for key in ("performance_rows", "conversion_rows")}
        total = max(counts.values())
        for key in counts:
            report[key] = report[key][:selected_limit]
        report["row_counts"] = counts
        report["observation_persisted"] = not ephemeral
        if total > selected_limit:
            report["remaining_rows"] = total - selected_limit
            report["next_action"] = "Use history export for all retained rows, or a narrower query."
        artifact_failure = _artifact_generation_failure(report)
        outcome = (
            "PARTIAL"
            if artifact_failure is not None
            else "PARTIAL_OUTPUT"
            if total > selected_limit
            else "OK"
        )
        try:
            emit(
                "report " + provider,
                report,
                total_count=total,
                outcome=outcome,
                maximum_bytes=preferences.max_output_bytes,
            )
        except AdhelmError as exc:
            if exc.code != "OUTPUT_LIMIT_EXCEEDED":
                raise
            compact, artifact_refs = _compact_report_output(report, total, artifact_failure)
            try:
                emit(
                    "report " + provider,
                    compact,
                    total_count=total,
                    outcome="PARTIAL",
                    maximum_bytes=preferences.max_output_bytes,
                    artifact_refs=artifact_refs,
                )
            except AdhelmError as compact_error:
                if compact_error.code != "OUTPUT_LIMIT_EXCEEDED":
                    raise
                emit(
                    "report " + provider,
                    _minimal_report_output(report, artifact_failure, artifact_refs),
                    total_count=total,
                    outcome="PARTIAL",
                    maximum_bytes=preferences.max_output_bytes,
                    artifact_refs=artifact_refs,
                )
            raise typer.Exit(5) from None
        if artifact_failure is not None:
            raise typer.Exit(5)

    guarded("report " + provider, work)


def _artifact_generation_failure(report: dict[str, Any]) -> str | None:
    artifact_result = report.get("artifact_result")
    if not isinstance(artifact_result, dict):
        return None
    failure = artifact_result.get("generation_failure_code")
    if isinstance(failure, str) and 1 <= len(failure) <= 80 and failure.isascii():
        return failure
    return None


def _safe_digest(value: object) -> str | None:
    if (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    ):
        return value
    return None


def _compact_report_output(
    report: dict[str, Any], total: int, artifact_failure: str | None
) -> tuple[dict[str, Any], list[str]]:
    """Keep retained evidence and generated-file references visible under output bounds."""
    artifact_result = report.get("artifact_result")
    source_record_digest = _safe_digest(report.get("retained_record_digest"))
    compact_artifacts: dict[str, Any] = {
        "generation_failure_code": artifact_failure,
        "artifact_count": 0,
    }
    artifact_refs: list[str] = []
    if isinstance(artifact_result, dict):
        for name in ("source_record_digest", "checkpoint_digest"):
            value = _safe_digest(artifact_result.get(name))
            if value is not None:
                compact_artifacts[name] = value
        policy = artifact_result.get("effective_policy")
        if isinstance(policy, dict):
            outcome = policy.get("outcome")
            reason = policy.get("reason")
            if outcome in {"GENERATE", "SUPPRESSED", "NEEDS_CONFIGURATION"}:
                compact_artifacts["effective_policy_outcome"] = outcome
            if isinstance(reason, str) and 1 <= len(reason) <= 80 and reason.isascii():
                compact_artifacts["effective_policy_reason"] = reason
        artifacts = artifact_result.get("artifacts")
        if isinstance(artifacts, list):
            for item in artifacts[:3]:
                if not isinstance(item, dict):
                    continue
                artifact_digest = _safe_digest(item.get("artifact_digest"))
                if artifact_digest is not None:
                    artifact_refs.append("artifact:sha256:" + artifact_digest)
    compact_artifacts["artifact_count"] = len(artifact_refs)
    return (
        {
            "retained_record_digest": source_record_digest,
            "observation_persisted": bool(report.get("observation_persisted")),
            "row_counts": {
                "performance_rows": total,
                "conversion_rows": len(report.get("conversion_rows", []))
                if isinstance(report.get("conversion_rows"), list)
                else None,
            },
            "output_limit_exceeded": True,
            "full_result_retained": source_record_digest is not None,
            "artifact_result": compact_artifacts,
            "next_action": (
                "Use report show with the retained record digest, a narrower report query, "
                "or the configured local artifact destination."
            ),
        },
        artifact_refs,
    )


def _minimal_report_output(
    report: dict[str, Any], artifact_failure: str | None, artifact_refs: list[str]
) -> dict[str, Any]:
    """Minimum partial result that still identifies retained and published evidence.

    Configurations may use the 1 KiB minimum output bound. The rich bounded
    result can legitimately exceed that with three artifact digests, so this
    final tier intentionally retains only facts needed to continue safely.
    """
    source_record_digest = _safe_digest(report.get("retained_record_digest"))
    return {
        "retained_record_digest": source_record_digest,
        "observation_persisted": bool(report.get("observation_persisted")),
        "output_limit_exceeded": True,
        "full_result_retained": source_record_digest is not None,
        "artifact_result": {
            "generation_failure_code": artifact_failure,
            "artifact_count": len(artifact_refs),
        },
        "next_action": "Use the retained record or artifact reference.",
    }


@reports_app.command("ga4")
def ga4_report(
    ctx: typer.Context,
    profile: str,
    input_file: Annotated[Path, typer.Option("--input")],
    stream_id: str,
    ephemeral: bool = False,
    ci: bool = False,
    limit: int | None = None,
) -> None:
    """Read the selected GA4 property through its configured read profile."""
    _report(ctx, "ga4", profile, input_file, stream_id, ephemeral, ci, limit)


@reports_app.command("openai-ads")
def openai_report(
    ctx: typer.Context,
    profile: str,
    input_file: Annotated[Path, typer.Option("--input")],
    ephemeral: bool = False,
    ci: bool = False,
    limit: int | None = None,
) -> None:
    """Read bounded daily OpenAI campaign aggregates through the selected profile."""
    _report(ctx, "openai_ads", profile, input_file, None, ephemeral, ci, limit)


@reports_app.command("catalog")
def report_catalog_command(
    ctx: typer.Context, provider: str | None = None, cursor: int = 0, limit: int | None = None
) -> None:
    """List the latest retained report revisions without repeating provider reads."""

    def work() -> None:
        from adhelm.ledger import Ledger
        from adhelm.saved_reports import report_catalog

        selected_limit, maximum_bytes = _page_preferences(ctx, cursor, limit)
        result = report_catalog(
            Ledger(root(ctx)), provider=provider, cursor=cursor, limit=selected_limit
        )
        emit(
            "report catalog",
            result,
            total_count=result["total_count"],
            next_cursor=result["next_cursor"],
            maximum_bytes=maximum_bytes,
        )

    guarded("report catalog", work)


@reports_app.command("show")
def report_show_command(
    ctx: typer.Context,
    record_digest: str,
    cursor: int = 0,
    limit: int | None = None,
    include_campaign_names: bool = False,
) -> None:
    """Read a specific retained report revision through the redacted sharing boundary."""

    def work() -> None:
        from adhelm.credentials import export_key
        from adhelm.ledger import Ledger
        from adhelm.report_sharing import report_for_output
        from adhelm.saved_reports import load_report

        selected_limit, maximum_bytes = _page_preferences(ctx, cursor, limit)
        report, provenance = load_report(Ledger(root(ctx)), record_digest)
        data = report_for_output(
            report, export_key(), include_campaign_names=include_campaign_names
        )
        count = max(len(data["performance_rows"]), len(data["conversion_rows"]))
        totals = {key: len(data[key]) for key in ("performance_rows", "conversion_rows")}
        for key in totals:
            data[key] = data[key][cursor : cursor + selected_limit]
        emit(
            "report show",
            {**data, "provenance": provenance, "row_counts": totals},
            total_count=count,
            next_cursor=str(cursor + selected_limit) if cursor + selected_limit < count else None,
            maximum_bytes=maximum_bytes,
        )

    guarded("report show", work)


@app.command("audit")
def audit_command(
    ctx: typer.Context,
    record_digest: str,
    input_file: Annotated[Path, typer.Option("--input")],
    at: str | None = None,
    cursor: int = 0,
    limit: int | None = None,
    provider_rules: Annotated[bool, typer.Option("--provider-rules")] = False,
    account: Annotated[str | None, typer.Option("--account")] = None,
    inventory_digest: Annotated[str | None, typer.Option("--inventory-digest")] = None,
    drift: Annotated[bool, typer.Option("--drift/--no-drift")] = True,
) -> None:
    """Audit verified retained evidence against customer-declared thresholds."""
    command = "audit --provider-rules" if provider_rules else "audit"

    def work() -> None:
        from datetime import UTC, datetime

        from adhelm.audit import audit_provider, audit_report
        from adhelm.canonical import digest
        from adhelm.credentials import export_key
        from adhelm.ledger import Ledger
        from adhelm.projections import _redact_payload
        from adhelm.saved_reports import load_report

        selected_limit, maximum_bytes = _page_preferences(ctx, cursor, limit)
        if not provider_rules and (
            account is not None or inventory_digest is not None or not drift
        ):
            raise AdhelmError(
                "INVALID_AUDIT_SELECTION",
                "Retained inventory selection applies only to `--provider-rules`.",
                exit_code=2,
            )
        policy = validate_json_file(AuditPolicy, input_file)
        now = datetime.now(UTC)
        try:
            as_of = datetime.fromisoformat(at) if at is not None else now
            if as_of.tzinfo is None or as_of > now:
                raise ValueError("Invalid audit evaluation time")
        except ValueError as exc:
            raise AdhelmError(
                "INVALID_AUDIT_TIME", "Audit time must be aware and not in the future.", exit_code=2
            ) from exc
        ledger = Ledger(root(ctx))
        report, provenance = load_report(ledger, record_digest)
        if provider_rules:
            # The provider rule table reads retained records only. No provider
            # request, credential read, or ledger append happens on this path.
            from adhelm.saved_audit_evidence import load_audit_evidence

            # Evidence defaults to the account the audited report describes; an
            # explicit `--account` that disagrees is refused rather than mixed.
            evidence, evidence_provenance = load_audit_evidence(
                ledger,
                provider=report.envelope.provider,
                account_ref=account or report.envelope.source_account_ref,
                inventory_record_digest=inventory_digest,
                include_earlier=drift,
            )
            provider_result = audit_provider(
                report,
                source_record_digest=record_digest,
                policy=policy,
                as_of=as_of,
                evidence=evidence,
            )
            data = _redact_payload(provider_result.model_dump(mode="json"), export_key(), False)
            sections = data["sections"]
            section_keys = (
                "findings",
                "recommendations",
                "no_recommendations",
                "drift_observations",
                "bound_exceeded",
            )
            audit_keys = ("findings", "recommendations")
            full_sections = (
                {key: list(sections[key]) for key in section_keys} if sections is not None else {}
            )
            full_audit = {key: list(data["audit"][key]) for key in audit_keys}
            totals = {
                **{key: len(value) for key, value in full_sections.items()},
                **{f"audit_{key}": len(value) for key, value in full_audit.items()},
            }
            count = max(totals.values())
            data.update(
                {
                    "provider_audit_digest": digest(provider_result),
                    "provenance": {**provenance, **evidence_provenance},
                    "row_counts": totals,
                    "next_page_evaluation_time": as_of.isoformat(),
                }
            )
            # Serve the largest page that fits the saved byte bound. A smaller
            # page is paging, not omission: `row_counts` stays complete and the
            # next cursor names exactly where this page stopped.
            page = selected_limit
            while True:
                for key, value in full_sections.items():
                    sections[key] = value[cursor : cursor + page]
                for key, value in full_audit.items():
                    data["audit"][key] = value[cursor : cursor + page]
                next_cursor = str(cursor + page) if cursor + page < count else None
                if _fits(
                    command,
                    data,
                    maximum_bytes,
                    outcome="INCOMPLETE_COVERAGE",
                    total_count=count,
                    next_cursor=next_cursor,
                ):
                    break
                if page <= 1:
                    raise AdhelmError(
                        "OUTPUT_LIMIT_EXCEEDED",
                        "One provider audit row exceeds the saved output bound.",
                        next_action="Raise the saved output byte preference; no row was omitted.",
                    )
                page = max(1, page // 2)
            emit(
                command,
                data,
                outcome="INCOMPLETE_COVERAGE",
                total_count=count,
                next_cursor=next_cursor,
                maximum_bytes=maximum_bytes,
            )
            return
        result = audit_report(
            report, source_record_digest=record_digest, policy=policy, as_of=as_of
        )
        data = _redact_payload(result.model_dump(mode="json"), export_key(), False)
        count = max(len(data["findings"]), len(data["recommendations"]))
        totals = {key: len(data[key]) for key in ("findings", "recommendations")}
        for key in totals:
            data[key] = data[key][cursor : cursor + selected_limit]
        data.update(
            {
                "audit_digest": digest(result),
                "provenance": provenance,
                "row_counts": totals,
                "next_page_evaluation_time": as_of.isoformat(),
            }
        )
        emit(
            "audit",
            data,
            outcome="INCOMPLETE_COVERAGE",
            total_count=count,
            next_cursor=str(cursor + selected_limit) if cursor + selected_limit < count else None,
            maximum_bytes=maximum_bytes,
        )

    guarded(command, work)


@reports_app.command("google-ads")
def google_report(
    ctx: typer.Context,
    profile: str,
    input_file: Annotated[Path, typer.Option("--input")],
    ephemeral: bool = False,
    ci: bool = False,
    limit: int | None = None,
) -> None:
    """Read fixed Google Ads daily metrics with separate OAuth/developer credentials."""
    _report(ctx, "google_ads", profile, input_file, None, ephemeral, ci, limit)


@reports_app.command("meta-ads")
def meta_report(
    ctx: typer.Context,
    profile: str,
    input_file: Annotated[Path, typer.Option("--input")],
    ephemeral: bool = False,
    ci: bool = False,
    limit: int | None = None,
) -> None:
    """Read fixed Meta daily metrics at the declared campaign or ad grain."""
    _report(ctx, "meta_ads", profile, input_file, None, ephemeral, ci, limit)


@reports_app.command("linkedin-ads")
def linkedin_report(
    ctx: typer.Context,
    profile: str,
    input_file: Annotated[Path, typer.Option("--input")],
    ephemeral: bool = False,
    ci: bool = False,
    limit: int | None = None,
) -> None:
    """Read fixed LinkedIn daily UTC metrics with explicit completeness limits."""
    _report(ctx, "linkedin_ads", profile, input_file, None, ephemeral, ci, limit)


@reports_app.command("tiktok-ads")
def tiktok_report(
    ctx: typer.Context,
    profile: str,
    input_file: Annotated[Path, typer.Option("--input")],
    ephemeral: bool = False,
    ci: bool = False,
    limit: int | None = None,
) -> None:
    """Read bounded TikTok daily campaign/ad delivery through a READ profile."""
    _report(ctx, "tiktok_ads", profile, input_file, None, ephemeral, ci, limit)


@reports_app.command("reddit-ads")
def reddit_report(
    ctx: typer.Context,
    profile: str,
    input_file: Annotated[Path, typer.Option("--input")],
    ephemeral: bool = False,
    ci: bool = False,
    limit: int | None = None,
) -> None:
    """Read bounded Reddit UTC delivery with exact native-spend provenance."""
    _report(ctx, "reddit_ads", profile, input_file, None, ephemeral, ci, limit)


@app.command("inventory")
def inventory_command(
    ctx: typer.Context,
    provider: str,
    profile: str,
    ephemeral: bool = False,
    ci: bool = False,
    maximum_records: int = 500,
    limit: int | None = None,
) -> None:
    """Read configured campaign hierarchy and retain normalized snapshots."""

    def work() -> None:
        from typing import cast, get_args

        from adhelm.inventory import InventoryProvider, run_inventory

        if provider not in get_args(InventoryProvider):
            raise AdhelmError(
                "INVENTORY_CAPABILITY_UNAVAILABLE", "This provider inventory is not implemented."
            )

        selected_limit, maximum_bytes = _page_preferences(ctx, 0, limit)
        result = run_inventory(
            root(ctx),
            cast(InventoryProvider, provider),
            profile,
            ephemeral=ephemeral,
            ci_enabled=ci,
            maximum_records=maximum_records,
        )
        totals = {
            key: len(result[key])
            for key in ("resources", "snapshot_record_digests", "change_record_digests")
        }
        for key in totals:
            result[key] = result[key][:selected_limit]
        partial = any(count > selected_limit for count in totals.values())
        result["result_counts"] = totals
        if partial:
            result["next_action"] = (
                "Use history export for the complete retained inventory."
                if not ephemeral
                else "Increase the explicit output limit to review more ephemeral resources."
            )
        emit(
            "inventory",
            result,
            outcome="PARTIAL_OUTPUT" if partial else "OK",
            maximum_bytes=maximum_bytes,
            total_count=totals["resources"],
        )

    guarded("inventory", work)


def _page_preferences(ctx: typer.Context, cursor: int, limit: int | None) -> tuple[int, int]:
    preferences = load_configuration(root(ctx)).output
    selected_limit = preferences.max_rows if limit is None else limit
    if cursor < 0 or not 1 <= selected_limit <= 500:
        raise AdhelmError(
            "INVALID_PAGE", "Expected nonnegative cursor and limit 1–500.", exit_code=2
        )
    return selected_limit, preferences.max_output_bytes


@creative_app.command("analyze")
def creative_analyze(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    ephemeral: bool = False,
) -> None:
    """Extract deterministic local raster-image features; no upload or model call."""

    def work() -> None:
        from adhelm.creative import MAX_IMAGE_BYTES

        try:
            data = read_regular_input_file(input_file, maximum_bytes=MAX_IMAGE_BYTES)
        except AdhelmError as error:
            if error.code != "INPUT_TOO_LARGE":
                raise
            raise AdhelmError(
                "CREATIVE_MEDIA_TOO_LARGE",
                "Image bytes exceed the configured analysis bound.",
                exit_code=2,
            ) from None
        # Decode before checking the exact compact receipt.  Decoder work has no
        # vault or ledger effect; all writes occur only after the receipt fits.
        asset, analysis = analyze_local_image(data)
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        preview = creative_image_analysis_receipt_preview(asset, analysis, persisted=not ephemeral)
        if not _fits("creative analyze", preview, maximum):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Creative analysis receipt exceeds the configured output bound.",
                exit_code=2,
            )
        if not ephemeral:
            receipt = retain_local_image_analysis(directory, data, prepared=(asset, analysis))
        else:
            receipt = ephemeral_local_image_analysis(data)
        emit(
            "creative analyze",
            receipt.model_dump(mode="json", warnings="error"),
            maximum_bytes=maximum,
        )

    guarded("creative analyze", work)


@creative_app.command("register")
def creative_register(
    ctx: typer.Context, input_file: Annotated[Path, typer.Option("--input")]
) -> None:
    """Register one local identity image or declared text-only creative version."""

    def work() -> None:
        request = validate_json_file(CreativeRegistrationRequest, input_file)
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        if not _fits("creative register", creative_registration_receipt_preview(), maximum):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Creative registration receipt exceeds the configured output bound.",
                exit_code=2,
            )
        receipt = register_creative_request(directory, request)
        emit(
            "creative register",
            receipt.model_dump(mode="json", warnings="error"),
            maximum_bytes=maximum,
        )

    guarded("creative register", work)


@creative_app.command("catalog")
def creative_catalog(ctx: typer.Context, limit: int | None = None, cursor: int = 0) -> None:
    def work() -> None:
        directory = root(ctx)
        preferences = load_configuration(directory).output
        selected_limit = preferences.max_rows if limit is None else limit
        page, total, next_cursor = creative_catalog_entries(
            directory, cursor=cursor, limit=selected_limit
        )
        emit(
            "creative catalog",
            {"versions": page},
            maximum_bytes=preferences.max_output_bytes,
            total_count=total,
            next_cursor=next_cursor,
        )

    guarded("creative catalog", work)


@creative_app.command("show")
def creative_show(ctx: typer.Context, record_digest: str) -> None:
    """Show a redacted historical local creative version from one checkpoint."""

    def work() -> None:
        directory = root(ctx)
        maximum = load_configuration(directory).output.max_output_bytes
        data = creative_show_data(directory, record_digest)
        emit("creative show", data, maximum_bytes=maximum)

    guarded("creative show", work)


@measurement_app.command("validate-event")
def measurement_event(input_file: Annotated[Path, typer.Option("--input")]) -> None:
    """Validate a customer-declared event schema locally, without collection or egress."""

    def work() -> None:
        from adhelm.canonical import digest

        event = validate_json_file(EventContract, input_file)
        emit(
            "measurement validate-event",
            {
                "contract_digest": digest(event),
                "field_count": len(event.fields),
                "validation_scope": "LOCAL_DECLARATION_ONLY",
                "remote_write_authorized": False,
                "live_evidence_verified": False,
            },
        )

    guarded("measurement validate-event", work)


@measurement_app.command("check-stage")
def measurement_stage(input_file: Annotated[Path, typer.Option("--input")]) -> None:
    """Check supplied GTM inventory preconditions; does not discover or stage resources."""

    def work() -> None:
        from adhelm.canonical import digest
        from adhelm.measurement import validate_stage_gate

        inventory = validate_json_file(ContainerInventory, input_file)
        validate_stage_gate(inventory)
        emit(
            "measurement check-stage",
            {
                "inventory_digest": digest(inventory),
                "validation_scope": "LOCAL_DECLARATION_ONLY",
                "remote_write_authorized": False,
                "live_evidence_verified": False,
                "dedicated_workspace_required": True,
            },
        )

    guarded("measurement check-stage", work)


def main() -> None:
    try:
        result = app(standalone_mode=False)
        if isinstance(result, int):
            sys.exit(result)
    except click.ClickException:
        try:
            fail(
                "parse",
                AdhelmError(
                    "INVALID_COMMAND",
                    "Unknown command, option, or invalid argument.",
                    exit_code=2,
                    next_action="Run `adhelm discover` or `adhelm --help`.",
                ),
            )
        except typer.Exit as error:
            sys.exit(error.exit_code)
    except typer.Exit as error:
        sys.exit(error.exit_code)
    except (KeyboardInterrupt, typer.Abort):
        sys.exit(130)
    except Exception:
        # Never expose a traceback that could include rejected input or bearer credentials.
        try:
            fail(
                "internal",
                AdhelmError(
                    "INTERNAL_ERROR",
                    "Unexpected internal failure.",
                    exit_code=10,
                    next_action="Inspect local history before retrying any write.",
                ),
            )
        except typer.Exit as error:
            sys.exit(error.exit_code)


if __name__ == "__main__":
    main()
