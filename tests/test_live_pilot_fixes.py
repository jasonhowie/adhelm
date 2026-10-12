"""Regressions from the first read pilot. All data and transport responses are synthetic."""

from __future__ import annotations

import ast
import csv
import io
import json
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from test_google_ads import CONFIG, _client, _identity
from test_google_oauth_browser_ultra_review import _attempt
from test_oauth_enrollment import PKCE_CONTRACT, attempt
from test_release_artifacts import _script as release_script
from test_smoke_installed_wheel import _script as smoke_script

from adhelm import google_oauth_browser, oauth_enrollment
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.projections import export_verified
from adhelm.providers.google_ads import (
    GOOGLE_ADS_API_VERSION,
    GoogleAdsInventoryQuery,
    GoogleAdsReportQuery,
    _campaign_datetime,
)
from adhelm.report_sharing import report_for_output
from adhelm.reporting import ConversionBreakdownRow, NormalizedReport, canonical_metric_decimal
from adhelm.transport import SafeHttpClient
from adhelm.workflows import _report_provenance, persist_report


def query(*, names: bool = False, level: str = "CAMPAIGN") -> GoogleAdsReportQuery:
    return GoogleAdsReportQuery(
        account_ref=CONFIG.account_ref,
        report_id="report.pilot",
        query_contract_hash="a" * 64,
        window_start=date(2026, 9, 1),
        window_end=date(2026, 9, 1),
        source_timezone="UTC",
        include_campaign_names=names,
        level=level,
    )


def report(*, names: bool = False, metrics: dict[str, Any] | None = None) -> NormalizedReport:
    def handle(request: httpx.Request) -> httpx.Response:
        gaql = json.loads(request.content)["query"]
        if "FROM customer" in gaql:
            return httpx.Response(200, json=_identity())
        assert "metrics.all_conversions_value" in gaql
        assert ("campaign.name" in gaql) == names
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "campaign": {
                            "id": "11",
                            **({"name": "Acme Brand Search"} if names else {}),
                        },
                        "segments": {"date": "2026-09-01"},
                        "metrics": {
                            "impressions": "10",
                            "clicks": "2",
                            "costMicros": "12345",
                            **(
                                metrics
                                if metrics is not None
                                else {
                                    "conversions": 1.25,
                                    "conversionsValue": 3.125,
                                    "allConversions": 2.75,
                                    "allConversionsValue": 5.375,
                                }
                            ),
                        },
                    }
                ],
                "totalResultsCount": "1",
            },
        )

    client = _client(httpx.MockTransport(handle))
    return client.run_daily_report(
        CONFIG, query(names=names), identity=client.discover_identity(CONFIG)
    )


def test_fractional_counts_values_labels_and_exports_round_trip(tmp_path: Path) -> None:
    result = report(names=True)
    primary, all_conversions = result.conversion_rows
    assert primary.conversion_count is None
    assert primary.conversion_count_decimal == "1.25"
    assert all_conversions.conversion_count_decimal == "2.75"
    assert primary.conversion_value is not None and primary.conversion_value.minor_units == 312
    assert (
        all_conversions.conversion_value is not None
        and all_conversions.conversion_value.minor_units == 538
    )
    assert primary.provider_extensions["google_ads_conversion_value"] == "3.125"
    assert primary.conversion_kind != all_conversions.conversion_kind
    assert primary.attribution_model == "NOT_AVAILABLE_FROM_PROVIDER"
    assert primary.conversion_action_ref is None
    assert NormalizedReport.model_validate_json(canonical_bytes(result)) == result
    key = b"k" * 32
    default = report_for_output(result, key)
    named = report_for_output(result, key, include_campaign_names=True)
    assert "Acme Brand Search" not in json.dumps(default)
    assert (
        named["performance_rows"][0]["provider_extensions"]["google_ads_campaign_name"]
        == "Acme Brand Search"
    )
    assert (
        default["performance_rows"][0]["campaign_ref"]
        == named["performance_rows"][0]["campaign_ref"]
    )
    ledger = Ledger(tmp_path / "data")
    result = _report_provenance(result, query(names=True), ledger.checkpoint())
    persist_report(ledger, result)
    assert ledger.verify()["status"] == "VERIFIED"
    export_verified(ledger, tmp_path / "export", opaque_key=key)
    rows = list(csv.DictReader((tmp_path / "export/conversions.csv").open()))
    assert {row["conversion_count_decimal"] for row in rows} == {"1.25", "2.75"}
    assert {row["conversion_kind"] for row in rows} == {
        "GOOGLE_ADS_CONVERSIONS",
        "GOOGLE_ADS_ALL_CONVERSIONS",
    }
    assert {row["date"] for row in rows} == {"2026-09-01"}
    for path in (tmp_path / "export").iterdir():
        if path.is_file():
            assert "Acme Brand Search" not in path.read_text()


