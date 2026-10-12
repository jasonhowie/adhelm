"""Independent root-app review of GA4 key-event CREATE plan commands.

All preparation uses the existing fake OAuth store and ``httpx.MockTransport``
fixture. These cases invoke the installed root Typer application; no real
credential store, network, browser, or provider operation is permitted.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from test_ga4_admin_execution_observation import _ACCESS, _KEY, _NOW, _REFRESH, _SUBJECT
from test_ga4_create_cli import _write_request
from test_ga4_create_workflow import _setup
from typer.testing import CliRunner

import adhelm.cli as root_cli
import adhelm.ga4_create_cli as ga4_create_cli
from adhelm.config import save_configuration
from adhelm.errors import ExitCode
from adhelm.ga4_create_workflow import (
    RetainedGa4KeyEventCreatePlan,
    prepare_and_retain_ga4_key_event_create_plan,
)
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, OutputPreferences
from adhelm.saved_ga4_create_plans import load_ga4_key_event_create_plan


def _invoke(directory: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        root_cli.app,
        ["--data-dir", str(directory), "plan", "ga4-key-event", *arguments],
    )


def _root(*arguments: str) -> Any:
    return CliRunner().invoke(root_cli.app, list(arguments))


def _data(result: Any) -> dict[str, object]:
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert type(envelope["data"]) is dict
    return envelope["data"]


def _error_code(result: Any) -> str:
    assert result.exit_code != 0, result.output
    envelope = json.loads(result.output)
    return envelope["diagnostics"][0]["code"]


def _assert_private_values_absent(rendered: str, directory: Path, profile_id: str) -> None:
    for private in (
        _ACCESS,
        _REFRESH,
        _SUBJECT,
        profile_id,
        "qualified_request",
        "A customer-defined qualified request event.",
        "properties/123",
        "dataStreams/456",
        str(directory),
    ):
        assert private not in rendered


def test_root_create_at_minimum_bound_emits_only_a_compact_no_authority_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, ledger, _store, profile, configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    save_configuration(
        tmp_path,
        configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=1024)}),
        replace=True,
    )
    source = _write_request(tmp_path, request)

    monkeypatch.setattr(
        ga4_create_cli,
        "export_key",
        lambda: pytest.fail("create receipt must not acquire the sharing key"),
    )
    result = _invoke(tmp_path, "create", "--input", str(source))

    data = _data(result)
    assert len(result.output.encode("utf-8")) <= 1024
    assert data["execution_authority"] is False
    assert data["remote_mutations"] is False
    assert data["execution_command"] == "UNRELEASED"
    assert data["review_required"] is True
    assert data["review_command"] == f"plan ga4-key-event show {data['record_digest']}"
    assert data["maximum_incremental_spend"] == {"currency": "USD", "minor_units": 0}
    assert len(calls) == 3 and all(item.method == "GET" for item in calls)
    assert data["record_digest"] in {
        item["record_digest"] for item in ledger.checkpoint()["records_by_stream"]["changes"]
    }
    _assert_private_values_absent(result.output, tmp_path, profile.profile_id)


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        (b'{"profile_id":"private-sentinel",', "INVALID_JSON"),
        (b'{"schema_version":1.0,"profile_id":"private-sentinel"}', "SCHEMA_VALIDATION_FAILED"),
        (b'{"profile_id":"a","profile_id":"private-sentinel"}', "INVALID_JSON"),
        pytest.param(b"x" * 1_048_577, "INPUT_TOO_LARGE", id="oversized"),
    ],
)
def test_malformed_or_oversized_request_stops_before_configuration_or_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes,
    expected: str,
) -> None:
    source = tmp_path / "private-input-path.json"
    source.write_bytes(contents)

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid request reached configuration or workflow")

    monkeypatch.setattr(ga4_create_cli, "load_configuration", prohibited)
    monkeypatch.setattr(ga4_create_cli, "prepare_and_retain_ga4_key_event_create_plan", prohibited)
    monkeypatch.setattr(ga4_create_cli, "export_key", prohibited)
    result = _invoke(tmp_path, "create", "--input", str(source))

    assert _error_code(result) == expected
    assert "private-sentinel" not in result.output
    assert str(source) not in result.output
    assert not Ledger(tmp_path).records("changes")


def test_incomplete_matching_artifact_rule_fails_before_provider_key_or_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, ledger, _store, profile, configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    preferences = OutputPreferences(
        artifact_mode="RULES",
        purposes=["plan_review"],
        workflow_rules=[
            ArtifactRule(
                workflow="plan",
                stage="before_approval",
                providers=["ga4_admin"],
                profiles=[profile.profile_id],
                formats=["json"],
            )
        ],
        output_directory=None,
    )
    save_configuration(
        tmp_path,
        configuration.model_copy(update={"output": preferences}),
        replace=True,
    )
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("artifact refusal reached workflow or sharing-key access")

    monkeypatch.setattr(ga4_create_cli, "prepare_and_retain_ga4_key_event_create_plan", prohibited)
    monkeypatch.setattr(ga4_create_cli, "export_key", prohibited)
    result = _invoke(tmp_path, "create", "--input", str(source))

    assert _error_code(result) == "ARTIFACT_CONFIGURATION_REQUIRED"
    assert calls == []
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint
    assert not list(tmp_path.glob("artifact-*"))


def test_artifact_policy_or_output_change_after_preflight_stops_before_provider_or_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The workflow must use the exact output-policy snapshot that the CLI preflighted."""

    request, ledger, _store, profile, configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    changed = configuration.model_copy(
        update={
            "output": OutputPreferences(
                artifact_mode="RULES",
                purposes=["plan_review"],
                workflow_rules=[
                    ArtifactRule(
                        workflow="plan",
                        stage="before_approval",
                        providers=["ga4_admin"],
                        profiles=[profile.profile_id],
                        formats=["json"],
                    )
                ],
                output_directory=str(tmp_path),
            )
        }
    )
    prepare = ga4_create_cli.prepare_and_retain_ga4_key_event_create_plan

    def change_then_prepare(*args: object, **kwargs: object) -> RetainedGa4KeyEventCreatePlan:
        save_configuration(tmp_path, changed, replace=True)
        return prepare(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        ga4_create_cli,
        "prepare_and_retain_ga4_key_event_create_plan",
        change_then_prepare,
    )
    result = _invoke(tmp_path, "create", "--input", str(source))

    assert _error_code(result) == "CONFIGURATION_DRIFT"
    assert calls == []
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint
    assert not list(tmp_path.glob("artifact-*"))


