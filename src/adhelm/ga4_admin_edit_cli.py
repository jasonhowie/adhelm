"""Explicit EDIT-role configuration reads and bounded local historical review."""

from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from adhelm.canonical import digest
from adhelm.config import configuration_transaction, load_configuration
from adhelm.credentials import export_key
from adhelm.errors import AdhelmError
from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation
from adhelm.ga4_admin_edit_workflow import (
    read_ga4_admin_edit_inventory,
    validate_ga4_admin_edit_inventory,
)
from adhelm.ledger import Ledger
from adhelm.projections import _redact_payload
from adhelm.saved_ga4_admin_edit_inventories import (
    ga4_admin_edit_inventory_catalog,
    load_ga4_admin_edit_inventory,
    retain_ga4_admin_edit_inventory,
    validate_selection,
)
from adhelm.strict_json import validate_json_value

app = typer.Typer(no_args_is_help=True)


def _receipt(record_digest: str, inventory_digest: str, count: int) -> dict[str, Any]:
    return {
        "record_digest": record_digest,
        "inventory_digest": inventory_digest,
        "row_count": count,
        "evidence_kind": "EDIT_PROFILE_CONFIGURATION_READ",
        "persisted": True,
        "granted_scopes": "NOT_EVALUATED",
        "create_permission": "NOT_PREPROVABLE",
        "execution_authority": False,
        "remote_mutations": False,
    }


@app.command("read")
def read(
    ctx: typer.Context,
    profile: Annotated[str, typer.Option("--profile")],
    stream_id: Annotated[str, typer.Option("--stream-id")],
    ci_enabled: bool = False,
) -> None:
    """Read and retain key-event inventory under a configured EDIT profile; no writes."""
    from adhelm.cli import emit, guarded, root
    from adhelm.measurement_cli import _fits

    command = "measurement ga4-admin-edit read"

    def work() -> None:
        directory = root(ctx)
        configuration, selected, _ = validate_ga4_admin_edit_inventory(
            directory, profile, stream_id=stream_id, ci_enabled=ci_enabled
        )
        maximum = configuration.output.max_output_bytes
        if not _fits(
            command, _receipt("0" * 64, "0" * 64, 1000), maximum, outcome="CONFIGURATION_RETAINED"
        ):
            raise AdhelmError(
                "OUTPUT_LIMIT_EXCEEDED", "EDIT inventory receipt exceeds output bounds."
            )
        ledger = Ledger(directory)
        ledger.checkpoint()
        expected = digest(configuration)
        with configuration_transaction(directory) as transaction:
            transaction.require_unchanged(expected)
            key = export_key()
        started = datetime.now(UTC)
        inventory = read_ga4_admin_edit_inventory(
            directory,
            profile,
            stream_id=stream_id,
            fingerprint_key=key,
            ci_enabled=ci_enabled,
            expected_configuration_digest=expected,
        )
        finished = datetime.now(UTC)
        observation = validate_json_value(
            Ga4AdminEditInventoryObservation,
            {
                "profile_ref": selected.profile_id,
                "configuration_digest": expected,
                "profile_configuration_digest": digest(selected),
                "read_started_at": started.isoformat(),
                "read_finished_at": finished.isoformat(),
                "inventory": inventory.model_dump(mode="json", warnings="error"),
                "inventory_digest": digest(inventory),
            },
        )
        try:
            # Lock ordering is configuration -> ledger. No HTTP is performed
            # under either lock; cooperating config writers cannot race append.
            with configuration_transaction(directory) as transaction:
                transaction.require_unchanged(expected)
                record = retain_ga4_admin_edit_inventory(ledger, observation, fingerprint_key=key)
        except OSError:
            raise AdhelmError(
                "MEASUREMENT_RETENTION_UNVERIFIED",
                "The read finished, but durable EDIT inventory retention is unverified.",
                next_action="Inspect history verify and measurement ga4-admin-edit catalog before "
                "repeating; an observation may already be retained.",
            ) from None
        emit(
            command,
            _receipt(
                record["record_digest"],
                observation.inventory_digest,
                inventory.capacity.complete_observed_key_event_total,
            ),
            outcome="CONFIGURATION_RETAINED",
            maximum_bytes=maximum,
        )

    guarded(command, work)