def test_missing_zero_and_fractional_conversion_counts_are_distinct() -> None:
    assert report(metrics={}).conversion_rows == []
    zero = report(metrics={"conversions": 0, "conversionsValue": 0}).conversion_rows[0]
    assert zero.conversion_count == 0 and zero.conversion_count_decimal == "0"
    missing = report(metrics={"conversionsValue": 0}).conversion_rows[0]
    assert missing.conversion_count is None and missing.conversion_count_decimal is None
    assert "NOT_AVAILABLE_FROM_PROVIDER" in missing.data_quality_statuses


@pytest.mark.parametrize(
    "bad",
    [
        None,
        True,
        -1,
        "NaN",
        "Infinity",
        "1e999",
        "1e-999",
        "1e999999999999999999999999999999999999",
        "private@example.test",
    ],
)
def test_bad_conversion_numbers_fail_without_echo(bad: object) -> None:
    with pytest.raises(AdhelmError) as raised:
        report(metrics={"conversions": bad})
    assert raised.value.code == "PROVIDER_RESPONSE_INVALID"
    assert "private@example.test" not in str(raised.value)


def test_legacy_conversion_rows_keep_their_serialized_shape() -> None:
    old = ConversionBreakdownRow(
        report_id="report.legacy",
        conversion_source="GA4",
        conversion_kind="KEY_EVENT",
        attribution_model="UNKNOWN",
        conversion_count=2,
    ).model_dump(mode="json")
    assert "conversion_count_decimal" not in old
    assert (
        ConversionBreakdownRow.model_validate_json(canonical_bytes(old)).model_dump(mode="json")
        == old
    )
    with pytest.raises(ValidationError):
        ConversionBreakdownRow.model_validate({**old, "conversion_count_decimal": "2.5"})


@pytest.mark.parametrize(
    ("value", "expected"), [("1e-3", "0.001"), ("2.5000", "2.5"), ("0.000", "0")]
)
def test_canonical_decimal_spelling(value: str, expected: str) -> None:
    assert canonical_metric_decimal(value) == expected


def test_v25_inventory_preserves_time_and_rejects_removed_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        gaql = json.loads(request.content)["query"]
        if "FROM customer" in gaql:
            return httpx.Response(200, json=_identity())
        if "FROM campaign " in gaql:
            assert "campaign.start_date_time" in gaql
            assert "campaign.end_date_time" in gaql
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "campaign": {
                                "id": "11",
                                "status": "PAUSED",
                                "advertisingChannelType": "SEARCH",
                                "biddingStrategyType": "MANUAL_CPC",
                                "startDateTime": "2026-09-01 12:34:56",
                                "endDateTime": "2026-09-02 23:59:59",
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})

    client = _client(httpx.MockTransport(handler))
    inventory = client.inventory(
        CONFIG,
        GoogleAdsInventoryQuery(account_ref=CONFIG.account_ref),
        identity=client.discover_identity(CONFIG),
    )
    assert inventory.campaigns[0].start_date_time == datetime(2026, 9, 1, 12, 34, 56, tzinfo=UTC)
    assert inventory.campaigns[0].start_date == date(2026, 9, 1)


@pytest.mark.parametrize(
    "value", ["2026-09-01", "2026-02-30 00:00:00", "2026-11-01 01:30:00", "2026-03-08 02:30:00"]
)
def test_invalid_or_ambiguous_campaign_times_are_refused(value: str) -> None:
    with pytest.raises(AdhelmError):
        _campaign_datetime(value, "America/New_York")


