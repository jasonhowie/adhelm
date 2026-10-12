"""Fake-only tests for the private retained-plan approval bridge coordinator."""

from __future__ import annotations

import base64
import copy
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from test_ga4_create_workflow import _setup as _ga4_setup

import adhelm._installed_native_approval_bridge as bridge
import adhelm.ledger as ledger_module
from adhelm.approval_protocol import (
    ApprovalChallenge,
    ApprovalChallengeKind,
    ApprovalHostConfiguration,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError
from adhelm.ga4_create_workflow import prepare_and_retain_ga4_key_event_create_plan
from adhelm.ledger import Ledger
from adhelm.plan_records import revalidate_openai_pause_action
from adhelm.providers.openai_pause_contract import OpenAIPausePlannedAction
from adhelm.safety import SafetyKernel
from adhelm.saved_plans import retain_openai_pause_plan

_ROOT = Path(__file__).resolve().parents[1]
_ACTION_PATH = (
    _ROOT
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/openai-pause-action-v1.json"
)
_T0 = datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(seconds=1)
_T2 = _T0 + timedelta(seconds=2)


class _Clock:
    def __init__(self, values: list[datetime]) -> None:
        self._values = list(values)
        self._last = values[-1]

    def __call__(self) -> datetime:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


class _FakeAdapter:
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.calls: list[bytes] = []

    def exchange(self, request_frame: bytes, *, cancellation: object) -> bytes:
        assert type(request_frame) is bytes
        assert request_frame[:4] == (len(request_frame) - 4).to_bytes(4, "big")
        assert callable(getattr(cancellation, "cancelled", None))
        self.calls.append(request_frame)
        return self.response


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _action() -> OpenAIPausePlannedAction:
    return revalidate_openai_pause_action(
        OpenAIPausePlannedAction.model_validate_json(_ACTION_PATH.read_bytes())
    )


def _configuration() -> ApprovalHostConfiguration:
    return ApprovalHostConfiguration(
        host_ref="host:adhelm-macos-approval-v1",
        signing_key_ref="key:approval-test-v1",
    )


def _binding_metadata_fields(
    configuration: ApprovalHostConfiguration,
) -> tuple[tuple[str, object], ...]:
    data: dict[str, object] = {
        "schema_version": 1,
        "host_ref": configuration.host_ref,
        "signing_key_ref": configuration.signing_key_ref,
        "mach_service_name": "com.aptsonic.adhelm.approvalhost",
        "client_bundle_identifier": "com.aptsonic.adhelm.client",
        "client_team_identifier": "ABCDE12345",
        "host_bundle_identifier": "com.aptsonic.adhelm.host",
        "host_team_identifier": "ABCDE12345",
        "signing_key_application_tag_hex": "00616263ff",
        "signing_key_access_group": "ABCDE12345.com.aptsonic.adhelm.host",
        "key_storage": "KEYCHAIN_P256",
        "provenance_service": "com.aptsonic.adhelm.provenance",
        "provenance_account": "approval-host-v1",
        "maximum_challenge_bytes": configuration.maximum_challenge_bytes,
        "maximum_challenge_lifetime_seconds": configuration.maximum_challenge_lifetime_seconds,
        "maximum_issued_at_skew_seconds": configuration.maximum_issued_at_skew_seconds,
        "maximum_subplans": configuration.maximum_subplans,
        "maximum_policy_items": configuration.maximum_policy_items,
    }
    return tuple(data.items())


def _metadata_digest(configuration: ApprovalHostConfiguration | None = None) -> str:
    checked = _configuration() if configuration is None else configuration
    return hashlib.sha256(canonical_bytes(dict(_binding_metadata_fields(checked)))).hexdigest()


def _install_binding(monkeypatch: pytest.MonkeyPatch) -> ApprovalHostConfiguration:
    configuration = _configuration()
    binding = bridge._issue_compiled_installed_approval_bridge_binding_from_generated_metadata(
        metadata_fields=_binding_metadata_fields(configuration),
        expected_metadata_digest=_metadata_digest(configuration),
        expected_protocol_configuration_digest=configuration.configuration_digest,
    )
    monkeypatch.setattr(bridge, "_resolve_compiled_installed_binding", lambda: binding)
    return configuration


def _private_data(pending: object) -> object:
    return object.__getattribute__(pending, "_PendingInstalledApprovalBridgeRequest__data")


def _urlsafe_decode(value: object) -> bytes:
    assert type(value) is str
    return base64.urlsafe_b64decode(value + "=" * ((4 - len(value) % 4) % 4))


def _urlsafe_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _signed_receipt(
    challenge: ApprovalChallenge,
    *,
    configuration: ApprovalHostConfiguration,
    key: ec.EllipticCurvePrivateKey,
    issued_at: datetime = _T1,
) -> bytes:
    payload = receipt_signing_payload(
        challenge,
        configuration=configuration,
        receipt_id="approval-receipt:" + "B" * 43,
        issued_at=issued_at,
    )
    unsigned = parse_json(payload.canonical_unsigned_receipt)
    assert type(unsigned) is dict
    signature = key.sign(payload.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    unsigned["signature"] = _urlsafe_encode(signature)
    return canonical_bytes(unsigned)


def _candidate_for_pending(
    pending: object,
    *,
    configuration: ApprovalHostConfiguration,
    receipt_issued_at: datetime = _T1,
    native_observed_at: datetime = _T2,
) -> bytes:
    data = _private_data(pending)
    challenge = object.__getattribute__(data, "challenge")
    request = object.__getattribute__(data, "request")
    binding = object.__getattribute__(data, "binding")
    assert type(challenge) is ApprovalChallenge
    assert type(request) is bridge.NativeApprovalBridgeRequestV1
    request_data = parse_json(request.canonical_bytes)
    assert type(request_data) is dict
    key = ec.generate_private_key(ec.SECP256R1())
    public_key = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    receipt = _signed_receipt(
        challenge,
        configuration=configuration,
        key=key,
        issued_at=receipt_issued_at,
    )
    receipt_data = parse_json(receipt)
    assert type(receipt_data) is dict
    request_id = _urlsafe_decode(request_data["request_id"])
    metadata_digest = object.__getattribute__(
        binding, "_CompiledInstalledApprovalBridgeBinding__metadata_digest"
    )
    assert type(metadata_digest) is str
    return canonical_bytes(
        {
            "schema_version": 1,
            "message_type": "NATIVE_APPROVAL_BRIDGE_CANDIDATE_V1",
            "native_observation_status": "NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED",
            "request_id_digest": hashlib.sha256(request_id).hexdigest(),
            "request_digest": request.digest,
            "source_context_digest": request.source_context_digest,
            "metadata_digest": metadata_digest,
            "protocol_configuration_digest": request.protocol_configuration_digest,
            "canonical_challenge_b64url": request_data["canonical_challenge_b64url"],
            "challenge_digest": challenge.digest,
            "challenge_nonce_digest": hashlib.sha256(challenge.nonce).hexdigest(),
            "receipt_kind": challenge.kind.value,
            "expires_at": challenge.expires_at_text,
            "canonical_receipt_b64url": _urlsafe_encode(receipt),
            "receipt_digest": hashlib.sha256(receipt).hexdigest(),
            "receipt_id": receipt_data["receipt_id"],
            "active_public_key_x963_b64url": _urlsafe_encode(public_key),
            "active_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
            "native_observed_at": _timestamp(native_observed_at),
        }
    )


def _prepared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Ledger, SafetyKernel, object, ApprovalHostConfiguration, str]:
    action = _action()
    ledger = Ledger(tmp_path, profile_ref=action.plan.profile_ref)
    source = retain_openai_pause_plan(ledger, action)
    clock = _Clock([_T0, _T2, _T2, _T2, _T2, _T2, _T2, _T2, _T2, _T2, _T2, _T2])
    kernel = SafetyKernel(tmp_path, ledger, clock=clock)
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(_T2))
    configuration = _install_binding(monkeypatch)
    pending = bridge._prepare_installed_native_approval_request(
        kernel,
        ledger,
        source_plan_record_digest=source["record_digest"],
        grantor_ref="customer-grantor-1",
    )
    return ledger, kernel, pending, configuration, source["record_digest"]


