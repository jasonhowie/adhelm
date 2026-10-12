"""Synthetic GA4 CREATE execution through the private installed approval bridge."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta

import pytest
from test_ga4_create_execution_workflow import _material
from test_installed_native_approval_bridge import (
    _candidate_for_pending,
    _FakeAdapter,
    _install_binding,
)
from test_saved_ga4_key_event_candidates import _definition

import adhelm._installed_native_approval_bridge as bridge
from adhelm import ga4_create_execution_workflow as execution
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.errors import AdhelmError
from adhelm.providers import ga4_admin_key_event_create_adapter as adapter_module
from adhelm.safety import EffectStatus
from adhelm.saved_conversion_contracts import retain_conversion_contract

pytestmark = pytest.mark.usefixtures("simulate_released_writes")


def _claim_rows(ledger: object) -> list[dict[str, object]]:
    records = ledger.records("changes")  # type: ignore[union-attr]
    return [row for row in records if row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"]


def _install_synthetic_bridge(
    monkeypatch: pytest.MonkeyPatch,
    *,
    current: list[object],
    after_acceptance: Callable[[AcceptedNativeApproval], AcceptedNativeApproval | None]
    | None = None,
) -> list[_FakeAdapter]:
    """Install one fake native exchange without exposing it to the workflow API."""
    configuration = _install_binding(monkeypatch)
    adapters: list[_FakeAdapter] = []

    def approve(
        kernel: object,
        ledger: object,
        *,
        source_plan_record_digest: str,
        grantor_ref: str,
    ) -> object:
        assert grantor_ref == execution._INSTALLED_GA4_CREATE_LOGICAL_GRANTOR_REF
        assert type(kernel).__name__ == "SafetyKernel"
        pending = bridge._prepare_installed_native_approval_request(
            kernel,
            ledger,
            source_plan_record_digest=source_plan_record_digest,
            grantor_ref=grantor_ref,
        )
        now = current[0]
        assert type(now) is not object
        adapter = _FakeAdapter(
            _candidate_for_pending(
                pending,
                configuration=configuration,
                receipt_issued_at=now,
                native_observed_at=now,
            )
        )
        adapters.append(adapter)
        monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: adapter)
        accepted = bridge._exchange_and_record_pending_installed_native_approval(
            kernel, ledger, pending=pending
        )
        if after_acceptance is not None:
            replacement = after_acceptance(accepted)
            if replacement is not None:
                return replacement
        return accepted

    monkeypatch.setattr(execution, "_request_and_record_installed_native_approval", approve)
    return adapters


def _execute(tmp_path, material):
    return execution.execute_retained_ga4_key_event_create_with_installed_approval(
        tmp_path, material[0]
    )


def test_installed_approval_executes_exact_retained_create_once(tmp_path, monkeypatch) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]
    bridge_adapters = _install_synthetic_bridge(monkeypatch, current=material[7])

    result = _execute(tmp_path, material)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert scenario.posts == 1
    assert len(bridge_adapters) == 1 and bridge_adapters[0].calls
    claims = _claim_rows(ledger)
    assert len(claims) == 1
    challenge = claims[0]["payload"]["challenge"]
    assert type(challenge) is dict
    assert challenge["grantor_ref"] == execution._INSTALLED_GA4_CREATE_LOGICAL_GRANTOR_REF
    rows = ledger.checkpoint()["records_by_stream"]["changes"]
    claim_index = next(
        index for index, row in enumerate(rows) if row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    )
    start_index = next(
        index
        for index, row in enumerate(rows)
        if row["payload"].get("event_type") == "APPLY_STARTED"
    )
    assert claim_index < start_index


def test_default_compiled_binding_blocks_before_helper_oauth_or_http(tmp_path, monkeypatch) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]
    monkeypatch.setattr(
        bridge, "_resolve_pipe_adapter", lambda: pytest.fail("unexpected helper exchange")
    )
    monkeypatch.setattr(
        adapter_module,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: pytest.fail("unexpected OAuth resolution"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert not scenario.calls
    assert not _claim_rows(ledger)


def test_source_drift_after_native_claim_blocks_oauth_and_egress(tmp_path, monkeypatch) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, contract, scenario = material[4], material[8], material[-1]

    def supersede(_accepted: AcceptedNativeApproval) -> None:
        retain_conversion_contract(
            ledger,
            _definition(
                version=2,
                supersedes=contract.content_digest,
                effective_from=contract.definition.effective_from + timedelta(seconds=1),
            ),
        )

    bridge_adapters = _install_synthetic_bridge(
        monkeypatch, current=material[7], after_acceptance=supersede
    )
    monkeypatch.setattr(
        adapter_module,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: pytest.fail("superseded source resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "GA4_CREATE_PLAN_HISTORY_CONFLICT"
    assert len(bridge_adapters) == 1 and bridge_adapters[0].calls
    assert len(_claim_rows(ledger)) == 1
    assert not scenario.calls


def test_key_change_after_native_claim_blocks_oauth_and_egress(tmp_path, monkeypatch) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]

    def rotate_key(_accepted: AcceptedNativeApproval) -> None:
        monkeypatch.setattr(execution, "export_key", lambda: b"x" * 32)

    bridge_adapters = _install_synthetic_bridge(
        monkeypatch, current=material[7], after_acceptance=rotate_key
    )
    monkeypatch.setattr(
        adapter_module,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: pytest.fail("rotated key resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "FINGERPRINT_KEY_CHANGED"
    assert len(bridge_adapters) == 1 and bridge_adapters[0].calls
    assert len(_claim_rows(ledger)) == 1
    assert not scenario.calls


def test_configuration_drift_after_native_claim_blocks_oauth_and_egress(
    tmp_path, monkeypatch
) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, configuration, scenario = material[4], material[6], material[-1]

    def drift_configuration(_accepted: AcceptedNativeApproval) -> None:
        from adhelm.config import save_configuration

        save_configuration(
            tmp_path,
            configuration.model_copy(
                update={"output": configuration.output.model_copy(update={"max_rows": 12})}
            ),
            replace=True,
        )

    bridge_adapters = _install_synthetic_bridge(
        monkeypatch, current=material[7], after_acceptance=drift_configuration
    )
    monkeypatch.setattr(
        adapter_module,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: pytest.fail("configuration drift resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "CONFIGURATION_DRIFT"
    assert len(bridge_adapters) == 1 and bridge_adapters[0].calls
    assert len(_claim_rows(ledger)) == 1
    assert not scenario.calls


def test_mismatched_bridge_grant_cannot_reach_oauth_or_adapter(tmp_path, monkeypatch) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]

    def wrong_plan(accepted: AcceptedNativeApproval) -> AcceptedNativeApproval:
        assert accepted.direct_grant is not None
        return replace(
            accepted,
            direct_grant=accepted.direct_grant.model_copy(update={"plan_digest": "0" * 64}),
        )

    bridge_adapters = _install_synthetic_bridge(
        monkeypatch, current=material[7], after_acceptance=wrong_plan
    )
    monkeypatch.setattr(
        adapter_module,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: pytest.fail("mismatched grant resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "GA4_CREATE_APPROVAL_REQUIRED"
    assert len(bridge_adapters) == 1 and bridge_adapters[0].calls
    assert len(_claim_rows(ledger)) == 1
    assert not scenario.calls


def test_copied_bridge_grant_identity_cannot_reach_oauth_or_adapter(tmp_path, monkeypatch) -> None:
    material = list(_material(tmp_path, monkeypatch))
    ledger, scenario = material[4], material[-1]

    def wrong_identity(accepted: AcceptedNativeApproval) -> AcceptedNativeApproval:
        assert accepted.direct_grant is not None
        return replace(
            accepted,
            direct_grant=accepted.direct_grant.model_copy(
                update={"grantor_ref": "logical:forged-bridge-grantor-v1"}
            ),
        )

    bridge_adapters = _install_synthetic_bridge(
        monkeypatch, current=material[7], after_acceptance=wrong_identity
    )
    monkeypatch.setattr(
        adapter_module,
        "resolve_ga4_admin_edit_execution_binding",
        lambda _profile: pytest.fail("copied grant resolved OAuth"),
    )

    with pytest.raises(AdhelmError) as raised:
        _execute(tmp_path, material)

    assert raised.value.code == "USER_APPROVAL_HOST_REQUIRED"
    assert len(bridge_adapters) == 1 and bridge_adapters[0].calls
    assert len(_claim_rows(ledger)) == 1
    assert not scenario.calls


def test_installed_sibling_has_no_caller_approval_or_transport_inputs(
    tmp_path, monkeypatch
) -> None:
    signature = inspect.signature(
        execution.execute_retained_ga4_key_event_create_with_installed_approval
    )
    assert tuple(signature.parameters) == ("directory", "request")
    material = list(_material(tmp_path, monkeypatch))
    _install_synthetic_bridge(monkeypatch, current=material[7])
    monkeypatch.setattr(
        execution,
        "accept_approval_receipt",
        lambda *_args, **_kwargs: pytest.fail("installed route invoked legacy receipt acceptance"),
    )

    result = _execute(tmp_path, material)

    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
