"""Meta bundle-to-inventory/report integration with synthetic HTTP and OS storage."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from adhelm import _meta_ads_token_session as sessions
from adhelm import inventory, meta_token_credentials, meta_token_inspection, workflows
from adhelm.canonical import digest
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.meta_token_credentials import enroll_meta_token_profile, rotate_meta_token_credentials
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.providers.meta_ads import MetaAdsReportQuery

_REAL_CLIENT = httpx.Client
_NOW = datetime(2026, 9, 6, 12, tzinfo=UTC)
_SUBJECT = "fixture-subject-token-never-export"
_INSPECTOR = "fixture-inspector-token-never-export"


class _Store:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


class _Runtime:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.store = _Store()
        self.now = _NOW
        self.paths: list[str] = []
        self.status = "PAUSED"
        self.after_read: Callable[[str], None] = lambda _path: None

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "graph.facebook.com"
        path = request.url.path
        self.paths.append(path)
        if path == "/v26.0/debug_token":
            assert request.headers["authorization"] == f"Bearer {_INSPECTOR}"
            assert request.url.params["input_token"] == _SUBJECT
            data: dict[str, Any] = {
                "data": {
                    "app_id": "67890",
                    "user_id": "99999",
                    "is_valid": True,
                    "type": "USER",
                    "scopes": ["ads_read"],
                    "expires_at": int((_NOW + timedelta(hours=1)).timestamp()),
                    "data_access_expires_at": int((_NOW + timedelta(hours=2)).timestamp()),
                }
            }
        else:
            assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
            assert "input_token" not in request.url.params
            if path == "/v26.0/act_12345":
                data = {
                    "id": "12345",
                    "account_status": 1,
                    "currency": "USD",
                    "timezone_name": "UTC",
                }
            elif path.endswith("/campaigns"):
                data = {"data": [{"id": "11", "effective_status": self.status}]}
            elif path.endswith("/adsets"):
                data = {
                    "data": [{"id": "12", "campaign_id": "11", "effective_status": self.status}]
                }
            elif path.endswith("/ads"):
                data = {
                    "data": [
                        {
                            "id": "13",
                            "campaign_id": "11",
                            "adset_id": "12",
                            "effective_status": self.status,
                        }
                    ]
                }
            else:
                assert path == "/v26.0/act_12345/insights"
                row = {
                    "campaign_id": "11",
                    "date_start": "2026-09-01",
                    "date_stop": "2026-09-01",
                    "impressions": "10",
                    "clicks": "2",
                    "spend": "1.25",
                    "reach": "8",
                }
                if request.url.params["level"] == "ad":
                    row.update(adset_id="12", ad_id="13")
                data = {"data": [row]}
        self.after_read(path)
        return httpx.Response(200, json=data)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Runtime:
    runtime = _Runtime(tmp_path)
    monkeypatch.setattr(meta_token_credentials, "_credential_backend", lambda: runtime.store)
    monkeypatch.setattr(meta_token_inspection, "_now", lambda: runtime.now)
    monkeypatch.setattr(sessions, "_now", lambda: runtime.now)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _REAL_CLIENT(transport=httpx.MockTransport(runtime.handle), **kwargs),
    )
    for module in (inventory, workflows):
        monkeypatch.setattr(module, "export_key", lambda: b"e" * 32)
        monkeypatch.setattr(
            module,
            "credential",
            lambda *a, **kw: pytest.fail("generic credential resolver must not see a Meta bundle"),
        )
    enroll_meta_token_profile(
        tmp_path,
        MetaAccountTokenProfileDraft.model_validate(
            {
                "profile_id": "meta-reader",
                "account_ref": "12345",
                "client_application_ref": "67890",
                "currency": "USD",
                "timezone": "UTC",
                "purpose": "READ",
                "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
            }
        ),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    return runtime


def _query(root: Path, **updates: object) -> Path:
    value = {
        "account_ref": "12345",
        "report_id": "report:meta",
        "query_contract_hash": "a" * 64,
        "window_start": "2026-09-01",
        "window_end": "2026-09-01",
        "source_timezone": "UTC",
        "action_attribution_windows": ["7d_click", "1d_view"],
        "level": "CAMPAIGN",
    }
    value.update(updates)
    path = root / "query.json"
    path.write_text(json.dumps(value))
    return path


def _assert_no_sensitive_output(root: Path, result: dict[str, Any]) -> None:
    assert "12345" not in json.dumps(result)
    retained = "\n".join(p.read_text() for p in (root / "ledger").rglob("*.jsonl"))
    for value in (_SUBJECT, _INSPECTOR, "99999", "credential-binding:", "meta_token_principal_v1."):
        assert value not in json.dumps(result)
        assert value not in retained


def test_inventory_retains_complete_hierarchy_and_out_of_band_change(runtime: _Runtime) -> None:
    first = inventory.run_inventory(runtime.root, "meta_ads", "meta-reader")
    assert first["resource_counts"] == {"ACCOUNT": 1, "CAMPAIGN": 1, "AD_GROUP": 1, "AD": 1}
    assert len(first["snapshot_record_digests"]) == 4
    assert runtime.paths == [
        "/v26.0/debug_token",
        "/v26.0/act_12345",
        "/v26.0/act_12345/campaigns",
        "/v26.0/act_12345/adsets",
        "/v26.0/act_12345/ads",
    ]
    _assert_no_sensitive_output(runtime.root, first)
    runtime.status = "ACTIVE"
    second = inventory.run_inventory(runtime.root, "meta_ads", "meta-reader")
    assert len(second["snapshot_record_digests"]) == 4
    assert len(second["change_record_digests"]) == 3
    for record in Ledger(runtime.root).records("changes"):
        if record["record_kind"] == "CHANGE_EVENT":
            assert record["payload"]["origin"] == "UNKNOWN"
            assert record["payload"]["actor_ref"] is None
    _assert_no_sensitive_output(runtime.root, second)


@pytest.mark.parametrize("ephemeral", [False, True])
@pytest.mark.parametrize("level", ["CAMPAIGN", "AD"])
def test_report_uses_canonical_account_currency_and_query_provenance(
    runtime: _Runtime, ephemeral: bool, level: str
) -> None:
    path = _query(runtime.root, level=level)
    result = workflows.run_report(
        runtime.root, "meta_ads", "meta-reader", path, ephemeral=ephemeral
    )
    assert runtime.paths == ["/v26.0/debug_token", "/v26.0/act_12345", "/v26.0/act_12345/insights"]
    assert bool(result["retained_record_digest"]) is not ephemeral
    query = MetaAdsReportQuery.model_validate_json(path.read_text()).model_copy(
        update={"account_ref": "meta_ads.account.act_12345", "currency": "USD"}
    )
    assert result["envelope"]["query_contract_hash"] == digest(
        query.model_dump(mode="json", exclude={"query_contract_hash", "report_id"})
    )
    observations = [
        r
        for r in Ledger(runtime.root).records("performance")
        if r["record_kind"] == "PERFORMANCE_OBSERVATION"
    ]
    assert len(observations) == (0 if ephemeral else 1)
    _assert_no_sensitive_output(runtime.root, result)


@pytest.mark.parametrize("workflow", ["inventory", "report"])
def test_configuration_change_during_sharing_key_preflight_stops_before_http(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, workflow: str
) -> None:
    def changed_key() -> bytes:
        current = load_configuration(runtime.root)
        updated = current.model_copy(
            update={"output": current.output.model_copy(update={"max_output_bytes": 8192})}
        )
        save_configuration(runtime.root, updated, replace=True)
        return b"e" * 32

    monkeypatch.setattr(
        inventory if workflow == "inventory" else workflows, "export_key", changed_key
    )
    with pytest.raises(AdhelmError) as failure:
        if workflow == "inventory":
            inventory.run_inventory(runtime.root, "meta_ads", "meta-reader")
        else:
            workflows.run_report(runtime.root, "meta_ads", "meta-reader", _query(runtime.root))
    assert failure.value.code == "CONFIGURATION_CHANGED"
    assert runtime.paths == []


@pytest.mark.parametrize("workflow", ["inventory", "report"])
@pytest.mark.parametrize("ephemeral", [False, True])
def test_bundle_rotation_after_last_http_response_blocks_retention_and_output(
    runtime: _Runtime, workflow: str, ephemeral: bool
) -> None:
    def rotate(path: str) -> None:
        if path.endswith("/ads") or path.endswith("/insights"):
            rotate_meta_token_credentials(
                runtime.root,
                "meta-reader",
                subject_token_supplier=lambda: "replacement-subject",
                inspector_token_supplier=lambda: "replacement-inspector",
            )

    runtime.after_read = rotate
    with pytest.raises(AdhelmError):
        if workflow == "inventory":
            inventory.run_inventory(runtime.root, "meta_ads", "meta-reader", ephemeral=ephemeral)
        else:
            workflows.run_report(
                runtime.root, "meta_ads", "meta-reader", _query(runtime.root), ephemeral=ephemeral
            )
    for stream in ("changes", "performance"):
        assert all(
            record["record_kind"] == "SEGMENT_STARTED"
            for record in Ledger(runtime.root).records(stream)
        )


@pytest.mark.parametrize("workflow", ["inventory", "report"])
def test_expiry_inside_ledger_preappend_window_blocks_the_record(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, workflow: str
) -> None:
    original = Ledger.append
    calls = 0

    def append(ledger: Ledger, *args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        assert callable(kwargs.get("_before_durable_append"))
        check = kwargs["_before_durable_append"]

        def expire() -> None:
            nonlocal calls
            calls += 1
            runtime.now = _NOW + timedelta(hours=1)
            check()

        kwargs["_before_durable_append"] = expire
        return original(ledger, *args, **kwargs)

    monkeypatch.setattr(Ledger, "append", append)
    with pytest.raises(AdhelmError) as failure:
        if workflow == "inventory":
            inventory.run_inventory(runtime.root, "meta_ads", "meta-reader")
        else:
            workflows.run_report(runtime.root, "meta_ads", "meta-reader", _query(runtime.root))
    assert "EXPIRED" in failure.value.code
    assert calls == 1
    for stream in ("changes", "performance"):
        assert all(
            record["record_kind"] == "SEGMENT_STARTED"
            for record in Ledger(runtime.root).records(stream)
        )


@pytest.mark.parametrize(
    "updates",
    [{"currency": "EUR"}, {"source_timezone": "America/Chicago"}, {"account_ref": "54321"}],
)
def test_report_declaration_mismatch_stops_before_sharing_key(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, updates: dict[str, object]
) -> None:
    monkeypatch.setattr(
        workflows, "export_key", lambda: pytest.fail("invalid query reached sharing key")
    )
    with pytest.raises(AdhelmError) as failure:
        workflows.run_report(
            runtime.root, "meta_ads", "meta-reader", _query(runtime.root, **updates)
        )
    assert failure.value.code == "IDENTITY_MISMATCH"
    assert runtime.paths == []


@pytest.mark.parametrize("workflow", ["inventory", "report"])
def test_meta_bundle_ci_mode_is_explicitly_unsupported(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch, workflow: str
) -> None:
    for module in (inventory, workflows):
        monkeypatch.setattr(
            module, "export_key", lambda: pytest.fail("unsupported CI reached sharing key")
        )
    with pytest.raises(AdhelmError) as failure:
        if workflow == "inventory":
            inventory.run_inventory(runtime.root, "meta_ads", "meta-reader", ci_enabled=True)
        else:
            workflows.run_report(
                runtime.root, "meta_ads", "meta-reader", _query(runtime.root), ci_enabled=True
            )
    assert failure.value.code == "META_TOKEN_INTERACTIVE_STORE_REQUIRED"
    assert runtime.paths == []


@pytest.mark.parametrize("workflow", ["inventory", "report"])
def test_existing_cli_routes_accept_the_closed_meta_read_profile(
    runtime: _Runtime, workflow: str
) -> None:
    from typer.testing import CliRunner

    from adhelm.cli import app

    command = (
        ["inventory", "meta_ads", "meta-reader"]
        if workflow == "inventory"
        else ["report", "meta-ads", "meta-reader", "--input", str(_query(runtime.root))]
    )
    result = CliRunner().invoke(app, ["--data-dir", str(runtime.root), *command, "--ephemeral"])
    assert result.exit_code == 0, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["outcome"] == "OK"
    assert envelope["data"].get("observation_persisted", False) is False
    _assert_no_sensitive_output(runtime.root, envelope)


def test_report_artifact_projection_runs_after_retention_locks_are_released(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    from adhelm.config import ConfigurationTransaction

    transaction = sessions.configuration_transaction
    held = False
    append_checks = 0
    projected = False

    @contextmanager
    def observed_transaction(directory: Path) -> Iterator[ConfigurationTransaction]:
        nonlocal held
        assert not held
        with transaction(directory) as current:
            held = True
            try:
                yield current
            finally:
                held = False

    monkeypatch.setattr(sessions, "configuration_transaction", observed_transaction)
    original_append = Ledger.append

    def append(ledger: Ledger, *args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal append_checks
        check = kwargs["_before_durable_append"]

        def recheck() -> None:
            nonlocal append_checks
            assert held
            append_checks += 1
            check()

        kwargs["_before_durable_append"] = recheck
        return original_append(ledger, *args, **kwargs)

    monkeypatch.setattr(Ledger, "append", append)
    original_project = workflows.finalize_report_artifacts

    def project(*args: Any, **kwargs: Any) -> Any:
        nonlocal projected
        assert not held
        assert append_checks == 1
        projected = True
        return original_project(*args, **kwargs)

    monkeypatch.setattr(workflows, "finalize_report_artifacts", project)
    result = workflows.run_report(runtime.root, "meta_ads", "meta-reader", _query(runtime.root))
    assert projected
    assert result["retained_record_digest"]
