"""Explicit fixed measurement reads and bounded retained observation review."""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import typer

from adhelm import __version__
from adhelm.audits import COMPILABLE_OPERATION_KINDS
from adhelm.audits.gtm_web import evaluate_gtm_web_audit
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.ga4_admin_edit_cli import app as ga4_admin_edit_app
from adhelm.gtm_dependency_graph import build_gtm_dependency_graph
from adhelm.ledger import Ledger
from adhelm.measurement_comparison import (
    MeasurementComparisonRequest,
    compare_measurements,
)
from adhelm.measurement_records import (
    Ga4MeasurementSnapshot,
    GtmFullInventoryMeasurementSnapshot,
    GtmMeasurementSnapshot,
    OpenAIMeasurementSnapshot,
)
from adhelm.models import ResultEnvelope
from adhelm.projections import _redact_payload
from adhelm.saved_measurements import (
    load_measurement,
    measurement_catalog,
    measurement_rows,
    retain_measurement,
)

app = typer.Typer(no_args_is_help=True)
app.add_typer(ga4_admin_edit_app, name="ga4-admin-edit")


def _fits(command: str, data: dict[str, Any], maximum: int, **kwargs: Any) -> bool:
    kwargs.setdefault("outcome", "OK")
    return (
        len(
            canonical_bytes(
                ResultEnvelope(
                    tool_version=__version__,
                    command=command,
                    data=data,
                    **kwargs,
                )
            )
        )
        + 1
        <= maximum
    )


def _receipt(record_digest: str, snapshot_digest: str, provider: str, rows: int) -> dict[str, Any]:
    return {
        "record_digest": record_digest,
        "snapshot_digest": snapshot_digest,
        "provider": provider,
        "row_count": rows,
        "evidence_kind": "CONFIGURATION_READ",
        "persisted": True,
        "runtime_firing": "NOT_EVALUATED",
        "processed_receipt": "NOT_EVALUATED",
        "execution_authority": False,
        "remote_mutations": False,
    }


