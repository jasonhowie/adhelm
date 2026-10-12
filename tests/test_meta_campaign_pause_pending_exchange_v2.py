"""Fake-only one-use pending-to-M47 Meta campaign PAUSE V2 composition tests."""

from __future__ import annotations

import base64
import copy
import hashlib
import selectors
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

import adhelm._installed_meta_campaign_pause_approval_bridge_v2 as bridge
import adhelm._meta_campaign_pause_approval_bridge_transport_v2 as transport
import adhelm._native_approval_bridge_transport as pipes
import adhelm.ledger as ledger_module
from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    decode_meta_campaign_pause_approval_bridge_request_v2,
)
from adhelm.approval_acceptance import AcceptedNativeApproval
from adhelm.approval_protocol import (
    ApprovalHostConfiguration,
    parse_approval_challenge_wire,
    receipt_signing_payload,
)
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError, ExitCode
from adhelm.ledger import Ledger
from adhelm.providers.meta_pause_contract import MetaCampaignPausePlannedAction
from adhelm.safety import SafetyKernel
from adhelm.saved_meta_pause_plans import retain_meta_campaign_pause_plan

_ROOT = Path(__file__).resolve().parents[1]
_META_FIXTURE = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/MetaPause/"
    "meta-campaign-pause-approval-v1.json"
)
_V2_FIXTURE = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/"
    "NativeApprovalBridgeV2MetaPause/meta-campaign-pause-approval-bridge-v2.json"
)
_FIFO_MODE = 0o010600
_T0 = datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC)
_T1 = _T0 + timedelta(seconds=1)
_T2 = _T0 + timedelta(seconds=2)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self._scheduled: list[datetime] = []

    def schedule(self, *values: datetime) -> None:
        self._scheduled = list(values)

    def __call__(self) -> datetime:
        if self._scheduled:
            return self._scheduled.pop(0)
        return self.now


class _Cancellation:
    def __init__(self, value: bool = False) -> None:
        self.value = value

    def cancelled(self) -> bool:
        return self.value


class _FakeChild:
    def __init__(self, *, after_reap: Callable[[], None] | None = None) -> None:
        self._after_reap = after_reap
        self._did_after_reap = False
        self.closed: list[str] = []
        self.terminate_calls = 0
        self.kill_calls = 0

    def fileno(self, stream: str) -> int:
        return {"stdin": 10, "stdout": 11, "stderr": 12}[stream]

    def close_stream(self, stream: str) -> None:
        self.closed.append(stream)

    def poll(self) -> int | None:
        return None

    def reap(self, timeout_seconds: float) -> int | None:
        del timeout_seconds
        if not self._did_after_reap and self._after_reap is not None:
            self._did_after_reap = True
            self._after_reap()
        return 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _FakeSelector:
    def __init__(
        self,
        operations: _FakePosixOperations,
        *,
        cancellation_after_first_event: _Cancellation | None,
    ) -> None:
        self._operations = operations
        self._cancellation_after_first_event = cancellation_after_first_event
        self._select_calls = 0
        self.closed = False

    def register(self, file_descriptor: int, events: int) -> None:
        del file_descriptor, events

    def unregister(self, file_descriptor: int) -> None:
        del file_descriptor

    def select(self, timeout_seconds: float) -> list[tuple[int, int]]:
        assert 0 < timeout_seconds <= pipes._POLL_QUANTUM_SECONDS
        self._select_calls += 1
        if self._operations.events:
            events = self._operations.events.pop(0)
            if self._select_calls == 1 and self._cancellation_after_first_event is not None:
                self._cancellation_after_first_event.value = True
            return events
        self._operations.monotonic_value += pipes._EXCHANGE_DEADLINE_SECONDS + 1.0
        return []

    def close(self) -> None:
        self.closed = True


