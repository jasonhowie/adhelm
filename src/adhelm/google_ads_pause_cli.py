"""Public local planning and historical review for Google Ads campaign PAUSE.

This module deliberately has no provider mutation or approval path. It exposes
the existing guarded configured planner through a bounded CLI and, when the
saved or one-task output policy resolves to generation, delegates optional
static files to the separate retained-plan artifact renderer.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import typer
from pydantic import Field, field_validator, model_validator

from adhelm.artifacts import ArtifactDecision, ArtifactTaskOverride
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_pause_plan_records import google_ads_campaign_pause_plan_record
from adhelm.google_ads_pause_plan_views import (
    GoogleAdsCampaignPausePlanReview,
    review_google_ads_campaign_pause_plan,
)
from adhelm.google_ads_pause_workflow import GoogleAdsCampaignPausePlanRequest
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.models import Money, OutputPreferences, StrictModel
from adhelm.saved_google_ads_pause_plans import (
    google_ads_campaign_pause_plan_catalog,
    load_google_ads_campaign_pause_plan,
)
from adhelm.strict_json import validate_json_value

app = typer.Typer(no_args_is_help=True)

_PREFIX = "plan google-ads-campaign-pause"
_CREATE_OUTCOME = "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_RETAINED_NO_EXECUTION_AUTHORITY"
_HISTORY_OUTCOME = "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_HISTORY_NO_EXECUTION_AUTHORITY"
_DIGEST = re.compile(r"^[a-f0-9]{64}$", re.ASCII)
_MAX_UTC_TIMESTAMP = datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC)
_VIEW_MAXIMUM_BYTES = 65_536
_Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
_SuppressionReason = Literal[
    "ARTIFACT_MODE_OFF",
    "NO_MATCHING_WORKFLOW_RULE",
    "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST",
    "PURPOSE_NOT_ENABLED",
    "TASK_OVERRIDE_DISABLED",
]
_SUPPRESSION_REASONS = frozenset(
    {
        "ARTIFACT_MODE_OFF",
        "NO_MATCHING_WORKFLOW_RULE",
        "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST",
        "PURPOSE_NOT_ENABLED",
        "TASK_OVERRIDE_DISABLED",
    }
)
_GENERATION_REASONS = frozenset({"EXPLICIT_USER_REQUEST", "MATCHING_SAVED_RULE"})
_ARTIFACT_COMMAND = "artifacts google-ads-campaign-pause"


class _CatalogRow(StrictModel):
    record_digest: _Digest
    plan_digest: _Digest
    recorded_at: datetime
    expires_at: datetime
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    maximum_incremental_spend: Money
    execution_authority: Literal[False] = False

    @field_validator("recorded_at", "expires_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("catalog timestamp must be aware")
        return value.astimezone(UTC)

    @field_validator("execution_authority", mode="before")
    @classmethod
    def false_only(cls, value: object) -> bool:
        if type(value) is not bool or value is not False:
            raise ValueError("execution authority must be false")
        return value


class _Catalog(StrictModel):
    plans: list[_CatalogRow] = Field(max_length=500)
    checkpoint_digest: _Digest
    total_count: int = Field(ge=0)
    next_cursor: str | None
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]

    @field_validator("next_cursor", mode="before")
    @classmethod
    def cursor(cls, value: object) -> str | None:
        if value is None:
            return None
        if type(value) is not str or not value.isascii() or not value.isdecimal():
            raise ValueError("invalid cursor")
        return value


class _Provenance(StrictModel):
    record_digest: _Digest
    action_digest: _Digest
    plan_digest: _Digest
    checkpoint_digest: _Digest
    recorded_at: datetime
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    execution_authority: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("recorded_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("provenance timestamp must be aware")
        return value.astimezone(UTC)


class _ArtifactNotice(StrictModel):
    """Closed saved/task output-policy fact beside the retained plan receipt."""

    outcome: Literal["GENERATE", "SUPPRESSED"]
    reason: str
    generation_supported: Literal[True] = True

    @field_validator("generation_supported", mode="before")
    @classmethod
    def exact_generation_support(cls, value: object) -> bool:
        if type(value) is not bool or value is not True:
            raise ValueError("artifact support marker is invalid")
        return value

    @model_validator(mode="after")
    def exact_reason_for_outcome(self) -> _ArtifactNotice:
        allowed = _GENERATION_REASONS if self.outcome == "GENERATE" else _SUPPRESSION_REASONS
        if self.reason not in allowed:
            raise ValueError("artifact policy reason is invalid")
        return self


class _Receipt(StrictModel):
    record_digest: _Digest
    action_digest: _Digest
    plan_digest: _Digest
    subplan_digests: tuple[_Digest, ...] = Field(min_length=1, max_length=1)
    expires_at: datetime
    capability_status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"]
    maximum_incremental_spend: Money
    historical_evidence_status: Literal["HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"]
    review_required: Literal[True] = True
    review_command: str = Field(min_length=1, max_length=180)
    artifact_policy: _ArtifactNotice
    execution_authority: Literal[False] = False
    remote_mutations: Literal[False] = False
    review_authorizes_execution: Literal[False] = False
    requires_current_kernel_checks: Literal[True] = True

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("receipt expiry must be aware")
        return value.astimezone(UTC)


def _require_digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_SELECTION",
            "An exact retained Google Ads PAUSE record digest is required.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return value


def _artifact_invalid() -> AdhelmError:
    return AdhelmError(
        "INVALID_GOOGLE_ADS_PAUSE_ARTIFACT_REQUEST",
        "Google Ads PAUSE artifact options are inconsistent.",
        exit_code=ExitCode.INVALID_INPUT,
        next_action=(
            "Use --no-artifacts alone, or supply one or more formats with a local output directory."
        ),
    )


def _override(no_artifacts: object, formats: object, output: object) -> ArtifactTaskOverride | None:
    if type(no_artifacts) is not bool:
        raise _artifact_invalid()
    if formats is None:
        values: tuple[Literal["json", "csv", "html"], ...] = ()
    elif (
        type(formats) is list
        and len(formats) <= 3
        and len(set(formats)) == len(formats)
        and all(type(x) is str and x in {"json", "csv", "html"} for x in formats)
    ):
        values = cast(tuple[Literal["json", "csv", "html"], ...], tuple(sorted(formats)))
    else:
        raise _artifact_invalid()
    if output is not None and not isinstance(output, Path):
        raise _artifact_invalid()
    if no_artifacts:
        if values or output is not None:
            raise _artifact_invalid()
        return ArtifactTaskOverride(requested=False, purpose="plan_review")
    if values or output is not None:
        return ArtifactTaskOverride(
            requested=True,
            formats=values,
            output_directory=str(output) if output else None,
            purpose="plan_review",
        )
    return None


def _artifact_policy_invalid() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_POLICY_INVALID",
        "Google Ads PAUSE artifact policy is invalid.",
        exit_code=ExitCode.INVALID_INPUT,
    )


def _artifact_decision(
    preferences: OutputPreferences, profile_id: str, override: ArtifactTaskOverride | None
) -> ArtifactDecision:
    """Resolve the closed output policy before a Google read or plan retention."""
    from adhelm.google_ads_pause_plan_artifacts import (
        preflight_google_ads_campaign_pause_plan_artifacts,
    )

    if type(preferences) is not OutputPreferences or type(profile_id) is not str:
        raise _artifact_policy_invalid()
    try:
        decision = preflight_google_ads_campaign_pause_plan_artifacts(
            preferences,
            profile_id=profile_id,
            task_override=override,
        )
    except AdhelmError as error:
        if error.code != "ARTIFACT_CONFIGURATION_REQUIRED":
            raise
        # The rule or request selects generation but cannot name a complete
        # local destination. Say so, and name the installed command that
        # renders the same static history from an already retained plan.
        raise AdhelmError(
            "ARTIFACT_CONFIGURATION_REQUIRED",
            "The selected Google Ads PAUSE artifact policy needs local configuration.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Configure supported formats and an existing local output directory, use "
                f"--no-artifacts, or render a retained plan later with {_ARTIFACT_COMMAND}."
            ),
        ) from None
    if type(decision) is not ArtifactDecision or decision.outcome not in {
        "GENERATE",
        "SUPPRESSED",
    }:
        raise _artifact_policy_invalid()
    allowed = _GENERATION_REASONS if decision.outcome == "GENERATE" else _SUPPRESSION_REASONS
    if type(decision.reason) is not str or decision.reason not in allowed:
        raise _artifact_policy_invalid()
    return decision


def _artifact_notice(
    preferences: OutputPreferences, profile_id: str, override: ArtifactTaskOverride | None
) -> _ArtifactNotice:
    """Return the closed policy fact, dropping resolved paths and settings."""
    return _artifact_policy_notice(_artifact_decision(preferences, profile_id, override))


def _artifact_policy_notice(decision: object) -> _ArtifactNotice:
    try:
        if type(decision) is not ArtifactDecision:
            raise ValueError("decision")
        if decision.outcome == "GENERATE":
            outcome: Literal["GENERATE", "SUPPRESSED"] = "GENERATE"
        elif decision.outcome == "SUPPRESSED":
            outcome = "SUPPRESSED"
        else:
            raise ValueError("decision outcome")
        return _ArtifactNotice(outcome=outcome, reason=decision.reason)
    except Exception:
        raise _artifact_policy_invalid() from None


def _artifact_source_matches_receipt(receipt: object, source: object) -> None:
    """Bind optional files to the exact durable receipt without serializing source facts."""
    try:
        from adhelm.google_ads_pause_plan_artifacts import (
            GoogleAdsCampaignPausePlanArtifactProvenance,
        )

        if type(receipt) is not dict or (
            type(source) is not GoogleAdsCampaignPausePlanArtifactProvenance
        ):
            raise ValueError("receipt")
        expected = {
            "record_digest": receipt["record_digest"],
            "action_digest": receipt["action_digest"],
            "plan_digest": receipt["plan_digest"],
        }
        if any(
            type(value) is not str or _DIGEST.fullmatch(value) is None
            for value in expected.values()
        ):
            raise ValueError("receipt digest")
        if expected != {
            "record_digest": source.record_digest,
            "action_digest": source.action_digest,
            "plan_digest": source.plan_digest,
        }:
            raise ValueError("source mismatch")
    except Exception:
        raise AdhelmError(
            "GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_SOURCE_INVALID",
            "The retained Google Ads PAUSE source cannot be rendered safely.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _create_result_data(
    receipt: dict[str, object], *, artifact_result: dict[str, object] | None = None
) -> dict[str, object]:
    """Keep the durable plan receipt visible if optional artifact work fails."""
    return receipt if artifact_result is None else {**receipt, "artifact_result": artifact_result}


def _receipt(
    record_digest: str,
    action_digest: str,
    plan_digest: str,
    subplans: tuple[str, ...],
    expires_at: datetime,
    status: Literal["DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED"],
    spend: Money,
    artifact: _ArtifactNotice,
) -> dict[str, object]:
    return _Receipt(
        record_digest=record_digest,
        action_digest=action_digest,
        plan_digest=plan_digest,
        subplan_digests=subplans,
        expires_at=expires_at,
        capability_status=status,
        maximum_incremental_spend=spend,
        historical_evidence_status="HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
        review_command=f"{_PREFIX} show {record_digest}",
        artifact_policy=artifact,
    ).model_dump(mode="json", warnings="error")


def _require_receipt_fit(
    data: dict[str, object], maximum: int, *, artifact_refs: list[str] | None = None
) -> None:
    if not _fits(
        _PREFIX + " create",
        data,
        maximum,
        outcome=_CREATE_OUTCOME,
        artifact_refs=artifact_refs or [],
    ):
        raise AdhelmError(
            "OUTPUT_LIMIT_EXCEEDED",
            "The required Google Ads PAUSE plan receipt exceeds the saved output bound.",
            exit_code=ExitCode.PRECONDITION,
            next_action=(
                "Increase saved max_output_bytes before planning; no Google Ads read or "
                "plan retention was performed."
            ),
        )


def _read_request(path: Path) -> GoogleAdsCampaignPausePlanRequest:
    return validate_json_file(GoogleAdsCampaignPausePlanRequest, path)


def _retention_unverified() -> AdhelmError:
    return AdhelmError(
        "GOOGLE_ADS_PAUSE_PLAN_RECEIPT_UNVERIFIED",
        "The Google Ads PAUSE plan may be retained, but its receipt could not be verified.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Inspect history verify and plan google-ads-campaign-pause catalog before "
            "planning again; do not repeat an ambiguous plan preparation."
        ),
    )


def _receipt_from_retained(
    ledger: Ledger, value: object, artifact: _ArtifactNotice
) -> dict[str, object]:
    try:
        from adhelm.google_ads_pause_workflow import RetainedGoogleAdsCampaignPausePlan

        if type(value) is not RetainedGoogleAdsCampaignPausePlan:
            raise ValueError("result")
        record_digest = _require_digest(value.plan_record_digest)
        returned = google_ads_campaign_pause_plan_record(value.action)
        action, provenance = load_google_ads_campaign_pause_plan(ledger, record_digest)
        stored = google_ads_campaign_pause_plan_record(action)
        if (
            type(provenance) is not dict
            or provenance.get("record_digest") != record_digest
            or provenance.get("action_digest") != stored.action_digest
            or provenance.get("plan_digest") != stored.plan_digest
            or canonical_bytes(returned.action) != canonical_bytes(stored.action)
        ):
            raise ValueError("source")
        return _receipt(
            record_digest,
            stored.action_digest,
            stored.plan_digest,
            stored.action.plan.subplan_digests,
            stored.expires_at,
            stored.action.status,
            Money(
                minor_units=stored.maximum_incremental_spend.minor_units,
                currency=stored.maximum_incremental_spend.currency,
            ),
            artifact,
        )
    except Exception:
        # This boundary follows a possibly successful durable planner append.
        # A corrupt returned object, record validation failure, or reload error
        # cannot establish that no plan was retained, so callers must inspect
        # local history instead of treating it as a retryable invalid request.
        raise _retention_unverified() from None


def _closed_catalog(value: object) -> dict[str, Any]:
    try:
        return validate_json_value(_Catalog, cast(dict[str, Any], value)).model_dump(
            mode="json", warnings="error"
        )
    except Exception:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_CATALOG",
            "Google Ads PAUSE plan catalog is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _review_data(review: object, provenance: object) -> dict[str, object]:
    try:
        if type(review) is not GoogleAdsCampaignPausePlanReview:
            raise ValueError("review")
        return {
            "review": review.model_dump(mode="json", warnings="error"),
            "review_digest": review.review_digest,
            "provenance": validate_json_value(
                _Provenance, cast(dict[str, Any], provenance)
            ).model_dump(mode="json", warnings="error"),
            "historical_review": True,
            "execution_authority": False,
            "remote_mutations": False,
            "review_authorizes_execution": False,
        }
    except Exception:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_REVIEW",
            "Google Ads PAUSE plan review is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        ) from None


def _review(record: object, key: bytes, reviewed_at: datetime) -> GoogleAdsCampaignPausePlanReview:
    return review_google_ads_campaign_pause_plan(
        record, opaque_key=key, reviewed_at=reviewed_at, max_output_bytes=_VIEW_MAXIMUM_BYTES
    )


@app.command("create")
def create(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    no_artifacts: Annotated[bool, typer.Option("--no-artifacts")] = False,
    artifact_format: Annotated[list[str] | None, typer.Option("--artifact-format")] = None,
    artifact_output: Annotated[Path | None, typer.Option("--artifact-output")] = None,
) -> None:
    """Read a fixed baseline and retain one non-executable Google Ads PAUSE plan."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        from adhelm.google_ads_pause_artifact_cli import (
            _load_source,
            _require_same_source,
            _source_snapshot,
        )
        from adhelm.google_ads_pause_plan_artifacts import (
            google_ads_campaign_pause_plan_artifact_failure_preview,
            google_ads_campaign_pause_plan_artifact_failure_result,
            preflight_google_ads_campaign_pause_plan_artifact_output,
            render_google_ads_campaign_pause_plan_artifacts,
        )

        # Strict regular-file parsing completes before configuration, output
        # policy, protected credentials, HTTP, or durable plan retention.
        request = _read_request(input_file)
        directory = root(ctx)
        configuration = load_configuration(directory)
        decision = _artifact_decision(
            configuration.output,
            request.profile_id,
            _override(no_artifacts, artifact_format, artifact_output),
        )
        artifact = _artifact_policy_notice(decision)
        artifact_preview: dict[str, object] | None = None
        artifact_refs: list[str] = []
        if decision.outcome == "GENERATE":
            artifact_preview, artifact_refs = (
                google_ads_campaign_pause_plan_artifact_failure_preview(
                    decision,
                    source_record_digest="0" * 64,
                    checkpoint_digest="0" * 64,
                )
            )
        # Reserve a full durable receipt plus the longest allowed partial
        # artifact result before any credential, HTTP request, key, or file.
        _require_receipt_fit(
            _create_result_data(
                _receipt(
                    "0" * 64,
                    "0" * 64,
                    "0" * 64,
                    ("0" * 64,),
                    _MAX_UTC_TIMESTAMP,
                    "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
                    Money(minor_units=0, currency="USD"),
                    artifact,
                ),
                artifact_result=artifact_preview,
            ),
            configuration.output.max_output_bytes,
            artifact_refs=artifact_refs,
        )
        from adhelm.google_ads_pause_workflow import plan_configured_google_ads_campaign_pause

        retained = plan_configured_google_ads_campaign_pause(
            directory, request, expected_configuration_digest=digest(configuration)
        )
        receipt = _receipt_from_retained(Ledger(directory), retained, artifact)

        artifact_result: dict[str, object] | None = None
        artifact_refs = []
        artifact_failure = False
        if decision.outcome == "GENERATE":
            source_checkpoint_digest: str | None = None
            selected = _require_digest(receipt["record_digest"])
            try:
                action, provenance, source = _load_source(directory, selected)
                _artifact_source_matches_receipt(receipt, source)
                first_snapshot = _source_snapshot(action, provenance, source)
                source_checkpoint_digest = source.checkpoint_digest
                # The complete-document synthetic-key preflight runs after a
                # durable source reload and before the real key or writer.
                preflight_google_ads_campaign_pause_plan_artifact_output(
                    action, provenance, decision
                )
                try:
                    final_action, final_provenance, final_source = _load_source(directory, selected)
                    _require_same_source(
                        first_snapshot,
                        _source_snapshot(final_action, final_provenance, final_source),
                    )
                    _artifact_source_matches_receipt(receipt, final_source)
                except (AdhelmError, AttributeError, OSError, TypeError, ValueError):
                    # The earlier checkpoint satisfied the zero-key preflight but
                    # no longer describes the source that would reach a writer.
                    source_checkpoint_digest = None
                    raise
                rendered = render_google_ads_campaign_pause_plan_artifacts(
                    final_action,
                    final_provenance,
                    decision,
                    key=export_key(),
                )
            except AdhelmError as error:
                rendered = google_ads_campaign_pause_plan_artifact_failure_result(
                    decision,
                    source_record_digest=selected,
                    checkpoint_digest=source_checkpoint_digest,
                    failure_code=error.code,
                )
            except (AttributeError, OSError, TypeError, ValueError):
                rendered = google_ads_campaign_pause_plan_artifact_failure_result(
                    decision,
                    source_record_digest=selected,
                    checkpoint_digest=source_checkpoint_digest,
                    failure_code="GOOGLE_ADS_PAUSE_PLAN_ARTIFACT_SOURCE_UNVERIFIED",
                )
            artifact_result = rendered.to_data()
            artifact_refs = [
                "artifact:sha256:" + item.artifact_digest for item in rendered.artifacts
            ]
            artifact_failure = rendered.generation_failure_code is not None

        emit(
            _PREFIX + " create",
            _create_result_data(receipt, artifact_result=artifact_result),
            outcome="PARTIAL" if artifact_failure else _CREATE_OUTCOME,
            maximum_bytes=configuration.output.max_output_bytes,
            artifact_refs=artifact_refs,
        )
        if artifact_failure:
            raise typer.Exit(ExitCode.PARTIAL)

    guarded(_PREFIX + " create", work)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    """List retained Google Ads PAUSE plan metadata without credential or provider access."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_GOOGLE_ADS_PAUSE_PLAN_SELECTION",
                "Google Ads PAUSE plan pagination is invalid.",
                exit_code=ExitCode.INVALID_INPUT,
            )
        preferences = load_configuration(root(ctx)).output
        data = _closed_catalog(
            google_ads_campaign_pause_plan_catalog(
                Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
            )
        )
        while not _fits(
            _PREFIX + " catalog",
            data,
            preferences.max_output_bytes,
            outcome=_HISTORY_OUTCOME,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            plans = data["plans"]
            if type(plans) is not list or len(plans) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Google Ads PAUSE plan catalog exceeds the saved output bound.",
                    exit_code=ExitCode.PRECONDITION,
                    next_action="Increase saved max_output_bytes to review this catalog.",
                )
            plans.pop()
            data["next_cursor"] = str(cursor + len(plans))
        emit(
            _PREFIX + " catalog",
            data,
            outcome=_HISTORY_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded(_PREFIX + " catalog", work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str) -> None:
    """Show one redacted historical Google Ads PAUSE plan without refreshing it."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        selected = _require_digest(record_digest)
        directory = root(ctx)
        preferences = load_configuration(directory).output
        action, provenance = load_google_ads_campaign_pause_plan(Ledger(directory), selected)
        record = google_ads_campaign_pause_plan_record(action)
        reviewed_at = datetime.now(UTC)
        probe_data = _review_data(_review(record, b"\0" * 32, reviewed_at), provenance)
        if not _fits(
            _PREFIX + " show", probe_data, preferences.max_output_bytes, outcome=_HISTORY_OUTCOME
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Complete Google Ads PAUSE plan review exceeds the saved output bound.",
                exit_code=ExitCode.PRECONDITION,
                next_action=(
                    "Increase saved max_output_bytes to review every historical limitation."
                ),
            )
        emit(
            _PREFIX + " show",
            _review_data(_review(record, export_key(), reviewed_at), provenance),
            outcome=_HISTORY_OUTCOME,
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded(_PREFIX + " show", work)


__all__ = ["app"]