def _read(
    ctx: typer.Context,
    provider: Literal["ga4_admin", "openai_ads", "gtm"],
    profile: str,
    *,
    stream_id: str | None = None,
    container_id: str | None = None,
    maximum_records: int = 500,
    page_size: int = 500,
    ci_enabled: bool = False,
    full_inventory: bool = False,
) -> None:
    from adhelm.cli import emit, guarded, root

    command = {
        "ga4_admin": "measurement ga4-admin",
        "openai_ads": "measurement openai-conversions",
        "gtm": "measurement gtm",
    }[provider]
    # The retained snapshot kind, and therefore the ledger provider label, is
    # chosen by the opt-in request discriminator, not by the command name.
    receipt_provider = "gtm_web_inventory" if provider == "gtm" and full_inventory else provider

    def work() -> None:
        directory = root(ctx)
        configuration = load_configuration(directory)
        if provider == "gtm":
            from adhelm.gtm_measurement_workflow import validate_gtm_read

            if container_id is None:
                raise AdhelmError(
                    "EXPECTED_CONTAINER_REQUIRED",
                    "GTM requires an explicit expected web container.",
                    exit_code=2,
                )
            validate_gtm_read(directory, profile, container_id=container_id, ci_enabled=ci_enabled)
        selected = [p for p in configuration.profiles if p.profile_id == profile]
        if len(selected) != 1 or selected[0].provider != provider or selected[0].purpose != "READ":
            raise AdhelmError(
                "READ_PROFILE_REQUIRED",
                "An explicit matching measurement read profile is required.",
            )
        maximum = configuration.output.max_output_bytes
        if not _fits(
            command,
            _receipt("0" * 64, "0" * 64, receipt_provider, 5000),
            maximum,
            outcome="CONFIGURATION_RETAINED",
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED", "Measurement receipt exceeds the configured output bound."
            )
        ledger = Ledger(directory)
        ledger.checkpoint()
        snapshot: (
            Ga4MeasurementSnapshot
            | OpenAIMeasurementSnapshot
            | GtmMeasurementSnapshot
            | GtmFullInventoryMeasurementSnapshot
        )
        if provider == "ga4_admin":
            from adhelm.ga4_measurement_workflow import read_ga4_admin, validate_ga4_admin_read

            # Validate the profile and every public GA4 argument before this
            # branch acquires the local HMAC key. OpenAI settings reads never
            # need that sharing key at all.
            if stream_id is None:
                raise AdhelmError(
                    "EXPECTED_STREAM_REQUIRED",
                    "GA4 Admin requires an explicit expected web stream.",
                    exit_code=2,
                )
            validate_ga4_admin_read(
                directory,
                profile,
                stream_id=stream_id,
                ci_enabled=ci_enabled,
            )
            key = export_key()
            started = datetime.now(UTC)
            inventory = read_ga4_admin(
                directory, profile, stream_id=stream_id, fingerprint_key=key, ci_enabled=ci_enabled
            )
            # Ordering is presentation-independent; processing_order remains exact data.
            inventory = type(inventory).model_validate(
                {
                    **inventory.model_dump(),
                    **{
                        name: tuple(
                            sorted(getattr(inventory, name), key=lambda item: item.resource_ref)
                        )
                        for name in ("key_events", "event_create_rules", "event_edit_rules")
                    },
                }
            )
            snapshot = Ga4MeasurementSnapshot(inventory=inventory)
        elif provider == "gtm":
            from adhelm.gtm_measurement_workflow import read_gtm, read_gtm_full_inventory

            assert container_id is not None
            key = export_key()
            started = datetime.now(UTC)
            if full_inventory:
                discovery, _, complete = read_gtm_full_inventory(
                    directory,
                    profile,
                    container_id=container_id,
                    fingerprint_key=key,
                    ci_enabled=ci_enabled,
                )
                snapshot = GtmFullInventoryMeasurementSnapshot(
                    discovery=discovery, inventory=complete
                )
            else:
                discovery, gtm_inventory = read_gtm(
                    directory,
                    profile,
                    container_id=container_id,
                    fingerprint_key=key,
                    ci_enabled=ci_enabled,
                )
                gtm_inventory = type(gtm_inventory).model_validate(
                    {
                        **gtm_inventory.model_dump(),
                        "workspaces": tuple(
                            sorted(gtm_inventory.workspaces, key=lambda item: item.workspace_ref)
                        ),
                    }
                )
                snapshot = GtmMeasurementSnapshot(discovery=discovery, inventory=gtm_inventory)
        else:
            from adhelm.openai_measurement_workflow import read_openai_conversions

            started = datetime.now(UTC)
            identity, settings = read_openai_conversions(
                directory,
                profile,
                maximum_records=maximum_records,
                page_size=page_size,
                ci_enabled=ci_enabled,
            )
            snapshot = OpenAIMeasurementSnapshot(
                identity=identity,
                settings=tuple(
                    sorted(
                        settings,
                        key=lambda item: item.conversion_setting_ref,
                    )
                ),
            )
        finished = datetime.now(UTC)
        try:
            record = retain_measurement(
                ledger,
                snapshot,
                profile_ref=profile,
                read_started_at=started,
                read_finished_at=finished,
            )
        except OSError as exc:
            raise AdhelmError(
                "MEASUREMENT_RETENTION_UNVERIFIED",
                "The read finished, but durable measurement retention could not be verified.",
                next_action="Inspect history verify and measurement catalog before repeating "
                "the read; an observation may already be retained.",
            ) from exc
        emit(
            command,
            _receipt(
                record["record_digest"],
                record["payload"]["snapshot_digest"],
                snapshot.provider,
                snapshot.row_count,
            ),
            outcome="CONFIGURATION_RETAINED",
            maximum_bytes=maximum,
        )

    guarded(command, work)


@app.command("ga4-admin")
def ga4_admin(ctx: typer.Context, profile: str, stream_id: str, ci_enabled: bool = False) -> None:
    """Read and retain fixed GA4 Admin configuration through an explicit read profile."""
    _read(ctx, "ga4_admin", profile, stream_id=stream_id, ci_enabled=ci_enabled)


