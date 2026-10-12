"""The `pilot run` command, end to end, against fake providers.

Every request is served by `httpx.MockTransport`, every credential is a fixture
value, and the data directory is temporary. Nothing here reaches a provider, the
operating-system credential store or owner data.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import typer
from test_ga4_data_aggregates import _ga4_profile
from test_gtm_inventory import _Fixture as _GtmFixture
from test_gtm_inventory import _install_workflow_fixture, _workflow_profile
from test_reddit_audit_route import ACCOUNT, _profile, _Provider
from typer import _click as click
from typer.testing import CliRunner

import adhelm.cli
import adhelm.credentials
import adhelm.ga4_measurement_workflow as ga4_measurement_workflow
import adhelm.inventory as inventory
import adhelm.measurement_cli as measurement_cli
import adhelm.meta_token_credentials as meta_token_credentials
import adhelm.pilot_workflow as pilot_workflow
import adhelm.release_labels as release_labels
import adhelm.release_policy as release_policy
from adhelm import __version__, workflows
from adhelm.cli import _COMMANDS, app
from adhelm.config import load_configuration, save_configuration
from adhelm.models import Configuration, OutputPreferences, Profile
from adhelm.pilot_records import (
    RELEASE_PROVIDERS,
    PilotCoverage,
    PilotRecord,
    PilotWindow,
    pilot_steps,
)
from adhelm.release_labels import EXPERIMENTAL, LIVE_PILOTED, pilot_record_inventory, release_label
from adhelm.strict_json import validate_json_input

_RUNNER = CliRunner()
_REAL_CLIENT = httpx.Client
_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_TOKEN = "pilot-fixture-secret-7731"
_KEY = b"k" * 32
_WINDOW = ("2026-09-01", "2026-09-07")
_TZ = "America/Chicago"


@dataclass
class Environment:
    """One temporary data directory with fake provider transport and its inputs."""

    root: Path
    provider: str
    profile: str
    options: list[str]
    requests: list[httpx.Request] = field(default_factory=list)
    credential_reads: list[str] = field(default_factory=list)
    #: Identifiers the provider fixture serves; none may reach a record or an output.
    identifiers: list[str] = field(default_factory=list)

    @property
    def output(self) -> Path:
        return self.root / "records" / "pilot.json"

    def arguments(self, *, drop: str | None = None, **replace: str) -> list[str]:
        values = {
            "--provider": self.provider,
            "--profile": self.profile,
            "--ui-comparison": "MATCH",
            "--source-commit": _COMMIT,
            "--output": str(self.output),
            **replace,
        }
        arguments = ["--data-dir", str(self.root), "pilot", "run"]
        for flag, value in values.items():
            if flag != drop:
                arguments += [flag, value]
        return [*arguments, *self.options]

    def run(self, **replace: str) -> tuple[int, dict[str, Any]]:
        result = _RUNNER.invoke(app, self.arguments(**replace))
        return result.exit_code, json.loads(result.output)


def _install(
    monkeypatch: pytest.MonkeyPatch,
    environment: Environment,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    def serve(request: httpx.Request) -> httpx.Response:
        environment.requests.append(request)
        return handler(request)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _REAL_CLIENT(transport=httpx.MockTransport(serve), **kwargs),
    )

    def credential(*_args: object, **_kwargs: object) -> str:
        environment.credential_reads.append("read")
        return _TOKEN

    for module in (inventory, workflows, ga4_measurement_workflow):
        monkeypatch.setattr(module, "credential", credential)
    for module in (inventory, workflows, measurement_cli):
        monkeypatch.setattr(module, "export_key", lambda: _KEY)
    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: _KEY)


def _write_inputs(
    root: Path, query: dict[str, Any], window: tuple[str, str] = _WINDOW
) -> list[str]:
    report = root / "report-input.json"
    report.write_text(json.dumps({**query, "window_start": window[0], "window_end": window[1]}))
    policy = root / "audit-policy.json"
    policy.write_text(
        json.dumps(
            {"maximum_observation_age_seconds": 604_800, "require_conversion_evidence": False}
        )
    )
    (root / "records").mkdir()
    return ["--report-input", str(report), "--audit-policy", str(policy)]


@pytest.fixture
def reddit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Environment:
    save_configuration(tmp_path, Configuration(profiles=[_profile(), _ga4_profile()]))
    options = _write_inputs(
        tmp_path,
        {
            "account_ref": ACCOUNT,
            "report_id": "report.reddit.campaign",
            "query_contract_hash": "a" * 64,
        },
    )
    environment = Environment(tmp_path, "reddit_ads", "reddit_ads-read", options)
    _install(monkeypatch, environment, _Provider())
    return environment


def _ga4_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.url.host == "analyticsdata.googleapis.com":
        assert request.method == "POST" and path.endswith(":runReport")
        return httpx.Response(
            200,
            json={
                "dimensionHeaders": [],
                "metricHeaders": [{"name": "sessions"}],
                "rows": [{"dimensionValues": [], "metricValues": [{"value": "2"}]}],
                "rowCount": 1,
                "metadata": {"timeZone": _TZ, "currencyCode": "USD"},
            },
        )
    assert request.method == "GET"
    if path in {"/v1beta/properties/123"}:
        return httpx.Response(
            200, json={"name": "properties/123", "timeZone": _TZ, "currencyCode": "USD"}
        )
    if path == "/v1beta/properties/123/dataStreams":
        return httpx.Response(
            200,
            json={
                "dataStreams": [
                    {"name": "properties/123/dataStreams/456", "type": "WEB_DATA_STREAM"}
                ]
            },
        )
    if path.endswith("/keyEvents"):
        return httpx.Response(
            200,
            json={
                "keyEvents": [
                    {
                        "name": "properties/123/keyEvents/789",
                        "eventName": "purchase",
                        "deletable": True,
                        "custom": True,
                        "countingMethod": "ONCE_PER_EVENT",
                    }
                ]
            },
        )
    if path.endswith("/eventCreateRules"):
        return httpx.Response(200, json={"eventCreateRules": []})
    if path.endswith("/eventEditRules"):
        return httpx.Response(200, json={"eventEditRules": []})
    raise AssertionError(path)


@pytest.fixture
def ga4(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Environment:
    admin = Profile(
        profile_id="ga4-admin-read",
        provider="ga4_admin",
        account_ref="123",
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        purpose="READ",
        currency="USD",
        timezone=_TZ,
        credential_ref="ga4-admin-fixture",
    )
    save_configuration(tmp_path, Configuration(profiles=[_ga4_profile(), admin]))
    options = _write_inputs(
        tmp_path,
        {
            "property_id": "123",
            "account_ref": "123",
            "report_id": "report.ga4.legacy",
            "query_contract_hash": "d" * 64,
            "metrics": ["sessions"],
            "source_timezone": _TZ,
            "currency": "USD",
        },
    )
    options += ["--admin-profile", "ga4-admin-read", "--stream-id", "456"]
    environment = Environment(tmp_path, "ga4", "ga4.read", options)
    _install(monkeypatch, environment, _ga4_handler)
    return environment


@pytest.fixture
def gtm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Environment:
    _install_workflow_fixture(monkeypatch, tmp_path, _GtmFixture())
    save_configuration(
        tmp_path, Configuration(profiles=[_workflow_profile(), _ga4_profile()]), replace=True
    )
    (tmp_path / "records").mkdir()
    monkeypatch.setattr(measurement_cli, "export_key", lambda: _KEY)
    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: _KEY)
    return Environment(tmp_path, "gtm", "gtm-read", ["--container-id", "2"])


def _guarded_modules() -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    return (
        (inventory, workflows, ga4_measurement_workflow),
        (inventory, workflows, measurement_cli, adhelm.credentials),
    )


def forbid_provider_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a run reaches a provider, a credential or the export key.

    The environment fixtures install their own transport, credential and key
    doubles, so this must run AFTER the environment exists or it is overridden and
    guards nothing. A test that builds its environment with `getfixturevalue`, or
    that is marked `usefixtures`, calls this in its body. :func:`_untouched` then
    asserts the guard is still the one in place.
    """

    def touched(*_args: object, **_kwargs: object) -> Any:
        pytest.fail("the pilot reached a provider, a credential or the export key")

    credential_modules, key_modules = _guarded_modules()
    monkeypatch.setattr(httpx, "Client", touched)
    for module in credential_modules:
        monkeypatch.setattr(module, "credential", touched)
    for module in key_modules:
        monkeypatch.setattr(module, "export_key", touched)
    # The Meta account-token bundle is read through its own protected backend.
    monkeypatch.setattr(meta_token_credentials, "_credential_backend", touched)


