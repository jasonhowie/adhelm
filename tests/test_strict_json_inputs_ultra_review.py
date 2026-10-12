"""Independent adversarial coverage for strict JSON input boundaries."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from test_audit import AS_OF, SOURCE_DIGEST, _envelope, _policy
from test_conversion_contracts import _input as conversion_input
from test_ledger import _report_payload, change
from test_openai_compiler import _brief
from typer.testing import CliRunner

from adhelm import conversion_cli, workflows
from adhelm.audit import AuditPolicy, audit_from_json
from adhelm.candidate_records import CandidateCompilationRecord
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.providers.openai_ads import OpenAIAdsReportQuery
from adhelm.reporting import LedgerCheckpoint, NormalizedReport
from adhelm.saved_candidates import compile_candidate
from adhelm.strict_json import validate_json_input, validate_json_value


class _UntypedJsonValue(BaseModel):
    """A future permissive field must not let an overflowed JSON number through."""

    model_config = ConfigDict(strict=True)

    value: Any


def test_overflowed_json_number_fails_closed_before_a_permissive_model_can_hold_inf() -> None:
    with pytest.raises(AdhelmError) as error:
        validate_json_input(_UntypedJsonValue, b'{"value":{"nested":[1e9999]}}')
    assert error.value.code == "INVALID_JSON"
    assert "1e9999" not in error.value.message


def test_json_input_keeps_the_canonical_safe_integer_domain_before_schema_validation() -> None:
    with pytest.raises(AdhelmError) as error:
        validate_json_input(_UntypedJsonValue, b'{"value":9007199254740992}')
    assert error.value.code == "INVALID_CANONICAL_VALUE"

    with pytest.raises(AdhelmError) as error:
        validate_json_input(
            AuditPolicy,
            b'{"maximum_observation_age_seconds":9007199254740992,'
            b'"require_conversion_evidence":true}',
        )
    assert error.value.code == "INVALID_CANONICAL_VALUE"


@pytest.mark.parametrize("value", ["3600.0", "true", "-0.0", "1e9999"])
def test_audit_input_preserves_numeric_lexemes_for_strict_validation(value: str) -> None:
    report = NormalizedReport(envelope=_envelope(), performance_rows=[])
    raw = json.dumps(
        {
            "report": report.model_dump(mode="json"),
            "source_record_digest": SOURCE_DIGEST,
            "policy": _policy(maximum_observation_age_seconds=3600).model_dump(mode="json"),
            "as_of": AS_OF.isoformat().replace("+00:00", "Z"),
        },
        separators=(",", ":"),
    ).replace(
        '"maximum_observation_age_seconds":3600', f'"maximum_observation_age_seconds":{value}'
    )
    with pytest.raises(AdhelmError) as error:
        audit_from_json(raw)
    assert error.value.code == "INVALID_AUDIT_INPUT"
    assert value not in error.value.message


def _report_payload_with_empty_checkpoint(ledger: Ledger) -> dict[str, object]:
    checkpoint = ledger.checkpoint()
    checkpoints = [
        LedgerCheckpoint(
            stream_id=item["stream_id"],
            high_watermark_sequence=item["high_watermark"],
            head_digest=item["head_digest"],
        )
        for item in checkpoint["streams"].values()
    ]
    return _report_payload(ledger, checkpoints=checkpoints)


def test_ledger_rejects_float_and_bool_before_each_typed_stream_append(tmp_path: Path) -> None:
    changes = Ledger(tmp_path / "changes")
    with pytest.raises(AdhelmError) as error:
        changes.append("changes", "CHANGE_EVENT", change(spend_impact_minor=True, currency="USD"))
    assert error.value.code == "INVALID_LEDGER_PAYLOAD"
    assert changes.records("changes") == []

    performance = Ledger(tmp_path / "performance")
    bad_report = _report_payload_with_empty_checkpoint(performance)
    bad_report["observation_revision"] = 1.0
    with pytest.raises(AdhelmError) as error:
        performance.append(
            "performance", "PERFORMANCE_OBSERVATION", bad_report, provider="google_ads"
        )
    assert error.value.code == "INVALID_LEDGER_PAYLOAD"
    assert performance.records("performance") == []

    creative = Ledger(tmp_path / "creative")
    payload = {
        "schema_version": 1,
        "creative_family_id": "family:one",
        "creative_version_id": "creative:one",
        "provider_format": "DISPLAY",
        "locale": "en-US",
        "destination_ref": "destination:one",
        "surface_kind": "MEDIA",
        "rendition_refs": [],
        "components": [
            {
                "component_id": "headline",
                "component_role": "HEADLINE",
                "ordinal": False,
                "exact_text": "Safe copy",
                "normalized_text": "safe copy",
                "language": "en",
                "provider_slot": "headline_1",
                "provenance": "DECLARED",
                "confidence_basis_points": 10_000,
                "extraction_status": "DECLARED",
            }
        ],
        "serving_manifest_schema_version": 1,
        "serving_manifest_digest": "a" * 64,
        "message": None,
        "analyzer_refs": [],
        "provenance": "CUSTOMER_LOCAL",
        "created_at": "2026-09-05T00:00:00Z",
    }
    with pytest.raises(AdhelmError) as error:
        creative.append("creative", "CREATIVE_VERSION", payload)
    assert error.value.code == "INVALID_LEDGER_PAYLOAD"
    assert creative.records("creative") == []


def test_mapping_revalidation_keeps_json_tuple_and_datetime_semantics() -> None:
    definition = conversion_input()
    validated = validate_json_value(type(definition), definition.model_dump(mode="json"))
    assert validated == definition
    assert isinstance(validated.exclusions, tuple)
    assert validated.effective_from == definition.effective_from

    compilation = compile_candidate(_brief())
    record = CandidateCompilationRecord(
        compilation=compilation, candidate_digest=compilation.candidate_digest
    )
    rebuilt = validate_json_value(CandidateCompilationRecord, record.model_dump(mode="json"))
    assert rebuilt == record
    assert isinstance(rebuilt.compilation.creative_references, tuple)


def test_duplicate_conversion_input_stops_before_compilation_or_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = conversion_input().model_dump_json()
    assert '"version":1' in raw
    source = tmp_path / "conversion.json"
    source.write_text(raw.replace('"version":1', '"version":1,"version":1.0', 1))

    def forbidden(*args: object, **kwargs: object) -> object:
        pytest.fail("Duplicate JSON must not reach compilation or credential access")

    monkeypatch.setattr(conversion_cli, "compile_conversion_contract", forbidden)
    monkeypatch.setattr(conversion_cli, "export_key", forbidden)
    result = CliRunner().invoke(
        app,
        ["--data-dir", str(tmp_path / "data"), "conversion", "register", "--input", str(source)],
    )
    assert result.exit_code == 2, result.output
    output = json.loads(result.output)
    assert output["diagnostics"][0]["code"] == "INVALID_JSON"
    assert "version" not in output["diagnostics"][0]["message"]
    assert not (tmp_path / "data").exists()


def test_overflowed_report_query_stops_before_key_or_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = Profile(
        profile_id="reader",
        provider="openai_ads",
        account_ref="account_fixture",
        principal_ref="fixture-principal",
        client_ref="fixture-client",
        scopes=["fixture.scope"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="fixture-credential",
    )
    root = tmp_path / "data"
    save_configuration(root, Configuration(profiles=[profile]))
    source = tmp_path / "query.json"
    source.write_text(
        json.dumps(
            {
                "account_ref": "account_fixture",
                "report_id": "report.fixture",
                "query_contract_hash": "a" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "source_timezone": "UTC",
                "currency": "USD",
            },
            separators=(",", ":"),
        ).removesuffix("}")
        + ',"maximum_rows":1e9999}'
    )

    def forbidden(*args: object, **kwargs: object) -> object:
        pytest.fail("Invalid JSON must not reach history, key, credential, or transport")

    monkeypatch.setattr(workflows, "export_key", forbidden)
    monkeypatch.setattr(workflows, "credential", forbidden)
    monkeypatch.setattr(workflows.httpx, "Client", forbidden)
    with pytest.raises(AdhelmError) as error:
        workflows.run_report(root, "openai_ads", "reader", source)
    assert error.value.code == "INVALID_JSON"
    assert sorted(path.name for path in root.iterdir()) == ["config.json"]
    valid = OpenAIAdsReportQuery.model_validate_json(source.read_bytes().replace(b"1e9999", b"1"))
    assert valid.maximum_rows == 1
