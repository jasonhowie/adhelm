from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from test_ga4_admin_execution_observation import _ACCESS, _KEY, _NOW, _REFRESH, _SUBJECT
from test_ga4_create_workflow import _setup
from typer.testing import CliRunner

import adhelm.cli as root_cli
import adhelm.ga4_create_cli as ga4_create_cli
from adhelm.canonical import digest
from adhelm.config import save_configuration
from adhelm.errors import ExitCode
from adhelm.ga4_create_workflow import (
    Ga4KeyEventCreatePlanRequest,
    RetainedGa4KeyEventCreatePlan,
    prepare_and_retain_ga4_key_event_create_plan,
)
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, OutputPreferences
from adhelm.saved_ga4_create_plans import load_ga4_key_event_create_plan


def _install_root(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    monkeypatch.setattr(root_cli, "root", lambda _ctx: directory)


def _invoke(*arguments: str) -> Any:
    return CliRunner().invoke(ga4_create_cli.app, list(arguments))


def _invoke_installed(directory: Path, *arguments: str) -> Any:
    return CliRunner().invoke(
        root_cli.app,
        ["--data-dir", str(directory), "plan", "ga4-key-event", *arguments],
    )


def _write_request(directory: Path, request: Ga4KeyEventCreatePlanRequest) -> Path:
    path = directory / "ga4-create-request.json"
    path.write_text(request.model_dump_json())
    return path


def _error_code(result: Any) -> str:
    return cast(str, json.loads(result.output)["diagnostics"][0]["code"])


def test_compact_receipt_probe_fits_the_minimum_installed_bound() -> None:
    from adhelm.measurement_cli import _fits

    assert _fits(
        "plan ga4-key-event create",
        ga4_create_cli._receipt_probe(),
        1024,
        outcome="GA4_KEY_EVENT_CREATE_PLAN_RETAINED_NO_EXECUTION_AUTHORITY",
    )


def test_create_returns_compact_key_free_receipt_after_fixed_gets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, store, profile, config, _, calls, _ = _setup(tmp_path, monkeypatch)
    save_configuration(
        tmp_path,
        config.model_copy(update={"output": OutputPreferences(max_output_bytes=1024)}),
        replace=True,
    )
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)

    def no_cli_sharing_key() -> bytes:
        raise AssertionError("compact CREATE receipt must not acquire the sharing key")

    monkeypatch.setattr(ga4_create_cli, "export_key", no_cli_sharing_key)
    result = _invoke_installed(tmp_path, "create", "--input", str(source))

    assert result.exit_code == 0, result.output
    assert len(result.output.encode()) <= 1024
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "GA4_KEY_EVENT_CREATE_PLAN_RETAINED_NO_EXECUTION_AUTHORITY"
    data = envelope["data"]
    assert data["execution_authority"] is False
    assert data["remote_mutations"] is False
    assert data["execution_command"] == "UNRELEASED"
    assert data["review_required"] is True
    assert data["review_command"] == f"plan ga4-key-event show {data['record_digest']}"
    assert data["maximum_incremental_spend"] == {"currency": "USD", "minor_units": 0}
    assert len(calls) == 3 and all(call.method == "GET" for call in calls)
    assert store.reads == 1
    action, provenance = load_ga4_key_event_create_plan(ledger, data["record_digest"])
    assert provenance["record_digest"] == data["record_digest"]
    assert action.plan.plan_digest == data["plan_digest"]
    assert data["observation_record_digest"] == action.observation_binding.observation_record_digest
    records = ledger.checkpoint()["records_by_stream"]["changes"]
    assert [item["record_kind"] for item in records][-2:] == [
        "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION",
        "GA4_KEY_EVENT_CREATE_PLAN",
    ]
    for private in (
        _ACCESS,
        _REFRESH,
        _SUBJECT,
        profile.profile_id,
        profile.principal_ref,
        "properties/123",
        "qualified_request",
    ):
        assert private not in result.output