def _guard_is_armed() -> bool:
    credential_modules, key_modules = _guarded_modules()
    return (
        httpx.Client.__name__ == "touched"  # type: ignore[comparison-overlap]
        and all(module.credential.__name__ == "touched" for module in credential_modules)
        and all(module.export_key.__name__ == "touched" for module in key_modules)
        and meta_token_credentials._credential_backend.__name__ == "touched"
    )


@pytest.fixture
def no_provider_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """Arm the guard; list it AFTER the environment fixture in a test's parameters."""
    forbid_provider_access(monkeypatch)


def _record(environment: Environment) -> PilotRecord:
    return validate_json_input(PilotRecord, environment.output.read_bytes())


# --- a complete pilot ----------------------------------------------------------------------


def test_a_reddit_pilot_runs_every_step_and_writes_one_sealed_record(reddit: Environment) -> None:
    code, envelope = reddit.run()

    assert code == 0, envelope
    assert envelope["outcome"] == "PILOT_RECORDED"
    record = _record(reddit)
    assert record.provider == "reddit_ads"
    assert record.cli_version == __version__
    assert record.source_commit == _COMMIT
    assert record.ui_comparison == "MATCH"
    assert record.report_window is not None
    assert (
        record.report_window.window_start.isoformat(),
        record.report_window.window_end.isoformat(),
    ) == _WINDOW
    assert [(step.step, step.command) for step in record.steps] == list(pilot_steps("reddit_ads"))
    assert record.audit_coverage
    assert {item.status for item in record.audit_coverage} <= {
        "COVERED",
        "PARTIAL",
        "NOT_READ",
        "NOT_APPLICABLE",
    }
    assert envelope["data"]["record_digest"] == record.record_digest
    assert envelope["data"]["record_written"] is True
    assert envelope["data"]["remote_mutations"] is False
    assert envelope["provider_labels"][0]["provider"] == "reddit_ads"


