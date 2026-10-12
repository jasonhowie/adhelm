"""Real local Google Ads planning/artifact composition with synthetic edges only.

The retained plan comes from the configured planner with fake protected-store
and HTTP boundaries. Only the artifact command runs afterwards; it must add no
provider request, ledger record, or execution authority.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from test_google_ads_campaign_state import _campaign_response, _identity_response
from test_google_ads_pause_credentials import (
    _ACCESS,
    _DEVELOPER,
    _GENERIC_SERVICE,
    _grant,
    _MemoryStore,
    _profile,
    _wire,
)
from typer.testing import CliRunner

import adhelm.cli as root_cli
from adhelm import artifacts, credentials, google_ads_pause_cli
from adhelm import google_ads_pause_planning_session as sessions
from adhelm import google_ads_pause_workflow as workflow
from adhelm import google_oauth_credentials as oauth_credentials
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import ArtifactRule, Configuration, OutputPreferences, Profile
from adhelm.saved_google_ads_pause_plans import load_google_ads_campaign_pause_plan


class _Planner:
    """A configured local workspace whose only provider edge is the fake handler."""

    def __init__(self, root: Path, paths: list[str], profile_id: str) -> None:
        self.root = root
        self.paths = paths
        self.profile_id = profile_id

    def plan(self) -> str:
        retained = workflow.plan_configured_google_ads_campaign_pause(
            self.root,
            workflow.GoogleAdsCampaignPausePlanRequest(
                profile_id=self.profile_id, campaign_id="42"
            ),
        )
        return retained.plan_record_digest


class _Runtime:
    """One planned Google Ads PAUSE record plus its observed provider reads."""

    def __init__(self, root: Path, record_digest: str, paths: list[str], profile_id: str) -> None:
        self.root = root
        self.record_digest = record_digest
        self.paths = paths
        self.profile_id = profile_id


def _installed_at(
    monkeypatch: pytest.MonkeyPatch, base: datetime
) -> tuple[Profile, list[datetime]]:
    """Install the existing fake OAuth/developer slots on a chosen local clock.

    The shared fixture pins a far-future instant, but a retained plan must be
    recorded at or after its completed observation. Planning therefore runs on
    a clock slightly behind the real ledger clock.
    """
    current = [base]
    store = _MemoryStore()
    _wire(monkeypatch, store, current)
    profile = _profile()
    configuration = Configuration(profiles=[profile])
    slot = oauth_credentials.prepare_google_oauth_install(
        configuration, profile.profile_id, replace=False
    )
    oauth_credentials.install_google_oauth_grant(
        configuration,
        profile.profile_id,
        _grant(profile, expires_at=base + timedelta(minutes=30)),
        client_secret="m56-google-client-secret",
        expected_slot=slot,
    )
    assert profile.secondary_credential_ref is not None
    store.values[(_GENERIC_SERVICE, profile.secondary_credential_ref)] = _DEVELOPER
    return profile, current


@pytest.fixture(name="planner")
def _planner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Planner:
    monkeypatch.delenv("CI", raising=False)
    profile, current = _installed_at(
        monkeypatch, datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)
    )
    save_configuration(
        tmp_path,
        Configuration(profiles=[profile], output=OutputPreferences(max_output_bytes=262_144)),
    )
    monkeypatch.setattr(sessions, "_now", lambda: current[0])
    monkeypatch.setattr(workflow, "_now", lambda: current[0])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        assert request.headers["developer-token"] == _DEVELOPER
        body = json.loads(request.content)
        current[0] += timedelta(seconds=1)
        if "FROM customer" in body["query"]:
            calls.append("customer")
            return httpx.Response(200, json=_identity_response())
        calls.append("campaign")
        return httpx.Response(200, json=_campaign_response())

    monkeypatch.setattr(
        sessions,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler), trust_env=False),
    )
    monkeypatch.setattr(credentials, "export_key", lambda: b"r" * 32)
    monkeypatch.setattr(google_ads_pause_cli, "export_key", lambda: b"r" * 32)
    return _Planner(tmp_path, calls, profile.profile_id)


@pytest.fixture(name="runtime")
def _runtime(planner: _Planner) -> _Runtime:
    return _Runtime(planner.root, planner.plan(), planner.paths, planner.profile_id)


def _saved_before_approval_rule(planner: _Planner, destination: Path, formats: list[str]) -> None:
    """Save the plan-review rule a user configures to get files during planning."""
    configuration = load_configuration(planner.root)
    save_configuration(
        planner.root,
        configuration.model_copy(
            update={
                "output": OutputPreferences(
                    artifact_mode="RULES",
                    purposes=["plan_review"],
                    output_directory=str(destination),
                    workflow_rules=[
                        ArtifactRule(
                            workflow="plan",
                            stage="before_approval",
                            providers=["google_ads"],
                            profiles=[planner.profile_id],
                            formats=formats,
                        )
                    ],
                    max_output_bytes=262_144,
                )
            }
        ),
        replace=True,
    )


def _create(planner: _Planner, *, expected: int = 0) -> dict[str, Any]:
    request = planner.root / "google-ads-pause-plan-request.json"
    request.write_text(
        json.dumps({"schema_version": 1, "profile_id": planner.profile_id, "campaign_id": "42"}),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        root_cli.app,
        [
            "--data-dir",
            str(planner.root),
            "plan",
            "google-ads-campaign-pause",
            "create",
            "--input",
            str(request),
        ],
    )
    assert result.exit_code == expected, result.output
    value = json.loads(result.output)
    assert type(value) is dict
    return cast(dict[str, Any], value)


def _request(runtime: _Runtime, destination: Path, formats: list[str]) -> Path:
    path = runtime.root / "google-ads-artifact-request.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "formats": formats,
                "output_directory": str(destination),
            }
        ),
        encoding="utf-8",
    )
    return path


def _render(runtime: _Runtime, destination: Path, formats: list[str], *, expected: int = 0) -> Any:
    result = CliRunner().invoke(
        root_cli.app,
        [
            "--data-dir",
            str(runtime.root),
            "artifacts",
            "google-ads-campaign-pause",
            runtime.record_digest,
            "--input",
            str(_request(runtime, destination, formats)),
        ],
    )
    assert result.exit_code == expected, result.output
    value = json.loads(result.output)
    assert type(value) is dict
    return cast(dict[str, Any], value)


def _assert_only_planning_reads(runtime: _Runtime) -> None:
    assert runtime.paths == ["customer", "campaign"]
    history = b"".join(path.read_bytes() for path in runtime.root.rglob("*.jsonl"))
    for forbidden in (_ACCESS.encode(), _DEVELOPER.encode(), b"APPLY_STARTED"):
        assert forbidden not in history


def test_three_redacted_formats_render_from_an_actually_planned_record(
    runtime: _Runtime,
) -> None:
    destination = runtime.root / "plan-exports"
    destination.mkdir(mode=0o700)
    checkpoint = Ledger(runtime.root).checkpoint()["checkpoint_digest"]

    envelope = _render(runtime, destination, ["json", "csv", "html"])
    data = cast(dict[str, Any], envelope["data"])
    generated = cast(dict[str, Any], data["artifact_result"])

    assert len(generated["artifacts"]) == 3
    assert {item["format"] for item in generated["artifacts"]} == {"json", "csv", "html"}
    assert generated["source_record_digest"] == runtime.record_digest
    assert generated["checkpoint_digest"] == checkpoint
    assert data["execution_authority"] is False
    for item in generated["artifacts"]:
        path = Path(item["path"])
        assert path.parent == destination
        contents = path.read_text()
        assert data["plan_digest"] in contents
        assert "NOT_PREDICTED" in contents
        assert "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE" in contents
        assert "UNRELEASED" in contents
        for forbidden in (
            _ACCESS,
            _DEVELOPER,
            "m56-google-ads-refresh-token",
            "m56-google-client-secret",
            runtime.profile_id,
            "credential-binding:sha256:",
            "canonical_receipt",
            "canonical_challenge",
        ):
            assert forbidden not in contents
    assert len(list(destination.iterdir())) == 3
    assert Ledger(runtime.root).checkpoint()["checkpoint_digest"] == checkpoint
    _assert_only_planning_reads(runtime)


def test_regeneration_reuses_exact_files_without_more_provider_reads(runtime: _Runtime) -> None:
    destination = runtime.root / "plan-exports"
    destination.mkdir(mode=0o700)
    first = _render(runtime, destination, ["json"])["data"]["artifact_result"]["artifacts"]
    checkpoint = Ledger(runtime.root).checkpoint()["checkpoint_digest"]

    second = _render(runtime, destination, ["json"])["data"]["artifact_result"]["artifacts"]

    assert len(second) == 1
    assert second[0]["reused"] is True
    for field in ("path", "artifact_digest", "input_digest", "settings_digest"):
        assert second[0][field] == first[0][field]
    assert Ledger(runtime.root).checkpoint()["checkpoint_digest"] == checkpoint
    assert len(list(destination.iterdir())) == 1
    _assert_only_planning_reads(runtime)


def test_second_file_failure_keeps_the_retained_plan_and_first_verified_file(
    runtime: _Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = runtime.root / "plan-exports"
    destination.mkdir(mode=0o700)
    write = artifacts._write_versioned
    attempts = 0

    def fail_second(*args: object, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("synthetic private artifact failure")
        return write(*args, **kwargs)

    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    envelope = _render(runtime, destination, ["html", "json"], expected=5)
    generated = cast(dict[str, Any], envelope["data"]["artifact_result"])

    assert envelope["outcome"] == "PARTIAL"
    assert generated["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert len(generated["artifacts"]) == 1
    assert Path(generated["artifacts"][0]["path"]).is_file()
    assert len(list(destination.iterdir())) == 1
    assert "synthetic private artifact failure" not in json.dumps(envelope)

    catalog = CliRunner().invoke(
        root_cli.app,
        ["--data-dir", str(runtime.root), "plan", "google-ads-campaign-pause", "catalog"],
    )
    assert catalog.exit_code == 0, catalog.output
    assert json.loads(catalog.output)["total_count"] == 1
    _assert_only_planning_reads(runtime)


def test_saved_rule_generates_files_inline_during_configured_planning(
    planner: _Planner,
) -> None:
    """The saved before-approval rule now produces the files it configures."""
    destination = planner.root / "plan-exports"
    destination.mkdir(mode=0o700)
    _saved_before_approval_rule(planner, destination, ["csv", "json"])

    envelope = _create(planner)
    data = cast(dict[str, Any], envelope["data"])
    generated = cast(dict[str, Any], data["artifact_result"])

    assert envelope["outcome"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN_RETAINED_NO_EXECUTION_AUTHORITY"
    assert data["artifact_policy"] == {
        "outcome": "GENERATE",
        "reason": "MATCHING_SAVED_RULE",
        "generation_supported": True,
    }
    assert generated["generation_failure_code"] is None
    assert len(generated["artifacts"]) == len(envelope["artifact_refs"]) == 2
    assert {item["format"] for item in generated["artifacts"]} == {"csv", "json"}
    assert generated["source_record_digest"] == data["record_digest"]
    assert generated["checkpoint_digest"] == Ledger(planner.root).checkpoint()["checkpoint_digest"]
    for item in generated["artifacts"]:
        contents = Path(item["path"]).read_text()
        assert data["plan_digest"] in contents
        for forbidden in (_ACCESS, _DEVELOPER, planner.profile_id, "credential-binding:sha256:"):
            assert forbidden not in contents
    assert len(list(destination.iterdir())) == 2
    _assert_only_planning_reads(_Runtime(planner.root, "", planner.paths, planner.profile_id))


def test_on_request_default_planning_stays_file_free_without_a_sharing_key(
    planner: _Planner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Compact planning remains the default and never opens a sharing key."""
    destination = planner.root / "plan-exports"
    destination.mkdir(mode=0o700)
    configuration = load_configuration(planner.root)
    save_configuration(
        planner.root,
        configuration.model_copy(
            update={
                "output": OutputPreferences(
                    artifact_mode="ON_REQUEST",
                    output_directory=str(destination),
                    max_output_bytes=262_144,
                )
            }
        ),
        replace=True,
    )

    def forbidden() -> bytes:
        raise AssertionError("default planning must not obtain an artifact sharing key")

    monkeypatch.setattr(google_ads_pause_cli, "export_key", forbidden)
    data = cast(dict[str, Any], _create(planner)["data"])

    assert "artifact_result" not in data
    assert data["artifact_policy"] == {
        "outcome": "SUPPRESSED",
        "reason": "ON_REQUEST_REQUIRES_EXPLICIT_USER_REQUEST",
        "generation_supported": True,
    }
    assert list(destination.iterdir()) == []


