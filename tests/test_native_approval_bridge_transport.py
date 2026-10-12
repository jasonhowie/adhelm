"""Fake-only tests for the private one-shot native approval bridge transport."""

from __future__ import annotations

import ast
import hashlib
import selectors
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import adhelm._native_approval_bridge_transport as transport
from adhelm._native_approval_bridge_protocol import (
    NativeApprovalBridgeCandidateV1,
    NativeApprovalBridgeRequestV1,
    decode_native_approval_bridge_request_v1,
)
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.canonical import canonical_bytes, parse_json
from adhelm.errors import AdhelmError, ExitCode

_ROOT = Path(__file__).resolve().parents[1]
_VECTOR_PATH = (
    _ROOT
    / "native/macos-approval-host/Tests/ApprovalHostCoreTests/Fixtures/NativeApprovalBridge"
    / "native-approval-bridge-protocol-v1.json"
)
_FIFO_MODE = 0o010600


def _document() -> dict[str, object]:
    data = parse_json(_VECTOR_PATH.read_bytes())
    assert type(data) is dict
    return data


def _case() -> dict[str, object]:
    cases = _document()["source_cases"]
    assert type(cases) is list
    matching = [value for value in cases if type(value) is dict]
    assert len(matching) == 2
    return matching[0]


def _metadata(case: dict[str, object]) -> dict[str, object]:
    metadata = case["metadata_fixture"]
    assert type(metadata) is dict
    return metadata


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

    def exact_int(value: object) -> int:
        assert type(value) is int and not isinstance(value, bool)
        return value

    return ApprovalHostConfiguration(
        host_ref=str(metadata["host_ref"]),
        signing_key_ref=str(metadata["signing_key_ref"]),
        maximum_challenge_bytes=exact_int(metadata["maximum_challenge_bytes"]),
        maximum_challenge_lifetime_seconds=exact_int(
            metadata["maximum_challenge_lifetime_seconds"]
        ),
        maximum_issued_at_skew_seconds=exact_int(metadata["maximum_issued_at_skew_seconds"]),
        maximum_subplans=exact_int(metadata["maximum_subplans"]),
        maximum_policy_items=exact_int(metadata["maximum_policy_items"]),
    )


def _metadata_digest(case: dict[str, object]) -> str:
    return hashlib.sha256(canonical_bytes(_metadata(case))).hexdigest()