def test_receipt_output_limit_stops_before_workflow_provider_or_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, ledger, _store, _profile, _configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    monkeypatch.setattr(ga4_create_cli, "_receipt_probe", lambda: {"padding": "x" * 16_384})

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("oversized receipt preflight reached workflow or sharing-key access")

    monkeypatch.setattr(ga4_create_cli, "prepare_and_retain_ga4_key_event_create_plan", prohibited)
    monkeypatch.setattr(ga4_create_cli, "export_key", prohibited)
    result = _invoke(tmp_path, "create", "--input", str(source))

    assert _error_code(result) == "OUTPUT_LIMIT_EXCEEDED"
    assert calls == []
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


def test_forged_valid_shape_workflow_result_cannot_emit_an_unretained_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, ledger, _store, _profile, _configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    forged = RetainedGa4KeyEventCreatePlan(
        action=retained.action,
        configuration=retained.configuration,
        plan_record_digest="f" * 64,
        observation_record_digest=retained.observation_record_digest,
    )
    prior_calls = list(calls)
    monkeypatch.setattr(
        ga4_create_cli,
        "prepare_and_retain_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: forged,
    )
    monkeypatch.setattr(
        ga4_create_cli,
        "export_key",
        lambda: pytest.fail("receipt shaping must not acquire a sharing key"),
    )
    result = _invoke(tmp_path, "create", "--input", str(source))

    assert _error_code(result) == "GA4_CREATE_PLAN_RESULT_INVALID"
    assert "f" * 64 not in result.output
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint
    assert calls == prior_calls