def test_create_output_preflight_happens_before_workflow_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, config, _, calls, _ = _setup(tmp_path, monkeypatch)
    save_configuration(
        tmp_path,
        config.model_copy(update={"output": OutputPreferences(max_output_bytes=1024)}),
        replace=True,
    )
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    monkeypatch.setattr(ga4_create_cli, "_receipt_probe", lambda: {"padding": "x" * 5000})
    monkeypatch.setattr(
        ga4_create_cli,
        "prepare_and_retain_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: pytest.fail(
            "workflow must not run after failed receipt preflight"
        ),
    )

    result = _invoke("create", "--input", str(source))

    assert result.exit_code != 0
    assert _error_code(result) == "OUTPUT_LIMIT_EXCEEDED"
    assert calls == []
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


def test_create_binds_the_preflight_configuration_before_workflow_key_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, store, _, configuration, _, calls, _ = _setup(tmp_path, monkeypatch)
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)
    expected = digest(configuration)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]

    def drifted_workflow(
        directory: Path,
        supplied_request: Ga4KeyEventCreatePlanRequest,
        *,
        expected_configuration_digest: str | None = None,
    ) -> RetainedGa4KeyEventCreatePlan:
        assert expected_configuration_digest == expected
        save_configuration(
            directory,
            configuration.model_copy(
                update={
                    "output": configuration.output.model_copy(update={"max_rows": 7}),
                }
            ),
            replace=True,
        )
        return prepare_and_retain_ga4_key_event_create_plan(
            directory,
            supplied_request,
            expected_configuration_digest=expected_configuration_digest,
        )

    monkeypatch.setattr(
        ga4_create_cli, "prepare_and_retain_ga4_key_event_create_plan", drifted_workflow
    )

    result = _invoke("create", "--input", str(source))

    assert result.exit_code != 0
    assert _error_code(result) == "CONFIGURATION_DRIFT"
    assert calls == []
    assert store.reads == 0
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


def test_matching_saved_artifact_rule_generates_after_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, profile, config, _, calls, _ = _setup(tmp_path, monkeypatch)
    output = OutputPreferences(
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
    save_configuration(tmp_path, config.model_copy(update={"output": output}), replace=True)
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)
    monkeypatch.setattr(ga4_create_cli, "export_key", lambda: _KEY)

    result = _invoke("create", "--input", str(source))

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    artifact_result = data["artifact_result"]
    assert artifact_result["effective_policy"]["reason"] == "MATCHING_SAVED_RULE"
    assert artifact_result["generation_failure_code"] is None
    assert len(artifact_result["artifacts"]) == 1
    assert Path(artifact_result["artifacts"][0]["path"]).is_file()
    assert len(calls) == 3 and all(call.method == "GET" for call in calls)
    assert ledger.checkpoint()["status"] == "VERIFIED_CHECKPOINT"


@pytest.mark.parametrize(
    "contents,expected_code",
    [
        (b'{"profile_id":"first","profile_id":"second"}', "INVALID_JSON"),
        pytest.param(b"x" * 1_048_577, "INPUT_TOO_LARGE", id="oversized"),
    ],
)
def test_invalid_or_oversized_input_fails_before_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: bytes,
    expected_code: str,
) -> None:
    _install_root(monkeypatch, tmp_path)
    source = tmp_path / "invalid.json"
    source.write_bytes(contents)
    monkeypatch.setattr(
        ga4_create_cli,
        "prepare_and_retain_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: pytest.fail("invalid input reached workflow"),
    )

    result = _invoke("create", "--input", str(source))

    assert result.exit_code != 0
    assert _error_code(result) == expected_code
    assert not Ledger(tmp_path).records("changes")


