"""Private one-shot POSIX pipe plumbing for the proposed approval bridge.

This module has no public command, installed binding, or default runtime call
site.  It implements the fixed wire mechanics behind injected POSIX seams so
they can be tested without launching the helper.  It does not resolve native
metadata, trust a key, accept a receipt, write a ledger record, or authorize
provider work.
"""

from __future__ import annotations

import math
import os
import selectors
import stat
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Final, Literal, Protocol, cast

from adhelm._native_approval_bridge_protocol import (
    NativeApprovalBridgeCandidateV1,
    NativeApprovalBridgeRequestV1,
    revalidate_native_approval_bridge_request_v1_at_exchange,
    validate_native_approval_bridge_candidate_v1,
)
from adhelm.approval_protocol import ApprovalHostConfiguration
from adhelm.errors import AdhelmError, ExitCode

_HELPER_PATH: Final = (
    "/Applications/AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-approval-client"
)
_META_CAMPAIGN_PAUSE_HELPER_PATH: Final = (
    "/Applications/AdHelmApprovalHost.app/Contents/MacOS/"
    "adhelm-macos-meta-campaign-pause-approval-client"
)
_WORKING_DIRECTORY: Final = "/"
_SANITIZED_ENVIRONMENT: Final = (
    ("LANG", "C"),
    ("LC_ALL", "C"),
    ("TZ", "UTC"),
)
_REQUEST_MAXIMUM_BYTES: Final = 98_304
_CANDIDATE_MAXIMUM_BYTES: Final = 131_072
_FRAME_HEADER_BYTES: Final = 4
_PIPE_CHUNK_BYTES: Final = 16_384
_MAXIMUM_STDERR_BYTES: Final = 4_096
_EXCHANGE_DEADLINE_SECONDS: Final = 300.0
_POLL_QUANTUM_SECONDS: Final = 0.05
_CLEANUP_WAIT_SECONDS: Final = 0.25

_PipeName = Literal["stdin", "stdout", "stderr"]


class _NativeApprovalBridgeLaunchRole(Enum):
    """The two compile-time bridge helper roles; callers cannot supply selectors."""

    V1 = "V1"
    META_CAMPAIGN_PAUSE_V2 = "META_CAMPAIGN_PAUSE_V2"


@dataclass(frozen=True, slots=True)
class _NativeApprovalBridgeLaunchSpec:
    """Fixed launch selectors passed only to the private POSIX adapter."""

    executable: str
    argv: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    working_directory: str
    shell: bool
    close_fds: bool
    stdin_pipe: bool
    stdout_pipe: bool
    stderr_pipe: bool


class _NativeApprovalBridgeCancellation(Protocol):
    """Private cancellation seam for the one exchange."""

    def cancelled(self) -> bool: ...


class _NativeApprovalBridgeValidationClock(Protocol):
    """Private trusted UTC clock seam sampled before and after pipe exchange."""

    def utc_now(self) -> datetime: ...


class _NativeApprovalBridgePipeChild(Protocol):
    """Minimal owned child/process surface used by the POSIX state machine."""

    def fileno(self, stream: _PipeName) -> int: ...

    def close_stream(self, stream: _PipeName) -> None: ...

    def poll(self) -> int | None: ...

    def reap(self, timeout_seconds: float) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...


class _NativeApprovalBridgeSelector(Protocol):
    """Small selector seam; events carry only numeric file descriptor/mask."""

    def register(self, file_descriptor: int, events: int) -> None: ...

    def unregister(self, file_descriptor: int) -> None: ...

    def select(self, timeout_seconds: float) -> list[tuple[int, int]]: ...

    def close(self) -> None: ...


