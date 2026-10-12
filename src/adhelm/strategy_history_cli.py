"""Read retained strategy observations without granting current authority."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import typer

from adhelm.config import load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_cli import _fits
from adhelm.projections import _redact_payload
from adhelm.saved_strategies import load_strategy, strategy_catalog

app = typer.Typer(no_args_is_help=True)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    """List bounded sealed strategy observations from verified local history."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        preferences = load_configuration(root(ctx)).output
        if cursor < 0 or not 1 <= limit <= 500:
            raise AdhelmError(
                "INVALID_STRATEGY_SELECTION", "Invalid strategy pagination.", exit_code=2
            )
        data = strategy_catalog(
            Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
        )
        while not _fits(
            "strategy catalog",
            data,
            preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            if len(data["strategies"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Required strategy catalog row exceeds the saved output bound.",
                    next_action="Increase saved max_output_bytes to review this catalog.",
                )
            data["strategies"].pop()
            data["next_cursor"] = str(cursor + len(data["strategies"]))
        emit(
            "strategy catalog",
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded("strategy catalog", work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str) -> None:
    """Show one complete redacted historical observation from a verified checkpoint."""
    from adhelm.cli import emit, guarded, root

    def work() -> None:
        observation, provenance = load_strategy(Ledger(root(ctx)), record_digest)
        preferences = load_configuration(root(ctx)).output
        as_of = _utc_now()
        data: dict[str, Any] = observation.model_dump(mode="json")
        data["provenance"] = provenance
        data["historical_status"] = "RETAINED_OFFLINE_OBSERVATION"
        data["as_of"] = as_of.isoformat()
        data["expiry_status_at_as_of"] = (
            "EXPIRED_AT_AS_OF"
            if observation.expires_at <= as_of
            else "NOT_EXPIRED_AT_AS_OF_REMAINS_NON_EXECUTABLE"
        )
        # A strategy observation is atomic: no child review, native budget, or
        # provider-specific unmet gate may disappear merely to fit a response.
        probe = _redact_payload(data, b"\0" * 32, False)
        if not _fits(
            "strategy show",
            probe,
            preferences.max_output_bytes,
            outcome=observation.execution_status,
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED",
                "Complete retained strategy review exceeds the saved output bound.",
                next_action=(
                    "Increase saved max_output_bytes; required child reviews and planning "
                    "checks were not dropped."
                ),
            )
        emit(
            "strategy show",
            _redact_payload(data, export_key(), False),
            outcome=observation.execution_status,
            maximum_bytes=preferences.max_output_bytes,
        )

    guarded("strategy show", work)
