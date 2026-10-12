"""Fake-only transport coverage for the fixed Meta campaign PAUSE V2 route."""

from __future__ import annotations

import ast
import hashlib
import selectors
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

import adhelm._meta_campaign_pause_approval_bridge_transport_v2 as transport
import adhelm._native_approval_bridge_transport as pipes
from adhelm._meta_campaign_pause_approval_bridge_protocol_v2 import (
    NativeMetaCampaignPauseApprovalBridgeCandidateV2,
    NativeMetaCampaignPauseApprovalBridgeRequestV2,
    decode_meta_campaign_pause_approval_bridge_request_v2,
)
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError, ExitCode

_ROOT = Path(__file__).resolve().parents[1]
_VECTOR_PATH = (
    _ROOT / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/"
    "NativeApprovalBridgeV2MetaPause/meta-campaign-pause-approval-bridge-v2.json"
)
_FIFO_MODE = 0o010600


def _document() -> dict[str, object]:
    value = parse_json(_VECTOR_PATH.read_bytes())
    assert type(value) is dict
    return value


def _case() -> dict[str, object]:
    source_cases = _document()["source_cases"]
    assert type(source_cases) is list
    matching = [
        value
        for value in source_cases
        if type(value) is dict and value.get("case_id") == "meta_campaign_pause_whole_seconds"
    ]
    assert len(matching) == 1
    return matching[0]


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
    format_string = "%Y-%m-%dT%H:%M:%S.%fZ" if "." in value else "%Y-%m-%dT%H:%M:%SZ"
    return datetime.strptime(value, format_string).replace(tzinfo=UTC)


def _request(case: dict[str, object]) -> NativeMetaCampaignPauseApprovalBridgeRequestV2:
    raw = case["request_canonical_json"]
    assert type(raw) is str
    return decode_meta_campaign_pause_approval_bridge_request_v2(
        raw.encode("utf-8"),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
    )


def _candidate_bytes(case: dict[str, object]) -> bytes:
    raw = case["candidate_canonical_json"]
    assert type(raw) is str
    return raw.encode("utf-8")


class _Cancellation:
    def __init__(self, value: bool = False) -> None:
        self.value = value
        self.calls = 0

    def cancelled(self) -> bool:
        self.calls += 1
        return self.value


class _ValidationClock:
    def __init__(self, values: list[datetime]) -> None:
        self._values = list(values)
        self.calls = 0

    def utc_now(self) -> datetime:
        if self.calls >= len(self._values):
            raise ValueError("unexpected validation clock read")
        value = self._values[self.calls]
        self.calls += 1
        return value


class _FakeChild:
    def __init__(
        self,
        *,
        reap_results: list[int | None] | None = None,
        cancellation_after_successful_reap: _Cancellation | None = None,
    ) -> None:
        self._descriptors = {"stdin": 10, "stdout": 11, "stderr": 12}
        self._reap_results = list(reap_results or [0])
        self._cancellation_after_successful_reap = cancellation_after_successful_reap
        self.closed: list[str] = []
        self.terminate_calls = 0
        self.kill_calls = 0
        self.reap_calls: list[float] = []

    def fileno(self, stream: str) -> int:
        return self._descriptors[stream]

    def close_stream(self, stream: str) -> None:
        self.closed.append(stream)

    def poll(self) -> int | None:
        return None

    def reap(self, timeout_seconds: float) -> int | None:
        self.reap_calls.append(timeout_seconds)
        result = self._reap_results.pop(0) if self._reap_results else 0
        if result == 0 and self._cancellation_after_successful_reap is not None:
            self._cancellation_after_successful_reap.value = True
        return result

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1


class _FakeSelector:
    def __init__(
        self,
        operations: _FakePosixOperations,
        events: list[list[tuple[int, int]]],
        *,
        cancellation_after_first_event: _Cancellation | None = None,
    ) -> None:
        self._operations = operations
        self._events = list(events)
        self._cancellation_after_first_event = cancellation_after_first_event
        self._select_calls = 0
        self.closed = False
        self.registered: list[tuple[int, int]] = []
        self.unregistered: list[int] = []

    def register(self, file_descriptor: int, events: int) -> None:
        self.registered.append((file_descriptor, events))

    def unregister(self, file_descriptor: int) -> None:
        self.unregistered.append(file_descriptor)

    def select(self, timeout_seconds: float) -> list[tuple[int, int]]:
        assert 0 < timeout_seconds <= pipes._POLL_QUANTUM_SECONDS
        self._select_calls += 1
        if self._events:
            value = self._events.pop(0)
            if self._select_calls == 1 and self._cancellation_after_first_event is not None:
                self._cancellation_after_first_event.value = True
            return value
        self._operations.monotonic_value += pipes._EXCHANGE_DEADLINE_SECONDS + 1.0
        return []

    def close(self) -> None:
        self.closed = True