def test_catalog_is_key_and_provider_free(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    request, ledger, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    _install_root(monkeypatch, tmp_path)
    prior_calls = list(calls)
    monkeypatch.setattr(
        ga4_create_cli,
        "export_key",
        lambda: pytest.fail("historical catalog must not acquire sharing key"),
    )

    result = _invoke("catalog")

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["total_count"] == 1
    assert data["plans"][0]["record_digest"] == retained.plan_record_digest
    assert data["execution_authority"] is False
    assert data["remote_mutations"] is False
    assert data["execution_command"] == "UNRELEASED"
    assert calls == prior_calls


def test_catalog_rejects_forged_or_extra_agent_visible_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    _install_root(monkeypatch, tmp_path)
    prior_calls = list(calls)
    original = ga4_create_cli.ga4_key_event_create_plan_catalog

    def forged_catalog(*args: object, **kwargs: object) -> dict[str, object]:
        data = original(*args, **kwargs)
        data["execution_authority"] = True
        data["unexpected_detail"] = "catalog-sentinel-never-output"
        return data

    monkeypatch.setattr(ga4_create_cli, "ga4_key_event_create_plan_catalog", forged_catalog)
    result = _invoke("catalog")

    assert result.exit_code != 0
    assert _error_code(result) == "INVALID_GA4_CREATE_PLAN_CATALOG"
    assert "catalog-sentinel-never-output" not in result.output
    assert calls == prior_calls


def test_show_is_complete_redacted_history_and_does_not_read_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, _, profile, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    _install_root(monkeypatch, tmp_path)
    prior_calls = list(calls)
    monkeypatch.setattr(ga4_create_cli, "export_key", lambda: _KEY)
    monkeypatch.setattr(ga4_create_cli, "_now_utc", lambda: _NOW)

    result = _invoke("show", retained.plan_record_digest)

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["historical_review"] is True
    assert data["execution_authority"] is False
    assert data["remote_mutations"] is False
    assert data["review"]["expired_at_review"] is False
    assert calls == prior_calls
    for private in (
        _ACCESS,
        _REFRESH,
        _SUBJECT,
        profile.profile_id,
        profile.principal_ref,
        "properties/123",
        "qualified_request",
        "fingerprint_key_marker",
    ):
        assert private not in result.output


def test_show_rejects_extra_loader_provenance_before_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    _install_root(monkeypatch, tmp_path)
    action, provenance = load_ga4_key_event_create_plan(ledger, retained.plan_record_digest)
    sentinel = "show-provenance-private-sentinel-never-output"
    forged = {**provenance, "raw_token": sentinel}
    prior_calls = list(calls)
    monkeypatch.setattr(
        ga4_create_cli,
        "load_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: (action, forged),
    )
    monkeypatch.setattr(
        ga4_create_cli,
        "export_key",
        lambda: pytest.fail("invalid provenance must fail before sharing-key access"),
    )

    result = _invoke("show", retained.plan_record_digest)

    assert result.exit_code != 0
    assert _error_code(result) == "INVALID_GA4_CREATE_PLAN_REVIEW"
    assert sentinel not in result.output
    assert calls == prior_calls


def test_expired_retained_plan_remains_reviewable_without_provider_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    _install_root(monkeypatch, tmp_path)
    prior_calls = list(calls)
    monkeypatch.setattr(ga4_create_cli, "export_key", lambda: _KEY)
    monkeypatch.setattr(ga4_create_cli, "_now_utc", lambda: _NOW + timedelta(days=1))

    result = _invoke("show", retained.plan_record_digest)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["review"]["expired_at_review"] is True
    assert calls == prior_calls


def test_malformed_workflow_return_cannot_emit_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)
    secret = "malformed-workflow-sentinel-never-output"
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    monkeypatch.setattr(
        ga4_create_cli,
        "prepare_and_retain_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: {"action": secret},
    )

    result = _invoke("create", "--input", str(source))

    assert result.exit_code != 0
    assert _error_code(result) == "GA4_CREATE_PLAN_RESULT_INVALID"
    assert secret not in result.output
    assert calls == []
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