@app.command("catalog")
def catalog(ctx: typer.Context, cursor: int = 0, limit: int = 50) -> None:
    """List retained EDIT observations without credential or provider access."""
    from adhelm.cli import emit, guarded, root
    from adhelm.measurement_cli import _fits

    command = "measurement ga4-admin-edit catalog"

    def work() -> None:
        validate_selection(cursor, limit)
        preferences = load_configuration(root(ctx)).output
        data = ga4_admin_edit_inventory_catalog(
            Ledger(root(ctx)), cursor=cursor, limit=min(limit, preferences.max_rows)
        )
        while not _fits(
            command,
            data,
            preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        ):
            if len(data["inventories"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED", "EDIT inventory catalog exceeds output bounds."
                )
            data["inventories"].pop()
            data["next_cursor"] = str(cursor + len(data["inventories"]))
        emit(
            command,
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=data["next_cursor"],
            total_count=data["total_count"],
        )

    guarded(command, work)


@app.command("show")
def show(ctx: typer.Context, record_digest: str, cursor: int = 0, limit: int = 50) -> None:
    """Review historical configuration facts; history is not current permission or authority."""
    from adhelm.cli import emit, guarded, root
    from adhelm.measurement_cli import _fits

    command = "measurement ga4-admin-edit show"

    def work() -> None:
        validate_selection(cursor, limit)
        observation, provenance = load_ga4_admin_edit_inventory(Ledger(root(ctx)), record_digest)
        preferences = load_configuration(root(ctx)).output
        inventory = observation.inventory
        if cursor > len(inventory.key_events):
            raise AdhelmError(
                "INVALID_MEASUREMENT_SELECTION", "Cursor exceeds inventory rows.", exit_code=2
            )
        selected = inventory.key_events[cursor : cursor + min(limit, preferences.max_rows)]
        data: dict[str, Any] = {
            "inventory_digest": observation.inventory_digest,
            "profile_ref": observation.profile_ref,
            "property_ref": inventory.target.property_ref,
            "web_stream_ref": inventory.target.web_stream_ref,
            "read_started_at": observation.read_started_at.isoformat(),
            "read_finished_at": observation.read_finished_at.isoformat(),
            "evidence_kind": observation.evidence_kind,
            "consistency": observation.consistency,
            "implementation_evidence": "FIXTURE_VERIFIED_LIVE_UNVERIFIED",
            "content_authentication": "NOT_CHECKED",
            "granted_scopes": observation.granted_scopes,
            "create_permission": observation.create_permission,
            "runtime_firing": observation.runtime_firing,
            "processed_receipt": observation.processed_receipt,
            "execution_authority": False,
            "remote_mutations": False,
            "capacity": inventory.capacity.model_dump(mode="json"),
            "provenance": provenance,
            "rows": [
                item.model_dump(
                    mode="json",
                    include={
                        "resource_ref",
                        "counting_method",
                        "default_value",
                        "custom",
                        "deletable",
                    },
                )
                for item in selected
            ],
        }
        # Size preflight uses equal-length opaque references without looking up
        # the OS-store sharing key. Public review never emits member/content MACs.
        data = _redact_payload(data, b"\0" * 32, False)
        while True:
            next_cursor = (
                str(cursor + len(data["rows"]))
                if cursor + len(data["rows"]) < len(inventory.key_events)
                else None
            )
            if _fits(
                command,
                data,
                preferences.max_output_bytes,
                next_cursor=next_cursor,
                total_count=len(inventory.key_events),
            ):
                break
            if len(data["rows"]) <= 1:
                raise AdhelmError(
                    "OUTPUT_LIMIT_EXCEEDED",
                    "Required EDIT review exceeds saved output bounds.",
                    next_action="Increase max_output_bytes to review the full historical context.",
                )
            data["rows"].pop()
        # Rebuild redaction from the original references; never HMAC an already
        # redacted reference under the user's key.
        key = export_key()
        data.update(
            _redact_payload(
                {
                    "profile_ref": observation.profile_ref,
                    "property_ref": inventory.target.property_ref,
                    "web_stream_ref": inventory.target.web_stream_ref,
                    "rows": [
                        item.model_dump(
                            mode="json",
                            include={
                                "resource_ref",
                                "counting_method",
                                "default_value",
                                "custom",
                                "deletable",
                            },
                        )
                        for item in selected[: len(data["rows"])]
                    ],
                },
                key,
                False,
            )
        )
        emit(
            command,
            data,
            maximum_bytes=preferences.max_output_bytes,
            next_cursor=next_cursor,
            total_count=len(inventory.key_events),
        )

    guarded(command, work)