def _utc(value: object) -> datetime:
    assert type(value) is str
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _request(case: dict[str, object]) -> NativeApprovalBridgeRequestV1:
    raw = case["request_canonical_json"]
    assert type(raw) is str
    return decode_native_approval_bridge_request_v1(
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
        self._values = values
        self.calls = 0

    def utc_now(self) -> datetime:
        if self.calls >= len(self._values):
            raise ValueError("unexpected validation clock read")
        value = self._values[self.calls]
        self.calls += 1
        return value


class _FakeChild:
    def __init__(
        self, *, reap_results: list[int | None] | None = None, poll: int | None = None
    ) -> None:
        self._descriptors = {"stdin": 10, "stdout": 11, "stderr": 12}
        self._reap_results = list(reap_results or [0])
        self._poll = poll
        self.closed: list[str] = []
        self.terminate_calls = 0
        self.kill_calls = 0
        self.reap_calls: list[float] = []

    def fileno(self, stream: str) -> int:
        return self._descriptors[stream]

    def close_stream(self, stream: str) -> None:
        self.closed.append(stream)

    def poll(self) -> int | None:
        return self._poll

    def reap(self, timeout_seconds: float) -> int | None:
        self.reap_calls.append(timeout_seconds)
        return self._reap_results.pop(0) if self._reap_results else 0

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
        cancel_after_first_event: _Cancellation | None = None,
        interrupt_on_select_call: int | None = None,
    ) -> None:
        self._operations = operations
        self._events = list(events)
        self._cancel_after_first_event = cancel_after_first_event
        self._interrupt_on_select_call = interrupt_on_select_call
        self.registered: list[tuple[int, int]] = []
        self.unregistered: list[int] = []
        self.closed = False
        self._select_calls = 0

    def register(self, file_descriptor: int, events: int) -> None:
        self.registered.append((file_descriptor, events))

    def unregister(self, file_descriptor: int) -> None:
        self.unregistered.append(file_descriptor)

    def select(self, timeout_seconds: float) -> list[tuple[int, int]]:
        assert 0 < timeout_seconds <= transport._POLL_QUANTUM_SECONDS
        self._select_calls += 1
        if self._select_calls == self._interrupt_on_select_call:
            raise KeyboardInterrupt()
        if self._events:
            selected = self._events.pop(0)
            if self._select_calls == 1 and self._cancel_after_first_event is not None:
                self._cancel_after_first_event.value = True
            return selected
        self._operations.monotonic_value += transport._EXCHANGE_DEADLINE_SECONDS + 1.0
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
        launch_error: Exception | None = None,
        non_fifo_descriptor: int | None = None,
        write_result: int | None = None,
        cancellation_after_first_event: _Cancellation | None = None,
        interrupt_on_select_call: int | None = None,
        interrupt_on_read_descriptor: int | None = None,
    ) -> None:
        self.child = child or _FakeChild()
        self._stdout = bytearray(stdout)
        self._stderr = bytearray(stderr)
        self._events = events if events is not None else _events_for_streams(stdout, stderr)
        self._launch_error = launch_error
        self._non_fifo_descriptor = non_fifo_descriptor
        self._write_result = write_result
        self._cancellation_after_first_event = cancellation_after_first_event
        self._interrupt_on_select_call = interrupt_on_select_call
        self._interrupt_on_read_descriptor = interrupt_on_read_descriptor
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
        if file_descriptor == self._non_fifo_descriptor:
            return 0o100600
        return _FIFO_MODE

    def set_nonblocking(self, file_descriptor: int) -> None:
        self.nonblocking.append(file_descriptor)

    def open_selector(self) -> _FakeSelector:
        self.selector = _FakeSelector(
            self,
            self._events,
            cancel_after_first_event=self._cancellation_after_first_event,
            interrupt_on_select_call=self._interrupt_on_select_call,
        )
        return self.selector

    def read(self, file_descriptor: int, maximum_bytes: int) -> bytes:
        if file_descriptor == self._interrupt_on_read_descriptor:
            raise KeyboardInterrupt()
        buffer = self._stdout if file_descriptor == 11 else self._stderr
        if not buffer:
            return b""
        data = bytes(buffer[:maximum_bytes])
        del buffer[:maximum_bytes]
        return data

    def write(self, file_descriptor: int, data: bytes) -> int:
        self.writes.append((file_descriptor, data))
        return self._write_result if self._write_result is not None else len(data)

    def monotonic(self) -> float:
        return self.monotonic_value


def _events_for_streams(stdout: bytes, stderr: bytes) -> list[list[tuple[int, int]]]:
    events: list[list[tuple[int, int]]] = [[(10, selectors.EVENT_WRITE)]]
    events.extend(
        [(11, selectors.EVENT_READ)]
        for _ in range(
            (len(stdout) + transport._PIPE_CHUNK_BYTES - 1) // transport._PIPE_CHUNK_BYTES
        )
    )
    events.append([(11, selectors.EVENT_READ)])
    events.extend(
        [(12, selectors.EVENT_READ)]
        for _ in range(
            (len(stderr) + transport._PIPE_CHUNK_BYTES - 1) // transport._PIPE_CHUNK_BYTES
        )
    )
    events.append([(12, selectors.EVENT_READ)])
    return events


