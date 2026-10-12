"""Customer-declared GA4 <-> provider campaign joins, with no provider access.

`measurement join create` resolves one declared contract against the exact GA4
dimension values already retained by a fixed aggregate read.  It opens no
socket, reads no credential, and grants no provider authority.  Retention is
explicit and refuses rather than silently dropping the record.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from adhelm.campaign_joins import (
    CampaignJoinContract,
    CampaignJoinMap,
    refuse_cross_source_derivation,
    resolve_campaign_joins,
    validate_predecessor,
)
from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.projections import _redact_payload
from adhelm.saved_campaign_joins import (
    join_map_catalog,
    join_map_retention_installed,
    latest_join_map,
    load_join_map,
    retain_join_map,
    retention_unavailable,
)
from adhelm.saved_reports import load_report

app = typer.Typer(no_args_is_help=True)


def _invalid_pagination() -> AdhelmError:
    return AdhelmError("INVALID_JOIN_MAP_SELECTION", "Invalid join pagination.", exit_code=2)


def _resolved_map(
    ledger: Ledger, contract: CampaignJoinContract, ga4_report: str
) -> CampaignJoinMap:
    from adhelm.campaign_joins import ga4_join_rows_from_normalized

    report, _ = load_report(ledger, ga4_report)
    if report.envelope.provider != "ga4":
        raise AdhelmError(
            "JOIN_SOURCE_INVALID",
            "Only a retained GA4 report supplies GA4 join dimensions.",
            exit_code=2,
            next_action="Select a retained GA4 aggregate report with report catalog.",
        )
    if report.envelope.source_account_ref != contract.ga4_property.property_ref:
        raise AdhelmError(
            "JOIN_MAP_PROPERTY_MISMATCH",
            "The declared GA4 property differs from the retained report.",
            exit_code=2,
        )
    if report.envelope.report_timezone != contract.ga4_property.property_timezone:
        raise AdhelmError(
            "TIMEZONE_MISMATCH",
            "The declared GA4 property timezone differs from the retained report.",
            exit_code=2,
        )
    return resolve_campaign_joins(contract, ga4_join_rows_from_normalized(report))


def _proposal_rows(join_map: CampaignJoinMap) -> list[dict[str, Any]]:
    return [
        {
            "provider": item.provider,
            "provider_campaign_ref": item.provider_campaign_ref,
            "session_campaign_name": item.session_campaign_name,
            "session_source": item.session_source,
            "session_medium": item.session_medium,
            "basis": item.basis,
            "name_provenance": item.name_provenance,
            "status": item.status,
            "applied": False,
        }
        for item in join_map.proposals
    ]


def _conflict_rows(join_map: CampaignJoinMap) -> list[dict[str, Any]]:
    return [
        {
            "session_campaign_name": row.session_campaign_name,
            "session_source": row.session_source,
            "session_medium": row.session_medium,
            "conflict_reasons": list(row.conflict_reasons),
            "candidate_provider_campaign_refs": list(row.candidate_provider_campaign_refs),
        }
        for row in join_map.resolutions
        if row.join_status == "CONFLICT"
    ]


def _resolution_rows(join_map: CampaignJoinMap) -> list[dict[str, Any]]:
    return [
        {
            "session_campaign_name": row.session_campaign_name,
            "session_source": row.session_source,
            "session_medium": row.session_medium,
            "join_status": row.join_status,
            "join_source": row.join_source,
            "provider": row.provider,
            "provider_campaign_ref": row.provider_campaign_ref,
            "campaign_join_key": row.campaign_join_key,
            "match_kind": row.match_kind,
            "conflict_reasons": list(row.conflict_reasons),
            "candidate_provider_campaign_refs": list(row.candidate_provider_campaign_refs),
            "data_quality_statuses": list(row.data_quality_statuses),
        }
        for row in join_map.resolutions
    ]


@app.command("create")
def create(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    ga4_report: Annotated[str, typer.Option("--ga4-report")],
    retain: Annotated[bool, typer.Option("--retain")] = False,
) -> None:
    """Resolve a declared join contract against one retained GA4 report."""
    from adhelm.cli import emit, guarded, root
    from adhelm.measurement_cli import _fits

    def work() -> None:
        contract = validate_json_file(CampaignJoinContract, input_file)
        ledger = Ledger(root(ctx))
        join_map = _resolved_map(ledger, contract, ga4_report)
        if contract.version > 1:
            previous = latest_join_map(ledger, contract.contract_id)
            if previous is None:
                raise AdhelmError(
                    "JOIN_MAP_CHAIN_INVALID",
                    "No retained predecessor exists for this join contract.",
                    exit_code=2,
                )
            validate_predecessor(previous, contract)
        preferences = load_configuration(root(ctx)).output
        data: dict[str, Any] = {
            **join_map.summary(),
            "record_digest": None,
            # Preflighted with the value the emitted receipt will carry, so the
            # measured envelope is byte-exact rather than one byte larger.
            "persisted": retain,
            "retention_installed": join_map_retention_installed(),
            "ga4_report_record_digest": ga4_report,
            "remote_side_effects": False,
            "conflicts": _conflict_rows(join_map),
            "heuristic_proposals": _proposal_rows(join_map),
            "unmatched_declared_campaign_refs": list(join_map.unmatched_declared_campaign_refs),
            "timezone_mismatches": [
                item.model_dump(mode="json") for item in join_map.timezone_mismatches
            ],
        }
        # AGENTS rule 20: every refusal runs before the durable append, so
        # a failed-closed command can never leave a retained record the
        # receipt never named.  The placeholder digest is fixed length, so
        # preflighting the receipt bounds the emitted one exactly.
        if retain and not join_map_retention_installed():
            raise retention_unavailable()
        data["record_digest"] = "0" * 64 if retain else None
        refuse_cross_source_derivation(data)
        data = _redact_payload(data, export_key(), False)
        while not _fits("measurement join create", data, preferences.max_output_bytes):
            if data["heuristic_proposals"]:
                data["heuristic_proposals"].pop()
                continue
            if data["conflicts"]:
                data["conflicts"].pop()
                continue
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "The join receipt exceeds the configured output bound.",
                next_action="Increase the saved max_output_bytes and run the command again.",
            )
        if retain:
            record = retain_join_map(ledger, join_map)
            data["record_digest"] = record["record_digest"]
        emit("measurement join create", data, maximum_bytes=preferences.max_output_bytes)

    guarded("measurement join create", work)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    """List retained join maps from one verified checkpoint."""
    from adhelm.cli import emit, guarded, root
    from adhelm.measurement_cli import _fits

    def work() -> None:
        if cursor < 0 or not 1 <= limit <= 500:
            raise _invalid_pagination()
        preferences = load_configuration(root(ctx)).output
        data = join_map_catalog(
            Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
        )
        if data["join_maps"]:
            data = _redact_payload(data, export_key(), False)
        while not _fits(
            "measurement join catalog",
            data,
            preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            if len(data["join_maps"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED", "The join catalog exceeds the output bound."
                )
            data["join_maps"].pop()
            data["next_cursor"] = str(cursor + len(data["join_maps"]))
        emit(
            "measurement join catalog",
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded("measurement join catalog", work)


@app.command("show")
def show(
    ctx: typer.Context,
    record_digest: str,
    section: Annotated[str, typer.Option("--section")] = "rows",
    provider_report: Annotated[str | None, typer.Option("--provider-report")] = None,
    ga4_report: Annotated[str | None, typer.Option("--ga4-report")] = None,
    cursor: int = 0,
    limit: int = 50,
) -> None:
    """Review one retained join map, optionally beside two retained reports.

    `--section` declares which retained list defines exhaustion: the join
    resolutions (`rows`, the default) or the heuristic proposals
    (`proposals`).  One cursor window is applied to both lists, so paging a
    section never skips an entry of either, and both counts are always
    disclosed so a reader can tell whether the other list is exhausted too.
    """
    from adhelm.cli import emit, guarded, root
    from adhelm.measurement_cli import _fits

    def work() -> None:
        if cursor < 0 or not 1 <= limit <= 500:
            raise _invalid_pagination()
        if section not in {"rows", "proposals"}:
            raise AdhelmError(
                "INVALID_JOIN_MAP_SELECTION",
                "A join review section is either rows or proposals.",
                exit_code=2,
            )
        if (provider_report is None) != (ga4_report is None):
            raise AdhelmError(
                "INVALID_JOIN_MAP_SELECTION",
                "A side-by-side view needs both a provider and a GA4 retained report.",
                exit_code=2,
            )
        ledger = Ledger(root(ctx))
        join_map, provenance = load_join_map(ledger, record_digest)
        preferences = load_configuration(root(ctx)).output
        rows = _resolution_rows(join_map)
        # A HEURISTIC row is reviewable only with the basis and the provenance
        # of the name it was proposed from: the customer supplied it, and local
        # history does not retain it.
        proposals = _proposal_rows(join_map)
        total = len(rows) if section == "rows" else len(proposals)
        if cursor > total:
            raise _invalid_pagination()
        side_by_side: dict[str, Any] | None = None
        if provider_report is not None and ga4_report is not None:
            from adhelm.measurement_comparison import build_side_by_side_comparison

            provider_normalized, _ = load_report(ledger, provider_report)
            ga4_normalized, _ = load_report(ledger, ga4_report)
            side_by_side = build_side_by_side_comparison(
                join_map=join_map,
                provider_report=provider_normalized,
                ga4_report=ga4_normalized,
            ).model_dump(mode="json")

        window = min(limit, preferences.max_rows)
        while True:
            data: dict[str, Any] = {
                **join_map.summary(),
                "record_provenance": provenance,
                "section": section,
                "heuristic_proposals": proposals[cursor : cursor + window],
                "rows": rows[cursor : cursor + window],
            }
            if side_by_side is not None:
                data["side_by_side"] = side_by_side
            refuse_cross_source_derivation(data)
            data = _redact_payload(data, export_key(), False)
            next_cursor = str(cursor + window) if cursor + window < total else None
            if _fits(
                "measurement join show",
                data,
                preferences.max_output_bytes,
                next_cursor=next_cursor,
                total_count=total,
            ):
                break
            # Shrink the window itself rather than dropping the tail of one
            # list: the next page then starts exactly where this one ended, so
            # no retained resolution or proposal becomes unreachable.
            window -= 1
            if window == 0:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Required join review and one row exceed the output bound.",
                    next_action="Increase the saved max_output_bytes to review this map.",
                )
        emit(
            "measurement join show",
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=next_cursor,
            total_count=total,
        )

    guarded("measurement join show", work)


def register_commands(parent: typer.Typer) -> None:
    """Attach the `measurement join` command group to the measurement app."""
    parent.add_typer(app, name="join")


__all__ = ["app", "register_commands"]