def test_unconfigured_binding_fails_before_source_or_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _action()
    ledger = Ledger(tmp_path, profile_ref=action.plan.profile_ref)
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: _T0)
    monkeypatch.setattr(ledger, "checkpoint", lambda: pytest.fail("unexpected source read"))
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: pytest.fail("unexpected launch"))

    with pytest.raises(AdhelmError) as raised:
        bridge._request_and_record_installed_native_approval(
            kernel,
            ledger,
            source_plan_record_digest="a" * 64,
            grantor_ref="customer-grantor-1",
        )

    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"


def test_openai_retained_plan_is_reloaded_then_claimed_without_apply_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, pending, configuration, source_digest = _prepared(tmp_path, monkeypatch)
    adapter = _FakeAdapter(_candidate_for_pending(pending, configuration=configuration))
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: adapter)

    accepted = bridge._exchange_and_record_pending_installed_native_approval(
        kernel, ledger, pending=pending
    )

    assert accepted.direct_grant is not None
    assert accepted.receipt_claim_record_digest
    assert adapter.calls
    records = ledger.records("changes")
    assert any(
        row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
        and row["payload"]["source_plan_record_digest"] == source_digest
        for row in records
    )
    assert any(
        row["record_kind"] == "CHANGE_EVENT" and row["payload"]["event_type"] == "APPROVAL_GRANTED"
        for row in records
    )
    assert not any(
        row["record_kind"] == "CHANGE_EVENT" and row["payload"]["event_type"] == "APPLY_STARTED"
        for row in records
    )


