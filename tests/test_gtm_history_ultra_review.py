"""Adversarial retained-history checks for the fixture-only GTM measurement read."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from adhelm import __version__, gtm_measurement_workflow, measurement_cli
from adhelm.canonical import canonical_bytes, digest
from adhelm.cli import app, emit
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_records import GtmMeasurementSnapshot, MeasurementObservation
from adhelm.models import Configuration, OutputPreferences, Profile, ResultEnvelope
from adhelm.providers.gtm_identity import (
    GtmAccountFeatureAvailability,
    GtmContainerFeatureAvailability,
    GtmIdentityDiscovery,
)
from adhelm.providers.gtm_read import (
    GtmLatestHeaderCountStatus,
    GtmOpaqueCodeMetadata,
    GtmReadIdentity,
    GtmReadInventory,
    GtmResourceCounts,
    GtmVersionSnapshot,
    GtmWorkspaceMetadata,
)
from adhelm.saved_measurements import (
    load_measurement,
    measurement_rows,
    retain_measurement,
)

_READ_SCOPE = "https://www.googleapis.com/auth/tagmanager.readonly"
_PRIVATE_MARKER = "synthetic-private-gtm-source-must-not-escape"
_START = datetime(2026, 9, 4, tzinfo=UTC)


def _profile(**changes: object) -> Profile:
    values: dict[str, object] = {
        "profile_id": "gtm-read",
        "provider": "gtm",
        "account_ref": "1",
        "principal_ref": "principal.fixture",
        "client_ref": "client.fixture",
        "scopes": [_READ_SCOPE],
        "purpose": "READ",
        "credential_source": "EXPLICIT_CI",
        "credential_ref": "GTM_FIXTURE_TOKEN",
    }
    values.update(changes)
    return Profile.model_validate(values)


def _fingerprint(number: int) -> str:
    return f"{number:064x}"


def _counts(*, tags: int = 0, custom_templates: int = 0) -> GtmResourceCounts:
    return GtmResourceCounts(
        tags=tags,
        triggers=0,
        variables=0,
        folders=0,
        built_in_variables=0,
        zones=0,
        custom_templates=custom_templates,
        clients=0,
        gtag_configs=0,
        transformations=0,
    )


def _snapshot(
    *, opaque_count: int = 1, workspace_ids: tuple[str, ...] = ("9", "3")
) -> GtmMeasurementSnapshot:
    identity = GtmReadIdentity(
        account_ref="gtm.account.1",
        account_client_ref="1",
        container_ref="gtm.container.1.2",
        container_client_ref="2",
    )
    opaque_code = tuple(
        GtmOpaqueCodeMetadata(
            kind="CUSTOM_HTML_TAG",
            configuration_fingerprint=_fingerprint(index + 1),
        )
        for index in range(opaque_count)
    )
    discovery = GtmIdentityDiscovery(
        identity=identity,
        account_features=GtmAccountFeatureAvailability(support_multiple_containers="FALSE"),
        container_features=GtmContainerFeatureAvailability(support_workspaces="TRUE"),
    )
    inventory = GtmReadInventory(
        identity=identity,
        live=GtmVersionSnapshot(
            context="live",
            version_ref="gtm.container_version.1.2.7",
            configuration_fingerprint=_fingerprint(100),
            resource_counts=_counts(tags=opaque_count),
            opaque_code=opaque_code,
        ),
        latest=GtmVersionSnapshot(
            context="latest",
            version_ref="gtm.container_version.1.2.8",
            configuration_fingerprint=_fingerprint(101),
            resource_counts=_counts(),
        ),
        latest_header_count_status=GtmLatestHeaderCountStatus(status="ALL_HEADER_COUNTS_MATCH"),
        workspaces=tuple(
            GtmWorkspaceMetadata(
                workspace_ref=f"gtm.workspace.1.2.{workspace_id}",
                configuration_fingerprint=_fingerprint(200 + index),
            )
            for index, workspace_id in enumerate(workspace_ids)
        ),
    )
    return GtmMeasurementSnapshot(discovery=discovery, inventory=inventory)


def _configure(root: Path, *, output: OutputPreferences | None = None) -> None:
    save_configuration(
        root,
        Configuration(profiles=[_profile()], output=output or OutputPreferences()),
    )


def _stub_read(monkeypatch: pytest.MonkeyPatch, snapshot: GtmMeasurementSnapshot) -> None:
    monkeypatch.setattr(
        gtm_measurement_workflow,
        "validate_gtm_read",
        lambda *_args, **_kwargs: _profile(),
    )
    monkeypatch.setattr(
        gtm_measurement_workflow,
        "read_gtm",
        lambda *_args, **_kwargs: (snapshot.discovery, snapshot.inventory),
    )
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)


def _measurement(snapshot: GtmMeasurementSnapshot) -> MeasurementObservation:
    return MeasurementObservation(
        profile_ref="gtm-read",
        read_started_at=_START,
        read_finished_at=_START,
        snapshot=snapshot,
        snapshot_digest=digest(snapshot),
    )


def test_model_copy_parent_drift_cannot_retain_or_project_a_gtm_snapshot(tmp_path: Path) -> None:
    snapshot = _snapshot()
    moved_workspace = snapshot.inventory.workspaces[0].model_copy(
        update={"workspace_ref": "gtm.workspace.9.2.9"}
    )
    forged = snapshot.model_copy(
        update={
            "inventory": snapshot.inventory.model_copy(update={"workspaces": (moved_workspace,)})
        }
    )

    with pytest.raises(ValidationError):
        retain_measurement(
            Ledger(tmp_path),
            forged,
            profile_ref="gtm-read",
            read_started_at=_START,
            read_finished_at=_START,
        )
    assert not (tmp_path / "ledger").exists()

    with pytest.raises(ValidationError):
        _measurement(forged)

    projected_copy = _measurement(snapshot).model_copy(
        update={"snapshot": forged, "snapshot_digest": digest(forged)}
    )
    with pytest.raises(AdhelmError) as projected:
        measurement_rows(projected_copy)
    assert projected.value.code == "INVALID_MEASUREMENT_SELECTION"


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "case",
    [
        "duplicate_workspace",
        "wrong_live_context",
        "resource_count_total",
        "template_count_mismatch",
    ],
)
def test_gtm_snapshot_revalidation_rejects_each_new_binding_invariant(case: str) -> None:
    snapshot = _snapshot(workspace_ids=("9", "3"))
    inventory = snapshot.inventory
    if case == "duplicate_workspace":
        inventory = inventory.model_copy(
            update={"workspaces": (inventory.workspaces[0], inventory.workspaces[0])}
        )
    elif case == "wrong_live_context":
        inventory = inventory.model_copy(
            update={"live": inventory.live.model_copy(update={"context": "latest"})}
        )
    elif case == "resource_count_total":
        counts = inventory.live.resource_counts.model_copy(update={"tags": 4000, "triggers": 1})
        inventory = inventory.model_copy(
            update={"live": inventory.live.model_copy(update={"resource_counts": counts})}
        )
    else:
        template = GtmOpaqueCodeMetadata(
            kind="CUSTOM_TEMPLATE", configuration_fingerprint=_fingerprint(300)
        )
        inventory = inventory.model_copy(
            update={"live": inventory.live.model_copy(update={"opaque_code": (template,)})}
        )
    forged = snapshot.model_copy(update={"inventory": inventory})

    with pytest.raises(ValidationError):
        GtmMeasurementSnapshot.model_validate(forged.model_dump(mode="python"))


@pytest.mark.parametrize(  # type: ignore[untyped-decorator]
    "case", ["wrong_workspace_parent", "wrong_live_role"]
)
def test_rehashed_gtm_ledger_tamper_still_fails_verified_checkpoint(
    tmp_path: Path, case: str
) -> None:
    record = retain_measurement(
        Ledger(tmp_path),
        _snapshot(),
        profile_ref="gtm-read",
        read_started_at=_START,
        read_finished_at=_START,
    )
    path = next((tmp_path / "ledger" / "changes").glob("*.jsonl"))
    records = [json.loads(line) for line in path.read_text().splitlines()]
    index = next(
        index
        for index, candidate in enumerate(records)
        if candidate["record_digest"] == record["record_digest"]
    )
    serialized = records[index]
    if case == "wrong_workspace_parent":
        serialized["payload"]["snapshot"]["inventory"]["workspaces"][0]["workspace_ref"] = (
            "gtm.workspace.9.2.9"
        )
    else:
        serialized["payload"]["snapshot"]["inventory"]["live"]["context"] = "latest"
    serialized["payload"]["snapshot_digest"] = digest(serialized["payload"]["snapshot"])
    serialized["record_digest"] = digest(
        {field: value for field, value in serialized.items() if field != "record_digest"}
    )
    records[index] = serialized
    path.write_bytes(b"".join(canonical_bytes(item) + b"\n" for item in records))

    with pytest.raises(AdhelmError):
        Ledger(tmp_path).checkpoint()
    assert record["record_digest"] != serialized["record_digest"]


def test_injected_raw_code_from_a_model_copy_never_reaches_history_or_cli_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    snapshot = _snapshot()
    raw_marker = snapshot.inventory.live.opaque_code[0].model_copy(
        update={"raw_source": _PRIVATE_MARKER}
    )
    forged = snapshot.model_copy(
        update={
            "inventory": snapshot.inventory.model_copy(
                update={
                    "live": snapshot.inventory.live.model_copy(
                        update={"opaque_code": (raw_marker,)}
                    )
                }
            )
        }
    )
    _stub_read(monkeypatch, forged)

    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path), "measurement", "gtm", "gtm-read", "2", "--ci-enabled"],
    )

    assert result.exit_code == 0, result.output
    assert _PRIVATE_MARKER not in result.output
    observation, _ = load_measurement(
        Ledger(tmp_path), json.loads(result.output)["data"]["record_digest"]
    )
    assert _PRIVATE_MARKER.encode() not in canonical_bytes(observation)
    assert all("raw_source" not in row for row in measurement_rows(observation))

    serialized = observation.model_dump(mode="json")
    serialized["snapshot"]["inventory"]["live"]["opaque_code"][0]["raw_source"] = _PRIVATE_MARKER
    rejected_root = tmp_path / "serialized-raw-rejection"
    with pytest.raises(AdhelmError) as rejected:
        Ledger(rejected_root).append(
            "changes",
            "MEASUREMENT_OBSERVATION",
            serialized,
            provider="gtm",
            account_ref="gtm.account.1",
            source="UNKNOWN",
            observed_at=_START.isoformat(),
        )
    assert _PRIVATE_MARKER not in str(rejected.value)
    assert not (rejected_root / "ledger").exists()


def test_invalid_gtm_public_input_fails_before_sharing_key_or_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    calls: list[str] = []

    def forbidden_key() -> bytes:
        calls.append("key")
        raise AssertionError("invalid GTM input requested a sharing key")

    def forbidden_reader(*_args: object, **_kwargs: object) -> object:
        calls.append("reader")
        raise AssertionError("invalid GTM input reached the reader")

    monkeypatch.setattr(measurement_cli, "export_key", forbidden_key)
    monkeypatch.setattr(gtm_measurement_workflow, "read_gtm", forbidden_reader)
    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "gtm",
            "gtm-read",
            "not-a-native-container-id",
            "--ci-enabled",
        ],
    )

    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "INVALID_WORKFLOW_INPUT"
    assert calls == [] and not (tmp_path / "ledger").exists()


def test_gtm_show_pages_without_omission_and_preserves_uncertainty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = _snapshot(opaque_count=18, workspace_ids=("12", "2", "9"))
    _configure(tmp_path, output=OutputPreferences(max_rows=50, max_output_bytes=4096))
    _stub_read(monkeypatch, snapshot)
    runner = CliRunner()
    retained = runner.invoke(
        app,
        ["--data-dir", str(tmp_path), "measurement", "gtm", "gtm-read", "2", "--ci-enabled"],
    )
    assert retained.exit_code == 0, retained.output
    receipt = json.loads(retained.output)["data"]
    observation, _ = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    assert tuple(
        workspace.workspace_ref for workspace in observation.snapshot.inventory.workspaces
    ) == ("gtm.workspace.1.2.12", "gtm.workspace.1.2.2", "gtm.workspace.1.2.9")

    cursor = "0"
    pages = 0
    seen_rows: list[bytes] = []
    while True:
        shown = runner.invoke(
            app,
            [
                "--data-dir",
                str(tmp_path),
                "measurement",
                "show",
                receipt["record_digest"],
                "--cursor",
                cursor,
                "--limit",
                "50",
            ],
        )
        assert shown.exit_code == 0, shown.output
        assert len(shown.output.encode()) <= 4096
        envelope = json.loads(shown.output)
        data = envelope["data"]
        assert data["dependency_completeness"] == "NOT_PROVEN"
        assert data["observation_status"] == "SEQUENTIALLY_REREAD_NONATOMIC"
        assert data["staging_eligibility"] == "NOT_EVALUATED"
        assert data["latest_header_count_status"]["status"] == "ALL_HEADER_COUNTS_MATCH"
        assert data["identity"]["container_ref"].startswith("opaque:")
        assert data["rows"]
        seen_rows.extend(canonical_bytes(row) for row in data["rows"])
        pages += 1
        if envelope["next_cursor"] is None:
            break
        assert envelope["next_cursor"] == str(len(seen_rows))
        cursor = envelope["next_cursor"]

    assert pages > 1
    assert len(seen_rows) == len(set(seen_rows)) == receipt["row_count"]
    assert receipt["row_count"] == observation.snapshot.row_count


def test_emit_counts_the_utf8_newline_at_the_exact_output_boundary(
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = {"label": "雪"}
    raw = canonical_bytes(
        ResultEnvelope(
            tool_version=__version__,
            command="unicode-boundary",
            outcome="OK",
            data=data,
        )
    )

    emit("unicode-boundary", data, maximum_bytes=len(raw) + 1)
    assert capsys.readouterr().out.encode("utf-8") == raw + b"\n"

    with pytest.raises(AdhelmError) as raised:
        emit("unicode-boundary", data, maximum_bytes=len(raw))
    assert raised.value.code == "OUTPUT_LIMIT_EXCEEDED"