class _FakePosixOperations:
    def __init__(
        self,
        material: _Material,
        *,
        response: str = "valid",
        launch_error: Exception | None = None,
        cancellation_after_first_event: _Cancellation | None = None,
        after_reap: Callable[[], None] | None = None,
        launch_started: threading.Event | None = None,
        launch_release: threading.Event | None = None,
    ) -> None:
        self._material = material
        self._response = response
        self._launch_error = launch_error
        self._cancellation_after_first_event = cancellation_after_first_event
        self._launch_started = launch_started
        self._launch_release = launch_release
        self.child = _FakeChild(after_reap=after_reap)
        self.events: list[list[tuple[int, int]]] = [[(10, selectors.EVENT_WRITE)]]
        self.stdout = bytearray()
        self.stderr = bytearray()
        self.launch_specs: list[object] = []
        self.writes: list[tuple[int, bytes]] = []
        self.monotonic_value = 0.0
        self.selector: _FakeSelector | None = None

    def launch(self, spec: object) -> _FakeChild:
        self.launch_specs.append(spec)
        if self._launch_error is not None:
            raise self._launch_error
        if self._launch_started is not None:
            self._launch_started.set()
        if self._launch_release is not None:
            assert self._launch_release.wait(timeout=5.0)
        return self.child

    def fstat_mode(self, file_descriptor: int) -> int:
        del file_descriptor
        return _FIFO_MODE

    def set_nonblocking(self, file_descriptor: int) -> None:
        del file_descriptor

    def open_selector(self) -> _FakeSelector:
        self.selector = _FakeSelector(
            self,
            cancellation_after_first_event=self._cancellation_after_first_event,
        )
        return self.selector

    def read(self, file_descriptor: int, maximum_bytes: int) -> bytes:
        buffer = self.stdout if file_descriptor == 11 else self.stderr
        if not buffer:
            return b""
        chunk = bytes(buffer[:maximum_bytes])
        del buffer[:maximum_bytes]
        return chunk

    def write(self, file_descriptor: int, data: bytes) -> int:
        assert file_descriptor == 10
        self.writes.append((file_descriptor, data))
        if len(self.writes) == 1:
            if self._response == "valid":
                self.stdout.extend(_frame(_candidate_response(self._material, data[4:])))
            elif self._response != "empty":
                raise AssertionError("unknown fake response mode")
            self._queue_read_events()
        return len(data)

    def monotonic(self) -> float:
        return self.monotonic_value

    def _queue_read_events(self) -> None:
        self.events.extend(
            [(11, selectors.EVENT_READ)]
            for _ in range(
                (len(self.stdout) + pipes._PIPE_CHUNK_BYTES - 1) // pipes._PIPE_CHUNK_BYTES
            )
        )
        self.events.append([(11, selectors.EVENT_READ)])
        self.events.append([(12, selectors.EVENT_READ)])


@dataclass
class _Material:
    ledger: Ledger
    kernel: SafetyKernel
    clock: _Clock
    configuration: ApprovalHostConfiguration
    metadata_digest: str
    record_digest: str
    action: MetaCampaignPausePlannedAction
    reviewed_metadata: bridge._MetaCampaignPauseApprovalBridgeReviewedMetadataV2


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _fixture_data(path: Path) -> dict[str, object]:
    value = parse_json(path.read_bytes())
    assert type(value) is dict
    return value


def _configuration() -> ApprovalHostConfiguration:
    fixture = _fixture_data(_META_FIXTURE)
    raw = fixture["configuration"]
    assert type(raw) is dict
    return ApprovalHostConfiguration(**raw)


def _fixture_action(configuration: ApprovalHostConfiguration) -> MetaCampaignPausePlannedAction:
    fixture = _fixture_data(_META_FIXTURE)
    raw = fixture["challenge"]
    assert type(raw) is dict
    challenge = parse_approval_challenge_wire(canonical_bytes(raw), configuration=configuration)
    assert challenge.meta_pause_action is not None
    return challenge.meta_pause_action


def _metadata_digest() -> str:
    fixture = _fixture_data(_V2_FIXTURE)
    source_cases = fixture["source_cases"]
    assert type(source_cases) is list and source_cases
    first = source_cases[0]
    assert type(first) is dict
    metadata = first["metadata_fixture"]
    assert type(metadata) is dict
    return hashlib.sha256(canonical_bytes(metadata)).hexdigest()


