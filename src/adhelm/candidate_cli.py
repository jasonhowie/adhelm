"""Read retained candidate reviews without conferring plan or approval authority."""

from typing import Any

import typer

from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.projections import _redact_payload
from adhelm.saved_candidates import candidate_catalog, load_candidate

app = typer.Typer(no_args_is_help=True)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        preferences = load_configuration(root(ctx)).output
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_CANDIDATE_SELECTION", "Invalid candidate pagination.", exit_code=2
            )
        data = candidate_catalog(
            Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
        )
        while not _fits(
            "candidate catalog",
            data,
            preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            if len(data["candidates"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Required candidate catalog row exceeds the saved output bound.",
                    next_action="Increase saved max_output_bytes to review this catalog.",
                )
            data["candidates"].pop()
            data["next_cursor"] = str(cursor + len(data["candidates"]))
        emit(
            "candidate catalog",
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded("candidate catalog", work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str, cursor: int = 0, limit: int = 50) -> None:
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_CANDIDATE_SELECTION", "Invalid candidate pagination.", exit_code=2
            )
        compilation, provenance = load_candidate(Ledger(root(ctx)), record_digest)
        preferences = load_configuration(root(ctx)).output
        size = min(limit, preferences.max_rows)
        total = len(compilation.creative_references)
        if cursor > total:
            raise AdhelmError(
                "INVALID_CANDIDATE_SELECTION",
                "Cursor exceeds creative reference count.",
                exit_code=2,
            )
        data: dict[str, Any] = compilation.model_dump(mode="json")
        data["creative_references"] = data["creative_references"][cursor : cursor + size]
        data["row_counts"] = {"creative_references": total}
        data["provenance"] = provenance
        while True:
            next_cursor = (
                str(cursor + len(data["creative_references"]))
                if cursor + len(data["creative_references"]) < total
                else None
            )
            # Size keyed references before touching the real sharing key. The
            # fixed in-memory key is used only for the byte-count probe.
            if _fits(
                "candidate show",
                _redact_payload(data, b"\0" * 32, False),
                preferences.max_output_bytes,
                outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
                next_cursor=next_cursor,
                total_count=total,
            ):
                break
            if len(data["creative_references"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Required candidate review exceeds the saved output bound.",
                    next_action=(
                        "Increase saved max_output_bytes; critical checks were not dropped."
                    ),
                )
            data["creative_references"].pop()
        emit(
            "candidate show",
            _redact_payload(data, export_key(), False),
            outcome="OFFLINE_CANDIDATE_NOT_EXECUTABLE",
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=next_cursor,
            total_count=total,
        )

    guarded("candidate show", work)
