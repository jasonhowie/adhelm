"""Retained report selection and audit stay bound to verified local evidence."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adhelm.audit import audit_from_json
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money, OutputPreferences
from adhelm.reporting import LedgerCheckpoint, NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.saved_reports import load_report, report_catalog
from adhelm.workflows import persist_report

runner = CliRunner()


def retain(ledger: Ledger, query: str, clicks: int = 2) -> str:
    checkpoint = ledger.checkpoint()
    report = NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report:private",
            provider="google_ads",
            source_account_ref="account:private",
            query_contract_hash=query * 64,
            window_start=date(2026, 1, 1),
            window_end=date(2026, 1, 1),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="NOT_AVAILABLE_FROM_PROVIDER",
            observed_at=datetime(2026, 1, 2, tzinfo=UTC),
            provider_api_version="v25",
            connector_version="test",
            source_ledger_checkpoints=[
                LedgerCheckpoint(
                    stream_id=value["stream_id"],
                    high_watermark_sequence=value["high_watermark"],
                    head_digest=value["head_digest"],
                )
                for value in checkpoint["streams"].values()
            ],
        ),
        performance_rows=[
            PerformanceRow(
                report_id="report:private",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="CAMPAIGN",
                resource_ref=f"campaign:private:{index}",
                impressions=10,
                clicks=clicks,
                spend=Money(minor_units=400, currency="USD"),
            )
            for index in range(2)
        ],
    )
    return str(persist_report(ledger, report)["record_digest"])


def test_catalog_revisions_are_ordered_by_actual_append_and_old_digest_stays_readable(
    tmp_path: Path,
) -> None:
    ledger = Ledger(tmp_path)
    old = retain(ledger, "a")
    other = retain(ledger, "b")
    latest = retain(ledger, "a", clicks=3)
    catalog = report_catalog(ledger, limit=1)
    assert catalog["total_count"] == 2
    assert catalog["reports"][0]["record_digest"] == latest
    assert catalog["reports"][0]["revision"] == 2
    assert catalog["next_cursor"] == "1"
    assert report_catalog(ledger, cursor=1)["reports"][0]["record_digest"] == other
    report, provenance = load_report(ledger, old)
    assert report.performance_rows[0].clicks == 2
    assert provenance["revision"] == 1
    assert report_catalog(ledger, provider="meta_ads")["total_count"] == 0
    with pytest.raises(AdhelmError, match="unavailable"):
        load_report(ledger, "0" * 64)


def test_show_and_audit_honor_saved_limits_and_never_accept_unretained_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import adhelm.credentials

    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)
    save_configuration(tmp_path, Configuration(output=OutputPreferences(max_rows=1)))
    digest = retain(Ledger(tmp_path), "a")
    base = ["--data-dir", str(tmp_path)]
    shown = runner.invoke(app, [*base, "report", "show", digest])
    assert shown.exit_code == 0, shown.stdout
    page = json.loads(shown.stdout)
    assert len(page["data"]["performance_rows"]) == 1
    assert page["data"]["row_counts"]["performance_rows"] == 2
    assert page["next_cursor"] == "1"
    assert "private" not in shown.stdout
    assert page["data"]["provenance"]["record_digest"] == digest
    second = runner.invoke(app, [*base, "report", "show", digest, "--cursor", "1"])
    assert json.loads(second.stdout)["next_cursor"] is None

    policy = tmp_path / "audit-policy.json"
    policy.write_text(
        json.dumps(
            {
                "maximum_observation_age_seconds": 172800,
                "maximum_cpc": {"minor_units": 100, "currency": "USD"},
                "require_conversion_evidence": True,
            }
        )
    )
    command = [*base, "audit", digest, "--input", str(policy), "--at", "2026-01-03T00:00:00Z"]
    audited = runner.invoke(app, command)
    assert audited.exit_code == 0, audited.stdout
    result = json.loads(audited.stdout)
    assert result["outcome"] == "INCOMPLETE_COVERAGE"
    assert result["data"]["source_record_digest"] == digest
    assert len(result["data"]["findings"]) == 1
    assert result["data"]["row_counts"]["findings"] > 1
    assert "private" not in audited.stdout
    assert len(result["data"]["coverage"]) == 12
    assert all(
        item["status"] != "CHECKED"
        for item in result["data"]["coverage"]
        if item["dimension"] == "NATIVE_CONVERSION"
    )
    following = runner.invoke(app, [*command, "--cursor", "1"])
    assert json.loads(following.stdout)["data"]["audit_digest"] == result["data"]["audit_digest"]
    missing = runner.invoke(app, [*base, "audit", "0" * 64, "--input", str(policy)])
    assert missing.exit_code != 0
    assert json.loads(missing.stdout)["diagnostics"][0]["code"] == "REPORT_NOT_RETAINED"


def test_audit_json_rejects_duplicates_and_oversized_input() -> None:
    for raw in ('{"as_of":"2026-01-01T00:00:00Z","as_of":"2026-01-02T00:00:00Z"}', " " * 1_048_577):
        with pytest.raises(AdhelmError, match="strict schema"):
            audit_from_json(raw)
