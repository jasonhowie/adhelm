"""Configured pause planning across actual local boundaries with fake Meta HTTP."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from adhelm import meta_pause_cli, meta_token_credentials
from adhelm.cli import app
from adhelm.config import load_configuration, save_configuration
from adhelm.ledger import Ledger
from adhelm.meta_token_credentials import enroll_meta_token_profile, rotate_meta_token_credentials
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.saved_meta_pause_plans import load_meta_campaign_pause_plan

_HTTP_CLIENT = httpx.Client
_SUBJECT = "synthetic-planning-subject-token"
_INSPECTOR = "synthetic-planning-inspector-token"
_DIGEST_OR_OPAQUE_REF = re.compile(r"^(?:opaque:|evidence:sha256:)?[a-f0-9]{64}$", re.ASCII)
_DERIVED_ARTIFACT_FILENAME = re.compile(
    r"^artifact-(?:[a-f0-9]{64}|[a-f0-9]{16}-[a-f0-9]{16})(?:\.v[1-9][0-9]{0,3})?\.(?:json|csv|html)$",
    re.ASCII,
)
_SENSITIVE_OUTPUT_FIELDS = frozenset(
    {
        "principal_ref",
        "credential_binding_ref",
        "configuration_digest",
        "profile_configuration_digest",
        "subject_token",
        "inspector_token",
        "user_id",
        "access_token",
        "canonical_receipt_b64url",
        "active_public_key_x963_b64url",
        "raw_provider_marker",
    }
)
_OPAQUE_OUTPUT_FIELDS = frozenset(
    {
        "workspace_profile_ref",
        "profile_ref",
        "account_ref",
        "account_client_ref",
        "campaign_ref",
        "campaign_client_ref",
        "auth_profile_ref",
        "client_application_ref",
    }
)
_RAW_RUNTIME_MARKERS = (
    "99999",
    "12345",
    "67890",
    "987654321",
    "act_12345",
    "meta-editor",
)
_RAW_RUNTIME_NUMERIC_MARKERS = frozenset({99999, 12345, 67890, 987654321})
_TIMESTAMP_OUTPUT_FIELDS = frozenset(
    {
        "expires_at",
        "recorded_at",
        "reviewed_at",
        "read_started_at",
        "read_finished_at",
        "observation_interval_start",
        "observation_interval_end",
    }
)


def _is_derived_artifact_path(value: str) -> bool:
    """Allow fixture-ID digits only in the safe writer's terminal digest name."""

    # Fixtures write POSIX-style paths; real output on Windows uses drive paths.
    path: PurePath = (
        PureWindowsPath(value) if "\\" in value or ":" in value else PurePosixPath(value)
    )
    if not path.is_absolute() or _DERIVED_ARTIFACT_FILENAME.fullmatch(path.name) is None:
        return False
    return not any(marker in str(path.parent) for marker in _RAW_RUNTIME_MARKERS)


def _is_aware_iso_timestamp(value: str) -> bool:
    """Accept only canonical UTC or exact offset ISO values in closed time fields."""

    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return False
    if value.endswith("Z"):
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z") == value
    return parsed.isoformat() == value