class _NativeApprovalBridgePosixOperations(Protocol):
    """Private, fakeable POSIX operations.  Tests never use the system form."""

    def launch(self, spec: _NativeApprovalBridgeLaunchSpec) -> _NativeApprovalBridgePipeChild: ...

    def fstat_mode(self, file_descriptor: int) -> int: ...

    def set_nonblocking(self, file_descriptor: int) -> None: ...

    def open_selector(self) -> _NativeApprovalBridgeSelector: ...

    def read(self, file_descriptor: int, maximum_bytes: int) -> bytes: ...

    def write(self, file_descriptor: int, data: bytes) -> int: ...

    def monotonic(self) -> float: ...


class _NativeApprovalBridgePipeAdapter(Protocol):
    """Private exchange seam.  There is no default production caller."""

    def exchange(
        self,
        request_frame: bytes,
        *,
        cancellation: _NativeApprovalBridgeCancellation,
    ) -> bytes: ...


class _NativeApprovalBridgePipeWriter(Protocol):
    def write(self, file_descriptor: int, data: bytes) -> int: ...


class _SystemPipeStream(Protocol):
    def fileno(self) -> int: ...

    def close(self) -> None: ...


class _PreLaunchCancellation(Exception):
    pass


class _PreLaunchUnavailable(Exception):
    pass


class _PostLaunchUnverified(Exception):
    pass


class _DeadlineExceeded(Exception):
    pass


class _CancellationObserved(Exception):
    pass