def test_post_retention_receipt_io_failure_is_ambiguous_after_an_actual_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A receipt read failure cannot make a durable plan safe to repeat."""

    request, ledger, _store, _profile, _configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    sentinel = "post-retention-private-io-sentinel"

    def unavailable(*_args: object, **_kwargs: object) -> tuple[object, object]:
        raise OSError(sentinel)

    monkeypatch.setattr(ga4_create_cli, "load_ga4_key_event_create_plan", unavailable)
    result = _invoke(tmp_path, "create", "--input", str(source))

    assert result.exit_code == ExitCode.AMBIGUOUS
    assert _error_code(result) == "GA4_CREATE_PLAN_RECEIPT_UNVERIFIED"
    envelope = json.loads(result.output)
    assert (
        "do not repeat an ambiguous plan preparation" in envelope["diagnostics"][0]["next_action"]
    )
    assert sentinel not in result.output
    assert len(calls) == 3 and all(item.method == "GET" for item in calls)
    assert ledger.checkpoint()["checkpoint_digest"] != checkpoint
    assert [item["record_kind"] for item in ledger.checkpoint()["records_by_stream"]["changes"]][
        -2:
    ] == [
        "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION",
        "GA4_KEY_EVENT_CREATE_PLAN",
    ]


def test_root_catalog_is_closed_no_authority_history_without_key_or_record_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ledger, _store, profile, _configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    prior_calls = list(calls)

    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("catalog must not obtain a key, load a record, or prepare a plan")

    monkeypatch.setattr(ga4_create_cli, "export_key", prohibited)
    monkeypatch.setattr(ga4_create_cli, "load_ga4_key_event_create_plan", prohibited)
    monkeypatch.setattr(ga4_create_cli, "prepare_and_retain_ga4_key_event_create_plan", prohibited)
    result = _invoke(tmp_path, "catalog")

    data = _data(result)
    assert data["execution_authority"] is False
    assert data["remote_mutations"] is False
    assert data["execution_command"] == "UNRELEASED"
    assert data["historical_evidence_status"] == "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"
    assert data["plans"][0]["record_digest"] == retained.plan_record_digest
    assert data["plans"][0]["execution_authority"] is False
    assert calls == prior_calls
    _assert_private_values_absent(result.output, tmp_path, profile.profile_id)


def test_root_catalog_rejects_invalid_pagination_before_configuration_or_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prohibited(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("invalid pagination reached configuration or history")

    monkeypatch.setattr(ga4_create_cli, "load_configuration", prohibited)
    monkeypatch.setattr(ga4_create_cli, "ga4_key_event_create_plan_catalog", prohibited)
    result = _invoke(tmp_path, "catalog", "--cursor", "-1")

    assert _error_code(result) == "INVALID_GA4_CREATE_PLAN_SELECTION"


def test_root_show_is_redacted_historical_and_marks_an_expired_plan_without_provider_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ledger, _store, profile, _configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    prior_calls = list(calls)
    monkeypatch.setattr(ga4_create_cli, "export_key", lambda: _KEY)
    monkeypatch.setattr(ga4_create_cli, "_now_utc", lambda: _NOW + timedelta(days=1))
    result = _invoke(tmp_path, "show", retained.plan_record_digest)

    data = _data(result)
    assert data["historical_review"] is True
    assert data["execution_authority"] is False
    assert data["remote_mutations"] is False
    assert data["execution_command"] == "UNRELEASED"
    assert data["review"]["expired_at_review"] is True
    assert data["review"]["current_state_status"] == "RECHECK_REQUIRED_BEFORE_EGRESS"
    assert calls == prior_calls
    _assert_private_values_absent(result.output, tmp_path, profile.profile_id)


def test_show_rejects_forged_loader_provenance_before_sharing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loader result cannot introduce raw values into the historical review."""

    request, ledger, _store, _profile, _configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    action, provenance = load_ga4_key_event_create_plan(ledger, retained.plan_record_digest)
    forged = {**provenance, "raw_event_name": "qualified_request", "access_token": _ACCESS}
    prior_calls = list(calls)

    monkeypatch.setattr(
        ga4_create_cli,
        "load_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: (action, forged),
    )
    monkeypatch.setattr(
        ga4_create_cli,
        "export_key",
        lambda: pytest.fail("forged show provenance acquired a sharing key"),
    )
    result = _invoke(tmp_path, "show", retained.plan_record_digest)

    assert _error_code(result) == "INVALID_GA4_CREATE_PLAN_REVIEW"
    assert "qualified_request" not in result.output
    assert _ACCESS not in result.output
    assert calls == prior_calls