def _redacted_cli_data(output: str) -> dict[str, Any]:
    """Parse a result and reject semantic fixture facts without rejecting hashes.

    A raw native identifier must not occur in a meaningful output value.  A
    SHA-256 digest or opaque HMAC reference is allowed to contain the same
    digit sequence incidentally: it is a derived, fixed-width value rather
    than an identifier disclosure.
    """

    assert _SUBJECT not in output
    assert _INSPECTOR not in output
    value = json.loads(output)
    assert type(value) is dict

    def check(current: object, *, field: str | None = None) -> None:
        if type(current) is dict:
            for name, child in current.items():
                assert type(name) is str
                assert name not in _SENSITIVE_OUTPUT_FIELDS
                if name in _OPAQUE_OUTPUT_FIELDS:
                    assert type(child) is str
                    assert _DIGEST_OR_OPAQUE_REF.fullmatch(child) is not None
                    assert child.startswith("opaque:")
                check(child, field=name)
            return
        if type(current) is list:
            for child in current:
                check(child)
            return
        if type(current) is int:
            assert current not in _RAW_RUNTIME_NUMERIC_MARKERS
            return
        if type(current) is not str:
            return
        if (
            _DIGEST_OR_OPAQUE_REF.fullmatch(current) is not None
            or _is_derived_artifact_path(current)
            or (field in _TIMESTAMP_OUTPUT_FIELDS and _is_aware_iso_timestamp(current))
        ):
            return
        assert not current.startswith("meta_token_principal_v1.")
        assert not current.startswith("credential-binding:sha256:")
        assert not any(marker in current for marker in _RAW_RUNTIME_MARKERS)

    check(value)
    return value


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
        self.paths: list[str] = []
        self.campaign_parent = "12345"
        self.after_campaign: Callable[[], None] = lambda: None

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.host == "graph.facebook.com"
        self.paths.append(request.url.path)
        data: dict[str, Any]
        if request.url.path == "/v26.0/debug_token":
            assert request.headers["authorization"] == f"Bearer {_INSPECTOR}"
            assert request.url.params["input_token"] == _SUBJECT
            now = datetime.now(UTC)
            data = {
                "data": {
                    "app_id": "67890",
                    "user_id": "99999",
                    "is_valid": True,
                    "type": "USER",
                    "scopes": ["ads_management", "public_profile"],
                    "expires_at": int((now + timedelta(hours=1)).timestamp()),
                    "data_access_expires_at": int((now + timedelta(hours=2)).timestamp()),
                }
            }
        else:
            assert request.headers["authorization"] == f"Bearer {_SUBJECT}"
            assert "input_token" not in request.url.params
            if request.url.path == "/v26.0/act_12345":
                data = {
                    "id": "12345",
                    "currency": "USD",
                    "timezone_name": "UTC",
                    "account_status": 1,
                }
            else:
                assert request.url.path == "/v26.0/987654321"
                assert request.url.params["fields"] == (
                    "id,account_id,configured_status,effective_status"
                )
                data = {
                    "id": "987654321",
                    "account_id": self.campaign_parent,
                    "configured_status": "ACTIVE",
                    "effective_status": "WITH_ISSUES",
                }
                self.after_campaign()
        return httpx.Response(200, json=data)

    def call(self, *arguments: str, expected_exit: int = 0) -> dict[str, Any]:
        result = CliRunner().invoke(app, ["--data-dir", str(self.root), *arguments])
        assert result.exit_code == expected_exit, result.output
        return _redacted_cli_data(result.output)

    def plan(self, *, expected_exit: int = 0) -> dict[str, Any]:
        request = self.root / "request.json"
        request.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profile_id": "meta-editor",
                    "campaign_id": "987654321",
                    "expires_in_seconds": 300,
                }
            )
        )
        return self.call(
            "plan",
            "meta-campaign-pause",
            "create",
            "--input",
            str(request),
            expected_exit=expected_exit,
        )


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Runtime:
    monkeypatch.delenv("CI", raising=False)
    runtime = _Runtime(tmp_path)
    store = _Store()
    monkeypatch.setattr(meta_token_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(meta_pause_cli, "export_key", lambda: b"r" * 32)
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _HTTP_CLIENT(transport=httpx.MockTransport(runtime.handle), **kwargs),
    )
    enroll_meta_token_profile(
        tmp_path,
        MetaAccountTokenProfileDraft.model_validate(
            {
                "profile_id": "meta-editor",
                "account_ref": "12345",
                "client_application_ref": "67890",
                "purpose": "EDIT",
                "currency": "USD",
                "timezone": "UTC",
                "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
            }
        ),
        subject_token_supplier=lambda: _SUBJECT,
        inspector_token_supplier=lambda: _INSPECTOR,
    )
    return runtime


def test_cli_plan_retains_exact_observation_and_reviews_without_more_http(
    runtime: _Runtime,
) -> None:
    receipt = runtime.plan()
    assert receipt["data"]["execution_authority"] is False
    assert receipt["data"]["remote_mutations"] is False
    record_digest = receipt["data"]["record_digest"]
    action, provenance = load_meta_campaign_pause_plan(Ledger(runtime.root), record_digest)
    assert action.plan.plan_digest == receipt["data"]["plan_digest"]
    assert provenance["execution_authority"] is False
    catalog = runtime.call("plan", "meta-campaign-pause", "catalog")
    assert catalog["total_count"] == 1
    review = runtime.call("plan", "meta-campaign-pause", "show", record_digest)
    serialized = json.dumps(review)
    assert "WITH_ISSUES" in serialized
    assert "NOT_PREDICTED" in serialized
    assert not any(value in serialized for value in ("987654321", "act_12345", "meta-editor"))
    assert runtime.paths == ["/v26.0/debug_token", "/v26.0/act_12345", "/v26.0/987654321"]
    retained_bytes = b"".join(path.read_bytes() for path in runtime.root.rglob("*.jsonl"))
    assert b"APPLY_STARTED" not in retained_bytes
    assert _SUBJECT.encode() not in retained_bytes
    assert _INSPECTOR.encode() not in retained_bytes


def test_wrong_campaign_account_never_retains_plan(runtime: _Runtime) -> None:
    runtime.campaign_parent = "54321"
    result = runtime.plan(expected_exit=3)
    assert result["outcome"] == "FAILED_CLOSED"
    assert runtime.call("plan", "meta-campaign-pause", "catalog")["total_count"] == 0
    assert len(runtime.paths) == 3


def test_rotation_after_campaign_response_prevents_plan_retention(runtime: _Runtime) -> None:
    runtime.after_campaign = lambda: rotate_meta_token_credentials(
        runtime.root,
        "meta-editor",
        subject_token_supplier=lambda: "synthetic-replacement-subject",
        inspector_token_supplier=lambda: "synthetic-replacement-inspector",
    )
    result = runtime.plan(expected_exit=6)
    assert result["outcome"] == "FAILED_CLOSED"
    assert runtime.call("plan", "meta-campaign-pause", "catalog")["total_count"] == 0
    assert len(runtime.paths) == 3


def test_configuration_change_after_campaign_response_prevents_retention(runtime: _Runtime) -> None:
    def change() -> None:
        configuration = load_configuration(runtime.root)
        changed = configuration.model_copy(
            update={
                "output": configuration.output.model_copy(
                    update={"max_rows": configuration.output.max_rows - 1}
                )
            }
        )
        save_configuration(runtime.root, changed, replace=True)

    runtime.after_campaign = change
    result = runtime.plan(expected_exit=3)
    assert result["diagnostics"][0]["code"] == "CONFIGURATION_CHANGED"
    assert runtime.call("plan", "meta-campaign-pause", "catalog")["total_count"] == 0
    assert len(runtime.paths) == 3