class _SystemNativeApprovalBridgePipeChild:
    """Thin private wrapper around a binary ``Popen`` child."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process
        streams = {
            "stdin": process.stdin,
            "stdout": process.stdout,
            "stderr": process.stderr,
        }
        if any(stream is None for stream in streams.values()):
            raise ValueError("fixed pipe launch did not create every stream")
        self._streams = {name: cast(_SystemPipeStream, stream) for name, stream in streams.items()}

    def _stream(self, stream: _PipeName) -> _SystemPipeStream:
        value = self._streams.get(stream)
        if value is None:
            raise ValueError("pipe stream is unavailable")
        return value

    def fileno(self, stream: _PipeName) -> int:
        value = self._stream(stream)
        descriptor = value.fileno()
        if type(descriptor) is not int:
            raise ValueError("pipe descriptor is invalid")
        return descriptor

    def close_stream(self, stream: _PipeName) -> None:
        value = self._stream(stream)
        value.close()

    def poll(self) -> int | None:
        status = self._process.poll()
        if status is not None and (type(status) is not int or isinstance(status, bool)):
            raise ValueError("process status is invalid")
        return status

    def reap(self, timeout_seconds: float) -> int | None:
        try:
            status = self._process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return None
        if type(status) is not int or isinstance(status, bool):
            raise ValueError("process status is invalid")
        return status

    def terminate(self) -> None:
        self._process.terminate()

    def kill(self) -> None:
        self._process.kill()


class _SystemNativeApprovalBridgeSelector:
    """Private adapter around ``selectors.DefaultSelector``."""

    def __init__(self) -> None:
        self._selector = selectors.DefaultSelector()

    def register(self, file_descriptor: int, events: int) -> None:
        self._selector.register(file_descriptor, events)

    def unregister(self, file_descriptor: int) -> None:
        self._selector.unregister(file_descriptor)

    def select(self, timeout_seconds: float) -> list[tuple[int, int]]:
        selected = self._selector.select(timeout_seconds)
        result: list[tuple[int, int]] = []
        for key, mask in selected:
            if type(key.fd) is not int or type(mask) is not int:
                raise ValueError("selector event is invalid")
            result.append((key.fd, mask))
        return result

    def close(self) -> None:
        self._selector.close()


class _SystemNativeApprovalBridgePosixOperations:
    """Concrete system mechanics, intentionally unwired from product runtime."""

    def launch(self, spec: _NativeApprovalBridgeLaunchSpec) -> _NativeApprovalBridgePipeChild:
        if spec not in (
            _fixed_launch_spec(),
            _fixed_launch_spec_for_role(_NativeApprovalBridgeLaunchRole.META_CAMPAIGN_PAUSE_V2),
        ):
            raise ValueError("launch spec is not fixed")
        process = subprocess.Popen(
            [spec.executable],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=spec.working_directory,
            env=dict(spec.environment),
            shell=False,
            close_fds=True,
            text=False,
        )
        try:
            return _SystemNativeApprovalBridgePipeChild(process)
        except BaseException:
            _terminate_unwrapped_process(process)
            raise _PostLaunchUnverified() from None

    def fstat_mode(self, file_descriptor: int) -> int:
        return os.fstat(file_descriptor).st_mode

    def set_nonblocking(self, file_descriptor: int) -> None:
        os.set_blocking(file_descriptor, False)

    def open_selector(self) -> _NativeApprovalBridgeSelector:
        return _SystemNativeApprovalBridgeSelector()

    def read(self, file_descriptor: int, maximum_bytes: int) -> bytes:
        return os.read(file_descriptor, maximum_bytes)

    def write(self, file_descriptor: int, data: bytes) -> int:
        return os.write(file_descriptor, data)

    def monotonic(self) -> float:
        return time.monotonic()


class _PosixNativeApprovalBridgePipeAdapter:
    """One bounded fixed-launch exchange over owned anonymous POSIX pipes."""

    def __init__(
        self,
        operations: _NativeApprovalBridgePosixOperations,
        *,
        launch_role: _NativeApprovalBridgeLaunchRole = _NativeApprovalBridgeLaunchRole.V1,
    ) -> None:
        self._operations = operations
        self._launch_role = _checked_launch_role(launch_role)

    def exchange(
        self,
        request_frame: bytes,
        *,
        cancellation: _NativeApprovalBridgeCancellation,
    ) -> bytes:
        try:
            _require_frame(request_frame, maximum_bytes=_REQUEST_MAXIMUM_BYTES)
            deadline = _MonotonicDeadline(self._operations)
            _check_live(deadline, cancellation)
        except _CancellationObserved:
            raise _PreLaunchCancellation() from None
        except Exception:
            raise _PreLaunchUnavailable() from None

        child: _NativeApprovalBridgePipeChild | None = None
        selector: _NativeApprovalBridgeSelector | None = None
        try:
            try:
                child = self._operations.launch(_fixed_launch_spec_for_role(self._launch_role))
            except _PostLaunchUnverified:
                # The system wrapper can reach Popen, then fail while it
                # validates the child streams. It has already cleaned up the
                # process, but its outcome remains ambiguous to the caller.
                raise
            except Exception:
                raise _PreLaunchUnavailable() from None
            if not _is_exact_child(child):
                raise ValueError("child is invalid")
            selector = self._operations.open_selector()
            if not _is_exact_selector(selector):
                raise ValueError("selector is invalid")
            return self._exchange_open_child(
                child,
                selector,
                request_frame,
                deadline=deadline,
                cancellation=cancellation,
            )
        except _PreLaunchUnavailable:
            if child is None:
                raise
            _abort_child(child)
            raise _PostLaunchUnverified() from None
        except BaseException:
            if child is not None:
                _abort_child(child)
            raise _PostLaunchUnverified() from None
        finally:
            if selector is not None:
                _close_selector(selector)

    def _exchange_open_child(
        self,
        child: _NativeApprovalBridgePipeChild,
        selector: _NativeApprovalBridgeSelector,
        request_frame: bytes,
        *,
        deadline: _MonotonicDeadline,
        cancellation: _NativeApprovalBridgeCancellation,
    ) -> bytes:
        stdin_fd, stdout_fd, stderr_fd = _prepare_owned_pipes(child, selector, self._operations)
        pending = memoryview(request_frame)
        stdout = bytearray()
        stdout_expected: int | None = None
        stderr_count = 0
        stdin_closed = False
        stdout_eof = False
        stderr_eof = False

        while not (stdin_closed and stdout_eof and stderr_eof):
            _check_live(deadline, cancellation)
            events = selector.select(deadline.poll_timeout())
            if type(events) is not list:
                raise ValueError("selector results are invalid")
            for file_descriptor, mask in events:
                _check_live(deadline, cancellation)
                if type(file_descriptor) is not int or type(mask) is not int:
                    raise ValueError("selector event is invalid")
                if file_descriptor == stdin_fd:
                    if stdin_closed or mask != selectors.EVENT_WRITE:
                        raise ValueError("stdin selector event is invalid")
                    pending = _write_pending(
                        self._operations,
                        stdin_fd,
                        pending,
                    )
                    if not pending:
                        child.close_stream("stdin")
                        selector.unregister(stdin_fd)
                        stdin_closed = True
                elif file_descriptor == stdout_fd:
                    if stdout_eof or mask != selectors.EVENT_READ:
                        raise ValueError("stdout selector event is invalid")
                    chunk = _read_pipe_chunk(
                        self._operations,
                        stdout_fd,
                        maximum_bytes=_stdout_read_limit(stdout, stdout_expected),
                    )
                    if chunk is None:
                        continue
                    if not chunk:
                        if stdout_expected is None or len(stdout) != stdout_expected:
                            raise ValueError("stdout ended before one frame")
                        selector.unregister(stdout_fd)
                        stdout_eof = True
                    else:
                        stdout_expected = _append_stdout(
                            stdout,
                            chunk,
                            expected=stdout_expected,
                        )
                elif file_descriptor == stderr_fd:
                    if stderr_eof or mask != selectors.EVENT_READ:
                        raise ValueError("stderr selector event is invalid")
                    chunk = _read_pipe_chunk(
                        self._operations,
                        stderr_fd,
                        maximum_bytes=min(
                            _PIPE_CHUNK_BYTES,
                            _MAXIMUM_STDERR_BYTES - stderr_count + 1,
                        ),
                    )
                    if chunk is None:
                        continue
                    if not chunk:
                        selector.unregister(stderr_fd)
                        stderr_eof = True
                    else:
                        stderr_count += len(chunk)
                        if stderr_count > _MAXIMUM_STDERR_BYTES:
                            raise ValueError("stderr exceeds fixed bound")
                else:
                    raise ValueError("selector returned an unknown descriptor")

        status = _reap_successfully(child, deadline=deadline, cancellation=cancellation)
        if status != 0:
            raise ValueError("helper exited unsuccessfully")
        child.close_stream("stdout")
        child.close_stream("stderr")
        if stdout_expected is None or len(stdout) != stdout_expected:
            raise ValueError("stdout frame is incomplete")
        return bytes(stdout[_FRAME_HEADER_BYTES:])


class _MonotonicDeadline:
    """Fixed monotonic lifetime with rollback detection for one child exchange."""

    def __init__(self, operations: _NativeApprovalBridgePosixOperations) -> None:
        self._operations = operations
        started = _checked_monotonic(operations.monotonic())
        self._last = started
        self._deadline = started + _EXCHANGE_DEADLINE_SECONDS
        if not math.isfinite(self._deadline):
            raise ValueError("deadline is invalid")

    def checkpoint(self) -> float:
        current = _checked_monotonic(self._operations.monotonic())
        if current < self._last or current >= self._deadline:
            raise _DeadlineExceeded()
        self._last = current
        return current

    def poll_timeout(self) -> float:
        current = self.checkpoint()
        return min(_POLL_QUANTUM_SECONDS, self._deadline - current)


def exchange_native_approval_bridge_once_v1(
    request: NativeApprovalBridgeRequestV1,
    *,
    configuration: ApprovalHostConfiguration,
    expected_metadata_digest: str,
    adapter: _NativeApprovalBridgePipeAdapter,
    validation_clock: _NativeApprovalBridgeValidationClock,
    cancellation: _NativeApprovalBridgeCancellation,
) -> NativeApprovalBridgeCandidateV1:
    """Run exactly one private exchange through an injected pipe adapter.

    The caller cannot select a helper path, command, environment, trust pin,
    receipt, or execution target.  This function is not wired to a configured
    product runtime.  Any failure after a child was launched is ambiguous
    because native nonce consumption may already have happened.
    """
    try:
        exchange_started_at = _checked_utc_now(validation_clock)
        checked_request = revalidate_native_approval_bridge_request_v1_at_exchange(
            request,
            configuration=configuration,
            expected_metadata_digest=expected_metadata_digest,
            exchange_started_at=exchange_started_at,
        )
    except AdhelmError as exc:
        if exc.code.startswith("NATIVE_APPROVAL_BRIDGE_"):
            raise
        raise _transport_precondition() from None
    except Exception:
        raise _transport_precondition() from None

    try:
        response = adapter.exchange(
            _frame(checked_request.canonical_bytes),
            cancellation=cancellation,
        )
    except _PreLaunchCancellation:
        raise _transport_cancelled() from None
    except _PreLaunchUnavailable:
        raise _transport_precondition() from None
    except BaseException:
        raise _transport_unverified() from None

    try:
        _check_not_cancelled(cancellation)
        current_now = _checked_utc_now(validation_clock)
        candidate = validate_native_approval_bridge_candidate_v1(
            response,
            request=checked_request,
            configuration=configuration,
            expected_metadata_digest=expected_metadata_digest,
            exchange_started_at=exchange_started_at,
            current_now=current_now,
        )
        final_now = _checked_utc_now(validation_clock)
        if final_now < current_now or final_now >= candidate.expires_at:
            raise ValueError("candidate validity changed during local verification")
        _check_not_cancelled(cancellation)
        return candidate
    except BaseException:
        raise _transport_unverified() from None


def _fixed_launch_spec() -> _NativeApprovalBridgeLaunchSpec:
    """Preserve the original V1 helper selector exactly."""
    return _fixed_launch_spec_for_role(_NativeApprovalBridgeLaunchRole.V1)


def _fixed_launch_spec_for_role(
    role: _NativeApprovalBridgeLaunchRole,
) -> _NativeApprovalBridgeLaunchSpec:
    """Return one closed private launch description for a compile-time role."""
    checked_role = _checked_launch_role(role)
    if checked_role is _NativeApprovalBridgeLaunchRole.V1:
        helper_path = _HELPER_PATH
    elif checked_role is _NativeApprovalBridgeLaunchRole.META_CAMPAIGN_PAUSE_V2:
        helper_path = _META_CAMPAIGN_PAUSE_HELPER_PATH
    else:  # pragma: no cover - Enum validation makes this unreachable
        raise ValueError("launch role is invalid")
    return _NativeApprovalBridgeLaunchSpec(
        executable=helper_path,
        argv=(helper_path,),
        environment=_SANITIZED_ENVIRONMENT,
        working_directory=_WORKING_DIRECTORY,
        shell=False,
        close_fds=True,
        stdin_pipe=True,
        stdout_pipe=True,
        stderr_pipe=True,
    )


def _checked_launch_role(value: object) -> _NativeApprovalBridgeLaunchRole:
    if type(value) is not _NativeApprovalBridgeLaunchRole:
        raise ValueError("launch role is invalid")
    return value


def _prepare_owned_pipes(
    child: _NativeApprovalBridgePipeChild,
    selector: _NativeApprovalBridgeSelector,
    operations: _NativeApprovalBridgePosixOperations,
) -> tuple[int, int, int]:
    streams: tuple[_PipeName, _PipeName, _PipeName] = ("stdin", "stdout", "stderr")
    descriptors = tuple(child.fileno(stream) for stream in streams)
    if len(set(descriptors)) != 3 or any(
        type(descriptor) is not int or descriptor < 0 for descriptor in descriptors
    ):
        raise ValueError("pipe descriptors are invalid")
    for descriptor in descriptors:
        if not stat.S_ISFIFO(operations.fstat_mode(descriptor)):
            raise ValueError("fixed child stream is not a pipe")
        operations.set_nonblocking(descriptor)
    stdin_fd, stdout_fd, stderr_fd = descriptors
    selector.register(stdin_fd, selectors.EVENT_WRITE)
    selector.register(stdout_fd, selectors.EVENT_READ)
    selector.register(stderr_fd, selectors.EVENT_READ)
    return stdin_fd, stdout_fd, stderr_fd


def _write_pending(
    operations: _NativeApprovalBridgePipeWriter,
    file_descriptor: int,
    pending: memoryview,
) -> memoryview:
    if not pending:
        return pending
    submitted = bytes(pending[:_PIPE_CHUNK_BYTES])
    try:
        written = operations.write(file_descriptor, submitted)
    except BlockingIOError:
        return pending
    if type(written) is not int or isinstance(written, bool) or not 1 <= written <= len(submitted):
        raise ValueError("pipe write is invalid")
    return pending[written:]


def _read_pipe_chunk(
    operations: _NativeApprovalBridgePosixOperations,
    file_descriptor: int,
    *,
    maximum_bytes: int,
) -> bytes | None:
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise ValueError("pipe read bound is invalid")
    try:
        chunk = operations.read(file_descriptor, maximum_bytes)
    except BlockingIOError:
        return None
    if type(chunk) is not bytes or len(chunk) > maximum_bytes:
        raise ValueError("pipe read is invalid")
    return chunk


def _stdout_read_limit(stdout: bytearray, expected: int | None) -> int:
    if expected is None:
        return min(_PIPE_CHUNK_BYTES, _FRAME_HEADER_BYTES + _CANDIDATE_MAXIMUM_BYTES - len(stdout))
    return min(_PIPE_CHUNK_BYTES, expected + 1 - len(stdout))


def _append_stdout(stdout: bytearray, chunk: bytes, *, expected: int | None) -> int | None:
    if len(stdout) + len(chunk) > _FRAME_HEADER_BYTES + _CANDIDATE_MAXIMUM_BYTES:
        raise ValueError("candidate frame exceeds fixed bound")
    stdout.extend(chunk)
    if expected is None and len(stdout) >= _FRAME_HEADER_BYTES:
        length = int.from_bytes(stdout[:_FRAME_HEADER_BYTES], "big", signed=False)
        if not 1 <= length <= _CANDIDATE_MAXIMUM_BYTES:
            raise ValueError("candidate frame length is invalid")
        expected = _FRAME_HEADER_BYTES + length
    if expected is not None and len(stdout) > expected:
        raise ValueError("stdout has trailing frame bytes")
    return expected


def _reap_successfully(
    child: _NativeApprovalBridgePipeChild,
    *,
    deadline: _MonotonicDeadline,
    cancellation: _NativeApprovalBridgeCancellation,
) -> int:
    while True:
        _check_live(deadline, cancellation)
        status = child.poll()
        if status is not None:
            observed = _checked_status(status)
            reaped = child.reap(0.0)
            if reaped is None or _checked_status(reaped) != observed:
                raise ValueError("exited child was not reaped")
            return observed
        status = child.reap(deadline.poll_timeout())
        if status is not None:
            return _checked_status(status)


def _abort_child(child: _NativeApprovalBridgePipeChild) -> None:
    """Best-effort terminal cleanup; no exception can restore retry safety."""
    for stream in ("stdin", "stdout", "stderr"):
        try:
            child.close_stream(stream)
        except BaseException:
            pass
    try:
        child.terminate()
    except BaseException:
        pass
    try:
        reaped = child.reap(_CLEANUP_WAIT_SECONDS)
    except BaseException:
        reaped = None
    if reaped is None:
        try:
            child.kill()
        except BaseException:
            pass
        try:
            child.reap(_CLEANUP_WAIT_SECONDS)
        except BaseException:
            pass


def _terminate_unwrapped_process(process: subprocess.Popen[bytes]) -> None:
    """Avoid leaking a child if fixed stream-wrapper construction fails."""
    try:
        process.terminate()
    except BaseException:
        pass
    try:
        status = process.wait(timeout=_CLEANUP_WAIT_SECONDS)
    except BaseException:
        status = None
    if status is None:
        try:
            process.kill()
        except BaseException:
            pass
        try:
            process.wait(timeout=_CLEANUP_WAIT_SECONDS)
        except BaseException:
            pass


def _close_selector(selector: _NativeApprovalBridgeSelector) -> None:
    try:
        selector.close()
    except BaseException:
        pass


def _check_live(
    deadline: _MonotonicDeadline,
    cancellation: _NativeApprovalBridgeCancellation,
) -> None:
    _check_not_cancelled(cancellation)
    deadline.checkpoint()


def _check_not_cancelled(cancellation: _NativeApprovalBridgeCancellation) -> None:
    cancelled = cancellation.cancelled()
    if type(cancelled) is not bool:
        raise ValueError("cancellation state is invalid")
    if cancelled:
        raise _CancellationObserved()


def _checked_monotonic(value: object) -> float:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise ValueError("monotonic clock is invalid")
    numeric = cast(int | float, value)
    if not math.isfinite(numeric):
        raise ValueError("monotonic clock is invalid")
    return float(numeric)


def _checked_utc_now(clock: _NativeApprovalBridgeValidationClock) -> datetime:
    value = clock.utc_now()
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("trusted UTC clock is invalid")
    return value.astimezone(UTC)


def _checked_status(value: object) -> int:
    if type(value) is not int or isinstance(value, bool):
        raise ValueError("child exit status is invalid")
    return value


def _require_frame(value: object, *, maximum_bytes: int) -> None:
    if type(value) is not bytes or not value or len(value) > maximum_bytes + _FRAME_HEADER_BYTES:
        raise ValueError("frame is invalid")
    if len(value) < _FRAME_HEADER_BYTES:
        raise ValueError("frame header is missing")
    declared = int.from_bytes(value[:_FRAME_HEADER_BYTES], "big", signed=False)
    if not 1 <= declared <= maximum_bytes or len(value) != _FRAME_HEADER_BYTES + declared:
        raise ValueError("frame length is invalid")


def _frame(payload: bytes) -> bytes:
    if type(payload) is not bytes or not 1 <= len(payload) <= _REQUEST_MAXIMUM_BYTES:
        raise ValueError("request payload is invalid")
    return len(payload).to_bytes(_FRAME_HEADER_BYTES, "big", signed=False) + payload


def _is_exact_child(value: object) -> bool:
    return all(
        callable(getattr(value, name, None))
        for name in ("fileno", "close_stream", "poll", "reap", "terminate", "kill")
    )


def _is_exact_selector(value: object) -> bool:
    return all(
        callable(getattr(value, name, None))
        for name in ("register", "unregister", "select", "close")
    )


def _transport_cancelled() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_TRANSPORT_CANCELLED",
        "Native approval bridge exchange was cancelled before helper launch.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Start a fresh approval exchange after cancellation is cleared.",
    )


def _transport_precondition() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_TRANSPORT_UNAVAILABLE",
        "Native approval bridge helper exchange cannot start from the fixed local binding.",
        exit_code=ExitCode.PRECONDITION,
        next_action="Inspect the installed approval bridge binding before a fresh review.",
    )


def _transport_unverified() -> AdhelmError:
    return AdhelmError(
        "NATIVE_APPROVAL_BRIDGE_EXCHANGE_UNVERIFIED",
        "Native approval bridge exchange outcome is unverified after helper launch.",
        exit_code=ExitCode.AMBIGUOUS,
        next_action=(
            "Inspect native approval state and start a fresh review; do not retry this exchange."
        ),
    )