def test_final_claim_callback_revalidates_only_held_bridge_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, pending, configuration, _ = _prepared(tmp_path, monkeypatch)
    adapter = _FakeAdapter(_candidate_for_pending(pending, configuration=configuration))
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: adapter)
    append = ledger.append

    def no_ledger_read() -> None:
        pytest.fail("the final receipt callback must not re-read the ledger")

    def checked_append(*args: Any, **kwargs: Any) -> dict[str, Any]:
        callback = kwargs.get("_before_durable_append")
        if callback is not None:
            assert callable(callback)

            def checked_callback() -> None:
                with monkeypatch.context() as scoped:
                    scoped.setattr(ledger, "checkpoint", no_ledger_read)
                    callback()

            kwargs["_before_durable_append"] = checked_callback
        return append(*args, **kwargs)

    monkeypatch.setattr(ledger, "append", checked_append)
    accepted = bridge._exchange_and_record_pending_installed_native_approval(
        kernel, ledger, pending=pending
    )

    assert accepted.direct_grant is not None
    assert adapter.calls


def test_unrelated_append_only_suffix_is_allowed_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, pending, configuration, _ = _prepared(tmp_path, monkeypatch)
    retain_openai_pause_plan(ledger, _action())
    adapter = _FakeAdapter(_candidate_for_pending(pending, configuration=configuration))
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: adapter)

    accepted = bridge._exchange_and_record_pending_installed_native_approval(
        kernel, ledger, pending=pending
    )

    assert accepted.direct_grant is not None
    assert adapter.calls


def test_changed_prefix_after_candidate_is_ambiguous_and_never_records_a_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, pending, configuration, _ = _prepared(tmp_path, monkeypatch)
    adapter = _FakeAdapter(_candidate_for_pending(pending, configuration=configuration))
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: adapter)
    valid_checkpoint = ledger.checkpoint()
    forged_checkpoint = copy.deepcopy(valid_checkpoint)
    changes = forged_checkpoint["records_by_stream"]["changes"]
    assert type(changes) is list and changes
    changes[-1]["record_digest"] = "0" * 64
    monkeypatch.setattr(ledger, "checkpoint", lambda: forged_checkpoint)

    with pytest.raises(AdhelmError) as raised:
        bridge._exchange_and_record_pending_installed_native_approval(
            kernel, ledger, pending=pending
        )

    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_ACCEPTANCE_UNVERIFIED"
    assert not any(
        row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED" for row in ledger.records("changes")
    )