class _FakePosixOperations:
    def __init__(
        self,
        *,
        stdout: bytes,
        stderr: bytes = b"",
        child: _FakeChild | None = None,
        events: list[list[tuple[int, int]]] | None = None,
        cancellation_after_first_event: _Cancellation | None = None,
        launch_error: Exception | None = None,
    ) -> None:
        self.child = child or _FakeChild()
        self._stdout = bytearray(stdout)
        self._stderr = bytearray(stderr)
        self._events = events if events is not None else _events_for_streams(stdout, stderr)
        self._cancellation_after_first_event = cancellation_after_first_event
        self._launch_error = launch_error
        self.monotonic_value = 0.0
        self.launch_specs: list[object] = []
        self.nonblocking: list[int] = []
        self.writes: list[tuple[int, bytes]] = []
        self.selector: _FakeSelector | None = None

    def launch(self, spec: object) -> _FakeChild:
        self.launch_specs.append(spec)
        if self._launch_error is not None:
            raise self._launch_error
        return self.child

    def fstat_mode(self, file_descriptor: int) -> int:
        del file_descriptor
        return _FIFO_MODE

    def set_nonblocking(self, file_descriptor: int) -> None:
        self.nonblocking.append(file_descriptor)

    def open_selector(self) -> _FakeSelector:
        self.selector = _FakeSelector(
            self,
            self._events,
            cancellation_after_first_event=self._cancellation_after_first_event,
        )
        return self.selector

    def read(self, file_descriptor: int, maximum_bytes: int) -> bytes:
        buffer = self._stdout if file_descriptor == 11 else self._stderr
        if not buffer:
            return b""
        chunk = bytes(buffer[:maximum_bytes])
        del buffer[:maximum_bytes]
        return chunk

    def write(self, file_descriptor: int, data: bytes) -> int:
        self.writes.append((file_descriptor, data))
        return len(data)

    def monotonic(self) -> float:
        return self.monotonic_value


