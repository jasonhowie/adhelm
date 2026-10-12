"""Default tests are hermetic and have no provider or model dependency."""

import os
import socket
import subprocess
import sys
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

import adhelm.credential_lock as credential_lock
import adhelm.release_labels as release_labels

# Test modules whose subject is release or native-host tooling that deliberately targets
# POSIX hosts and has no Windows implementation. Each entry is an explained skip on
# Windows, not a hidden failure; the tooling is not part of the shipped command line.
_WINDOWS_UNSUPPORTED_TOOLING = {
    "test_dependency_license_inventory.py": "scripts/audit_dependency_licenses.py reads the "
    "lock file and license records through POSIX descriptors (O_NONBLOCK, dir_fd); the "
    "release dependency inventory is produced on the build host",
    "test_dependency_license_inventory_ultra_review.py": "scripts/audit_dependency_licenses.py "
    "reads the lock file and license records through POSIX descriptors; the release "
    "dependency inventory is produced on the build host",
    "test_build_native_approval_artifact.py": "the macOS approval-host artifact builder "
    "refuses non-POSIX hosts with NATIVE_ARTIFACT_PLATFORM_UNSUPPORTED (ADR 0003)",
    "test_compare_native_approval_artifacts.py": "compares macOS approval-host artifacts "
    "produced by the POSIX-only builder (ADR 0003)",
    "test_native_approval_artifact_builder_ultra_review.py": "exercises the macOS "
    "approval-host artifact builder, which refuses non-POSIX hosts (ADR 0003)",
    "test_meta_pause_artifact_profile_v2_ultra_review.py": "exercises the macOS "
    "approval-host artifact builder profiles, which refuse non-POSIX hosts (ADR 0003)",
    "test_meta_pause_artifact_pair_integration.py": "builds macOS approval-host artifact "
    "pairs with the POSIX-only builder (ADR 0003)",
    "test_native_approval_paired_build.py": "drives the macOS approval-host paired build, "
    "which refuses non-POSIX hosts (ADR 0003)",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if sys.platform != "win32":
        return
    for item in items:
        reason = _WINDOWS_UNSUPPORTED_TOOLING.get(item.path.name)
        if reason is not None:
            item.add_marker(pytest.mark.skip(reason=f"O3.1: {reason}"))


@pytest.fixture(autouse=True)
def prohibit_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("Default tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)


@pytest.fixture(autouse=True)
def isolate_credential_lock_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep export-key lock creation within each hermetic test's temporary directory."""
    monkeypatch.setattr(
        credential_lock,
        "_default_lock_root",
        lambda: tmp_path / "credential-locks",
    )


@pytest.fixture
def make_link() -> Callable[[Path, Path], None]:
    """Create a symbolic link; on Windows fall back to a junction for directories.

    Windows needs Developer Mode or elevation for symbolic links. A junction is the
    reparse point an unprivileged user can create, and the code under test must
    reject it just the same.
    """

    def make(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=target.is_dir())
        except OSError:
            if sys.platform != "win32":
                raise
            if not target.is_dir():
                pytest.skip(
                    "O3.1: this Windows host cannot create file symbolic links "
                    "(needs Developer Mode or elevation)"
                )
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                check=True,
                capture_output=True,
            )

    return make


@pytest.fixture
def make_pipe(tmp_path: Path) -> Iterator[Callable[[str], Path]]:
    """Create a pipe that a reader would block on, and return its path.

    A POSIX FIFO lives at a filesystem path. A Windows named pipe lives in the
    ``\\\\.\\pipe\\`` namespace, so the returned path is there and the server end is
    kept open until the test finishes.
    """
    servers: list[int] = []

    def make(name: str) -> Path:
        if sys.platform != "win32":
            path = tmp_path / name
            os.mkfifo(path)
            return path
        import _winapi

        pipe = rf"\\.\pipe\adhelm-test-{uuid.uuid4().hex}-{name}"
        servers.append(
            _winapi.CreateNamedPipe(
                pipe,
                _winapi.PIPE_ACCESS_DUPLEX,
                _winapi.PIPE_WAIT,
                1,
                4096,
                4096,
                0,
                0,
            )
        )
        return Path(pipe)

    yield make
    if sys.platform == "win32":
        import _winapi

        for server in servers:
            _winapi.CloseHandle(server)


@pytest.fixture
def simulate_released_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lift the release-policy gate at all four call sites, as a lifted policy would.

    The 0.1.0 build refuses every remote-mutation route at the release-policy
    gate, but the code behind the gate ships and keeps its tests. The fixture
    replaces `require_writes_released` so that the CLI decorator, the retained
    workflow entries, `SafetyKernel.execute` and
    `SafetyKernel.record_attempt_recovery` all stop refusing. A test that uses
    it exercises the code behind the gate with synthetic workflow seams. It is
    a test-only substitution: nothing in the installed package, configuration,
    environment or command line can do the same, and `test_release_policy.py`
    never uses it.
    """
    monkeypatch.setattr("adhelm.release_policy.require_writes_released", lambda: None)


@pytest.fixture(autouse=True)
def no_shipped_pilot_records(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Judge every provider `EXPERIMENTAL` unless a test supplies its own pilot records.

    The label loader reads the pilot records shipped with the installation. A test
    must not depend on whichever records the owner has committed, so the shipped
    directory is withheld; the label tests substitute directories they build.
    """
    monkeypatch.setattr(release_labels, "pilot_records_directory", lambda: None)
    release_labels.pilot_record_inventory.cache_clear()
    yield
    release_labels.pilot_record_inventory.cache_clear()


@pytest.fixture
def tmp_pilot_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty pilot record directory the label loader reads in place of the shipped one."""
    directory = tmp_path / "pilots"
    directory.mkdir()
    monkeypatch.setattr(release_labels, "pilot_records_directory", lambda: directory)
    release_labels.pilot_record_inventory.cache_clear()
    return directory


@pytest.fixture
def committed_pilot_records(monkeypatch: pytest.MonkeyPatch) -> Path:
    """The pilot records committed under `docs/pilots/`, which a release actually ships.

    The autouse fixture withholds them so most tests do not depend on what the owner has
    committed. A test that must fail when a committed record changes a provider's label (the
    CHANGELOG and README label tests) uses this one.
    """
    directory = Path(__file__).resolve().parents[1] / "docs" / "pilots"
    monkeypatch.setattr(release_labels, "pilot_records_directory", lambda: directory)
    release_labels.pilot_record_inventory.cache_clear()
    return directory