def test_every_google_ads_field_literal_exists_in_the_pinned_official_metadata() -> None:
    root = Path(__file__).resolve().parents[1]
    metadata = json.loads((root / "tests/fixtures/google_ads_v25_fields.json").read_text())
    assert metadata["api_version"] == GOOGLE_ADS_API_VERSION
    resources = {field.split(".")[0] for field in metadata["fields"]}
    observed = set()
    for path in [
        *(root / "src/adhelm").glob("google_ads*.py"),
        *(root / "src/adhelm/providers").glob("google_ads*.py"),
    ]:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                observed.update(
                    field
                    for field in re.findall(r"\b[a-z_]+\.[a-z_]+(?:\.[a-z_]+)*", node.value)
                    if field.split(".")[0] in resources
                )
    assert observed
    assert observed <= metadata["fields"].keys()
    assert all(metadata["fields"][field]["selectable"] == "True" for field in observed)
    assert "campaign.start_date" not in observed
    assert "campaign.end_date" not in observed


def test_google_error_detail_survives_transport_and_classification() -> None:
    secret = "private@example.test"

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "message": secret,
                    "details": [
                        {
                            "@type": (
                                "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure"
                            ),
                            "requestId": "request-123",
                            "errors": [
                                {
                                    "errorCode": {"queryError": "UNRECOGNIZED_FIELD"},
                                    "message": secret,
                                }
                            ],
                        }
                    ],
                }
            },
        )

    with pytest.raises(AdhelmError) as raised:
        _client(httpx.MockTransport(handle)).discover_identity(CONFIG)
    assert raised.value.status_code == 400
    assert raised.value.provider_code == "queryError.UNRECOGNIZED_FIELD"
    assert raised.value.provider_request_id == "request-123"
    assert "Upgrade AdHelm" in raised.value.next_action
    assert secret not in str(vars(raised.value))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"x" * 16385,
        b'{"error":{"details":[{"@type":"type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure","errors":[{"errorCode":{"queryError":[]}}]}]}}',
    ],
)
def test_malformed_error_details_do_not_replace_the_http_failure(body: bytes) -> None:
    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400, content=body))),
        {"googleads.googleapis.com"},
    )
    with pytest.raises(AdhelmError) as raised:
        transport.request("GET", "https://googleads.googleapis.com/v25/test")
    assert raised.value.code == "PROVIDER_REQUEST_FAILED"
    assert raised.value.status_code == 400
    assert raised.value.provider_code is None


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("google", [True, False])
def test_manual_oauth_handoff_shows_progress_only_on_stderr(
    monkeypatch: pytest.MonkeyPatch, google: bool
) -> None:
    output, errors = io.StringIO(), Terminal()
    monkeypatch.setattr("sys.stdout", output)
    monkeypatch.setattr("sys.stderr", errors)
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: pytest.fail("manual handoff launched a process")
    )
    if google:
        selected = _attempt("http://127.0.0.1:43123/oauth/callback/" + "a" * 32)
        monkeypatch.setattr("adhelm.google_oauth_protocol._utc_now", lambda: selected.issued_at)
        google_oauth_browser.open_system_browser(selected, no_browser=True)
        verifier = selected.code_verifier
    else:
        selected_generic = attempt(PKCE_CONTRACT)
        oauth_enrollment.open_system_browser(selected_generic, no_browser=True)
        verifier = selected_generic.code_verifier
    assert output.getvalue() == ""
    assert "https://" in errors.getvalue()
    assert "Waiting for" in errors.getvalue() and "Ctrl-C" in errors.getvalue()
    assert verifier not in errors.getvalue()


@pytest.mark.parametrize("state", [None, "READY_FOR_USER_AUTHORIZATION", "IMPLEMENTING"])
def test_installed_wheel_smoke_refuses_wrong_release_state(state: str | None) -> None:
    smoke = smoke_script()
    with pytest.raises(smoke.SmokeError, match="release"):
        smoke.check_version(
            {
                "outcome": "OK",
                "tool_version": "0.1.1",
                "data": {"version": "0.1.1", "release_state": state},
            },
            "0.1.1",
        )


