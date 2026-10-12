"""Receipt-to-kernel configured CREATE tests with ephemeral keys and fake HTTP."""

from datetime import timedelta

import pytest
from test_approval_protocol import _config, _private_key, _public_x963, _signed_receipt
from test_ga4_admin_execution_observation import _KEY, _NOW
from test_ga4_admin_key_event_create_adapter import _CreateScenario, _install_adapter_http
from test_ga4_create_workflow import _setup
from test_saved_ga4_key_event_candidates import _definition

from adhelm import ga4_create_execution_workflow as execution
from adhelm import ledger as ledger_module
from adhelm.approval_acceptance import PinnedApprovalTrust
from adhelm.approval_protocol import build_ga4_key_event_create_challenge
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan
from adhelm.providers import ga4_admin_key_event_create_adapter as adapter_module
from adhelm.safety import EffectStatus
from adhelm.saved_conversion_contracts import retain_conversion_contract

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def _material(tmp_path, monkeypatch):
    request, ledger, store, profile, config, current, _, contract = _setup(tmp_path, monkeypatch)
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    key = _private_key()
    challenge = build_ga4_key_event_create_challenge(
        retained.action,
        grantor_ref="test-customer",
        expires_at=_NOW + timedelta(seconds=120),
        configuration=_config(),
        nonce=bytes(range(32)),
        issued_at=_NOW,
        now=_NOW,
    )
    receipt = _signed_receipt(challenge, key=key, issued_at=_NOW + timedelta(seconds=1))
    trust = PinnedApprovalTrust(_config(), _public_x963(key), ledger.profile_ref)
    current[0] = _NOW + timedelta(seconds=2)
    monkeypatch.setattr(execution, "_now", lambda: current[0])
    monkeypatch.setattr(execution, "export_key", lambda: _KEY)
    monkeypatch.setattr(adapter_module, "export_key", lambda: _KEY)
    monkeypatch.setattr(
        ledger_module, "_utc_now", lambda: current[0].isoformat().replace("+00:00", "Z")
    )
    scenario = _CreateScenario()
    _install_adapter_http(monkeypatch, scenario)
    parsed = execution.Ga4KeyEventCreateExecutionRequest(
        plan_record_digest=retained.plan_record_digest, fresh=request.fresh
    )
    return parsed, receipt, challenge, trust, ledger, store, config, current, contract, scenario


def _execute(tmp_path, material):
    request, receipt, challenge, trust, *_ = material
    return execution.execute_retained_ga4_key_event_create(
        tmp_path, request, receipt=receipt, expected_challenge=challenge, trust=trust
    )


def test_exact_receipt_to_configured_create_and_durable_verification(tmp_path, monkeypatch):
    material = _material(tmp_path, monkeypatch)
    ledger, scenario = material[4], material[-1]
    result = _execute(tmp_path, material)
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert scenario.posts == 1
    rows = ledger.checkpoint()["records_by_stream"]["changes"]
    kinds = [row["record_kind"] for row in rows]
    claim_index = kinds.index("APPROVAL_RECEIPT_CLAIMED")
    start_index = next(
        i for i, row in enumerate(rows) if row["payload"].get("event_type") == "APPLY_STARTED"
    )
    assert claim_index < start_index
    assert not ledger.unresolved_starts()
    assert not ledger.unresolved_runs()


def test_invalid_signature_fails_before_oauth_and_http(tmp_path, monkeypatch):
    material = list(_material(tmp_path, monkeypatch))
    material[1] = material[1].replace(b'"signature":"', b'"signature":"A', 1)
    before = material[5].reads
    with pytest.raises(AdhelmError):
        _execute(tmp_path, material)
    assert material[5].reads == before
    assert not material[-1].calls
    assert not any(
        row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for row in material[4].records("changes")
    )


def test_current_fingerprint_key_required_before_receipt_claim(tmp_path, monkeypatch):
    material = _material(tmp_path, monkeypatch)
    monkeypatch.setattr(execution, "export_key", lambda: b"different-test-fingerprint-key-123456")
    before = material[5].reads
    with pytest.raises(AdhelmError):
        _execute(tmp_path, material)
    assert material[5].reads == before
    assert not material[-1].calls
    assert not any(
        row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for row in material[4].records("changes")
    )


def test_changed_configuration_fails_before_private_key(tmp_path, monkeypatch):
    material = _material(tmp_path, monkeypatch)
    config = material[6]
    save_configuration(
        tmp_path,
        config.model_copy(update={"output": config.output.model_copy(update={"max_rows": 12})}),
        replace=True,
    )
    monkeypatch.setattr(execution, "export_key", lambda: pytest.fail("unexpected key access"))
    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)
    assert raised.value.code == "CONFIGURATION_DRIFT"
    assert not material[-1].calls


def test_new_conversion_definition_blocks_before_receipt_claim(tmp_path, monkeypatch):
    material = _material(tmp_path, monkeypatch)
    contract = material[8]
    new = _definition(
        version=2,
        supersedes=contract.content_digest,
        effective_from=contract.definition.effective_from + timedelta(seconds=1),
    )
    retain_conversion_contract(material[4], new)
    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)
    assert raised.value.code == "GA4_CREATE_PLAN_HISTORY_CONFLICT"
    assert not material[-1].calls
    assert not any(
        row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for row in material[4].records("changes")
    )


def test_ambiguous_create_never_replays_receipt_or_post(tmp_path, monkeypatch):
    material = _material(tmp_path, monkeypatch)
    material[-1].mode = "lost"
    result = _execute(tmp_path, material)
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.recovery_required
    assert material[-1].posts == 1
    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)
    assert raised.value.code == "RECOVERY_REQUIRED"
    assert material[-1].posts == 1
