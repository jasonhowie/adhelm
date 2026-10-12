"""Adversarial boundaries for retained measurement observations and CLI review."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from adhelm import measurement_cli, openai_measurement_workflow
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.measurement_records import MeasurementObservation, OpenAIMeasurementSnapshot
from adhelm.models import Configuration, Profile
from adhelm.providers.openai_ads import OpenAIAdsIdentity
from adhelm.providers.openai_conversions import OpenAIAdsConversionSetting
from adhelm.saved_measurements import load_measurement, measurement_catalog, measurement_rows


def _profile() -> Profile:
    return Profile(
        profile_id="openai-measurement-read",
        provider="openai_ads",
        account_ref="account_fixture",
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["account-scoped-api-key"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_source="EXPLICIT_CI",
        credential_ref="OPENAI_ADS_FIXTURE_TOKEN",
    )


def _measurement_result() -> tuple[OpenAIAdsIdentity, tuple[OpenAIAdsConversionSetting, ...]]:
    identity = OpenAIAdsIdentity(
        account_ref="openai_ads.account.account_fixture",
        account_client_ref="account_fixture",
        timezone="UTC",
        currency="USD",
        status="active",
    )
    return identity, (
        OpenAIAdsConversionSetting(
            account_ref=identity.account_ref,
            conversion_setting_ref="openai_ads.conversion_setting.setting_fixture",
            conversion_setting_client_ref="setting_fixture",
            event_type="order_created",
            attribution_window_days=30,
            status="NOT_ARCHIVED",
            campaign_count=0,
            version=1,
        ),
    )


def _configure(root: Path) -> None:
    save_configuration(root, Configuration(profiles=[_profile()]))


def _ga4_profile() -> Profile:
    return Profile(
        profile_id="ga4-measurement-read",
        provider="ga4_admin",
        account_ref="123",
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_source="EXPLICIT_CI",
        credential_ref="GA4_ADMIN_FIXTURE_TOKEN",
    )


def test_invalid_ga4_public_input_fails_before_sharing_key_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_configuration(tmp_path, Configuration(profiles=[_ga4_profile()]))
    monkeypatch.setattr(
        measurement_cli,
        "export_key",
        lambda: (_ for _ in ()).throw(AssertionError("invalid GA4 input requested sharing key")),
    )

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "ga4-admin",
            "ga4-measurement-read",
            "not-a-native-stream-id",
        ],
    )

    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "INVALID_WORKFLOW_INPUT"


def test_openai_measurement_read_never_requires_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    monkeypatch.setattr(
        openai_measurement_workflow,
        "read_openai_conversions",
        lambda *_args, **_kwargs: _measurement_result(),
    )
    monkeypatch.setattr(
        measurement_cli,
        "export_key",
        lambda: (_ for _ in ()).throw(AssertionError("OpenAI read requested sharing key")),
    )

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "openai-conversions",
            "openai-measurement-read",
            "--ci-enabled",
        ],
    )

    assert result.exit_code == 0, result.output
    assert measurement_catalog(Ledger(tmp_path))["total_count"] == 1


def test_preappend_ledger_rejection_is_not_reported_as_uncertain_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(tmp_path)
    monkeypatch.setattr(
        openai_measurement_workflow,
        "read_openai_conversions",
        lambda *_args, **_kwargs: _measurement_result(),
    )
    monkeypatch.setattr(
        measurement_cli,
        "retain_measurement",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AdhelmError("INVALID_LEDGER_PAYLOAD", "Rejected before append.", exit_code=2)
        ),
    )

    result = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "openai-conversions",
            "openai-measurement-read",
            "--ci-enabled",
        ],
    )

    assert result.exit_code != 0
    assert json.loads(result.output)["diagnostics"][0]["code"] == "INVALID_LEDGER_PAYLOAD"
    assert not measurement_catalog(Ledger(tmp_path))["measurements"]


def test_invalid_record_digest_type_fails_closed_before_checkpoint(tmp_path: Path) -> None:
    with pytest.raises(AdhelmError) as raised:
        load_measurement(Ledger(tmp_path), True)  # type: ignore[arg-type]
    assert raised.value.code == "INVALID_MEASUREMENT_SELECTION"
    assert not (tmp_path / "ledger").exists()


def test_measurement_rows_revalidates_a_model_copy_before_projecting_it() -> None:
    identity, settings = _measurement_result()
    snapshot = OpenAIMeasurementSnapshot(identity=identity, settings=settings)
    observation = MeasurementObservation(
        profile_ref="openai-measurement-read",
        read_started_at=datetime(2026, 9, 4, tzinfo=UTC),
        read_finished_at=datetime(2026, 9, 4, 0, 0, 1, tzinfo=UTC),
        snapshot=snapshot,
        snapshot_digest=digest(snapshot),
    )
    forged_setting = settings[0].model_copy(
        update={"conversion_setting_ref": "openai_ads.conversion_setting.other"}
    )
    forged = observation.model_copy(
        update={"snapshot": snapshot.model_copy(update={"settings": (forged_setting,)})}
    )

    with pytest.raises(AdhelmError) as raised:
        measurement_rows(forged)
    assert raised.value.code == "INVALID_MEASUREMENT_SELECTION"


def test_ledger_append_retries_a_short_write_before_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "measurement.jsonl"
    real_fdopen = os.fdopen
    writes: list[bytes] = []

    class PartialWriter:
        def __init__(self, file: Any) -> None:
            self.file = file

        def __enter__(self) -> PartialWriter:
            return self

        def __exit__(self, *_args: object) -> None:
            self.file.close()

        def write(self, data: bytes) -> int:
            chunk = data[:3] if not writes else data
            writes.append(chunk)
            result = self.file.write(chunk)
            if not isinstance(result, int):
                raise AssertionError("fixture write did not report a byte count")
            return result

        def flush(self) -> None:
            self.file.flush()

        def fileno(self) -> int:
            result = self.file.fileno()
            if not isinstance(result, int):
                raise AssertionError("fixture file has no descriptor")
            return result

    def partial_fdopen(*args: Any, **kwargs: Any) -> PartialWriter:
        return PartialWriter(real_fdopen(*args, **kwargs))

    monkeypatch.setattr(os, "fdopen", partial_fdopen)
    Ledger._append_bytes(destination, b"complete-record\n")

    assert destination.read_bytes() == b"complete-record\n"
    assert len(writes) >= 2