def test_pending_request_is_single_use_after_prelaunch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, kernel, pending, _configuration_value, _ = _prepared(tmp_path, monkeypatch)
    monkeypatch.setattr(
        bridge,
        "_resolve_pipe_adapter",
        lambda: pytest.fail("transport resolver should be replaced by a fixed failure"),
    )

    with pytest.raises(AdhelmError) as first:
        bridge._exchange_and_record_pending_installed_native_approval(
            kernel, ledger, pending=pending
        )
    assert first.value.code == "NATIVE_APPROVAL_BRIDGE_TRANSPORT_UNAVAILABLE"
    with pytest.raises(AdhelmError) as raised:
        bridge._exchange_and_record_pending_installed_native_approval(
            kernel, ledger, pending=pending
        )

    assert raised.value.code == "NATIVE_APPROVAL_BRIDGE_PENDING_CONSUMED"


def test_ga4_retained_plan_prepares_only_the_fixed_direct_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, _profile, _config, current, _calls, _contract = _ga4_setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    configuration = _install_binding(monkeypatch)
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: current[0])
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: pytest.fail("unexpected exchange"))

    pending = bridge._prepare_installed_native_approval_request(
        kernel,
        ledger,
        source_plan_record_digest=retained.plan_record_digest,
        grantor_ref="customer-grantor-1",
    )
    data = _private_data(pending)
    source = object.__getattribute__(data, "source")
    challenge = object.__getattribute__(data, "challenge")
    request_value = object.__getattribute__(data, "request")

    assert source.approval_kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
    assert challenge.kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
    assert request_value.kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
    assert request_value.metadata_digest == _metadata_digest()
    assert request_value.protocol_configuration_digest == configuration.configuration_digest


def test_ga4_retained_plan_bridge_candidate_claims_without_apply_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _store, _profile, _config, current, _calls, _contract = _ga4_setup(
        tmp_path, monkeypatch
    )
    retained = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    configuration = _install_binding(monkeypatch)
    issued_at = current[0]
    observed_at = issued_at + timedelta(seconds=2)
    clock = _Clock([issued_at, observed_at, observed_at, observed_at, observed_at, observed_at])
    kernel = SafetyKernel(tmp_path, ledger, clock=clock)
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(observed_at))
    pending = bridge._prepare_installed_native_approval_request(
        kernel,
        ledger,
        source_plan_record_digest=retained.plan_record_digest,
        grantor_ref="customer-grantor-1",
    )
    adapter = _FakeAdapter(
        _candidate_for_pending(
            pending,
            configuration=configuration,
            receipt_issued_at=issued_at + timedelta(seconds=1),
            native_observed_at=observed_at,
        )
    )
    monkeypatch.setattr(bridge, "_resolve_pipe_adapter", lambda: adapter)

    accepted = bridge._exchange_and_record_pending_installed_native_approval(
        kernel, ledger, pending=pending
    )

    assert accepted.kind is ApprovalChallengeKind.GA4_KEY_EVENT_CREATE_PLAN
    assert accepted.direct_grant is not None
    assert adapter.calls
    assert not any(
        row["record_kind"] == "CHANGE_EVENT" and row["payload"]["event_type"] == "APPLY_STARTED"
        for row in ledger.records("changes")
    )


def test_constructed_private_binding_or_pending_fails_closed_before_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = _action()
    ledger = Ledger(tmp_path, profile_ref=action.plan.profile_ref)
    kernel = SafetyKernel(tmp_path, ledger, clock=lambda: _T0)
    forged_binding = object.__new__(bridge._CompiledInstalledApprovalBridgeBinding)
    monkeypatch.setattr(bridge, "_resolve_compiled_installed_binding", lambda: forged_binding)
    monkeypatch.setattr(ledger, "checkpoint", lambda: pytest.fail("unexpected source read"))

    with pytest.raises(AdhelmError) as binding_error:
        bridge._prepare_installed_native_approval_request(
            kernel,
            ledger,
            source_plan_record_digest="a" * 64,
            grantor_ref="customer-grantor-1",
        )
    assert binding_error.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"

    forged_pending = object.__new__(bridge._PendingInstalledApprovalBridgeRequest)
    with pytest.raises(AdhelmError) as pending_error:
        bridge._exchange_and_record_pending_installed_native_approval(
            kernel, ledger, pending=forged_pending
        )
    assert pending_error.value.code == "NATIVE_APPROVAL_BRIDGE_PENDING_INVALID"