def test_inline_partial_write_keeps_the_retained_receipt_and_first_file(
    planner: _Planner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed second file leaves a truthful PARTIAL receipt, not a lost plan."""
    destination = planner.root / "plan-exports"
    destination.mkdir(mode=0o700)
    _saved_before_approval_rule(planner, destination, ["html", "json"])
    write = artifacts._write_versioned
    attempts = 0

    def fail_second(*args: object, **kwargs: object) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError("synthetic private artifact failure")
        return write(*args, **kwargs)

    monkeypatch.setattr(artifacts, "_write_versioned", fail_second)
    envelope = _create(planner, expected=5)
    data = cast(dict[str, Any], envelope["data"])
    generated = cast(dict[str, Any], data["artifact_result"])

    assert envelope["outcome"] == "PARTIAL"
    assert generated["generation_failure_code"] == "LOCAL_IO_FAILED"
    assert len(generated["artifacts"]) == len(envelope["artifact_refs"]) == 1
    assert Path(generated["artifacts"][0]["path"]).is_file()
    assert len(list(destination.iterdir())) == 1
    assert "synthetic private artifact failure" not in json.dumps(envelope)
    action, provenance = load_google_ads_campaign_pause_plan(
        Ledger(planner.root), cast(str, data["record_digest"])
    )
    assert provenance["record_digest"] == data["record_digest"]
    assert action.plan.plan_digest == data["plan_digest"]


def test_inline_sharing_key_failure_keeps_the_plan_and_writes_no_file(
    planner: _Planner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A protected-store failure downgrades output, never plan retention."""
    destination = planner.root / "plan-exports"
    destination.mkdir(mode=0o700)
    _saved_before_approval_rule(planner, destination, ["json"])

    def unavailable_key() -> bytes:
        raise AdhelmError("EXPORT_KEY_UNAVAILABLE", "Private export key is unavailable.")

    monkeypatch.setattr(google_ads_pause_cli, "export_key", unavailable_key)
    envelope = _create(planner, expected=5)
    data = cast(dict[str, Any], envelope["data"])
    generated = cast(dict[str, Any], data["artifact_result"])

    assert envelope["outcome"] == "PARTIAL"
    assert generated["generation_failure_code"] == "LOCAL_ARTIFACT_FAILURE"
    assert generated["artifacts"] == []
    assert list(destination.iterdir()) == []
    action, _provenance = load_google_ads_campaign_pause_plan(
        Ledger(planner.root), cast(str, data["record_digest"])
    )
    assert action.plan.plan_digest == data["plan_digest"]
