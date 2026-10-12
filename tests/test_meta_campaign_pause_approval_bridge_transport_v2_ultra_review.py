"""Independent fake-only adversarial coverage for the Meta PAUSE V2 transport."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

import adhelm._meta_campaign_pause_approval_bridge_transport_v2 as transport
import adhelm._native_approval_bridge_transport as pipes
from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeRequestV2,
    decode_meta_campaign_pause_approval_bridge_request_v2,
)
from adhelm._native_approval_bridge_protocol import decode_native_approval_bridge_request_v1
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError, ExitCode

_ROOT = Path(__file__).resolve().parents[1]
_V2_VECTOR = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/"
    "NativeApprovalBridgeV2MetaPause/meta-campaign-pause-approval-bridge-v2.json"
)
_V1_VECTOR = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/"
    "NativeApprovalBridge/native-approval-bridge-protocol-v1.json"
)
_FIFO_MODE = 0o010600


def _document(path: Path) -> dict[str, object]:
    value = parse_json(path.read_bytes())
    assert type(value) is dict
    assert canonical_bytes(value) == path.read_bytes()
    return value


def _case(path: Path, case_id: str) -> dict[str, object]:
    source_cases = _document(path)["source_cases"]
    assert type(source_cases) is list
    matches = [
        value for value in source_cases if type(value) is dict and value.get("case_id") == case_id
    ]
    assert len(matches) == 1
    return matches[0]


def _metadata(case: dict[str, object]) -> dict[str, object]:
    value = case["metadata_fixture"]
    assert type(value) is dict
    return value


def _configuration(case: dict[str, object]) -> ApprovalHostConfiguration:
    metadata = _metadata(case)
    fields = (
        "host_ref",
        "signing_key_ref",
        "maximum_challenge_bytes",
        "maximum_challenge_lifetime_seconds",
        "maximum_issued_at_skew_seconds",
        "maximum_subplans",
        "maximum_policy_items",
    )
    assert all(field in metadata for field in fields)
    assert type(metadata["host_ref"]) is str
    assert type(metadata["signing_key_ref"]) is str
    for field in fields[2:]:
        assert type(metadata[field]) is int and not isinstance(metadata[field], bool)
    return ApprovalHostConfiguration(
        host_ref=metadata["host_ref"],
        signing_key_ref=metadata["signing_key_ref"],
        maximum_challenge_bytes=cast(int, metadata["maximum_challenge_bytes"]),
        maximum_challenge_lifetime_seconds=cast(
            int, metadata["maximum_challenge_lifetime_seconds"]
        ),
        maximum_issued_at_skew_seconds=cast(int, metadata["maximum_issued_at_skew_seconds"]),
        maximum_subplans=cast(int, metadata["maximum_subplans"]),
        maximum_policy_items=cast(int, metadata["maximum_policy_items"]),
    )


def _metadata_digest(case: dict[str, object]) -> str:
    return hashlib.sha256(canonical_bytes(_metadata(case))).hexdigest()


def _time(value: object) -> datetime:
    assert type(value) is str
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _request(
    case: dict[str, object],
) -> NativeMetaCampaignPauseApprovalBridgeRequestV2:
    raw = case["request_canonical_json"]
    assert type(raw) is str
    return decode_meta_campaign_pause_approval_bridge_request_v2(
        raw.encode("utf-8"),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
    )


class _Cancellation:
    def cancelled(self) -> bool:
        return False


class _Clock:
    def __init__(self, samples: list[datetime]) -> None:
        self._samples = samples
        self._index = 0

    def utc_now(self) -> datetime:
        value = self._samples[self._index]
        self._index += 1
        return value


class _NoLaunchOperations:
    def __init__(self) -> None:
        self.launches = 0

    def launch(self, _spec: object) -> object:
        self.launches += 1
        pytest.fail("a rejected request must not launch a helper")


class _InterruptChild:
    def __init__(self) -> None:
        self.closed: list[str] = []
        self.terminate_calls = 0
        self.kill_calls = 0
        self.reap_calls = 0

    def fileno(self, stream: str) -> int:
        return {"stdin": 90, "stdout": 91, "stderr": 92}[stream]

    def close_stream(self, stream: str) -> None:
        self.closed.append(stream)

    def poll(self) -> int | None:
        return None

    def reap(self, _timeout_seconds: float) -> int | None:
        self.reap_calls += 1
        return None if self.reap_calls == 1 else 0

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _InterruptSelector:
    def __init__(self) -> None:
        self.closed = False

    def register(self, _file_descriptor: int, _events: int) -> None:
        return None

    def unregister(self, _file_descriptor: int) -> None:
        return None

    def select(self, _timeout_seconds: float) -> list[tuple[int, int]]:
        raise KeyboardInterrupt()

    def close(self) -> None:
        self.closed = True


class _InterruptOperations:
    def __init__(self) -> None:
        self.child = _InterruptChild()
        self.selector = _InterruptSelector()
        self.launch_specs: list[object] = []

    def launch(self, spec: object) -> _InterruptChild:
        self.launch_specs.append(spec)
        return self.child

    def fstat_mode(self, _file_descriptor: int) -> int:
        return _FIFO_MODE

    def set_nonblocking(self, _file_descriptor: int) -> None:
        return None

    def open_selector(self) -> _InterruptSelector:
        return self.selector

    def read(self, _file_descriptor: int, _maximum_bytes: int) -> bytes:
        pytest.fail("KeyboardInterrupt must stop before a pipe read")

    def write(self, _file_descriptor: int, _data: bytes) -> int:
        pytest.fail("KeyboardInterrupt must stop before a pipe write")

    def monotonic(self) -> float:
        return 0.0


def _adapter(
    operations: object,
) -> transport._MetaCampaignPauseApprovalBridgePipeAdapterV2:
    return transport._make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(
        cast(pipes._NativeApprovalBridgePosixOperations, operations)
    )


def _exchange(
    case: dict[str, object],
    adapter: transport._MetaCampaignPauseApprovalBridgePipeAdapterV2,
    *,
    samples: list[datetime],
) -> object:
    return transport.exchange_meta_campaign_pause_approval_bridge_once_v2(
        _request(case),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
        adapter=adapter,
        validation_clock=_Clock(samples),
        cancellation=_Cancellation(),
    )


def _assert_precondition(error: pytest.ExceptionInfo[AdhelmError], code: str) -> None:
    assert error.value.code == code
    assert error.value.exit_code == ExitCode.PRECONDITION


def _assert_protocol_invalid(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_PROTOCOL_INVALID"
    assert error.value.exit_code == ExitCode.INVALID_INPUT


def _assert_ambiguous(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_EXCHANGE_UNVERIFIED"
    assert error.value.exit_code == ExitCode.AMBIGUOUS
    assert "do not retry this exchange" in error.value.next_action.lower()


def test_cross_version_request_and_exchange_start_skew_fail_before_pipe_launch() -> None:
    v2_case = _case(_V2_VECTOR, "meta_campaign_pause_whole_seconds")
    v1_case = _case(_V1_VECTOR, "swift_openai_pause")
    v1_raw = v1_case["request_canonical_json"]
    assert type(v1_raw) is str
    v1_request = decode_native_approval_bridge_request_v1(
        v1_raw.encode("utf-8"),
        configuration=_configuration(v1_case),
        expected_metadata_digest=_metadata_digest(v1_case),
    )
    v1_operations = _NoLaunchOperations()
    with pytest.raises(AdhelmError) as cross_version:
        transport.exchange_meta_campaign_pause_approval_bridge_once_v2(
            cast(NativeMetaCampaignPauseApprovalBridgeRequestV2, v1_request),
            configuration=_configuration(v2_case),
            expected_metadata_digest=_metadata_digest(v2_case),
            adapter=_adapter(v1_operations),
            validation_clock=_Clock([_time(v2_case["exchange_started_at"])]),
            cancellation=_Cancellation(),
        )
    _assert_protocol_invalid(cross_version)
    assert v1_operations.launches == 0

    skew_operations = _NoLaunchOperations()
    with pytest.raises(AdhelmError) as skew:
        _exchange(
            v2_case,
            _adapter(skew_operations),
            samples=[_time(v2_case["exchange_started_at"]) - timedelta(seconds=120)],
        )
    _assert_precondition(
        skew,
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_TIME_INVALID",
    )
    assert skew_operations.launches == 0


def test_post_popen_wrapper_failure_is_ambiguous_but_oserror_is_prelaunch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _case(_V2_VECTOR, "meta_campaign_pause_whole_seconds")

    class _Stream:
        def fileno(self) -> int:
            return 100

        def close(self) -> None:
            return None

    class _WrapperFaultProcess:
        stdin = _Stream()
        stdout = None
        stderr = _Stream()

        def __init__(self) -> None:
            self.terminate_calls = 0
            self.kill_calls = 0
            self.wait_calls = 0

        def terminate(self) -> None:
            self.terminate_calls += 1

        def kill(self) -> None:
            self.kill_calls += 1

        def wait(self, timeout: float) -> int:
            del timeout
            self.wait_calls += 1
            return 0

    process = _WrapperFaultProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(AdhelmError) as wrapper_fault:
        _exchange(
            case,
            _adapter(pipes._SystemNativeApprovalBridgePosixOperations()),
            samples=[_time(case["exchange_started_at"])],
        )
    _assert_ambiguous(wrapper_fault)
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls == 1

    def _raise_oserror(*_args: object, **_kwargs: object) -> object:
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(subprocess, "Popen", _raise_oserror)
    with pytest.raises(AdhelmError) as prelaunch_fault:
        _exchange(
            case,
            _adapter(pipes._SystemNativeApprovalBridgePosixOperations()),
            samples=[_time(case["exchange_started_at"])],
        )
    _assert_precondition(
        prelaunch_fault,
        "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_UNAVAILABLE",
    )


def test_keyboard_interrupt_after_launch_closes_and_reaps_without_candidate() -> None:
    case = _case(_V2_VECTOR, "meta_campaign_pause_whole_seconds")
    operations = _InterruptOperations()
    with pytest.raises(AdhelmError) as interrupted:
        _exchange(
            case,
            _adapter(operations),
            samples=[_time(case["exchange_started_at"])],
        )
    _assert_ambiguous(interrupted)
    assert operations.launch_specs == [
        pipes._fixed_launch_spec_for_role(
            pipes._NativeApprovalBridgeLaunchRole.META_CAMPAIGN_PAUSE_V2
        )
    ]
    assert set(operations.child.closed) == {"stdin", "stdout", "stderr"}
    assert operations.child.terminate_calls == 1
    assert operations.child.kill_calls == 1
    assert operations.selector.closed is True