@app.command("ga4-admin-access")
def ga4_admin_access(
    ctx: typer.Context,
    read_profile: Annotated[str, typer.Option("--read-profile")],
    edit_profile: Annotated[str, typer.Option("--edit-profile")],
    stream_id: Annotated[str, typer.Option("--stream-id")],
    ci_enabled: bool = False,
) -> None:
    """Check READ and EDIT resource access with fixed GETs; does not prove create permission."""
    from adhelm.cli import emit, guarded, root
    from adhelm.ga4_admin_access_workflow import (
        Ga4AdminAccessRequest,
        Ga4AdminAccessResult,
        check_ga4_admin_access,
        validate_ga4_admin_access,
    )

    command = "measurement ga4-admin-access"

    def work() -> None:
        directory = root(ctx)
        request = Ga4AdminAccessRequest(
            read_profile_id=read_profile,
            edit_profile_id=edit_profile,
            stream_id=stream_id,
            ci_enabled=ci_enabled,
        )
        configuration, _, _ = validate_ga4_admin_access(directory, request)
        maximum = configuration.output.max_output_bytes
        preview = Ga4AdminAccessResult(observed_service_level="GOOGLE_ANALYTICS_STANDARD")
        if not _fits(
            command, preview.model_dump(mode="json"), maximum, outcome="ACCESS_CHECK_ONLY"
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "GA4 access-check result exceeds the configured output bound.",
                exit_code=2,
            )
        result = check_ga4_admin_access(directory, request)
        emit(
            command,
            result.model_dump(mode="json"),
            outcome="ACCESS_CHECK_ONLY",
            maximum_bytes=maximum,
        )

    guarded(command, work)


@app.command("openai-conversions")
def openai_conversions(
    ctx: typer.Context,
    profile: str,
    maximum_records: int = 500,
    page_size: int = 500,
    ci_enabled: bool = False,
) -> None:
    """Read and retain standard conversion definitions, without event payloads."""
    _read(
        ctx,
        "openai_ads",
        profile,
        maximum_records=maximum_records,
        page_size=page_size,
        ci_enabled=ci_enabled,
    )


@app.command("gtm")
def gtm(
    ctx: typer.Context,
    profile: str,
    container_id: str,
    ci_enabled: bool = False,
    full_inventory: bool = False,
) -> None:
    """Read and retain GTM version counts and accessible workspace metadata.

    `--full-inventory` opts in to the complete fixed inventory: every
    documented collection of the live version, the latest version and every
    accessible workspace, with per-collection pagination completeness and raw
    reference edges.  It never proves a dependency graph or deletion safety,
    and custom-code source is classified and hashed but never retained.
    """

    _read(
        ctx,
        "gtm",
        profile,
        container_id=container_id,
        ci_enabled=ci_enabled,
        full_inventory=full_inventory,
    )