def test_incomplete_constructed_workflow_result_has_only_fixed_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    incomplete = object.__new__(RetainedGa4KeyEventCreatePlan)
    monkeypatch.setattr(
        ga4_create_cli,
        "prepare_and_retain_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: incomplete,
    )

    result = _invoke("create", "--input", str(source))

    assert result.exit_code != 0
    assert _error_code(result) == "GA4_CREATE_PLAN_RESULT_INVALID"
    assert "AttributeError" not in result.output
    assert calls == []
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


def test_receipt_rejects_a_valid_shape_result_with_unretained_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    genuine = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    prior_calls = list(calls)
    forged = replace(genuine, plan_record_digest="f" * 64)
    monkeypatch.setattr(
        ga4_create_cli,
        "prepare_and_retain_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: forged,
    )

    result = _invoke("create", "--input", str(source))

    assert result.exit_code != 0
    assert _error_code(result) == "GA4_CREATE_PLAN_RESULT_INVALID"
    assert "f" * 64 not in result.output
    assert calls == prior_calls
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


def test_post_retention_receipt_read_failure_is_ambiguous_and_does_not_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    _install_root(monkeypatch, tmp_path)
    source = _write_request(tmp_path, request)
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    prior_calls = list(calls)
    sentinel = "receipt-load-private-sentinel-never-output"
    monkeypatch.setattr(
        ga4_create_cli,
        "prepare_and_retain_ga4_key_event_create_plan",
        lambda *_args, **_kwargs: retained,
    )

    def unavailable(*_args: object, **_kwargs: object) -> tuple[object, object]:
        raise OSError(sentinel)

    monkeypatch.setattr(ga4_create_cli, "load_ga4_key_event_create_plan", unavailable)

    result = _invoke("create", "--input", str(source))

    assert result.exit_code == ExitCode.AMBIGUOUS
    assert _error_code(result) == "GA4_CREATE_PLAN_RECEIPT_UNVERIFIED"
    output = json.loads(result.output)
    assert "do not repeat an ambiguous plan preparation" in output["diagnostics"][0]["next_action"]
    assert sentinel not in result.output
    assert calls == prior_calls
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint
    action, provenance = load_ga4_key_event_create_plan(ledger, retained.plan_record_digest)
    assert provenance["record_digest"] == retained.plan_record_digest
    assert action.plan.plan_digest == retained.action.plan.plan_digest


def test_installed_plan_group_and_closed_schemas_are_discoverable() -> None:
    discovery = CliRunner().invoke(root_cli.app, ["discover"])
    assert discovery.exit_code == 0, discovery.output
    assert {
        "plan ga4-key-event create",
        "plan ga4-key-event catalog",
        "plan ga4-key-event show",
    } <= set(json.loads(discovery.output)["data"]["commands"])
    for name in ("ga4-key-event-create-plan-request", "ga4-key-event-create-plan-record"):
        result = CliRunner().invoke(root_cli.app, ["schema", name, "--root-only"])
        assert result.exit_code == 0, result.output
        schema = json.loads(result.output)["data"]["schema"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["schema_version"]["const"] == 1


def test_show_preflights_complete_review_before_sharing_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, _, _, _, config, _, calls, _ = _setup(tmp_path, monkeypatch)
    save_configuration(
        tmp_path,
        config.model_copy(update={"output": OutputPreferences(max_output_bytes=1024)}),
        replace=True,
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    _install_root(monkeypatch, tmp_path)
    prior_calls = list(calls)
    monkeypatch.setattr(ga4_create_cli, "_now_utc", lambda: _NOW)
    monkeypatch.setattr(
        ga4_create_cli,
        "export_key",
        lambda: pytest.fail("complete-review fit must precede sharing key access"),
    )

    result = _invoke("show", retained.plan_record_digest)

    assert result.exit_code != 0
    assert _error_code(result) == "OUTPUT_LIMIT_EXCEEDED"
    assert calls == prior_calls