def test_a_ga4_pilot_reads_the_admin_inventory_and_the_data_report(ga4: Environment) -> None:
    code, envelope = ga4.run()

    assert code == 0, envelope
    record = _record(ga4)
    assert [(step.step, step.command) for step in record.steps] == list(pilot_steps("ga4"))
    hosts = {request.url.host for request in ga4.requests}
    assert hosts == {"analyticsadmin.googleapis.com", "analyticsdata.googleapis.com"}


def test_a_gtm_pilot_audits_the_retained_full_inventory_and_has_no_report(gtm: Environment) -> None:
    code, envelope = gtm.run()

    assert code == 0, envelope
    record = _record(gtm)
    assert record.report_window is None
    assert [step.step for step in record.steps] == [
        "ENROLLMENT",
        "DOCTOR",
        "INVENTORY",
        "AUDIT",
        "HISTORY_VERIFY",
    ]
    assert record.audit_coverage


def test_the_owner_supplied_ui_comparison_is_recorded_as_given(reddit: Environment) -> None:
    code, _ = reddit.run(**{"--ui-comparison": "MISMATCH"})

    assert code == 0
    assert _record(reddit).ui_comparison == "MISMATCH"


# --- the record and the output carry no identifiers ---------------------------------------


def _without_hex(text: str) -> str:
    return re.sub(r"[0-9a-f]{40,}", "", text)


def _forbidden_strings(environment: Environment) -> list[str]:
    return [
        _TOKEN,
        ACCOUNT,
        "123",
        "456",
        "789",
        "purchase",
        "c1",
        "g1",
        "ad1",
        "reddit_ads-read",
        "ga4-admin-read",
        "customer",
        "principal.fixture",
        "client.fixture",
        str(environment.root),
        str(Path.home()),
    ]


@pytest.mark.parametrize("name", ["reddit", "ga4", "gtm"])
def test_the_record_and_the_command_output_hold_no_identifier_name_token_or_payload(
    name: str, request: pytest.FixtureRequest
) -> None:
    environment: Environment = request.getfixturevalue(name)
    result = _RUNNER.invoke(app, environment.arguments())
    assert result.exit_code == 0, result.output
    text = environment.output.read_text()
    record = _record(environment)

    shown = result.output.replace(str(environment.output), "")
    for fragment in _forbidden_strings(environment):
        # Digests are random hex; an identifier is looked for in everything else. The
        # command output names the new file's path and nothing else of the root.
        pattern = re.compile(r"(?<![0-9A-Za-z])" + re.escape(fragment) + r"(?![0-9A-Za-z])")
        assert pattern.search(_without_hex(text)) is None, fragment
        if fragment != str(environment.root):
            assert pattern.search(_without_hex(shown)) is None, fragment
    # The pseudonym is a keyed hash of the profile's account, not the account.
    assert record.account_pseudonym.startswith("opaque:")
    # The file has the schema's fields and no others.
    assert set(json.loads(text)) == set(PilotRecord.model_fields)


def test_the_pseudonym_is_keyed_to_the_owners_local_key(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    reddit.run()
    first = _record(reddit).account_pseudonym
    reddit.output.unlink()
    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"q" * 32)

    reddit.run()

    assert _record(reddit).account_pseudonym != first


# --- a pilot only reads --------------------------------------------------------------------


def test_every_pilot_step_is_a_registered_read_command_not_a_remote_mutation() -> None:
    unreleased = set(release_policy.unreleased_commands())
    for provider in RELEASE_PROVIDERS:
        for _step, command in pilot_steps(provider):
            assert command in _COMMANDS, command
            assert command not in unreleased, command
            assert not command.startswith(("execute", "recover", "plan", "compile")), command
    assert "pilot run" not in unreleased


@pytest.mark.parametrize("name", ["reddit", "ga4"])
def test_a_pilot_sends_only_fixed_reads(name: str, request: pytest.FixtureRequest) -> None:
    environment: Environment = request.getfixturevalue(name)
    assert environment.run()[0] == 0

    methods = {request.method for request in environment.requests}
    assert methods <= {"GET", "POST"}
    # The only POSTs are the providers' report queries.
    for provider_request in environment.requests:
        if provider_request.method == "POST":
            assert provider_request.url.path.endswith(("/reports", ":runReport"))


# --- the selection is validated before any provider or credential is touched ----------------


def _untouched(environment: Environment) -> None:
    # A guard that an environment fixture replaced would let a late refusal pass.
    assert _guard_is_armed(), "the provider-access guard was overridden by a fixture"
    assert not environment.output.exists()
    assert not (environment.root / "ledger").exists()


