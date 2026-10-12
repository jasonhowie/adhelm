"""Local customer-defined conversion meaning, separate from provider operations."""

from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from adhelm.config import load_configuration
from adhelm.conversion_contracts import ConversionContractInput, compile_conversion_contract
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.input_files import validate_json_file
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.projections import _redact_payload
from adhelm.saved_conversion_contracts import (
    conversion_catalog,
    load_conversion_contract,
    retain_conversion_contract,
)

app = typer.Typer(no_args_is_help=True)


@app.command("register")
def register(ctx: typer.Context, input_file: Annotated[Path, typer.Option("--input")]) -> None:
    """Validate and retain customer-declared meaning; grants no provider authority."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        try:
            definition = validate_json_file(ConversionContractInput, input_file)
        except ValidationError as exc:
            if any(
                error["type"] == "missing"
                and error["loc"]
                and error["loc"][0]
                in {
                    "business_outcome",
                    "authoritative_source",
                    "qualifying_occurrence",
                    "counting_and_deduplication",
                }
                for error in exc.errors(include_input=False)
            ):
                raise AdhelmError(
                    "USER_DEFINITION_REQUIRED",
                    "Supply explicit customer-defined outcome, source, qualification, "
                    "and counting meaning before registration.",
                    exit_code=2,
                    next_action="Inspect schema conversion-definition; labels or inferred "
                    "click/form behavior cannot substitute for the customer definition.",
                ) from None
            raise
        contract = compile_conversion_contract(definition)
        maximum = load_configuration(root(ctx)).output.max_output_bytes
        receipt: dict[str, Any] = {
            "record_digest": "0" * 64,
            "content_digest": contract.content_digest,
            "version": contract.definition.version,
            "provenance": "CUSTOMER_DECLARED",
            "persisted": True,
            "execution_authority": False,
            "remote_mutations": False,
        }
        if not _fits("conversion register", receipt, maximum, outcome="DEFINITION_RETAINED"):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED", "Conversion receipt exceeds the configured output bound."
            )
        try:
            record = retain_conversion_contract(Ledger(root(ctx)), definition)
        except OSError:
            raise AdhelmError(
                "CONVERSION_RETENTION_UNVERIFIED",
                "Durable retention of the conversion definition could not be verified.",
                next_action="Inspect history verify and conversion catalog before registering "
                "again; a version may already be retained.",
            ) from None
        receipt["record_digest"] = record["record_digest"]
        emit("conversion register", receipt, maximum_bytes=maximum, outcome="DEFINITION_RETAINED")

    guarded("conversion register", work)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_CONVERSION_SELECTION", "Invalid conversion pagination.", exit_code=2
            )
        preferences = load_configuration(root(ctx)).output
        data = conversion_catalog(
            Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
        )
        if data["contracts"]:
            data = _redact_payload(data, export_key(), False)
        while not _fits(
            "conversion catalog",
            data,
            preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            if len(data["contracts"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED", "Conversion catalog exceeds the output bound."
                )
            data["contracts"].pop()
            data["next_cursor"] = str(cursor + len(data["contracts"]))
        emit(
            "conversion catalog",
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded("conversion catalog", work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str, cursor: int = 0, limit: int = 50) -> None:
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_CONVERSION_SELECTION", "Invalid conversion pagination.", exit_code=2
            )
        contract, provenance = load_conversion_contract(Ledger(root(ctx)), record_digest)
        preferences = load_configuration(root(ctx)).output
        definition = contract.definition.model_dump(mode="json")
        if cursor > len(definition):
            raise AdhelmError(
                "INVALID_CONVERSION_SELECTION", "Cursor exceeds definition fields.", exit_code=2
            )
        # Preserve canonical field names during redaction, then page whole
        # fields. Required provenance/authority facts accompany every page.
        definition = _redact_payload(definition, export_key(), False)
        fields = [{"field": name, "value": value} for name, value in sorted(definition.items())]
        selected = fields[cursor : cursor + min(limit, preferences.max_rows)]
        data: dict[str, Any] = {
            "content_digest": contract.content_digest,
            "version": contract.definition.version,
            "supersedes_digest": contract.definition.supersedes_ref,
            "provenance": provenance,
            "definition_origin": "CUSTOMER_DECLARED",
            "definition_projection": "REDACTED",
            "semantic_accuracy": "NOT_VERIFIED",
            "execution_authority": False,
            "remote_configuration": "NOT_CREATED",
            "rows": selected,
        }
        while True:
            next_cursor = (
                str(cursor + len(data["rows"]))
                if cursor + len(data["rows"]) < len(fields)
                else None
            )
            if _fits(
                "conversion show",
                data,
                preferences.max_output_bytes,
                next_cursor=next_cursor,
                total_count=len(fields),
            ):
                break
            if len(data["rows"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Required conversion review and one field exceed the output bound.",
                    next_action="Increase the saved max_output_bytes to review this definition.",
                )
            data["rows"].pop()
        emit(
            "conversion show",
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=next_cursor,
            total_count=len(fields),
        )

    guarded("conversion show", work)