@pytest.fixture
def material(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Material:
    configuration = _configuration()
    action = _fixture_action(configuration)
    ledger = Ledger(tmp_path, profile_ref=action.plan.profile_ref)
    source = retain_meta_campaign_pause_plan(ledger, action)
    clock = _Clock(_T0)
    kernel = SafetyKernel(tmp_path, ledger, clock=clock)
    monkeypatch.setattr(ledger_module, "_utc_now", lambda: _timestamp(_T2))
    metadata_digest = _metadata_digest()
    return _Material(
        ledger=ledger,
        kernel=kernel,
        clock=clock,
        configuration=configuration,
        metadata_digest=metadata_digest,
        record_digest=cast(str, source["record_digest"]),
        action=action,
        reviewed_metadata=bridge._make_meta_campaign_pause_approval_bridge_reviewed_metadata_v2(
            configuration,
            metadata_digest,
        ),
    )


def _pending(material: _Material) -> bridge._PendingInstalledMetaCampaignPauseApprovalBridgeV2:
    return bridge._prepare_installed_meta_campaign_pause_approval_bridge_v2(
        material.kernel,
        material.ledger,
        source_plan_record_digest=material.record_digest,
        reviewed_metadata=material.reviewed_metadata,
        nonce=bytes(range(32)),
        request_id=bytes(range(32, 64)),
    )


def _candidate_response(material: _Material, request_bytes: bytes) -> bytes:
    request = decode_meta_campaign_pause_approval_bridge_request_v2(
        request_bytes,
        configuration=material.configuration,
        expected_metadata_digest=material.metadata_digest,
    )
    request_data = parse_json(request.canonical_bytes)
    assert type(request_data) is dict
    request_id_text = request_data["request_id"]
    challenge_text = request_data["canonical_challenge_b64url"]
    assert type(request_id_text) is str and type(challenge_text) is str
    request_id = base64.urlsafe_b64decode(request_id_text + "=" * (-len(request_id_text) % 4))
    canonical_challenge = base64.urlsafe_b64decode(
        challenge_text + "=" * (-len(challenge_text) % 4)
    )
    challenge = parse_approval_challenge_wire(
        canonical_challenge,
        configuration=material.configuration,
    )
    key = ec.generate_private_key(ec.SECP256R1())
    signing = receipt_signing_payload(
        challenge,
        configuration=material.configuration,
        receipt_id="approval-receipt:" + "M" * 43,
        issued_at=_T1,
    )
    receipt_data = parse_json(signing.canonical_unsigned_receipt)
    assert type(receipt_data) is dict
    receipt_data["signature"] = _b64(
        key.sign(signing.domain_separated_bytes, ec.ECDSA(hashes.SHA256()))
    )
    receipt = canonical_bytes(receipt_data)
    public_key = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return canonical_bytes(
        {
            "schema_version": 2,
            "message_type": "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_CANDIDATE_V2",
            "native_observation_status": "NATIVE_CURRENT_RECEIPT_VERIFIED_AND_NONCE_CONSUMED",
            "request_id_digest": hashlib.sha256(request_id).hexdigest(),
            "request_digest": request.digest,
            "source_context_digest": request.source_context_digest,
            "metadata_digest": material.metadata_digest,
            "protocol_configuration_digest": material.configuration.configuration_digest,
            "canonical_challenge_b64url": _b64(challenge.canonical_bytes),
            "challenge_digest": challenge.digest,
            "challenge_nonce_digest": hashlib.sha256(challenge.nonce).hexdigest(),
            "receipt_kind": challenge.kind.value,
            "expires_at": challenge.expires_at_text,
            "canonical_receipt_b64url": _b64(receipt),
            "receipt_digest": hashlib.sha256(receipt).hexdigest(),
            "receipt_id": receipt_data["receipt_id"],
            "active_public_key_x963_b64url": _b64(public_key),
            "active_public_key_fingerprint": hashlib.sha256(public_key).hexdigest(),
            "native_observed_at": _timestamp(_T2),
        }
    )


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def _adapter(
    operations: _FakePosixOperations,
) -> transport._MetaCampaignPauseApprovalBridgePipeAdapterV2:
    return transport._make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(
        cast(pipes._NativeApprovalBridgePosixOperations, operations)
    )


def _exchange(
    material: _Material,
    pending: bridge._PendingInstalledMetaCampaignPauseApprovalBridgeV2,
    operations: _FakePosixOperations,
    *,
    cancellation: _Cancellation | None = None,
) -> AcceptedNativeApproval:
    return bridge._exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
        material.kernel,
        material.ledger,
        pending=pending,
        adapter=_adapter(operations),
        cancellation=cancellation or _Cancellation(),
    )