@pytest.mark.parametrize(
    ("replace", "drop"),
    [
        ({"--provider": "hubspot"}, None),
        ({"--provider": "ga4_admin"}, None),
        ({"--provider": "apple_ads"}, None),
        ({"--ui-comparison": "NOT_COMPARED"}, None),
        ({"--ui-comparison": "match"}, None),
        ({"--source-commit": "main"}, None),
        ({"--source-commit": "0123456789ABCDEF0123456789abcdef01234567"}, None),
        ({"--source-commit": "0123456"}, None),
    ],
)
def test_an_invalid_selection_is_refused_before_anything_is_touched(
    replace: dict[str, str],
    drop: str | None,
    reddit: Environment,
    no_provider_access: None,
) -> None:
    code, envelope = reddit.run(**replace)

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_SELECTION"
    _untouched(reddit)


@pytest.mark.parametrize("flag", ["--report-input", "--audit-policy"])
def test_a_missing_required_option_is_refused_before_anything_is_touched(
    flag: str, reddit: Environment, no_provider_access: None
) -> None:
    index = reddit.options.index(flag)
    del reddit.options[index : index + 2]

    code, envelope = reddit.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_SELECTION"
    _untouched(reddit)


@pytest.mark.parametrize(
    "extra",
    [
        ["--container-id", "2"],
        ["--stream-id", "456"],
        ["--admin-profile", "ga4-admin-read"],
    ],
)
def test_an_option_that_does_not_apply_to_the_provider_is_refused(
    extra: list[str], reddit: Environment, no_provider_access: None
) -> None:
    reddit.options += extra

    code, envelope = reddit.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_SELECTION"
    _untouched(reddit)


def test_gtm_and_ga4_refuse_the_options_of_the_other_providers(
    gtm: Environment, no_provider_access: None
) -> None:
    gtm.options += ["--report-input", str(gtm.root / "report-input.json")]

    code, envelope = gtm.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_SELECTION"


def test_ga4_requires_its_admin_profile_and_stream(
    ga4: Environment, no_provider_access: None
) -> None:
    for flag in ("--admin-profile", "--stream-id"):
        options = list(ga4.options)
        index = ga4.options.index(flag)
        del ga4.options[index : index + 2]
        code, envelope = ga4.run()
        assert code == 2
        assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_SELECTION"
        ga4.options[:] = options


def test_an_existing_output_is_never_replaced(
    reddit: Environment, no_provider_access: None
) -> None:
    reddit.output.write_text("owner notes")

    code, envelope = reddit.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_SELECTION"
    assert reddit.output.read_text() == "owner notes"


def test_a_missing_output_directory_is_refused(
    reddit: Environment, no_provider_access: None
) -> None:
    code, envelope = reddit.run(**{"--output": str(reddit.root / "absent" / "pilot.json")})

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_SELECTION"


@pytest.mark.parametrize(
    "window",
    [
        ("2026-09-01", "2026-09-06"),
        ("2026-09-01", "2026-09-08"),
        ("2026-09-01", "2026-09-01"),
        ("2026-09-07", "2026-09-01"),
    ],
)
def test_a_report_window_other_than_seven_days_is_refused_up_front(
    window: tuple[str, str], reddit: Environment, no_provider_access: None
) -> None:
    query = json.loads((reddit.root / "report-input.json").read_text())
    _write_inputs_again(reddit.root, query, window)

    code, envelope = reddit.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_WINDOW"
    _untouched(reddit)


def _write_inputs_again(root: Path, query: dict[str, Any], window: tuple[str, str]) -> None:
    query["window_start"], query["window_end"] = window
    (root / "report-input.json").write_text(json.dumps(query))


@pytest.mark.parametrize(
    "mutation",
    [{"window_start": "first of September"}, {"window_start": None}, {"window_end": 7}],
)
def test_a_report_input_without_a_readable_window_is_refused(
    mutation: dict[str, Any], reddit: Environment, no_provider_access: None
) -> None:
    query = {**json.loads((reddit.root / "report-input.json").read_text()), **mutation}
    (reddit.root / "report-input.json").write_text(json.dumps(query))

    code, envelope = reddit.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_WINDOW"
    _untouched(reddit)


@pytest.mark.parametrize(
    "document", ["[]", '["2026-09-01", "2026-09-08"]', '"2026-09-01"', "7", "null"]
)
def test_a_report_input_that_is_not_a_json_object_declares_no_window(
    document: str, reddit: Environment, no_provider_access: None
) -> None:
    """``report <provider>`` refuses this as a schema failure; the pilot reads the window first."""
    (reddit.root / "report-input.json").write_text(document)

    code, envelope = reddit.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_PILOT_WINDOW"
    _untouched(reddit)


def _edit_report_input(environment: Environment, **changes: Any) -> None:
    path = environment.root / "report-input.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), **changes}))