@app.command("gtm-graph")
def gtm_graph(
    ctx: typer.Context,
    record_digest: str,
    section: str = "NODES",
    scope_ref: Annotated[str | None, typer.Option("--scope-ref")] = None,
    workspace_limit: Annotated[int | None, typer.Option("--workspace-limit")] = None,
    union: bool = False,
    cursor: int = 0,
    limit: int = 50,
) -> None:
    """Build the GTM dependency graph and read-only audit from one retained observation.

    Everything here is computed from the selected `gtm_web_inventory` record in
    verified local history.  No provider request is made, no credential is read,
    and nothing is appended: the graph and audit are a deterministic function of
    an observation the ledger already holds, so they are reproducible from
    `graph_digest` and `source.record_digest` without a second retained copy.

    `--section` selects the paginated rows: `NODES`, `EDGES`, `FINDINGS`, or
    `COVERAGE`.  `--union` adds the opt-in container-wide classification, which
    is the only view that mixes workspace and version scopes.  `--workspace-limit`
    declares the container's Tag Manager workspace allowance, which no documented
    read returns; without it the stage gate is reported as not evaluated rather
    than guessed.
    """

    from adhelm.cli import emit, guarded, root

    command = "measurement gtm-graph"

    def work() -> None:
        selected = "UNION" if union else section.upper()
        if selected not in _GRAPH_SECTIONS:
            raise AdhelmError(
                "INVALID_MEASUREMENT_SELECTION",
                "Section must be one of NODES, EDGES, FINDINGS, COVERAGE or UNION.",
                exit_code=2,
            )
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_MEASUREMENT_SELECTION", "Invalid graph pagination.", exit_code=2
            )
        directory = root(ctx)
        observation, provenance = load_measurement(Ledger(directory), record_digest)
        snapshot = observation.snapshot
        if not isinstance(snapshot, GtmFullInventoryMeasurementSnapshot):
            raise AdhelmError(
                "MEASUREMENT_KIND_UNSUPPORTED",
                "A GTM dependency graph requires a retained full-inventory observation.",
                exit_code=2,
                next_action="Read the container with `measurement gtm --full-inventory` first.",
            )
        preferences = load_configuration(directory).output
        inventory = snapshot.inventory
        graph = build_gtm_dependency_graph(inventory, include_union=selected == "UNION")
        audit = evaluate_gtm_web_audit(
            inventory=inventory,
            graph=graph,
            source_record_digest=provenance["record_digest"],
            snapshot_digest=observation.snapshot_digest,
        )
        rows = _graph_rows(graph, audit, selected, scope_ref)
        if cursor > len(rows):
            raise AdhelmError(
                "INVALID_MEASUREMENT_SELECTION", "Cursor exceeds graph row count.", exit_code=2
            )
        data: dict[str, Any] = {
            "graph_digest": digest(graph),
            "audit_digest": digest(audit),
            "capability_version": graph.capability_version,
            "rule_table_version": audit.rule_table_version,
            "implementation_evidence": "FIXTURE_VERIFIED_LIVE_UNVERIFIED",
            "identity": graph.identity.model_dump(mode="json"),
            "section": selected,
            "scope_ref_filter": scope_ref,
            "source": {
                "record_digest": provenance["record_digest"],
                "snapshot_digest": observation.snapshot_digest,
                "checkpoint_digest": provenance["checkpoint_digest"],
                "recorded_at": provenance["recorded_at"],
                "profile_ref": observation.profile_ref,
            },
            "retention": {
                "graph_retained": False,
                "reason": "DERIVED_DETERMINISTICALLY_FROM_A_RETAINED_OBSERVATION",
            },
            "live_provider_reads": graph.live_provider_reads,
            "ledger_appends": audit.ledger_appends,
            "live_latest_status": graph.live_latest_status,
            "reference_coverage": graph.reference_coverage,
            "inventory_completeness": graph.inventory_completeness,
            "container_blocking_reasons": list(graph.container_blocking_reasons),
            "counts": graph.counts.model_dump(mode="json"),
            "scope_composition": graph.scope_composition,
            "deletion_authority": graph.deletion_authority,
            "cleanup_operations": graph.cleanup_operations,
            "ui_reconciliation": graph.ui_reconciliation,
            "recommendation_status": audit.recommendation_status,
            "compilable_operation_kinds": sorted(COMPILABLE_OPERATION_KINDS["gtm"]),
            "finding_counts_by_rule": [list(item) for item in audit.finding_counts_by_rule],
            "no_recommendations": [
                item.model_dump(mode="json") for item in audit.no_recommendations
            ],
            "scopes": [
                item.model_dump(mode="json", exclude={"sequencing_cycles"}) for item in graph.scopes
            ],
            "stage_gate": _stage_gate(inventory, workspace_limit),
            "rows": rows[cursor : cursor + min(limit, preferences.max_rows)],
        }
        data = _redact_payload(data, export_key(), False)
        while True:
            next_cursor = (
                str(cursor + len(data["rows"])) if cursor + len(data["rows"]) < len(rows) else None
            )
            if _fits(
                command,
                data,
                preferences.max_output_bytes,
                next_cursor=next_cursor,
                total_count=len(rows),
            ):
                break
            if len(data["rows"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "The required graph summary and one row exceed the output bound.",
                    next_action="Increase the saved max_output_bytes, or filter with --scope-ref.",
                )
            data["rows"].pop()
        emit(
            command,
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=next_cursor,
            total_count=len(rows),
        )

    guarded(command, work)


_GRAPH_SECTIONS = ("NODES", "EDGES", "FINDINGS", "COVERAGE", "UNION")


def _graph_rows(
    graph: Any, audit: Any, section: str, scope_ref: str | None
) -> list[dict[str, Any]]:
    if section == "NODES":
        items = [item for item in graph.nodes if scope_ref is None or item.scope_ref == scope_ref]
    elif section == "EDGES":
        items = [item for item in graph.edges if scope_ref is None or item.scope_ref == scope_ref]
    elif section == "FINDINGS":
        items = [
            item for item in audit.findings if scope_ref is None or item.scope_ref == scope_ref
        ]
    elif section == "UNION":
        items = list(graph.union or ())
    else:
        items = list(audit.coverage)
    return [{"row_type": section, **item.model_dump(mode="json")} for item in items]


def _stage_gate(inventory: Any, workspace_limit: int | None) -> dict[str, Any]:
    """Report the stage gate over the retained observation, never guessing an input."""

    from adhelm.measurement import validate_gtm_stage_gate

    result: dict[str, Any] = {
        "workspace_limit_declared": workspace_limit,
        "inventory_completeness": inventory.completeness.status,
        "live_latest_status": inventory.live_latest.status,
        "evidence": "RETAINED_OBSERVATION",
        "remote_write_authorized": False,
        "staged": False,
    }
    if workspace_limit is None:
        result["outcome"] = "NOT_EVALUATED"
        result["code"] = "WORKSPACE_LIMIT_REQUIRED"
        result["detail"] = (
            "No documented Tag Manager read returns a container's workspace allowance; "
            "declare it with --workspace-limit to evaluate the slot half of the gate."
        )
        return result
    try:
        gate = validate_gtm_stage_gate(inventory, workspace_limit=workspace_limit)
    except AdhelmError as error:
        result["outcome"] = "BLOCKED"
        result["code"] = error.code
        result["detail"] = error.message
        return result
    result["outcome"] = "PRECONDITIONS_MET"
    result["code"] = None
    result["detail"] = "Live equals latest, every applicable surface was read, and a slot is free."
    result["base_live_version_ref"] = gate.base_live_version_ref
    result["dedicated_workspace_required"] = gate.dedicated_workspace_required
    result["existing_workspace_reuse_allowed"] = gate.existing_workspace_reuse_allowed
    return result


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        preferences = load_configuration(root(ctx)).output
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_MEASUREMENT_SELECTION", "Invalid measurement pagination.", exit_code=2
            )
        data = measurement_catalog(
            Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
        )
        while not _fits(
            "measurement catalog",
            data,
            preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            if len(data["measurements"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED", "Measurement catalog exceeds output bound."
                )
            data["measurements"].pop()
            data["next_cursor"] = str(cursor + len(data["measurements"]))
        emit(
            "measurement catalog",
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded("measurement catalog", work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str, cursor: int = 0, limit: int = 50) -> None:
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_MEASUREMENT_SELECTION", "Invalid measurement pagination.", exit_code=2
            )
        observation, provenance = load_measurement(Ledger(root(ctx)), record_digest)
        preferences = load_configuration(root(ctx)).output
        rows = measurement_rows(observation)
        if cursor > len(rows):
            raise AdhelmError(
                "INVALID_MEASUREMENT_SELECTION",
                "Cursor exceeds measurement row count.",
                exit_code=2,
            )
        selected_rows = rows[cursor : cursor + min(limit, preferences.max_rows)]
        data: dict[str, Any] = {
            "snapshot_digest": observation.snapshot_digest,
            "provider": observation.snapshot.provider,
            "profile_ref": observation.profile_ref,
            "identity": (
                observation.snapshot.inventory.identity.model_dump(mode="json")
                if isinstance(observation.snapshot, Ga4MeasurementSnapshot)
                else observation.snapshot.identity.model_dump(mode="json")
            ),
            "capability_version": observation.snapshot.capability_version,
            "implementation_evidence": "FIXTURE_VERIFIED_LIVE_UNVERIFIED",
            "read_started_at": observation.read_started_at.isoformat(),
            "read_finished_at": observation.read_finished_at.isoformat(),
            "evidence_kind": observation.evidence_kind,
            "consistency": observation.consistency,
            "runtime_firing": observation.runtime_firing,
            "processed_receipt": observation.processed_receipt,
            "native_optimization_association": observation.native_optimization_association,
            "change_attribution": observation.change_attribution,
            "execution_authority": False,
            "provenance": provenance,
            "rows": selected_rows,
        }
        if isinstance(observation.snapshot, GtmFullInventoryMeasurementSnapshot):
            complete_snapshot = observation.snapshot
            data.update(
                {
                    "inventory_coverage": complete_snapshot.inventory_coverage,
                    # The M01.1 follow-up: the one flag a dependency proof must
                    # consult before reading a missing edge as evidence was
                    # retained but never surfaced for review.
                    "reference_coverage": complete_snapshot.inventory.reference_coverage,
                    "dependency_completeness": complete_snapshot.dependency_completeness,
                    "ui_reconciliation": complete_snapshot.ui_reconciliation,
                    "usage_context": complete_snapshot.discovery.usage_context,
                    "counts": complete_snapshot.inventory.counts.model_dump(mode="json"),
                    "completeness": complete_snapshot.inventory.completeness.model_dump(
                        mode="json"
                    ),
                    "live_latest": complete_snapshot.inventory.live_latest.model_dump(mode="json"),
                    "observation_status": complete_snapshot.inventory.observation_status,
                    "dependency_graph": complete_snapshot.inventory.dependency_graph,
                    "custom_code_source": complete_snapshot.inventory.custom_code_source,
                }
            )
        if isinstance(observation.snapshot, GtmMeasurementSnapshot):
            snapshot = observation.snapshot
            data.update(
                {
                    "inventory_coverage": snapshot.inventory_coverage,
                    "dependency_completeness": snapshot.dependency_completeness,
                    "usage_context": snapshot.discovery.usage_context,
                    "account_features": snapshot.discovery.account_features.model_dump(),
                    "container_features": snapshot.discovery.container_features.model_dump(),
                    "latest_header_count_status": (
                        snapshot.inventory.latest_header_count_status.model_dump(mode="json")
                    ),
                    "observation_status": snapshot.inventory.observation_status,
                    "staging_eligibility": snapshot.inventory.staging_eligibility,
                }
            )
        data = _redact_payload(data, export_key(), False)
        while True:
            next_cursor = (
                str(cursor + len(data["rows"])) if cursor + len(data["rows"]) < len(rows) else None
            )
            if _fits(
                "measurement show",
                data,
                preferences.max_output_bytes,
                next_cursor=next_cursor,
                total_count=len(rows),
            ):
                break
            if len(data["rows"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Required measurement review and one row exceed the output bound.",
                    next_action="Increase the saved max_output_bytes to review this observation.",
                )
            data["rows"].pop()
        emit(
            "measurement show",
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=next_cursor,
            total_count=len(rows),
        )

    guarded("measurement show", work)


@app.command("compare")
def compare(ctx: typer.Context, before_record_digest: str, after_record_digest: str) -> None:
    """Compare two complete retained observations from one verified local checkpoint."""
    from adhelm.cli import emit, guarded, root
    from adhelm.measurement_comparison_artifacts import (
        measurement_comparison_artifact_failure_preview,
        preflight_measurement_comparison_artifact_output,
        preflight_measurement_comparison_artifacts,
        render_measurement_comparison_artifacts,
    )

    def work() -> None:
        request = MeasurementComparisonRequest(
            before_record_digest=before_record_digest,
            after_record_digest=after_record_digest,
        )
        preferences = load_configuration(root(ctx)).output
        ledger = Ledger(root(ctx))
        comparison = compare_measurements(
            ledger, request.before_record_digest, request.after_record_digest
        )
        decision = preflight_measurement_comparison_artifacts(
            preferences,
            provider=comparison.scope.provider,
            profile_id=comparison.before.profile_ref,
        )
        data = comparison.model_dump(mode="json")
        data["comparison_digest"] = comparison.comparison_digest
        data["implementation_evidence"] = "FIXTURE_VERIFIED_LIVE_UNVERIFIED"
        artifact_refs: list[str] = []
        if decision.outcome == "GENERATE":
            comparison = preflight_measurement_comparison_artifact_output(
                ledger, comparison, decision
            )
            preview, artifact_refs = measurement_comparison_artifact_failure_preview(
                decision,
                before_record_digest=request.before_record_digest,
                after_record_digest=request.after_record_digest,
                checkpoint_digest=comparison.checkpoint_digest,
                comparison_digest=comparison.comparison_digest,
            )
            data["artifact_result"] = preview
        # One complete comparison is the review unit. A count or byte limit
        # must never conceal a changed fact or its source-coverage caveats.
        if len(comparison.facts) > preferences.max_rows or not _fits(
            "measurement compare",
            _redact_payload(data, b"\0" * 32, False),
            preferences.max_output_bytes,
            outcome="HISTORICAL_CONFIGURATION_COMPARISON",
            total_count=len(comparison.facts),
            artifact_refs=artifact_refs,
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Complete measurement comparison exceeds the saved output bounds.",
                next_action=(
                    "Increase saved max_rows or max_output_bytes to review all facts and "
                    "source limitations together."
                ),
            )
        key = export_key()
        artifact_failed = False
        if decision.outcome == "GENERATE":
            artifact_result = render_measurement_comparison_artifacts(
                ledger, comparison, decision, key=key
            )
            data["artifact_result"] = artifact_result.to_data()
            artifact_refs = [
                "artifact:sha256:" + item.artifact_digest for item in artifact_result.artifacts
            ]
            artifact_failed = artifact_result.generation_failure_code is not None
        emit(
            "measurement compare",
            _redact_payload(data, key, False),
            outcome="PARTIAL" if artifact_failed else "HISTORICAL_CONFIGURATION_COMPARISON",
            maximum_bytes=preferences.max_output_bytes,
            total_count=len(comparison.facts),
            artifact_refs=artifact_refs,
        )
        if artifact_failed:
            raise typer.Exit(5)

    guarded("measurement compare", work)