def _claims(ledger: Ledger) -> list[dict[str, Any]]:
    return [
        row for row in ledger.records("changes") if row["record_kind"] == "APPROVAL_RECEIPT_CLAIMED"
    ]


def _assert_pending_consumed(
    material: _Material,
    pending: bridge._PendingInstalledMetaCampaignPauseApprovalBridgeV2,
    operations: _FakePosixOperations,
) -> None:
    with pytest.raises(AdhelmError) as reused:
        _exchange(material, pending, operations)
    assert reused.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"
    assert operations.launch_specs == []


def _rewritten_checkpoint(ledger: Ledger) -> dict[str, Any]:
    checkpoint = copy.deepcopy(ledger.checkpoint())
    streams = checkpoint["streams"]
    assert type(streams) is dict
    performance = streams["performance"]
    assert type(performance) is dict
    performance["stream_id"] = "replaced:performance"
    return checkpoint


def test_success_consumes_before_fixed_exchange_and_claims_without_provider_apply(
    material: _Material,
) -> None:
    pending = _pending(material)
    material.clock.now = _T2
    operations = _FakePosixOperations(material)

    accepted = _exchange(material, pending, operations)

    assert accepted.kind.value == "META_CAMPAIGN_PAUSE_PLAN"
    assert accepted.direct_grant is not None
    assert len(operations.launch_specs) == 1
    assert operations.writes and operations.writes[0][1][4:]
    assert len(_claims(material.ledger)) == 1
    events = [row["payload"] for row in material.ledger.records("changes")]
    assert any(event.get("event_type") == "APPROVAL_GRANTED" for event in events)
    assert not any(event.get("event_type") == "APPLY_STARTED" for event in events)
    _assert_pending_consumed(material, pending, _FakePosixOperations(material))


def test_definite_prelaunch_failure_spends_pending_without_claim(
    material: _Material,
) -> None:
    pending = _pending(material)
    material.clock.now = _T2
    operations = _FakePosixOperations(material, launch_error=OSError("fake prelaunch"))

    with pytest.raises(AdhelmError) as rejected:
        _exchange(material, pending, operations)

    assert (
        rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_UNAVAILABLE"
    )
    assert rejected.value.exit_code == ExitCode.PRECONDITION
    assert len(operations.launch_specs) == 1
    assert _claims(material.ledger) == []
    _assert_pending_consumed(material, pending, _FakePosixOperations(material))


def test_prelaunch_cancellation_spends_pending_without_launch_or_claim(
    material: _Material,
) -> None:
    pending = _pending(material)
    material.clock.now = _T2
    cancellation = _Cancellation(True)
    operations = _FakePosixOperations(material)

    with pytest.raises(AdhelmError) as rejected:
        _exchange(material, pending, operations, cancellation=cancellation)

    assert (
        rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_CANCELLED"
    )
    assert rejected.value.exit_code == ExitCode.PRECONDITION
    assert operations.launch_specs == []
    assert _claims(material.ledger) == []
    _assert_pending_consumed(material, pending, _FakePosixOperations(material))


