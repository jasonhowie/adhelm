"""Independent fake-only checks for configured GA4 CREATE execution.

These cases exercise the receipt-to-runtime ordering without a real approval
host, OAuth bundle, provider request, or credential store.  They use the
existing signed ephemeral fixtures only to prove that an incoming receipt
cannot bypass the local configuration, current-source, and one-use boundaries.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_approval_protocol import _config, _private_key, _public_x963
from test_ga4_admin_execution_observation import _ACCESS, _REFRESH, _SUBJECT
from test_ga4_create_execution_workflow import _execute, _material
from test_saved_ga4_key_event_candidates import _definition

from adhelm import ga4_create_execution_workflow as execution
from adhelm.approval_acceptance import PinnedApprovalTrust
from adhelm.approval_protocol import ApprovalChallenge, ApprovalChallengeKind
from adhelm.canonical import canonical_bytes
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.providers import ga4_admin_key_event_create_adapter as adapter_module
from adhelm.safety import SafetyKernel
from adhelm.saved_conversion_contracts import retain_conversion_contract

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def _claim_rows(ledger: object) -> list[dict[str, object]]:
    records = ledger.records("changes")  # type: ignore[union-attr]
    return [row for row in records if row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"]


def _forged_challenge() -> ApprovalChallenge:
    """Build an exact-class object whose wire bytes cannot be a challenge."""

    forged = object.__new__(ApprovalChallenge)
    object.__setattr__(forged, "kind", ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN)
    object.__setattr__(forged, "canonical_bytes", b'{"schema_version":1}')
    return forged


def test_forged_expected_challenge_never_reaches_oauth_or_claim(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = list(_material(tmp_path / "limited-host-configuration", monkeypatch))
    material[2] = _forged_challenge()
    ledger, scenario = material[4], material[-1]

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path / "limited-host-configuration", material)

    assert raised.value.code == "APPROVAL_CHALLENGE_INVALID"
    assert not scenario.calls
    assert not _claim_rows(ledger)


def test_wrong_pinned_key_fails_before_oauth(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]
    material[3] = PinnedApprovalTrust(_config(), _public_x963(_private_key()), ledger.profile_ref)

    with pytest.raises(AdhelmError) as wrong_key:
        _execute(tmp_path, material)

    assert wrong_key.value.code == "INVALID_APPROVAL_RECEIPT_SIGNATURE"
    assert not scenario.calls
    assert not _claim_rows(ledger)


def test_sampled_host_limit_cannot_be_overridden_by_expected_challenge(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]
    material[3] = PinnedApprovalTrust(
        _config(maximum_challenge_bytes=1),
        material[3].public_key_x963,
        ledger.profile_ref,
    )

    with pytest.raises(AdhelmError) as limited:
        _execute(tmp_path, material)

    assert limited.value.code == "APPROVAL_CHALLENGE_INVALID"
    assert not scenario.calls
    assert not _claim_rows(ledger)


def test_configuration_drift_fails_before_private_key_or_oauth(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = list(_material(tmp_path, monkeypatch))
    configuration, scenario = material[6], material[-1]
    save_configuration(
        tmp_path,
        configuration.model_copy(
            update={"output": configuration.output.model_copy(update={"max_rows": 12})}
        ),
        replace=True,
    )
    monkeypatch.setattr(
        execution, "export_key", lambda: pytest.fail("configuration drift accessed key")
    )
    monkeypatch.setattr(
        adapter_module,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: pytest.fail("configuration drift resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "CONFIGURATION_DRIFT"
    assert not scenario.calls


def test_new_relevant_source_after_receipt_claim_blocks_configured_factory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, contract, scenario = material[4], material[8], material[-1]
    original_accept = execution.accept_approval_receipt

    def accept_then_supersede(*args: object, **kwargs: object):
        accepted = original_accept(*args, **kwargs)  # type: ignore[arg-type]
        retain_conversion_contract(
            ledger,
            _definition(
                version=2,
                supersedes=contract.content_digest,
                effective_from=contract.definition.effective_from + timedelta(seconds=1),
            ),
        )
        return accepted

    monkeypatch.setattr(execution, "accept_approval_receipt", accept_then_supersede)
    monkeypatch.setattr(
        adapter_module,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: pytest.fail("superseded source resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "GA4_CREATE_PLAN_HISTORY_CONFLICT"
    assert len(_claim_rows(ledger)) == 1
    assert not scenario.calls


def test_key_rotation_after_claim_blocks_before_oauth_or_http(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]
    original_accept = execution.accept_approval_receipt

    def accept_then_rotate(*args: object, **kwargs: object):
        accepted = original_accept(*args, **kwargs)  # type: ignore[arg-type]
        monkeypatch.setattr(adapter_module, "export_key", lambda: b"x" * 32)
        return accepted

    monkeypatch.setattr(execution, "accept_approval_receipt", accept_then_rotate)
    monkeypatch.setattr(
        adapter_module,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: pytest.fail("rotated key resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "FINGERPRINT_KEY_CHANGED"
    assert len(_claim_rows(ledger)) == 1
    assert not scenario.calls


def test_uncertain_post_claim_recording_consumes_exact_receipt_without_replay(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]
    original_record = SafetyKernel.record_direct_grant

    def fail_after_claim(self: SafetyKernel, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise RuntimeError("private durable-record failure")

    monkeypatch.setattr(SafetyKernel, "record_direct_grant", fail_after_claim)
    with pytest.raises(AdhelmError) as first:
        _execute(tmp_path, material)

    assert first.value.code == "APPROVAL_RECORDING_UNVERIFIED"
    assert "private" not in str(first.value)
    assert len(_claim_rows(ledger)) == 1
    assert not scenario.calls

    monkeypatch.setattr(SafetyKernel, "record_direct_grant", original_record)
    with pytest.raises(AdhelmError) as replay:
        _execute(tmp_path, material)

    assert replay.value.code == "APPROVAL_RECEIPT_REPLAY"
    assert not scenario.calls


def test_result_and_durable_execution_history_never_expose_oauth_or_subject_values(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    material = _material(tmp_path, monkeypatch)
    ledger = material[4]

    result = _execute(tmp_path, material)
    rendered = canonical_bytes(result.model_dump(mode="json", warnings="error"))
    retained = canonical_bytes(ledger.checkpoint())

    for secret in (_ACCESS, _REFRESH, _SUBJECT):
        assert secret.encode() not in rendered
        assert secret.encode() not in retained