def _frame(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


def _exchange(
    case: dict[str, object],
    operations: _FakePosixOperations,
    *,
    cancellation: _Cancellation | None = None,
    clock: _ValidationClock | None = None,
) -> NativeApprovalBridgeCandidateV1:
    return transport.exchange_native_approval_bridge_once_v1(
        _request(case),
        configuration=_configuration(case),
        expected_metadata_digest=_metadata_digest(case),
        adapter=transport._PosixNativeApprovalBridgePipeAdapter(operations),
        validation_clock=clock
        or _ValidationClock(
            [
                _utc(case["exchange_started_at"]),
                _utc(case["current_now"]),
                _utc(case["current_now"]),
            ]
        ),
        cancellation=cancellation or _Cancellation(),
    )


def _assert_unverified(error: pytest.ExceptionInfo[AdhelmError]) -> None:
    assert error.value.code == "NATIVE_APPROVAL_BRIDGE_EXCHANGE_UNVERIFIED"
    assert error.value.exit_code == ExitCode.AMBIGUOUS
    assert "PRIVATE_SENTINEL" not in error.value.message
    assert "retry this exchange" in error.value.next_action


def test_fixed_fake_posix_exchange_frames_once_reaps_and_returns_private_candidate() -> None:
    case = _case()
    candidate_raw = _candidate_bytes(case)
    operations = _FakePosixOperations(stdout=_frame(candidate_raw))

    candidate = _exchange(case, operations)

    expected_candidate = _case()["candidate"]
    assert type(expected_candidate) is dict
    assert candidate.receipt_id == expected_candidate["receipt_id"]
    assert operations.launch_specs == [transport._fixed_launch_spec()]
    spec = operations.launch_specs[0]
    assert spec == transport._fixed_launch_spec()
    assert spec.executable == transport._HELPER_PATH
    assert spec.argv == (transport._HELPER_PATH,)
    assert spec.environment == (("LANG", "C"), ("LC_ALL", "C"), ("TZ", "UTC"))
    assert spec.working_directory == "/"
    assert spec.shell is False and spec.close_fds is True
    assert spec.stdin_pipe and spec.stdout_pipe and spec.stderr_pipe
    assert operations.writes == [(10, _frame(_request(case).canonical_bytes))]
    assert operations.nonblocking == [10, 11, 12]
    assert operations.child.terminate_calls == 0
    assert operations.child.kill_calls == 0
    assert set(operations.child.closed) == {"stdin", "stdout", "stderr"}
    assert operations.selector is not None and operations.selector.closed


def test_fixed_system_launch_shape_uses_fake_popen_without_executing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Stream:
        def __init__(self, descriptor: int) -> None:
            self.descriptor = descriptor

        def fileno(self) -> int:
            return self.descriptor

        def close(self) -> None:
            return None

    class Process:
        stdin = Stream(30)
        stdout = Stream(31)
        stderr = Stream(32)

        def poll(self) -> int | None:
            return None

        def wait(self, timeout: float) -> int:
            del timeout
            return 0

        def terminate(self) -> None:
            return None

        def kill(self) -> None:
            return None

    def fake_popen(arguments: list[str], **kwargs: object) -> Process:
        calls.append((arguments, kwargs))
        return Process()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    operations = transport._SystemNativeApprovalBridgePosixOperations()
    child = operations.launch(transport._fixed_launch_spec())

    assert child.fileno("stdin") == 30
    assert calls == [
        (
            [transport._HELPER_PATH],
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
        operations.launch(replace(transport._fixed_launch_spec(), working_directory="/private"))
    assert len(calls) == 1


def test_system_stream_wrapper_failure_terminates_and_reaps_its_owned_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        def fileno(self) -> int:
            return 40

        def close(self) -> None:
            return None

    class Process:
        stdin = Stream()
        stdout = None
        stderr = Stream()

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

    process = Process()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    with pytest.raises(transport._PostLaunchUnverified):
        transport._SystemNativeApprovalBridgePosixOperations().launch(
            transport._fixed_launch_spec()
        )
    assert process.terminate_calls == 1
    assert process.wait_calls == 1
    assert process.kill_calls == 0


def test_prelaunch_cancellation_never_launches_a_helper() -> None:
    case = _case()
    operations = _FakePosixOperations(stdout=b"")
    with pytest.raises(AdhelmError) as error:
        _exchange(case, operations, cancellation=_Cancellation(True))
    assert error.value.code == "NATIVE_APPROVAL_BRIDGE_TRANSPORT_CANCELLED"
    assert error.value.exit_code == ExitCode.PRECONDITION
    assert operations.launch_specs == []


def test_postlaunch_child_wrapper_failure_is_ambiguous_not_a_precondition() -> None:
    case = _case()
    operations = _FakePosixOperations(
        stdout=b"",
        launch_error=transport._PostLaunchUnverified(),
    )
    with pytest.raises(AdhelmError) as error:
        _exchange(case, operations)
    _assert_unverified(error)
    assert operations.launch_specs == [transport._fixed_launch_spec()]


def test_prelaunch_os_error_remains_a_precondition() -> None:
    case = _case()
    operations = _FakePosixOperations(stdout=b"", launch_error=OSError("not launched"))
    with pytest.raises(AdhelmError) as error:
        _exchange(case, operations)
    assert error.value.code == "NATIVE_APPROVAL_BRIDGE_TRANSPORT_UNAVAILABLE"
    assert error.value.exit_code == ExitCode.PRECONDITION
    assert operations.child.terminate_calls == 0


@pytest.mark.parametrize(
    "stdout,stderr",
    [
        (b"", b""),
        (_frame(_candidate_bytes(_case())) + b"TRAILING", b""),
        (_frame(_candidate_bytes(_case())), b"PRIVATE_SENTINEL" * 1_000),
    ],
)
def test_postlaunch_frame_or_stderr_failure_is_ambiguous_and_redacted(
    stdout: bytes, stderr: bytes
) -> None:
    case = _case()
    operations = _FakePosixOperations(
        stdout=stdout,
        stderr=stderr,
        child=_FakeChild(reap_results=[None, 0]),
    )
    with pytest.raises(AdhelmError) as error:
        _exchange(case, operations)
    _assert_unverified(error)
    assert operations.child.terminate_calls == 1
    assert operations.child.kill_calls == 1
    assert set(operations.child.closed) == {"stdin", "stdout", "stderr"}
    assert len(operations.launch_specs) == 1


def test_cancellation_after_launch_kills_reaps_closes_and_never_retries() -> None:
    case = _case()
    cancellation = _Cancellation()
    operations = _FakePosixOperations(
        stdout=_frame(_candidate_bytes(case)),
        child=_FakeChild(reap_results=[None, 0]),
        cancellation_after_first_event=cancellation,
    )
    with pytest.raises(AdhelmError) as error:
        _exchange(case, operations, cancellation=cancellation)
    _assert_unverified(error)
    assert len(operations.launch_specs) == 1
    assert operations.child.terminate_calls == 1
    assert operations.child.kill_calls == 1
    assert set(operations.child.closed) == {"stdin", "stdout", "stderr"}


def test_cancellation_after_adapter_return_never_releases_a_candidate() -> None:
    case = _case()
    cancellation = _Cancellation()

    class ReturningAdapter:
        def exchange(self, request_frame: bytes, *, cancellation: object) -> bytes:
            assert request_frame == _frame(_request(case).canonical_bytes)
            assert type(cancellation) is _Cancellation
            cancellation.value = True
            return _candidate_bytes(case)

    with pytest.raises(AdhelmError) as error:
        transport.exchange_native_approval_bridge_once_v1(
            _request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            adapter=ReturningAdapter(),
            validation_clock=_ValidationClock([_utc(case["exchange_started_at"])]),
            cancellation=cancellation,
        )
    _assert_unverified(error)


@pytest.mark.parametrize(
    "select_interrupt,read_interrupt",
    [(1, None), (None, 11)],
)
def test_interrupt_after_launch_aborts_child_before_fixed_ambiguous_result(
    select_interrupt: int | None, read_interrupt: int | None
) -> None:
    case = _case()
    operations = _FakePosixOperations(
        stdout=_frame(_candidate_bytes(case)),
        child=_FakeChild(reap_results=[None, 0]),
        interrupt_on_select_call=select_interrupt,
        interrupt_on_read_descriptor=read_interrupt,
    )
    with pytest.raises(AdhelmError) as error:
        _exchange(case, operations)
    _assert_unverified(error)
    assert operations.child.terminate_calls == 1
    assert operations.child.kill_calls == 1
    assert set(operations.child.closed) == {"stdin", "stdout", "stderr"}


def test_deadline_and_non_pipe_descriptor_fail_closed_after_launch() -> None:
    case = _case()
    deadline_operations = _FakePosixOperations(
        stdout=b"", child=_FakeChild(reap_results=[None, 0]), events=[]
    )
    with pytest.raises(AdhelmError) as deadline_error:
        _exchange(case, deadline_operations)
    _assert_unverified(deadline_error)
    assert deadline_operations.child.terminate_calls == 1

    pipe_operations = _FakePosixOperations(
        stdout=_frame(_candidate_bytes(case)),
        child=_FakeChild(reap_results=[None, 0]),
        non_fifo_descriptor=11,
    )
    with pytest.raises(AdhelmError) as pipe_error:
        _exchange(case, pipe_operations)
    _assert_unverified(pipe_error)
    assert pipe_operations.child.terminate_calls == 1


def test_inconsistent_polled_and_reaped_exit_status_is_not_a_clean_exchange() -> None:
    case = _case()
    operations = _FakePosixOperations(
        stdout=_frame(_candidate_bytes(case)),
        child=_FakeChild(poll=1, reap_results=[0, None, 0]),
    )
    with pytest.raises(AdhelmError) as error:
        _exchange(case, operations)
    _assert_unverified(error)
    assert operations.child.terminate_calls == 1
    assert operations.child.kill_calls == 1


@pytest.mark.parametrize(
    "final_now",
    [
        datetime(2030, 1, 1, 0, 0, 30, tzinfo=UTC),
        datetime(2030, 1, 1, 0, 4, tzinfo=UTC),
    ],
)
def test_final_local_clock_rollback_or_expiry_after_candidate_validation_is_ambiguous(
    final_now: datetime,
) -> None:
    case = _case()
    operations = _FakePosixOperations(stdout=_frame(_candidate_bytes(case)))
    clock = _ValidationClock(
        [
            _utc(case["exchange_started_at"]),
            _utc(case["current_now"]),
            final_now,
        ]
    )
    with pytest.raises(AdhelmError) as error:
        _exchange(case, operations, clock=clock)
    _assert_unverified(error)


def test_request_window_is_checked_at_exchange_start_before_helper_launch() -> None:
    case = _case()
    operations = _FakePosixOperations(stdout=b"")
    with pytest.raises(AdhelmError) as error:
        _exchange(
            case,
            operations,
            clock=_ValidationClock([datetime(2029, 12, 31, 23, 58, tzinfo=UTC)]),
        )
    assert error.value.code == "NATIVE_APPROVAL_BRIDGE_TIME_INVALID"
    assert operations.launch_specs == []


def test_invalid_post_consumption_candidate_is_never_returned_as_private_evidence() -> None:
    case = _case()
    candidate = _case()["candidate"]
    assert type(candidate) is dict
    forged = dict(candidate)
    forged["native_observation_status"] = "PRIVATE_SENTINEL"
    operations = _FakePosixOperations(stdout=_frame(canonical_bytes(forged)))
    with pytest.raises(AdhelmError) as error:
        _exchange(case, operations)
    _assert_unverified(error)
    assert operations.child.terminate_calls == 0
    assert operations.child.kill_calls == 0


def test_pipe_writer_rejects_a_reported_count_above_the_submitted_chunk() -> None:
    class OverreportingOperations:
        def write(self, file_descriptor: int, data: bytes) -> int:
            del file_descriptor, data
            return transport._PIPE_CHUNK_BYTES + 1

    with pytest.raises(ValueError):
        transport._write_pending(
            OverreportingOperations(), 10, memoryview(b"x" * (transport._PIPE_CHUNK_BYTES + 1))
        )


def test_transport_has_no_receipt_acceptance_or_runtime_wiring_import() -> None:
    path = _ROOT / "src/adhelm/_native_approval_bridge_transport.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
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
        "adhelm.approval_acceptance.PinnedApprovalTrust",
        "adhelm.approval_acceptance.accept_approval_receipt",
        "adhelm.ledger.Ledger",
        "adhelm.cli",
    ):
        assert forbidden not in imports
    assert (
        "exchange_native_approval_bridge_once_v1("
        not in path.read_text(encoding="utf-8").split(
            "def exchange_native_approval_bridge_once_v1(", 1
        )[1]
    )


def test_constructed_request_or_bad_clock_fails_before_launch() -> None:
    case = _case()
    operations = _FakePosixOperations(stdout=b"")
    forged = object.__new__(NativeApprovalBridgeRequestV1)
    with pytest.raises(AdhelmError) as request_error:
        transport.exchange_native_approval_bridge_once_v1(
            forged,
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            adapter=transport._PosixNativeApprovalBridgePipeAdapter(operations),
            validation_clock=_ValidationClock([_utc(case["exchange_started_at"])]),
            cancellation=_Cancellation(),
        )
    assert request_error.value.code == "NATIVE_APPROVAL_BRIDGE_PROTOCOL_INVALID"
    assert operations.launch_specs == []

    class BadClock:
        def utc_now(self) -> datetime:
            return datetime(2030, 1, 1)

    with pytest.raises(AdhelmError) as clock_error:
        transport.exchange_native_approval_bridge_once_v1(
            _request(case),
            configuration=_configuration(case),
            expected_metadata_digest=_metadata_digest(case),
            adapter=transport._PosixNativeApprovalBridgePipeAdapter(operations),
            validation_clock=BadClock(),
            cancellation=_Cancellation(),
        )
    assert clock_error.value.code == "NATIVE_APPROVAL_BRIDGE_TRANSPORT_UNAVAILABLE"
    assert operations.launch_specs == []
