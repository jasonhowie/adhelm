"""Local GA4 key-event candidates tied to retained customer declarations."""

from pathlib import Path
from typing import Annotated, Any

import typer

from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.ga4_key_event_candidates import (
    Ga4KeyEventCandidateInput,
    compile_ga4_key_event_candidate,
)
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.projections import _redact_payload
from adhelm.saved_conversion_contracts import load_conversion_contract

app = typer.Typer(no_args_is_help=True)
_PREFIX = "measurement key-event-candidate"


@app.command("compile")
def compile_candidate(
    ctx: typer.Context,
    input_file: Annotated[Path, typer.Option("--input")],
    retain: bool = False,
) -> None:
    """Compile a fixed non-executable key-event candidate from a retained definition."""
    from adhelm.cli import emit, guarded, root
    from adhelm.saved_ga4_key_event_candidates import retain_ga4_key_event_candidate

    command = _PREFIX + " compile"

    def work() -> None:
        request = validate_json_file(Ga4KeyEventCandidateInput, input_file)
        directory = root(ctx)
        preferences = load_configuration(directory).output
        ledger = Ledger(directory)
        contract, provenance = load_conversion_contract(
            ledger, request.conversion_contract_record_digest
        )
        candidate = compile_ga4_key_event_candidate(
            request,
            retained_contract_record_digest=provenance["record_digest"],
            retained_contract=contract,
        )
        if retain:
            receipt = {
                "record_digest": "0" * 64,
                "candidate_digest": candidate.candidate_digest,
                "conversion_contract_record_digest": provenance["record_digest"],
                "conversion_content_digest": contract.content_digest,
                "execution_status": "OFFLINE_CANDIDATE_NOT_EXECUTABLE",
                "persisted": True,
                "execution_authority": False,
                "remote_mutations": False,
            }
            if not _fits(
                command, receipt, preferences.max_output_bytes, outcome="CANDIDATE_RETAINED"
            ):
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "The complete candidate retention receipt exceeds the saved output bound.",
                )
            try:
                record = retain_ga4_key_event_candidate(
                    ledger,
                    request,
                    retained_contract_record_digest=provenance["record_digest"],
                    retained_contract=contract,
                )
            except OSError:
                raise AdhelmError(
                    "GA4_KEY_EVENT_CANDIDATE_RETENTION_UNVERIFIED",
                    "Durable candidate retention could not be verified.",
                    next_action="Inspect history verify and measurement key-event-candidate "
                    "catalog before retrying; a candidate may already be retained.",
                ) from None
            receipt["record_digest"] = record["record_digest"]
            emit(
                command,
                receipt,
                outcome="CANDIDATE_RETAINED",
                maximum_bytes=preferences.max_output_bytes,
            )
            return
        data = {"candidate": candidate.model_dump(mode="json"), "provenance": provenance}
        if not _fits(
            command,
            _redact_payload(data, b"\0" * 32, False),
            preferences.max_output_bytes,
            outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "The complete candidate review exceeds the saved output bound.",
                next_action="Increase max_output_bytes to review all planning and evidence limits.",
            )
        emit(
            command,
            _redact_payload(data, export_key(), False),
            outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded(command, work)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    """List retained candidate digests without a new read or execution grant."""
    from adhelm.cli import emit, guarded, root
    from adhelm.saved_ga4_key_event_candidates import ga4_key_event_candidate_catalog

    command = _PREFIX + " catalog"

    def work() -> None:
        preferences = load_configuration(root(ctx)).output
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_GA4_KEY_EVENT_CANDIDATE_SELECTION", "Invalid pagination.", exit_code=2
            )
        data = ga4_key_event_candidate_catalog(
            Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
        )
        while not _fits(
            command,
            data,
            preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            if len(data["candidates"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Required candidate catalog metadata exceeds the saved output bound.",
                    next_action="Increase max_output_bytes to review this catalog.",
                )
            data["candidates"].pop()
            data["next_cursor"] = str(cursor + len(data["candidates"]))
        emit(
            command,
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded(command, work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str) -> None:
    """Show one complete redacted historical candidate with every unmet planning gate."""
    from adhelm.cli import emit, guarded, root
    from adhelm.saved_ga4_key_event_candidates import load_ga4_key_event_candidate

    command = _PREFIX + " show"

    def work() -> None:
        candidate, provenance = load_ga4_key_event_candidate(Ledger(root(ctx)), record_digest)
        preferences = load_configuration(root(ctx)).output
        data: dict[str, Any] = {
            "candidate": candidate.model_dump(mode="json"),
            "provenance": provenance,
            "historical_status": "RETAINED_OFFLINE_OBSERVATION",
            "current_definition_status": "NOT_EVALUATED",
        }
        if not _fits(
            command,
            _redact_payload(data, b"\0" * 32, False),
            preferences.max_output_bytes,
            outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Complete candidate review exceeds the saved output bound.",
                next_action=(
                    "Increase max_output_bytes; no planning or evidence limits were dropped."
                ),
            )
        emit(
            command,
            _redact_payload(data, export_key(), False),
            outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded(command, work)