def test_prelaunch_source_rewrite_is_precondition_and_spends_pending(
    material: _Material, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _pending(material)
    material.clock.now = _T2
    rewritten = _rewritten_checkpoint(material.ledger)
    monkeypatch.setattr(material.ledger, "checkpoint", lambda: rewritten)
    operations = _FakePosixOperations(material)

    with pytest.raises(AdhelmError) as rejected:
        _exchange(material, pending, operations)

    assert (
        rejected.value.code
        == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_EXCHANGE_PRECONDITION"
    )
    assert rejected.value.exit_code == ExitCode.PRECONDITION
    assert operations.launch_specs == []
    assert _claims(material.ledger) == []
    _assert_pending_consumed(material, pending, _FakePosixOperations(material))


def test_lost_reply_and_cancellation_after_launch_are_ambiguous_and_spent(
    material: _Material,
) -> None:
    pending = _pending(material)
    material.clock.now = _T2
    lost = _FakePosixOperations(material, response="empty")
    with pytest.raises(AdhelmError) as lost_error:
        _exchange(material, pending, lost)
    assert (
        lost_error.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_EXCHANGE_UNVERIFIED"
    )
    assert lost_error.value.exit_code == ExitCode.AMBIGUOUS
    assert len(lost.launch_specs) == 1
    assert _claims(material.ledger) == []
    _assert_pending_consumed(material, pending, _FakePosixOperations(material))

    pending = _pending(material)
    cancellation = _Cancellation()
    cancelled = _FakePosixOperations(
        material,
        cancellation_after_first_event=cancellation,
    )
    with pytest.raises(AdhelmError) as cancellation_error:
        _exchange(material, pending, cancelled, cancellation=cancellation)
    assert (
        cancellation_error.value.code
        == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_EXCHANGE_UNVERIFIED"
    )
    assert len(cancelled.launch_specs) == 1
    assert _claims(material.ledger) == []
    _assert_pending_consumed(material, pending, _FakePosixOperations(material))


def test_postcandidate_source_rewrite_is_acceptance_ambiguity_without_claim(
    material: _Material, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = _pending(material)
    material.clock.now = _T2
    original_checkpoint = material.ledger.checkpoint
    after_exchange = {"value": False}
    rewritten = _rewritten_checkpoint(material.ledger)

    def checkpoint() -> dict[str, Any]:
        return rewritten if after_exchange["value"] else original_checkpoint()

    monkeypatch.setattr(material.ledger, "checkpoint", checkpoint)
    operations = _FakePosixOperations(
        material,
        after_reap=lambda: after_exchange.__setitem__("value", True),
    )

    with pytest.raises(AdhelmError) as rejected:
        _exchange(material, pending, operations)

    assert (
        rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_ACCEPTANCE_UNVERIFIED"
    )
    assert len(operations.launch_specs) == 1
    assert _claims(material.ledger) == []
    _assert_pending_consumed(material, pending, _FakePosixOperations(material))


def test_benign_history_extension_during_exchange_preserves_prefix_and_claims(
    material: _Material,
) -> None:
    pending = _pending(material)
    material.clock.now = _T2

    def extend_history() -> None:
        retain_meta_campaign_pause_plan(material.ledger, material.action)

    operations = _FakePosixOperations(
        material,
        after_reap=extend_history,
    )

    accepted = _exchange(material, pending, operations)

    assert accepted.direct_grant is not None
    assert len(operations.launch_specs) == 1
    assert len(_claims(material.ledger)) == 1


@pytest.mark.parametrize("samples", [[_T2, _T1], [_T2, _T2, _T0 + timedelta(seconds=300)]])
def test_postlaunch_clock_rollback_or_expiry_is_ambiguous_without_claim(
    material: _Material,
    samples: list[datetime],
) -> None:
    pending = _pending(material)
    material.clock.schedule(*samples)
    operations = _FakePosixOperations(material)

    with pytest.raises(AdhelmError) as rejected:
        _exchange(material, pending, operations)

    assert (
        rejected.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_EXCHANGE_UNVERIFIED"
    )
    assert len(operations.launch_specs) == 1
    assert _claims(material.ledger) == []
    _assert_pending_consumed(material, pending, _FakePosixOperations(material))


def test_concurrent_attempts_launch_at_most_once_and_do_not_add_second_claim(
    material: _Material,
) -> None:
    pending = _pending(material)
    material.clock.now = _T2
    started = threading.Event()
    release = threading.Event()
    first_operations = _FakePosixOperations(
        material,
        launch_started=started,
        launch_release=release,
    )
    first_result: list[object] = []
    first_failure: list[BaseException] = []

    def first() -> None:
        try:
            first_result.append(_exchange(material, pending, first_operations))
        except BaseException as error:  # pragma: no cover - asserted below
            first_failure.append(error)

    worker = threading.Thread(target=first)
    worker.start()
    assert started.wait(timeout=5.0)
    second_operations = _FakePosixOperations(material)
    with pytest.raises(AdhelmError) as second_error:
        _exchange(material, pending, second_operations)
    release.set()
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert first_failure == []
    assert len(first_result) == 1
    assert (
        second_error.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_CONSUMED"
    )
    assert len(first_operations.launch_specs) == 1
    assert second_operations.launch_specs == []
    assert len(_claims(material.ledger)) == 1