@pytest.mark.parametrize("name", ["reddit", "ga4"])
def test_a_report_input_that_is_not_json_is_refused_with_its_own_diagnostic_before_any_step(
    name: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment: Environment = request.getfixturevalue(name)
    forbid_provider_access(monkeypatch)
    (environment.root / "report-input.json").write_text("{not json")

    code, envelope = environment.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INVALID_JSON"
    _untouched(environment)


@pytest.mark.parametrize("name", ["reddit", "ga4"])
def test_a_report_input_the_report_command_would_reject_is_refused_before_any_step(
    name: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole closed query schema is checked up front, not only the window."""
    environment: Environment = request.getfixturevalue(name)
    forbid_provider_access(monkeypatch)
    _edit_report_input(environment, unexpected_field="x")

    code, envelope = environment.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    _untouched(environment)


@pytest.mark.parametrize("name", ["reddit", "ga4"])
def test_a_report_for_another_account_than_the_profile_is_refused_before_any_step(
    name: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment: Environment = request.getfixturevalue(name)
    forbid_provider_access(monkeypatch)
    _edit_report_input(environment, account_ref="a2_someone_else")

    code, envelope = environment.run()

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "IDENTITY_MISMATCH"
    _untouched(environment)


def test_a_ga4_report_for_another_property_than_the_profile_is_refused_before_any_step(
    ga4: Environment, no_provider_access: None
) -> None:
    _edit_report_input(ga4, property_id="999")

    code, envelope = ga4.run()

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "IDENTITY_MISMATCH"
    _untouched(ga4)


def test_the_ga4_aggregate_contract_is_selected_by_its_discriminator_up_front(
    ga4: Environment, no_provider_access: None
) -> None:
    """`aggregate_shape` selects the aggregate query model exactly as `report ga4` does."""
    _edit_report_input(ga4, aggregate_shape="KEY_EVENT_TOTALS")

    code, envelope = ga4.run()

    # The legacy report fields are not the aggregate model's, so the schema refuses them.
    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    _untouched(ga4)


def test_an_invalid_audit_policy_is_refused_before_any_step(
    reddit: Environment, no_provider_access: None
) -> None:
    (reddit.root / "audit-policy.json").write_text(json.dumps({"unknown_threshold": 1}))

    code, envelope = reddit.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    _untouched(reddit)


def test_an_unsafe_input_file_is_refused_before_any_step(
    reddit: Environment, no_provider_access: None
) -> None:
    link = reddit.root / "linked-input.json"
    link.symlink_to(reddit.root / "report-input.json")
    reddit.options[reddit.options.index("--report-input") + 1] = str(link)

    code, envelope = reddit.run()

    assert code == 2
    assert envelope["diagnostics"][0]["code"] == "INPUT_FILE_INVALID"
    _untouched(reddit)


@pytest.mark.parametrize(
    "profile",
    ["no-such-profile", "ga4.read"],
)
def test_the_profile_must_be_a_read_profile_for_the_provider(
    profile: str, reddit: Environment, no_provider_access: None
) -> None:
    code, envelope = reddit.run(**{"--profile": profile})

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "READ_PROFILE_REQUIRED"
    _untouched(reddit)


def test_an_edit_profile_cannot_run_a_pilot(reddit: Environment, no_provider_access: None) -> None:
    edit = _profile().model_copy(update={"profile_id": "reddit-edit", "purpose": "EDIT"})
    save_configuration(
        reddit.root,
        Configuration(profiles=[_profile(), _ga4_profile(), edit]),
        replace=True,
    )

    code, envelope = reddit.run(**{"--profile": "reddit-edit"})

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "READ_PROFILE_REQUIRED"


# --- an incomplete pilot writes nothing ---------------------------------------------------------


def _steps(envelope: dict[str, Any]) -> dict[str, str]:
    return {row["step"]: row["status"] for row in envelope["data"]["steps"]}


def test_a_failed_doctor_stops_the_pilot_before_any_provider_request(
    reddit: Environment,
) -> None:
    save_configuration(reddit.root, Configuration(profiles=[_profile()]), replace=True)

    code, envelope = reddit.run()

    assert code == 3
    assert envelope["outcome"] == "PILOT_INCOMPLETE"
    assert envelope["data"]["record_written"] is False
    assert _steps(envelope) == {
        "ENROLLMENT": "PASSED",
        "DOCTOR": "FAILED",
        "INVENTORY": "NOT_RUN",
        "REPORT": "NOT_RUN",
        "AUDIT": "NOT_RUN",
        "HISTORY_VERIFY": "NOT_RUN",
    }
    assert reddit.requests == [] and reddit.credential_reads == []
    assert not reddit.output.exists()


def test_a_missing_ga4_admin_profile_is_refused_before_any_step(
    ga4: Environment, no_provider_access: None
) -> None:
    save_configuration(ga4.root, Configuration(profiles=[_ga4_profile()]), replace=True)

    code, envelope = ga4.run()

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "READ_PROFILE_REQUIRED"
    _untouched(ga4)


@pytest.mark.parametrize("admin", ["no-such-profile", "ga4.read", "ga4-admin-edit"])
def test_the_admin_profile_must_be_a_read_ga4_admin_profile(
    admin: str, ga4: Environment, no_provider_access: None
) -> None:
    edit = Profile(
        profile_id="ga4-admin-edit",
        provider="ga4_admin",
        account_ref="123",
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["https://www.googleapis.com/auth/analytics.edit"],
        purpose="EDIT",
        currency="USD",
        timezone=_TZ,
        credential_ref="ga4-admin-fixture",
    )
    configuration = load_configuration(ga4.root)
    save_configuration(
        ga4.root,
        Configuration(profiles=[*configuration.profiles, edit]),
        replace=True,
    )
    ga4.options[ga4.options.index("--admin-profile") + 1] = admin

    code, envelope = ga4.run()

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "READ_PROFILE_REQUIRED"
    _untouched(ga4)


@pytest.mark.parametrize(
    ("suffix", "status", "failed", "passed"),
    [
        ("/ad_accounts/" + ACCOUNT, 403, "INVENTORY", ["ENROLLMENT", "DOCTOR"]),
        ("/reports", 500, "REPORT", ["ENROLLMENT", "DOCTOR", "INVENTORY"]),
    ],
)
def test_a_provider_failure_stops_the_pilot_and_writes_no_record(
    suffix: str,
    status: int,
    failed: str,
    passed: list[str],
    reddit: Environment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    served = _Provider()

    def failing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(suffix):
            return httpx.Response(status, json={})
        return served(request)

    _install(monkeypatch, reddit, failing)

    code, envelope = reddit.run()

    assert code != 0
    assert envelope["outcome"] == "PILOT_INCOMPLETE"
    steps = _steps(envelope)
    assert [name for name, state in steps.items() if state == "PASSED"] == passed
    assert steps[failed] == "FAILED"
    row = next(row for row in envelope["data"]["steps"] if row["step"] == failed)
    assert row["diagnostic_code"] and row["next_action"]
    assert "NOT_RUN" in steps.values() and steps["HISTORY_VERIFY"] == "NOT_RUN"
    assert not reddit.output.exists()
    assert _TOKEN not in json.dumps(envelope)


def test_an_account_the_profile_does_not_name_fails_identity_and_writes_no_record(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = _Provider()

    def other_account(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v3/ad_accounts/{ACCOUNT}":
            return httpx.Response(
                200, json={"data": {"id": "a2_other", "currency": "USD", "time_zone_id": "UTC"}}
            )
        return served(request)

    _install(monkeypatch, reddit, other_account)

    code, envelope = reddit.run()

    assert code != 0
    assert _steps(envelope)["INVENTORY"] == "FAILED"
    assert not reddit.output.exists()


def test_a_report_input_changed_after_validation_cannot_put_another_window_in_the_record(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record's window is the one the report was retained for, not the one declared."""
    other = PilotWindow(window_start=date(2026, 8, 1), window_end=date(2026, 8, 7))
    monkeypatch.setattr(pilot_workflow, "_declared_window", lambda *_args: other)

    code, envelope = reddit.run()

    assert code == 3
    assert _steps(envelope)["REPORT"] == "FAILED"
    row = next(row for row in envelope["data"]["steps"] if row["step"] == "REPORT")
    assert row["diagnostic_code"] == "PILOT_REPORT_NOT_RETAINED_FOR_WINDOW"
    assert not reddit.output.exists()


def test_an_output_created_during_the_run_is_not_overwritten(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = _Provider()

    def creating(request: httpx.Request) -> httpx.Response:
        reddit.output.write_text("created during the run")
        return served(request)

    _install(monkeypatch, reddit, creating)

    code, envelope = reddit.run()

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "PILOT_OUTPUT_EXISTS"
    assert reddit.output.read_text() == "created during the run"


def test_a_step_whose_output_is_not_a_result_envelope_fails_the_pilot_without_echoing_it(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_an_envelope(*_args: object, **_kwargs: object) -> int:
        typer.echo("plain text, not an envelope " + _TOKEN)
        return 0

    monkeypatch.setattr(adhelm.cli, "app", not_an_envelope)

    code, envelope = reddit.run()

    assert code == 10
    row = envelope["data"]["steps"][0]
    assert (row["step"], row["status"], row["outcome"]) == ("ENROLLMENT", "FAILED", "UNREADABLE")
    assert _TOKEN not in json.dumps(envelope)
    assert not reddit.output.exists()


# --- each acceptance rule refuses what it should, and a refused run writes nothing -------------


def _tamper(
    monkeypatch: pytest.MonkeyPatch,
    step: str,
    change: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    """Replace one step's displayed data with what a different provider answer would show."""
    real = pilot_workflow._invoke

    def tampered(root: Path, name: str, command: str, arguments: list[str]) -> Any:
        result = real(root, name, command, arguments)
        if name != step:
            return result
        return dataclasses.replace(result, data=change(dict(result.data)))

    monkeypatch.setattr(pilot_workflow, "_invoke", tampered)


def _failed_row(envelope: dict[str, Any]) -> dict[str, Any]:
    failed = [row for row in envelope["data"]["steps"] if row["status"] == "FAILED"]
    assert len(failed) == 1, envelope["data"]["steps"]
    return failed[0]


@pytest.mark.parametrize("name", ["reddit", "ga4", "gtm"])
def test_an_inventory_that_was_not_retained_complete_fails_the_pilot(
    name: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment: Environment = request.getfixturevalue(name)
    _tamper(
        monkeypatch,
        "INVENTORY",
        lambda data: (
            {key: value for key, value in data.items() if key != "record_digest"}
            | {"status": "INVENTORY_INCOMPLETE", "persisted": False, "ephemeral": True}
        ),
    )

    code, envelope = environment.run()

    assert code == 3
    row = _failed_row(envelope)
    assert (row["step"], row["diagnostic_code"]) == ("INVENTORY", "PILOT_INVENTORY_NOT_RETAINED")
    assert row["next_action"]
    assert not environment.output.exists()


@pytest.mark.parametrize("name", ["reddit", "ga4"])
def test_a_configuration_that_does_not_list_the_read_profiles_fails_enrollment(
    name: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enrollment is observed, not assumed: `config show` must list the READ profiles."""
    environment: Environment = request.getfixturevalue(name)
    _tamper(monkeypatch, "ENROLLMENT", lambda data: {**data, "profiles": []})

    code, envelope = environment.run()

    assert code == 3
    row = _failed_row(envelope)
    assert (row["step"], row["diagnostic_code"]) == ("ENROLLMENT", "PILOT_READ_PROFILE_MISSING")
    assert not environment.output.exists()


def test_a_ga4_inventory_without_a_retained_digest_fails_the_pilot(
    ga4: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GA4 holds its inventory to the same retained-digest rule as GTM."""
    _tamper(
        monkeypatch,
        "INVENTORY",
        lambda data: {key: value for key, value in data.items() if key != "record_digest"},
    )

    code, envelope = ga4.run()

    assert code == 3
    assert _failed_row(envelope)["diagnostic_code"] == "PILOT_INVENTORY_NOT_RETAINED"
    assert not ga4.output.exists()


@pytest.mark.parametrize("name", ["reddit", "ga4", "gtm"])
def test_an_audit_without_a_whole_coverage_table_fails_the_pilot(
    name: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment: Environment = request.getfixturevalue(name)
    _tamper(monkeypatch, "AUDIT", lambda data: {"section": "FINDINGS", "sections": {}})

    code, envelope = environment.run()

    assert code == 3
    row = _failed_row(envelope)
    assert (row["step"], row["diagnostic_code"]) == ("AUDIT", "PILOT_AUDIT_COVERAGE_UNAVAILABLE")
    assert not environment.output.exists()


def test_local_history_that_did_not_verify_fails_the_pilot(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tamper(monkeypatch, "HISTORY_VERIFY", lambda data: {**data, "status": "NOT_VERIFIED"})

    code, envelope = reddit.run()

    assert code == 3
    row = _failed_row(envelope)
    assert (row["step"], row["diagnostic_code"]) == ("HISTORY_VERIFY", "PILOT_HISTORY_NOT_VERIFIED")
    assert not reddit.output.exists()


def test_the_report_is_accepted_from_the_retained_record_not_from_how_it_was_displayed(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The displayed page may omit the envelope; the retained report decides the window."""
    _tamper(monkeypatch, "REPORT", lambda data: {k: v for k, v in data.items() if k != "envelope"})

    code, envelope = reddit.run()

    assert code == 0, envelope
    assert _record(reddit).report_window is not None


def test_a_report_digest_that_history_does_not_hold_fails_the_pilot(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tamper(monkeypatch, "REPORT", lambda data: {**data, "retained_record_digest": "0" * 64})

    code, envelope = reddit.run()

    assert code == 3
    row = _failed_row(envelope)
    assert (row["step"], row["diagnostic_code"]) == (
        "REPORT",
        "PILOT_REPORT_NOT_RETAINED_FOR_WINDOW",
    )
    assert not reddit.output.exists()


def test_a_report_the_saved_output_bound_cannot_show_fails_with_the_accurate_code(
    reddit: Environment,
) -> None:
    """A retained report shown in compact form is an output-bound failure, never `not retained`."""
    save_configuration(
        reddit.root,
        Configuration(
            profiles=[_profile(), _ga4_profile()],
            output=OutputPreferences(max_output_bytes=3_000),
        ),
        replace=True,
    )

    code, envelope = reddit.run()

    assert code == 5
    assert _steps(envelope)["INVENTORY"] == "PASSED"
    row = _failed_row(envelope)
    assert (row["step"], row["outcome"]) == ("REPORT", "PARTIAL")
    assert row["diagnostic_code"] == "OUTPUT_LIMIT_EXCEEDED"
    assert "max_output_bytes" in row["next_action"]
    assert not reddit.output.exists()


def test_a_step_the_cli_rejects_before_running_fails_the_pilot(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    def rejecting(*_args: object, **_kwargs: object) -> int:
        raise click.ClickException("No such option")

    monkeypatch.setattr(adhelm.cli, "app", rejecting)

    code, envelope = reddit.run()

    assert code == 2
    row = _failed_row(envelope)
    assert (row["step"], row["outcome"]) == ("ENROLLMENT", "REJECTED")
    assert not reddit.output.exists()


def test_a_step_whose_data_is_not_an_object_fails_the_pilot_as_unreadable(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    def odd(*_args: object, **_kwargs: object) -> int:
        typer.echo(json.dumps({"data": [], "diagnostics": [], "outcome": "OK"}))
        return 0

    monkeypatch.setattr(adhelm.cli, "app", odd)

    code, envelope = reddit.run()

    assert code == 10
    assert _failed_row(envelope)["outcome"] == "UNREADABLE"


def test_a_platform_the_record_schema_has_no_entry_for_is_refused_before_any_step(
    reddit: Environment, no_provider_access: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pilot_workflow, "_PLATFORMS", {})

    code, envelope = reddit.run()

    assert code == 3
    assert envelope["diagnostics"][0]["code"] == "PILOT_PLATFORM_UNSUPPORTED"
    _untouched(reddit)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"sections": {}},
        {"sections": {"coverage": []}},
        {"sections": {"coverage": "COVERED"}},
        {"sections": {"coverage": [{"dimension": "ACCOUNT"}]}},
        {"sections": {"coverage": [{"dimension": "account", "status": "COVERED"}]}},
        {"sections": {"coverage": [{"dimension": "ACCOUNT", "status": "SOMETIMES"}]}},
        {"sections": {"coverage": ["ACCOUNT"]}},
    ],
)
def test_an_audit_coverage_table_that_is_absent_or_malformed_is_not_accepted(
    data: dict[str, Any],
) -> None:
    assert pilot_workflow._coverage("reddit_ads", data) is None


def test_the_gtm_coverage_is_the_coverage_section_only() -> None:
    row = {"dimension": "TAGS", "status": "PARTIAL"}

    assert pilot_workflow._coverage("gtm", {"section": "FINDINGS", "rows": [row]}) is None
    assert pilot_workflow._coverage("gtm", {"section": "COVERAGE", "rows": [row]}) == [
        PilotCoverage(dimension="TAGS", status="PARTIAL")
    ]


def test_a_partial_record_file_is_removed_when_writing_fails(
    reddit: Environment, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert reddit.run()[0] == 0
    record = _record(reddit)
    target = reddit.root / "records" / "second.json"

    def failing(_descriptor: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(pilot_workflow.os, "fsync", failing)
    with pytest.raises(OSError, match="disk full"):
        pilot_workflow._write_record(target, record)

    assert not target.exists()


# --- what a record does to labels ---------------------------------------------------------------


def test_a_committed_record_labels_its_provider_and_only_for_this_build(
    reddit: Environment, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert reddit.run()[0] == 0
    committed = tmp_path / "docs-pilots"
    committed.mkdir()
    (committed / "reddit_ads.json").write_bytes(reddit.output.read_bytes())

    assert release_label("reddit_ads") is not None
    assert release_label("reddit_ads").label == EXPERIMENTAL  # type: ignore[union-attr]

    monkeypatch.setattr(release_labels, "pilot_records_directory", lambda: committed)
    pilot_record_inventory.cache_clear()
    labelled = release_label("reddit_ads")
    assert labelled is not None and labelled.label == LIVE_PILOTED
    assert release_label("meta_ads").label == EXPERIMENTAL  # type: ignore[union-attr]

    # The same record is stale for any other build version.
    monkeypatch.setattr(release_labels, "__version__", "9.9.9")
    pilot_record_inventory.cache_clear()
    stale = release_label("reddit_ads")
    assert stale is not None and stale.label == EXPERIMENTAL


def test_running_a_pilot_changes_no_label_by_itself(reddit: Environment) -> None:
    assert reddit.run()[0] == 0

    assert {label.label for label in release_labels.provider_labels()} == {EXPERIMENTAL}


# --- discovery --------------------------------------------------------------------------------


def test_discover_lists_each_providers_pilot_steps_and_options() -> None:
    result = _RUNNER.invoke(app, ["discover"])
    pilot = json.loads(result.output)["data"]["pilot"]

    assert pilot["command"] == "pilot run"
    assert pilot["report_window_days"] == 7
    assert pilot["ui_comparison"] == ["MATCH", "MISMATCH"]
    assert set(pilot["providers"]) == set(RELEASE_PROVIDERS)
    for provider, listed in pilot["providers"].items():
        assert [(row["step"], row["command"]) for row in listed["steps"]] == list(
            pilot_steps(provider)
        )
    assert pilot["providers"]["gtm"]["options"] == ["--container-id"]
    assert pilot["providers"]["ga4"]["options"] == [
        "--admin-profile",
        "--stream-id",
        "--report-input",
        "--audit-policy",
    ]
    assert pilot["providers"]["linkedin_ads"]["options"] == ["--report-input", "--audit-policy"]
    assert pilot["remote_mutations"] is False