def test_artifact_gate_reads_packaged_constants_without_executing() -> None:
    release = release_script()
    with pytest.raises(release.ReleaseError):
        release.require_release_constants(
            b'__version__ = "0.1.1"\nRELEASE_STATE = "READY_FOR_USER_AUTHORIZATION"', "0.1.1"
        )
    release.require_release_constants(
        b'__version__ = "0.1.1"\nRELEASE_STATE = "RELEASED_0_1_0"\n'
        b'raise RuntimeError("must not execute")',
        "0.1.1",
    )


def test_retained_report_requires_explicit_name_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from adhelm.cli import app
    from adhelm.config import save_configuration
    from adhelm.models import Configuration

    root = tmp_path / "state"
    save_configuration(root, Configuration())
    ledger = Ledger(root)
    selected = _report_provenance(report(names=True), query(names=True), ledger.checkpoint())
    record = persist_report(ledger, selected)
    monkeypatch.setattr("adhelm.credentials.export_key", lambda: b"k" * 32)
    runner = CliRunner()
    args = ["--data-dir", str(root), "report", "show", record["record_digest"]]
    hidden = runner.invoke(app, args)
    shown = runner.invoke(app, [*args, "--include-campaign-names"])
    assert hidden.exit_code == shown.exit_code == 0
    assert "Acme Brand Search" not in hidden.stdout
    assert "Acme Brand Search" in shown.stdout
    assert (
        json.loads(hidden.stdout)["data"]["provenance"]
        == json.loads(shown.stdout)["data"]["provenance"]
    )


def test_conversion_audit_reports_partial_evidence_without_claiming_it_was_unread() -> None:
    from test_google_ads_audit import _sections

    result = _sections(report=report())
    native = next(item for item in result.coverage if item.dimension == "NATIVE_CONVERSION")
    assert native.status == "PARTIAL"
    assert native.evidence_sources == ("PERFORMANCE_OBSERVATION",)
    assert native.evidence_record_count == 1
    assert "GA_NATIVE_CONVERSION_NOT_READ" not in {item.rule_id for item in result.findings}


@pytest.mark.parametrize(
    ("host", "payload", "code"),
    [
        (
            "graph.facebook.com",
            {"error": {"code": 100, "error_subcode": 33, "message": "private@example.test"}},
            "100:33",
        ),
        (
            "api.linkedin.com",
            {"serviceErrorCode": 65600, "message": "private@example.test"},
            "65600",
        ),
        ("business-api.tiktok.com", {"code": 40001, "message": "private@example.test"}, "40001"),
        ("ads-api.reddit.com", {"error": {"code": 400, "message": "private@example.test"}}, "400"),
        (
            "api.ads.openai.com",
            {"error": {"code": "INVALID_ARGUMENT", "message": "private@example.test"}},
            "INVALID_ARGUMENT",
        ),
    ],
)
def test_provider_error_fields_never_echo_free_text(
    host: str, payload: dict[str, Any], code: str
) -> None:
    transport = SafeHttpClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(400, json=payload))),
        {host},
    )
    with pytest.raises(AdhelmError) as raised:
        transport.request("GET", f"https://{host}/fixed")
    assert raised.value.provider_code == code
    assert "private@example.test" not in str(vars(raised.value))


def test_provider_request_id_cannot_reflect_the_bearer() -> None:
    secret = "sensitive-bearer-value"
    transport = SafeHttpClient(
        httpx.Client(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(400, json={}, headers={"request-id": secret})
            )
        ),
        {"api.linkedin.com"},
    )
    with pytest.raises(AdhelmError) as raised:
        transport.request(
            "GET", "https://api.linkedin.com/fixed", headers={"authorization": f"Bearer {secret}"}
        )
    assert raised.value.provider_request_id is None


def test_redirect_url_is_not_printed_to_a_nonterminal(monkeypatch: pytest.MonkeyPatch) -> None:
    from adhelm.oauth_handoff import show_authorization_handoff

    stream = io.StringIO()
    monkeypatch.setattr("sys.stderr", stream)
    show_authorization_handoff("https://accounts.google.com/", provider="Google")
    assert stream.getvalue() == ""