def _events_for_streams(stdout: bytes, stderr: bytes) -> list[list[tuple[int, int]]]:
    events: list[list[tuple[int, int]]] = [[(10, selectors.EVENT_WRITE)]]
    events.extend(
        [(11, selectors.EVENT_READ)]
        for _ in range((len(stdout) + pipes._PIPE_CHUNK_BYTES - 1) // pipes._PIPE_CHUNK_BYTES)
    )
    events.append([(11, selectors.EVENT_READ)])
    events.extend(
        [(12, selectors.EVENT_READ)]
        for _ in range((len(stderr) + pipes._PIPE_CHUNK_BYTES - 1) // pipes._PIPE_CHUNK_BYTES)
    )
    events.append([(12, selectors.EVENT_READ)])
    return events


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def _exchange(
    case: dict[str, object],
    adapter: transport._MetaCampaignPauseApprovalBridgePipeAdapterV2,
    *,
    cancellation: _Cancellation | None = None,
    clock: _ValidationClock | None = None,
) -> NativeMetaCampaignPauseApprovalBridgeCandidateV2:
    return transport.exchange_meta_campaign_pause_approval_bridge_once_v2(
        _request(case),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
        adapter=adapter,
        validation_clock=clock
        or _ValidationClock(
            [
                _time(case["exchange_started_at"]),
                _time(case["current_now"]),
                _time(case["current_now"]),
            ]
        ),
        cancellation=cancellation or _Cancellation(),
    )


def _v2_adapter(
    operations: _FakePosixOperations,
) -> transport._MetaCampaignPauseApprovalBridgePipeAdapterV2:
    return transport._make_meta_campaign_pause_approval_bridge_pipe_adapter_v2(operations)


def _assert_unverified(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_EXCHANGE_UNVERIFIED"
    assert error.value.exit_code == ExitCode.AMBIGUOUS
    assert "PRIVATE_SENTINEL" not in error.value.message
    assert "do not retry this exchange" in error.value.next_action


def test_fixed_v2_posix_exchange_frames_once_and_returns_private_candidate() -> None:
    case = _case()
    operations = _FakePosixOperations(stdout=_frame(_candidate_bytes(case)))

    candidate = _exchange(case, _v2_adapter(operations))

    expected = case["candidate"]
    assert type(expected) is dict
    assert candidate.receipt_id == expected["receipt_id"]
    expected_spec = pipes._fixed_launch_spec_for_role(
        pipes._NativeApprovalBridgeLaunchRole.META_CAMPAIGN_PAUSE_V2
    )
    assert operations.launch_specs == [expected_spec]
    assert expected_spec.executable == pipes._META_CAMPAIGN_PAUSE_HELPER_PATH
    assert expected_spec.argv == (pipes._META_CAMPAIGN_PAUSE_HELPER_PATH,)
    assert expected_spec.environment == (("LANG", "C"), ("LC_ALL", "C"), ("TZ", "UTC"))
    assert expected_spec.working_directory == "/"
    assert expected_spec.shell is False and expected_spec.close_fds is True
    assert expected_spec.stdin_pipe and expected_spec.stdout_pipe and expected_spec.stderr_pipe
    assert operations.writes == [(10, _frame(_request(case).canonical_bytes))]
    assert operations.nonblocking == [10, 11, 12]
    assert operations.child.terminate_calls == 0
    assert operations.child.kill_calls == 0
    assert set(operations.child.closed) == {"stdin", "stdout", "stderr"}


def test_closed_v2_launch_role_does_not_change_v1_spec_or_accept_foreign_role() -> None:
    assert pipes._fixed_launch_spec() == pipes._fixed_launch_spec_for_role(
        pipes._NativeApprovalBridgeLaunchRole.V1
    )
    assert pipes._fixed_launch_spec().executable == pipes._HELPER_PATH
    assert (
        pipes._fixed_launch_spec_for_role(
            pipes._NativeApprovalBridgeLaunchRole.META_CAMPAIGN_PAUSE_V2
        ).executable
        == pipes._META_CAMPAIGN_PAUSE_HELPER_PATH
    )
    operations = _FakePosixOperations(stdout=b"")
    with pytest.raises(ValueError):
        pipes._PosixNativeApprovalBridgePipeAdapter(
            operations,
            launch_role=cast(pipes._NativeApprovalBridgeLaunchRole, "not-a-role"),
        )


def test_system_launch_admits_only_exact_v2_spec_without_executing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class _Stream:
        def __init__(self, descriptor: int) -> None:
            self.descriptor = descriptor

        def fileno(self) -> int:
            return self.descriptor

        def close(self) -> None:
            return None

    class _Process:
        stdin = _Stream(30)
        stdout = _Stream(31)
        stderr = _Stream(32)

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float) -> int:
            del timeout
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    def _fake_popen(arguments: list[str], **kwargs: object) -> _Process:
        calls.append((arguments, kwargs))
        return _Process()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    operations = pipes._SystemNativeApprovalBridgePosixOperations()
    specification = pipes._fixed_launch_spec_for_role(
        pipes._NativeApprovalBridgeLaunchRole.META_CAMPAIGN_PAUSE_V2
    )
    assert operations.launch(specification).fileno("stdin") == 30
    assert calls == [
        (
            [pipes._META_CAMPAIGN_PAUSE_HELPER_PATH],
            {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": "/",
                "env": {"LANG": "C", "LC_ALL": "C", "TZ": "UTC"},
                "shell": False,
                "close_fds": True,
                "text": False,
            },
        )
    ]
    with pytest.raises(ValueError):
        operations.launch(replace(specification, working_directory="/private"))
    assert len(calls) == 1


def test_malformed_request_and_prelaunch_cancellation_never_call_adapter() -> None:
    case = _case()

    with pytest.raises(AdhelmError) as invalid_error:
        transport.exchange_meta_campaign_pause_approval_bridge_once_v2(
            cast(NativeMetaCampaignPauseApprovalBridgeRequestV2, object()),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            adapter=_v2_adapter(_FakePosixOperations(stdout=b"")),
            validation_clock=_ValidationClock([_time(case["exchange_started_at"])]),
            cancellation=_Cancellation(),
        )
    assert invalid_error.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_PROTOCOL_INVALID"

    with pytest.raises(AdhelmError) as cancelled_error:
        _exchange(
            case,
            _v2_adapter(_FakePosixOperations(stdout=b"")),
            cancellation=_Cancellation(True),
        )
    assert (
        cancelled_error.value.code
        == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_CANCELLED"
    )
    assert cancelled_error.value.exit_code == ExitCode.PRECONDITION


def test_prelaunch_adapter_failure_is_a_fixed_precondition() -> None:
    case = _case()
    with pytest.raises(AdhelmError) as error:
        _exchange(
            case,
            cast(transport._MetaCampaignPauseApprovalBridgePipeAdapterV2, object()),
        )
    assert error.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_UNAVAILABLE"
    assert error.value.exit_code == ExitCode.PRECONDITION

    operations = _FakePosixOperations(stdout=b"")
    default_v1_adapter = pipes._PosixNativeApprovalBridgePipeAdapter(operations)
    with pytest.raises(AdhelmError) as role_error:
        _exchange(
            case,
            cast(transport._MetaCampaignPauseApprovalBridgePipeAdapterV2, default_v1_adapter),
        )
    assert (
        role_error.value.code
        == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_UNAVAILABLE"
    )
    assert operations.launch_specs == []


@pytest.mark.parametrize(
    "stdout,stderr",
    [
        (b"", b""),
        (_frame(_candidate_bytes(_case())) + b"TRAILING", b""),
        (_frame(_candidate_bytes(_case())), b"PRIVATE_SENTINEL" * 1_000),
    ],
)
def test_postlaunch_frame_or_stderr_failure_is_ambiguous_and_aborts(
    stdout: bytes, stderr: bytes
) -> None:
    case = _case()
    operations = _FakePosixOperations(
        stdout=stdout,
        stderr=stderr,
        child=_FakeChild(reap_results=[None, 0]),
    )
    with pytest.raises(AdhelmError) as error:
        _exchange(case, _v2_adapter(operations))
    _assert_unverified(error)
    assert len(operations.launch_specs) == 1
    assert operations.child.terminate_calls == 1
    assert operations.child.kill_calls == 1
    assert set(operations.child.closed) == {"stdin", "stdout", "stderr"}


def test_cancellation_after_launch_is_ambiguous_and_never_retries() -> None:
    case = _case()
    cancellation = _Cancellation()
    operations = _FakePosixOperations(
        stdout=_frame(_candidate_bytes(case)),
        child=_FakeChild(reap_results=[None, 0]),
        cancellation_after_first_event=cancellation,
    )
    with pytest.raises(AdhelmError) as error:
        _exchange(case, _v2_adapter(operations), cancellation=cancellation)
    _assert_unverified(error)
    assert len(operations.launch_specs) == 1
    assert operations.child.terminate_calls == 1
    assert operations.child.kill_calls == 1
    assert set(operations.child.closed) == {"stdin", "stdout", "stderr"}


def test_postlaunch_child_wrapper_failure_is_ambiguous_not_a_precondition() -> None:
    case = _case()
    operations = _FakePosixOperations(
        stdout=b"",
        launch_error=pipes._PostLaunchUnverified(),
    )
    with pytest.raises(AdhelmError) as error:
        _exchange(case, _v2_adapter(operations))
    _assert_unverified(error)
    assert operations.launch_specs == [
        pipes._fixed_launch_spec_for_role(
            pipes._NativeApprovalBridgeLaunchRole.META_CAMPAIGN_PAUSE_V2
        )
    ]


def test_prelaunch_os_error_remains_a_v2_precondition() -> None:
    case = _case()
    operations = _FakePosixOperations(stdout=b"", launch_error=OSError("not launched"))
    with pytest.raises(AdhelmError) as error:
        _exchange(case, _v2_adapter(operations))
    assert error.value.code == "NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_TRANSPORT_UNAVAILABLE"
    assert error.value.exit_code == ExitCode.PRECONDITION
    assert operations.child.terminate_calls == 0


def test_postexchange_cancellation_candidate_and_final_clock_fail_closed() -> None:
    case = _case()
    cancellation = _Cancellation()
    with pytest.raises(AdhelmError) as cancellation_error:
        _exchange(
            case,
            _v2_adapter(
                _FakePosixOperations(
                    stdout=_frame(_candidate_bytes(case)),
                    child=_FakeChild(cancellation_after_successful_reap=cancellation),
                )
            ),
            cancellation=cancellation,
            clock=_ValidationClock([_time(case["exchange_started_at"])]),
        )
    _assert_unverified(cancellation_error)

    candidate = case["candidate"]
    assert type(candidate) is dict
    forged = dict(candidate)
    forged["native_observation_status"] = "PRIVATE_SENTINEL"
    with pytest.raises(AdhelmError) as candidate_error:
        _exchange(
            case,
            _v2_adapter(_FakePosixOperations(stdout=_frame(canonical_bytes(forged)))),
        )
    _assert_unverified(candidate_error)

    with pytest.raises(AdhelmError) as clock_error:
        _exchange(
            case,
            _v2_adapter(_FakePosixOperations(stdout=_frame(_candidate_bytes(case)))),
            clock=_ValidationClock(
                [
                    _time(case["exchange_started_at"]),
                    _time(case["current_now"]),
                    _time(case["exchange_started_at"]),
                ]
            ),
        )
    _assert_unverified(clock_error)


def test_transport_has_no_acceptance_runtime_or_provider_import() -> None:
    source = _ROOT / "src/adhelm/_meta_campaign_pause_approval_bridge_transport_v2.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        f"{node.module}.{alias.name}" if node.module else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    for forbidden in (
        "adhelm.approval_acceptance",
        "adhelm._installed_meta_campaign_pause_approval_bridge_v2",
        "adhelm.ledger",
        "adhelm.providers",
        "adhelm.cli",
    ):
        assert forbidden not in imports