def test_show_bad_selector_and_oversize_review_fail_before_sharing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request, _ledger, _store, _profile, configuration, _current, calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    prior_calls = list(calls)

    def prohibited_key() -> bytes:
        raise AssertionError("invalid or oversized review acquired a sharing key")

    monkeypatch.setattr(ga4_create_cli, "export_key", prohibited_key)
    bad = _invoke(tmp_path, "show", "private-sentinel")
    assert _error_code(bad) == "INVALID_GA4_CREATE_PLAN_SELECTION"
    assert "private-sentinel" not in bad.output

    save_configuration(
        tmp_path,
        configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=1024)}),
        replace=True,
    )
    monkeypatch.setattr(ga4_create_cli, "_now_utc", lambda: _NOW)
    oversized = _invoke(tmp_path, "show", retained.plan_record_digest)
    assert _error_code(oversized) == "OUTPUT_LIMIT_EXCEEDED"
    assert calls == prior_calls


def test_root_discovery_and_schemas_expose_only_closed_request_and_record_shapes() -> None:
    discovery = _root("discover")
    data = _data(discovery)
    assert {
        "plan ga4-key-event create",
        "plan ga4-key-event catalog",
        "plan ga4-key-event show",
    } <= set(data["commands"])

    request_schema = _data(_root("schema", "ga4-key-event-create-plan-request", "--root-only"))[
        "schema"
    ]
    record_schema = _data(_root("schema", "ga4-key-event-create-plan-record", "--root-only"))[
        "schema"
    ]
    assert request_schema["additionalProperties"] is False
    assert request_schema["properties"]["schema_version"]["const"] == 1
    assert request_schema["properties"]["expires_in_seconds"]["minimum"] == 60
    assert request_schema["properties"]["expires_in_seconds"]["maximum"] == 900
    assert record_schema["additionalProperties"] is False
    assert record_schema["properties"]["execution_authority"]["const"] is False
    assert record_schema["properties"]["requires_current_kernel_checks"]["const"] is True


def test_closed_catalog_does_not_emit_forged_raw_event_or_token_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent-visible catalog boundary validates helper output before emit."""

    request, _ledger, _store, _profile, _configuration, _current, _calls, _contract = _setup(
        tmp_path, monkeypatch
    )
    source = _write_request(tmp_path, request)
    del source  # The setup supplies an installed configuration; catalog has no input file.
    forged: dict[str, Any] = {
        "plans": [
            {
                "record_digest": "a" * 64,
                "plan_digest": "b" * 64,
                "recorded_at": _NOW.isoformat(),
                "expires_at": (_NOW + timedelta(minutes=5)).isoformat(),
                "capability_status": "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
                "maximum_incremental_spend": {"minor_units": 0, "currency": "USD"},
                "execution_authority": False,
                "raw_event_name": "qualified_request",
                "access_token": _ACCESS,
            }
        ],
        "checkpoint_digest": "c" * 64,
        "total_count": 1,
        "next_cursor": None,
        "historical_evidence_status": "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
    }
    monkeypatch.setattr(
        ga4_create_cli,
        "ga4_key_event_create_plan_catalog",
        lambda *_args, **_kwargs: forged,
    )
    result = _invoke(tmp_path, "catalog")

    assert _error_code(result) == "INVALID_GA4_CREATE_PLAN_CATALOG"
    assert "qualified_request" not in result.output
    assert _ACCESS not in result.output