def test_conversion_upcaster_preserves_original_record() -> None:
    from adhelm.reporting import upcast_conversion_row

    original = ConversionBreakdownRow(
        report_id="report.legacy",
        conversion_source="GA4",
        conversion_kind="KEY_EVENT",
        attribution_model="UNKNOWN",
        conversion_count=3,
    )
    before = canonical_bytes(original)
    upgraded = upcast_conversion_row(original)
    assert upgraded.schema_version == 2
    assert upgraded.conversion_count_decimal == "3"
    assert upgraded.conversion_count == original.conversion_count
    assert canonical_bytes(original) == before
    assert upcast_conversion_row(upgraded) == upgraded


def test_schedule_precision_upgrade_does_not_invent_an_external_change(tmp_path: Path) -> None:
    from adhelm.inventory import _change_for_snapshot, _snapshot

    ledger = Ledger(tmp_path / "history")

    def snapshot(start: str, shape: str, budget: int = 100) -> dict[str, Any]:
        return _snapshot(
            provider_api_version="v25",
            resource_type="CAMPAIGN",
            resource_ref="campaign.fixture",
            parents=[],
            state={
                "status": "PAUSED",
                "schedule_start": start,
                "currency": "USD",
                "budget_minor": budget,
            },
            status="PAUSED",
            quality=[shape],
        )

    old = snapshot("2026-01-01", "GOOGLE_ADS_STATE_SHAPE_2026-09-09.1")
    original = ledger.append(
        "changes", "RESOURCE_SNAPSHOT", old, provider="google_ads", account_ref="account.fixture"
    )
    new = snapshot("2026-01-01T05:00:00+00:00", "GOOGLE_ADS_STATE_SHAPE_2026-10-12.1")
    assert _change_for_snapshot(ledger, new, "google_ads", "account.fixture") is None
    changed = snapshot("2026-01-01T05:00:00+00:00", "GOOGLE_ADS_STATE_SHAPE_2026-10-12.1", 200)
    event = _change_for_snapshot(ledger, changed, "google_ads", "account.fixture")
    assert event is not None
    assert [delta["path"] for delta in event["field_deltas"]] == ["normalized_state.budget_minor"]
    assert ledger.records("changes")[-1] == original


@pytest.mark.parametrize("padded", [False, True])
@pytest.mark.parametrize("compressed", [False, True])
def test_streamed_project_error_keeps_classification_and_bounded_diagnostics(
    padded: bool, compressed: bool
) -> None:
    import gzip

    from test_google_ads_cloud_project_access import _client as project_client
    from test_google_ads_cloud_project_access import _failure

    payload: Any = _failure("CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION")
    payload["error"]["details"][0]["requestId"] = "request-456"
    raw = json.dumps(payload).encode() + (b" " * 18_000 if padded else b"")
    encoded = gzip.compress(raw) if compressed else raw
    iterations = 0

    class OnePassBody(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            nonlocal iterations
            iterations += 1
            assert iterations == 1
            yield encoded

    def handle(request: httpx.Request) -> httpx.Response:
        assert "developer-token" not in request.headers
        return httpx.Response(
            403,
            stream=OnePassBody(),
            headers={"content-encoding": "gzip"} if compressed else {},
        )

    with pytest.raises(AdhelmError) as raised:
        project_client(handle).discover_identity(CONFIG)
    error = raised.value
    assert error.code == "GOOGLE_ADS_CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
    assert error.provider_error_code == "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
    assert "Explorer access" in error.next_action
    assert iterations == 1
    if padded:
        assert error.provider_request_id is None
        assert error.provider_code == "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
    else:
        assert error.provider_request_id == "request-456"
        assert error.provider_code == "authorizationError.CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"


def test_mixed_provider_errors_preserve_specific_project_guidance() -> None:
    from test_google_ads_cloud_project_access import _client as project_client
    from test_google_ads_cloud_project_access import _failure

    payload: Any = _failure("CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION")
    payload["error"]["details"][0]["errors"].insert(
        0, {"errorCode": {"queryError": "UNRECOGNIZED_FIELD"}}
    )
    with pytest.raises(AdhelmError) as raised:
        project_client(lambda _: httpx.Response(403, json=payload)).discover_identity(CONFIG)
    assert raised.value.code == "GOOGLE_ADS_CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
    assert raised.value.provider_code == "queryError.UNRECOGNIZED_FIELD"
    assert raised.value.provider_error_code == "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
    assert "Explorer access" in raised.value.next_action
