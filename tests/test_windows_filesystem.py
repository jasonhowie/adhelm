"""Native Windows tests for selected-file input, credential locks and artifact writes (O3.1).

These run only on Windows and drive the real Win32 behavior: reparse points,
named pipes, DACLs written and read with ``icacls``, directory handles that pin a
path, and a lock holder killed mid-hold. The cross-platform suites (input files,
credential lock, assets, artifacts, storage usage, guidance, Google OAuth setup)
run the same behavior on every platform; this file holds what only Windows can
express. Every provider-facing path uses fake stores and transports.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from adhelm import artifact_workflows, artifacts, assets, guidance, input_files, storage_usage
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError
from adhelm.google_oauth_setup import read_google_installed_client
from adhelm.models import OutputPreferences
from adhelm.private_directories import ensure_private_directory
from adhelm.reporting import NormalizedReport, PerformanceRow, ReportEnvelope
from adhelm.windows_acl import FILE_SUPPORTS_OPEN_BY_FILE_ID

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "O3.1: native Windows filesystem boundary. POSIX hosts run the POSIX descriptor "
        "suites for the same modules; windows_acl policy is tested everywhere."
    ),
)

if sys.platform == "win32":
    from adhelm import windows_filesystem


def icacls(*arguments: str) -> str:
    result = subprocess.run(["icacls", *arguments], capture_output=True, text=True, check=True)
    return result.stdout


def grant_everyone(path: Path) -> None:
    """Give ``Everyone`` read access: the weakest possible exposure of a private object."""
    inherit = "(OI)(CI)" if path.is_dir() else ""
    icacls(str(path), "/grant", f"*S-1-1-0:{inherit}R")


def assert_private_listing(path: Path, *, protected: bool) -> None:
    """Check ``icacls`` independently of the code under test: only the user, SYSTEM, admins."""
    listing = icacls(str(path))
    for forbidden in ("Everyone", "BUILTIN\\Users", "Authenticated Users"):
        assert forbidden not in listing
    assert os.environ["USERNAME"].lower() in listing.lower()
    assert "NT AUTHORITY\\SYSTEM" in listing
    if protected:
        assert "(I)" not in listing


def handle_count() -> int:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetProcessHandleCount.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    count = wintypes.DWORD()
    assert kernel32.GetProcessHandleCount(kernel32.GetCurrentProcess(), ctypes.byref(count))
    return count.value


def make_junction(link: Path, target: Path) -> None:
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True
    )


def test_created_state_directories_are_private_and_the_data_root_is_accepted(
    tmp_path: Path,
) -> None:
    target = tmp_path / "data" / "nested" / "leaf"
    ensure_private_directory(target)
    # The production privacy policy, not just a listing: every created directory
    # is owned by this user and names no other principal.
    for directory in (tmp_path / "data", tmp_path / "data" / "nested", target):
        windows_filesystem.open_directory(directory, private_tail=1).close()


# --- R02.1: selected regular-file input ---------------------------------------------


def test_regular_input_is_read_through_a_handle_that_is_then_released(tmp_path: Path) -> None:
    selected = tmp_path / "request.json"
    selected.write_bytes(b'{"minor_units":5}')
    assert input_files.read_json_input_file(selected) == b'{"minor_units":5}'
    selected.unlink()
    selected.write_bytes(b"{}")
    assert input_files.read_json_input_file(selected) == b"{}"


def test_every_input_outcome_releases_its_handles(
    tmp_path: Path, make_pipe: Callable[[str], Path]
) -> None:
    good = tmp_path / "good.json"
    good.write_bytes(b"{}")
    oversized = tmp_path / "large.json"
    oversized.write_bytes(b"x" * 64)
    directory = tmp_path / "folder"
    directory.mkdir()
    pipe = make_pipe("handles")

    def attempt() -> None:
        input_files.read_regular_input_file(good, maximum_bytes=64)
        for selected, bound in ((oversized, 8), (directory, 64), (pipe, 64), (tmp_path / "no", 64)):
            with pytest.raises(AdhelmError):
                input_files.read_regular_input_file(selected, maximum_bytes=bound)

    attempt()
    baseline = handle_count()
    for _ in range(40):
        attempt()
    assert handle_count() <= baseline + 2


def test_maximum_byte_boundary_is_exact(tmp_path: Path) -> None:
    selected = tmp_path / "bound.bin"
    selected.write_bytes(b"a" * 4096)
    assert input_files.read_regular_input_file(selected, maximum_bytes=4096) == b"a" * 4096
    with pytest.raises(AdhelmError) as error:
        input_files.read_regular_input_file(selected, maximum_bytes=4095)
    assert error.value.code == "INPUT_TOO_LARGE"


def test_final_component_reparse_points_are_rejected_not_followed(
    tmp_path: Path, make_link: Callable[[Path, Path], None]
) -> None:
    target = tmp_path / "private-target.json"
    target.write_bytes(b'{"secret":"never-read"}')
    link = tmp_path / "link.json"
    make_link(link, target)
    with pytest.raises(AdhelmError) as symlink:
        input_files.read_json_input_file(link)
    assert symlink.value.code == "INPUT_FILE_INVALID"
    assert "never-read" not in str(symlink.value)

    folder = tmp_path / "folder"
    folder.mkdir()
    junction = tmp_path / "junction.json"
    make_junction(junction, folder)
    with pytest.raises(AdhelmError) as junction_error:
        input_files.read_json_input_file(junction)
    assert junction_error.value.code == "INPUT_FILE_INVALID"


def test_a_junction_in_a_parent_directory_remains_usable(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "request.json").write_bytes(b"{}")
    alias = tmp_path / "alias"
    make_junction(alias, real)
    assert input_files.read_json_input_file(alias / "request.json") == b"{}"


def test_device_and_alternate_data_stream_inputs_are_rejected(tmp_path: Path) -> None:
    for selected in (Path(r"\\.\NUL"), Path("NUL")):
        with pytest.raises(AdhelmError) as device:
            input_files.read_json_input_file(selected)
        assert device.value.code == "INPUT_FILE_INVALID"

    plain = tmp_path / "request.json"
    plain.write_bytes(b"{}")
    stream = tmp_path / "request.json:hidden"
    stream.write_bytes(b'{"hidden":true}')
    with pytest.raises(AdhelmError) as alternate:
        input_files.read_json_input_file(stream)
    assert alternate.value.code == "INPUT_FILE_INVALID"
    assert input_files.read_json_input_file(plain) == b"{}"


@pytest.mark.parametrize("change", ["delete", "truncate"])
def test_removal_and_truncation_during_a_read_are_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    selected = tmp_path / "request.json"
    selected.write_bytes(b"x" * 100_000)
    real_read = os.read
    changed = False

    def changing_read(descriptor: int, count: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, count)
        if not changed:
            changed = True
            if change == "delete":
                selected.unlink()
            else:
                with selected.open("r+b") as handle:
                    handle.truncate(10)
        return chunk

    monkeypatch.setattr(input_files.os, "read", changing_read)
    with pytest.raises(AdhelmError) as error:
        input_files.read_regular_input_file(selected, maximum_bytes=1_000_000)
    assert error.value.code == "INPUT_FILE_CHANGED"


def test_replacing_a_file_that_is_being_read_is_refused_by_the_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = tmp_path / "request.json"
    selected.write_bytes(b"x" * 100_000)
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"y" * 100_000)
    real_read = os.read
    refused: list[bool] = []

    def read_then_try_to_replace(descriptor: int, count: int) -> bytes:
        chunk = real_read(descriptor, count)
        with pytest.raises(PermissionError):
            replacement.replace(selected)
        refused.append(True)
        return chunk

    monkeypatch.setattr(input_files.os, "read", read_then_try_to_replace)
    assert input_files.read_regular_input_file(selected, maximum_bytes=1_000_000) == b"x" * 100_000
    assert refused


# --- R02.2: credential lock directory, lock file and client file --------------------

HOLDER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from adhelm.credential_lock import credential_mutation_lock\n"
    "with credential_mutation_lock('reference', root_override=Path(sys.argv[1])):\n"
    "    print('held', flush=True)\n"
    "    time.sleep(120)\n"
)


def lock(root: Path, reference: str = "reference") -> None:
    with credential_mutation_lock(reference, root_override=root):
        pass


def test_lock_directory_and_file_are_private_to_the_user(tmp_path: Path) -> None:
    root = tmp_path / "state" / "locks"
    lock(root)
    assert_private_listing(root, protected=True)
    (lockfile,) = root.iterdir()
    assert_private_listing(lockfile, protected=True)
    assert (tmp_path / "state").is_dir()


def test_a_killed_holder_releases_the_lock_without_false_ownership(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(root)], stdout=subprocess.PIPE, text=True
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        with pytest.raises(AdhelmError) as busy:
            lock(root)
        assert busy.value.code == "CREDENTIAL_SETUP_BUSY"
    finally:
        holder.kill()
        holder.wait(timeout=10)
    # Windows releases a terminated process's byte-range locks asynchronously, so
    # ownership returns within a short bound rather than at the instant of exit.
    deadline = time.monotonic() + 15
    while True:
        try:
            lock(root)
            break
        except AdhelmError as error:
            assert error.code == "CREDENTIAL_SETUP_BUSY"
            assert time.monotonic() < deadline, "the killed holder's lock was never released"
            time.sleep(0.1)
    lock(root)


def test_the_lock_never_waits_when_another_holder_has_it(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    with credential_mutation_lock("reference", root_override=root):
        for _ in range(3):
            with pytest.raises(AdhelmError) as busy:
                lock(root)
            assert busy.value.code == "CREDENTIAL_SETUP_BUSY"
    lock(root)


def test_a_weak_lock_directory_or_file_is_rejected(tmp_path: Path) -> None:
    weak_root = tmp_path / "weak-root" / "locks"
    lock(weak_root)
    grant_everyone(weak_root)
    with pytest.raises(AdhelmError) as directory:
        lock(weak_root)
    assert directory.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"

    weak_file = tmp_path / "weak-file" / "locks"
    lock(weak_file)
    (lockfile,) = weak_file.iterdir()
    grant_everyone(lockfile)
    with pytest.raises(AdhelmError) as file:
        lock(weak_file)
    assert file.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"


def test_a_lock_directory_owned_by_someone_else_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "locks"
    lock(root)
    try:
        icacls(str(root), "/setowner", "NT AUTHORITY\\SYSTEM")
    except subprocess.CalledProcessError:
        pytest.skip("O3.1: changing an owner needs SeRestorePrivilege on this Windows host")
    with pytest.raises(AdhelmError) as foreign:
        lock(root)
    assert foreign.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"


def test_an_ancestor_others_can_replace_is_rejected_before_the_root_is_created(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "shared-parent"
    parent.mkdir()
    icacls(str(parent), "/grant", "*S-1-5-32-545:(OI)(CI)M")
    with pytest.raises(AdhelmError) as unsafe:
        lock(parent / "locks")
    assert unsafe.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"
    assert not (parent / "locks").exists()


def test_junction_roots_and_ancestors_are_rejected_without_touching_the_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    root_link = tmp_path / "root-link"
    make_junction(root_link, target)
    with pytest.raises(AdhelmError) as root:
        lock(root_link)
    assert root.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"

    with pytest.raises(AdhelmError) as ancestor:
        lock(root_link / "nested" / "locks")
    assert ancestor.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"
    assert list(target.iterdir()) == []


def test_a_held_lock_directory_and_its_ancestors_cannot_be_renamed(tmp_path: Path) -> None:
    root = tmp_path / "state" / "locks"
    with credential_mutation_lock("reference", root_override=root):
        with pytest.raises(PermissionError):
            os.rename(root, tmp_path / "state" / "moved")
        with pytest.raises(PermissionError):
            os.rename(tmp_path / "state", tmp_path / "moved-state")
    os.rename(root, tmp_path / "state" / "moved")


def test_lock_file_aliases_and_directories_in_its_place_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "aliased" / "locks"
    lock(root)
    (lockfile,) = root.iterdir()
    os.link(lockfile, root / "second-name")
    with pytest.raises(AdhelmError) as hardlink:
        lock(root)
    assert hardlink.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"

    other = tmp_path / "other" / "locks"
    lock(other)
    (name,) = [item.name for item in other.iterdir()]
    (other / name).unlink()
    (other / name).mkdir()
    with pytest.raises(AdhelmError) as directory:
        lock(other)
    assert directory.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"


@pytest.mark.parametrize(
    "root",
    [
        Path(r"\\localhost\C$\adhelm-locks"),
        Path(r"\\?\C:\adhelm-locks"),
        Path(r"\\.\C:\adhelm-locks"),
    ],
)
def test_network_and_device_namespace_roots_are_rejected(root: Path) -> None:
    with pytest.raises(AdhelmError) as unsafe:
        lock(root)
    assert unsafe.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"
    assert not Path(r"C:\adhelm-locks").exists()


def test_lock_handles_are_released_on_every_outcome(tmp_path: Path) -> None:
    root = tmp_path / "locks"

    def attempt() -> None:
        lock(root)
        with credential_mutation_lock("reference", root_override=root):
            with pytest.raises(AdhelmError):
                lock(root)
        with pytest.raises(AdhelmError):
            lock(tmp_path / "a" / ".." / "b")

    attempt()
    baseline = handle_count()
    for _ in range(40):
        attempt()
    assert handle_count() <= baseline + 2


def client_file(directory: Path) -> Path:
    path = directory / "client.json"
    client_id = "123456-fixture.apps.googleusercontent.com"
    path.write_text(json.dumps({"installed": {"client_id": client_id, "client_secret": "fixture"}}))
    return path


def test_a_private_client_file_reads_and_a_shared_one_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client_id = "123456-fixture.apps.googleusercontent.com"
    path = client_file(tmp_path)
    assert read_google_installed_client(path, expected_client_ref=client_id).client_ref == client_id

    grant_everyone(path)

    def never(*args: object, **kwargs: object) -> object:
        pytest.fail("A client file readable by others must be rejected before any read")

    monkeypatch.setattr("adhelm.google_oauth_setup.os.fdopen", never)
    with pytest.raises(AdhelmError) as error:
        read_google_installed_client(path, expected_client_ref=client_id)
    assert error.value.code == "GOOGLE_OAUTH_CLIENT_FILE_INVALID"
    assert "fixture" not in str(error.value)


# --- R02.3: asset vault, artifacts, storage observation and guidance ----------------

DATA = b"windows asset bytes"
DIGEST = hashlib.sha256(DATA).hexdigest()


def test_vault_directories_and_blob_are_private_and_the_roundtrip_is_stable(
    tmp_path: Path,
) -> None:
    blob = assets.store_image(tmp_path, DATA, DIGEST)
    assert assets.read_image(tmp_path, DIGEST) == DATA
    assert assets.store_image(tmp_path, DATA, DIGEST) == blob
    assert_private_listing(tmp_path / "assets", protected=True)
    assert_private_listing(blob.parent, protected=True)
    assert_private_listing(blob, protected=True)
    assert list(blob.parent.iterdir()) == [blob]


def test_weak_vault_directories_and_blobs_are_rejected_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = assets.store_image(tmp_path, DATA, DIGEST)
    grant_everyone(blob.parent)
    with pytest.raises(AdhelmError) as directory:
        assets.read_image(tmp_path, DIGEST)
    assert directory.value.code == "UNSAFE_ASSET_PATH"
    icacls(str(blob.parent), "/remove", "*S-1-1-0")
    assert assets.read_image(tmp_path, DIGEST) == DATA

    grant_everyone(blob)

    def never(*args: object, **kwargs: object) -> int:
        pytest.fail("A blob readable by others must be rejected before it is opened")

    monkeypatch.setattr(windows_filesystem, "open_regular_file", never)
    for operation in (
        lambda: assets.read_image(tmp_path, DIGEST),
        lambda: assets.store_image(tmp_path, DATA, DIGEST),
    ):
        with pytest.raises(AdhelmError) as exposed:
            operation()
        assert exposed.value.code == "UNSAFE_ASSET_PATH"


def test_hard_linked_and_reparse_vault_objects_are_rejected(tmp_path: Path) -> None:
    blob = assets.store_image(tmp_path, DATA, DIGEST)
    os.link(blob, blob.parent / "second-name")
    with pytest.raises(AdhelmError) as hardlinked:
        assets.read_image(tmp_path, DIGEST)
    assert hardlinked.value.code == "UNSAFE_ASSET_PATH"

    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "linked-root"
    make_junction(link, outside)
    with pytest.raises(AdhelmError) as junction:
        assets.store_image(link, DATA, DIGEST)
    assert junction.value.code == "UNSAFE_ASSET_PATH"
    assert list(outside.iterdir()) == []


def test_the_vault_cannot_be_renamed_while_a_read_is_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blob = assets.store_image(tmp_path, DATA, DIGEST)
    real_read = os.read

    def read_then_try_to_swap(descriptor: int, count: int) -> bytes:
        chunk = real_read(descriptor, count)
        with pytest.raises(PermissionError):
            os.rename(blob.parent, blob.parent.with_name("moved"))
        with pytest.raises(PermissionError):
            os.rename(tmp_path / "assets", tmp_path / "moved-assets")
        return chunk

    monkeypatch.setattr(assets.os, "read", read_then_try_to_swap)
    assert assets.read_image(tmp_path, DIGEST) == DATA
    assert blob.is_file()


def report() -> NormalizedReport:
    return NormalizedReport(
        envelope=ReportEnvelope(
            report_id="report-1",
            provider="google_ads",
            source_account_ref="local-account-1",
            query_contract_hash="a" * 64,
            window_start=date(2026, 1, 1),
            window_end=date(2026, 1, 2),
            source_timezone="UTC",
            report_timezone="UTC",
            currency="USD",
            attribution_model="provider",
            observed_at=datetime(2026, 1, 3, tzinfo=UTC),
            data_quality_statuses=["COMPLETE"],
            provider_api_version="v1",
            connector_version="v1",
        ),
        performance_rows=[
            PerformanceRow(
                report_id="report-1",
                resource_level="CAMPAIGN",
                metric_grain="CAMPAIGN",
                resource_type="campaign",
                resource_ref="campaign-local-id",
                dimension_type="placement",
                dimension_value="value",
                impressions=1,
                clicks=0,
                spend={"minor_units": 0, "currency": "USD"},
            )
        ],
    )


def decision(destination: Path) -> artifacts.ArtifactDecision:
    return artifacts.resolve_artifact_policy(
        artifacts.ArtifactContext(
            workflow="report",
            stage="completion",
            purpose="result",
            provider="google_ads",
            profile_id="read-profile",
        ),
        OutputPreferences(),
        task_override=artifacts.ArtifactTaskOverride(
            formats=("json",), output_directory=str(destination), purpose="result"
        ),
    )


def generate(destination: Path) -> artifacts.ArtifactMetadata:
    retained = artifacts.RetainedArtifactInput(report(), "b" * 64, "c" * 64)
    return artifacts.generate_artifacts(retained, decision(destination), key=b"k" * 32)[0]


def test_artifacts_are_private_and_the_destination_is_held_while_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "views"
    destination.mkdir()
    real_fsync = os.fsync
    attempts: list[str] = []

    def fsync_then_try_to_swap(descriptor: int) -> None:
        real_fsync(descriptor)
        with pytest.raises(PermissionError):
            os.rename(destination, tmp_path / "views-moved")
        attempts.append("blocked")

    monkeypatch.setattr(artifacts.os, "fsync", fsync_then_try_to_swap)
    metadata = generate(destination)
    assert attempts == ["blocked"]
    assert_private_listing(metadata.path, protected=True)
    assert [item.name for item in destination.iterdir()] == [metadata.path.name]


def test_an_exposed_existing_artifact_is_not_reused_or_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "views"
    destination.mkdir()
    first = generate(destination)
    grant_everyone(first.path)

    def never(*args: object, **kwargs: object) -> int:
        pytest.fail("An artifact readable by others must be rejected before it is read")

    monkeypatch.setattr(windows_filesystem, "open_regular_file", never)
    with pytest.raises(artifacts.ArtifactGenerationError) as error:
        generate(destination)
    assert error.value.failure_code == "UNSAFE_ARTIFACT_DESTINATION"
    assert error.value.completed == ()


def test_reparse_point_destinations_are_rejected_without_writing_through(
    tmp_path: Path, make_link: Callable[[Path, Path], None]
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    make_link(link, outside)
    with pytest.raises(AdhelmError, match="symbolic"):
        decision(link)
    assert list(outside.iterdir()) == []
    ancestor = tmp_path / "ancestor"
    make_junction(ancestor, outside)
    (outside / "views").mkdir()
    with pytest.raises(AdhelmError, match="symbolic"):
        decision(ancestor / "views")


def test_the_derived_cache_directory_is_private_and_a_weak_one_is_rejected(
    tmp_path: Path,
) -> None:
    created = artifact_workflows._open_cache_directory(tmp_path)
    try:
        with pytest.raises(PermissionError):
            os.rename(tmp_path / "artifact-cache", tmp_path / "moved-cache")
    finally:
        created.close()
    assert_private_listing(tmp_path / "artifact-cache", protected=True)

    grant_everyone(tmp_path / "artifact-cache")
    with pytest.raises(AdhelmError) as weak:
        artifact_workflows._open_cache_directory(tmp_path)
    assert weak.value.code == "UNSAFE_ARTIFACT_CACHE"


def test_the_cache_directory_keeps_every_ancestor_pinned_until_it_is_closed(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state" / "data"
    root.mkdir(parents=True)
    cache = artifact_workflows._open_cache_directory(root)
    try:
        # The root's own pin was released inside _open_cache_directory, yet neither the
        # cache root nor any directory above it can be renamed: the cache holds the chain.
        with pytest.raises(PermissionError):
            os.rename(root, tmp_path / "state" / "moved-data")
        with pytest.raises(PermissionError):
            os.rename(root.parent, tmp_path / "moved-state")
        with pytest.raises(PermissionError):
            os.rename(tmp_path, tmp_path.with_name(tmp_path.name + "-moved"))
    finally:
        cache.close()
    os.rename(root, tmp_path / "state" / "moved-data")
    os.rename(root.parent, tmp_path / "moved-state")


def test_a_child_directory_keeps_its_parents_pinned_after_the_parent_is_closed(
    tmp_path: Path,
) -> None:
    (tmp_path / "parent" / "child").mkdir(parents=True)
    parent = windows_filesystem.open_directory(tmp_path / "parent")
    child = parent.open_child_directory("child")
    parent.close()
    parent.close()  # closing twice is harmless
    try:
        with pytest.raises(PermissionError):
            os.rename(tmp_path / "parent", tmp_path / "moved-parent")
        with pytest.raises(PermissionError):
            os.rename(tmp_path / "parent" / "child", tmp_path / "parent" / "moved-child")
    finally:
        child.close()
    os.rename(tmp_path / "parent", tmp_path / "moved-parent")


def test_a_failed_handle_close_is_reported_after_every_handle_has_been_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    pinned = windows_filesystem.open_directory(tmp_path / "a" / "b")
    owned = list(pinned._handles)
    assert len(owned) >= 3
    real_close = windows_filesystem._CloseHandle
    attempted: list[int] = []

    def fail_the_first_close(handle: int) -> int:
        real_close(handle)
        attempted.append(handle)
        return 0 if len(attempted) == 1 else 1

    monkeypatch.setattr(windows_filesystem, "_CloseHandle", fail_the_first_close)
    with pytest.raises(OSError):
        pinned.close()
    assert sorted(attempted) == sorted(owned)
    pinned.close()  # already closed: nothing more to report
    monkeypatch.undo()
    # Nothing is left pinned, so the whole chain can be renamed again.
    os.rename(tmp_path / "a", tmp_path / "renamed")


def test_a_failed_close_does_not_replace_the_error_that_ended_the_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_close = windows_filesystem._CloseHandle

    def close_then_fail(handle: int) -> int:
        real_close(handle)
        return 0

    monkeypatch.setattr(windows_filesystem, "_CloseHandle", close_then_fail)
    with pytest.raises(RuntimeError, match="operation"):
        with windows_filesystem.open_directory(tmp_path):
            raise RuntimeError("operation failure")
    with pytest.raises(OSError, match="could not be closed"):
        with windows_filesystem.open_directory(tmp_path):
            pass


def track_directory_closes(monkeypatch: pytest.MonkeyPatch) -> tuple[set[int], list[int]]:
    """Make every pinned-directory close really happen and then report failure.

    Returns the directory handles opened and the directory handles closed, so a test
    can require that a failed close still closed everything that was opened.
    """
    real_open = windows_filesystem._open_directory_handle
    real_close = windows_filesystem._CloseHandle
    opened: set[int] = set()
    closed: list[int] = []

    def record(path: str, *, create: bool) -> int:
        handle = real_open(path, create=create)
        opened.add(handle)
        return handle

    def close_then_fail(handle: int) -> int:
        real_close(handle)
        if handle in opened:
            closed.append(handle)
        return 0

    monkeypatch.setattr(windows_filesystem, "_open_directory_handle", record)
    monkeypatch.setattr(windows_filesystem, "_CloseHandle", close_then_fail)
    return opened, closed


def assert_close_failure_replaces_the_refusal(
    raised: pytest.ExceptionInfo[OSError], cause: type[BaseException]
) -> None:
    # A plain OSError, not the refusal: callers classify it as a storage failure.
    assert type(raised.value) is OSError
    assert "could not be closed" in str(raised.value)
    assert isinstance(raised.value.__cause__, cause)


@pytest.mark.parametrize(
    ("fault", "cause"),
    [
        ("reparse-point", "UnsafeObjectError"),
        ("not-private", "UnsafeObjectError"),
        ("missing", "FileNotFoundError"),
    ],
)
def test_a_failed_close_on_an_open_error_path_replaces_the_refusal_and_chains_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, cause: str
) -> None:
    expected = {
        "UnsafeObjectError": windows_filesystem.UnsafeObjectError,
        "FileNotFoundError": FileNotFoundError,
    }[cause]
    base = tmp_path / "base"
    base.mkdir()
    target = base / "leaf"
    private_tail = 0
    if fault == "reparse-point":
        outside = tmp_path / "outside"
        outside.mkdir()
        make_junction(target, outside)
    elif fault == "not-private":
        target.mkdir()
        grant_everyone(target)
        private_tail = 1
    with pytest.raises(expected):
        windows_filesystem.open_directory(target, private_tail=private_tail)

    opened, closed = track_directory_closes(monkeypatch)
    with pytest.raises(OSError) as raised:
        windows_filesystem.open_directory(target, private_tail=private_tail)
    assert_close_failure_replaces_the_refusal(raised, expected)
    assert opened
    assert set(closed) == opened
    assert len(closed) == len(opened)
    monkeypatch.undo()
    # Every handle was really closed: nothing is left pinning the chain.
    os.rename(base, tmp_path / "renamed")


@pytest.mark.parametrize(
    ("fault", "plain_code", "failed_close_code"),
    [
        ("reparse-point", "UNSAFE_ASSET_PATH", "ASSET_STORAGE_FAILED"),
        ("missing", "ASSET_NOT_AVAILABLE", "ASSET_STORAGE_FAILED"),
    ],
)
def test_a_failed_close_turns_an_asset_refusal_into_a_storage_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    plain_code: str,
    failed_close_code: str,
) -> None:
    if fault == "reparse-point":
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "linked-root"
        make_junction(root, outside)
    else:
        root = tmp_path / "absent"
    with pytest.raises(AdhelmError) as plain:
        assets.read_image(root, DIGEST)
    assert plain.value.code == plain_code

    opened, closed = track_directory_closes(monkeypatch)
    with pytest.raises(AdhelmError) as failed:
        assets.read_image(root, DIGEST)
    assert failed.value.code == failed_close_code
    assert opened
    assert set(closed) == opened


@pytest.mark.parametrize("fault", ["reparse-point", "missing"])
def test_a_failed_close_turns_a_storage_root_refusal_into_an_observation_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    if fault == "reparse-point":
        outside = tmp_path / "outside"
        outside.mkdir()
        root = tmp_path / "linked-root"
        make_junction(root, outside)
    else:
        root = tmp_path / "absent"
    with pytest.raises(AdhelmError) as plain:
        storage_usage.observe_local_storage(root)
    assert plain.value.code == "STORAGE_ROOT_INVALID"

    opened, closed = track_directory_closes(monkeypatch)
    with pytest.raises(AdhelmError) as failed:
        storage_usage.observe_local_storage(root)
    assert failed.value.code == "STORAGE_OBSERVATION_FAILED"
    assert opened
    assert set(closed) == opened


@pytest.mark.parametrize("fault", ["reparse-point", "not-private"])
def test_a_failed_close_on_the_child_open_error_path_replaces_the_refusal_and_chains_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    private = False
    if fault == "reparse-point":
        outside = tmp_path / "outside"
        outside.mkdir()
        make_junction(base / "leaf", outside)
    else:
        (base / "leaf").mkdir()
        grant_everyone(base / "leaf")
        private = True
    parent = windows_filesystem.open_directory(base)
    try:
        with pytest.raises(windows_filesystem.UnsafeObjectError):
            parent.open_child_directory("leaf", private=private)

        opened, closed = track_directory_closes(monkeypatch)
        with pytest.raises(OSError) as raised:
            parent.open_child_directory("leaf", private=private)
        assert_close_failure_replaces_the_refusal(raised, windows_filesystem.UnsafeObjectError)
        assert len(opened) == 1
        assert closed == list(opened)
        monkeypatch.undo()
        # The failed child took no reference: the parent is still open and still pinned.
        assert parent._references == 1
        assert parent.current_state().is_directory
        with pytest.raises(PermissionError):
            os.rename(base, tmp_path / "renamed")
    finally:
        parent.close()
    os.rename(base, tmp_path / "renamed")


def test_a_failed_close_does_not_change_the_cache_error_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = artifact_workflows._open_cache_directory(tmp_path)
    created.close()
    grant_everyone(tmp_path / "artifact-cache")
    with pytest.raises(AdhelmError) as plain:
        artifact_workflows._open_cache_directory(tmp_path)
    assert plain.value.code == "UNSAFE_ARTIFACT_CACHE"

    opened, closed = track_directory_closes(monkeypatch)
    with pytest.raises(AdhelmError) as failed:
        artifact_workflows._open_cache_directory(tmp_path)
    assert failed.value.code == "UNSAFE_ARTIFACT_CACHE"
    assert opened
    assert set(closed) == opened


def test_a_closed_pinned_directory_refuses_every_operation(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    (folder / "child").mkdir(parents=True)
    (folder / "file").write_bytes(b"data")
    pinned = windows_filesystem.open_directory(folder)
    child = pinned.open_child_directory("child")
    pinned.close()
    operations: list[Callable[[], object]] = [
        lambda: pinned.stat("file"),
        lambda: pinned.lstat("file"),
        lambda: pinned.open_file("file"),
        lambda: pinned.create_file("new"),
        lambda: pinned.open_lock_file("lock"),
        lambda: pinned.link("file", "linked"),
        lambda: pinned.replace("file", "other"),
        lambda: pinned.unlink("file"),
        pinned.names,
        pinned.current_state,
        pinned.directory_stat,
        lambda: pinned.open_child_directory("child"),
    ]
    try:
        for operation in operations:
            with pytest.raises(windows_filesystem.UnsafeObjectError):
                operation()
        # Nothing was created, linked, replaced or removed by a refused operation.
        assert sorted(entry.name for entry in folder.iterdir()) == ["child", "file"]
        # The child that outlives its closed parent keeps working and keeps the chain pinned.
        assert child.current_state().is_directory
        with pytest.raises(PermissionError):
            os.rename(folder, tmp_path / "moved")
    finally:
        child.close()
    os.rename(folder, tmp_path / "moved")


def test_storage_observation_counts_reparse_points_without_following_them(
    tmp_path: Path, make_link: Callable[[Path, Path], None]
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private-name.bin").write_bytes(b"sensitive" * 10)
    (root / "local.bin").write_bytes(b"12345")
    make_junction(root / "junction", outside)
    make_link(root / "link", outside)
    result = storage_usage.observe_local_storage(root)
    assert result["total_apparent_bytes"] == 5
    assert result["categories"]["other"]["symlink_count"] == 2
    assert result["entry_count"] == 3
    assert "private-name" not in json.dumps(result)


def test_storage_observation_never_reads_file_contents_and_closes_every_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The scan does open an attribute-only handle (FILE_READ_ATTRIBUTES, no read access)
    # to every child to inspect it without following a reparse point. What it must never
    # do is open a file for reading or read a byte of content.
    for name in ("one", "two/three", "two/four/five"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"data")

    def never(*args: object, **kwargs: object) -> int:
        pytest.fail("File contents must not be read for a metadata observation")

    monkeypatch.setattr(windows_filesystem, "open_regular_file", never)
    monkeypatch.setattr(os, "read", never)
    storage_usage.observe_local_storage(tmp_path)
    baseline = handle_count()
    for _ in range(20):
        storage_usage.observe_local_storage(tmp_path)
    assert handle_count() <= baseline + 2


def test_the_observed_root_cannot_be_renamed_during_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    (root / "child").mkdir(parents=True)
    (root / "child" / "file").write_bytes(b"1")
    original = storage_usage._scan_windows
    swaps: list[str] = []

    def scan_then_try_to_swap(*args: object) -> object:
        result = original(*args)  # type: ignore[arg-type]
        with pytest.raises(PermissionError):
            os.rename(root, tmp_path / "former-root")
        swaps.append("blocked")
        return result

    monkeypatch.setattr(storage_usage, "_scan_windows", scan_then_try_to_swap)
    assert storage_usage.observe_local_storage(root)["entry_count"] == 2
    assert swaps == ["blocked", "blocked"]


def test_guidance_traversal_rejects_reparse_points_and_allows_parent_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_root = tmp_path / "real" / "guidance"
    skill = real_root / "ad-program-optimization"
    skill.mkdir(parents=True)
    source = guidance._bundle_root() / "ad-program-optimization" / "SKILL.md"
    (skill / "SKILL.md").write_bytes(source.read_bytes())

    alias = tmp_path / "alias"
    make_junction(alias, tmp_path / "real")
    monkeypatch.setattr(guidance, "_bundle_root", lambda: alias / "guidance")
    assert guidance.guidance_document("ad-program-optimization")["section"] == "main"

    root_link = tmp_path / "root-link"
    make_junction(root_link, real_root)
    monkeypatch.setattr(guidance, "_bundle_root", lambda: root_link)
    with pytest.raises(AdhelmError, match="symbolic links"):
        guidance.guidance_document("ad-program-optimization")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(b"not a bundled skill")
    swapped = tmp_path / "swapped"
    (swapped).mkdir()
    make_junction(swapped / "ad-program-optimization", outside)
    monkeypatch.setattr(guidance, "_bundle_root", lambda: swapped)
    with pytest.raises(AdhelmError, match="symbolic links"):
        guidance.guidance_document("ad-program-optimization")


# --- Stable identity: a volume or object without a trustworthy file ID is refused -------


def break_identity(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    if mode == "unsupported-volume":
        # What FAT and exFAT report: no support for opening by file ID.
        monkeypatch.setattr(windows_filesystem, "_volume_flags", lambda handle: 0)
        return
    real_query = windows_filesystem._query

    def zero_file_id(handle: int, information_class: int, structure: ctypes.Structure) -> None:
        real_query(handle, information_class, structure)
        if isinstance(structure, windows_filesystem._IdInfo):
            structure.FileId[:] = [0] * 16

    monkeypatch.setattr(windows_filesystem, "_query", zero_file_id)


IDENTITY_FAILURES = ["unsupported-volume", "zero-file-id"]


def test_the_real_volume_reports_a_trustworthy_identity(tmp_path: Path) -> None:
    state = windows_filesystem.path_state(str(tmp_path))
    assert any(state.file_id) and state.volume_serial != 0
    handle = windows_filesystem._open_any(str(tmp_path))
    try:
        assert windows_filesystem._volume_flags(handle) & FILE_SUPPORTS_OPEN_BY_FILE_ID
    finally:
        windows_filesystem._close_handle(handle)


@pytest.mark.parametrize("mode", IDENTITY_FAILURES)
def test_selected_input_without_a_trustworthy_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    selected = tmp_path / "request.json"
    selected.write_bytes(b"{}")
    break_identity(monkeypatch, mode)
    with pytest.raises(AdhelmError) as error:
        input_files.read_json_input_file(selected)
    assert error.value.code == "INPUT_FILE_INVALID"


@pytest.mark.parametrize("mode", IDENTITY_FAILURES)
def test_the_asset_vault_without_a_trustworthy_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    assets.store_image(tmp_path, DATA, DIGEST)
    break_identity(monkeypatch, mode)
    with pytest.raises(AdhelmError) as read:
        assets.read_image(tmp_path, DIGEST)
    assert read.value.code == "UNSAFE_ASSET_PATH"
    with pytest.raises(AdhelmError) as write:
        assets.store_image(tmp_path / "other", DATA, DIGEST)
    assert write.value.code == "UNSAFE_ASSET_PATH"
    assert not (tmp_path / "other").exists()


@pytest.mark.parametrize("mode", IDENTITY_FAILURES)
def test_an_artifact_destination_without_a_trustworthy_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    destination = tmp_path / "views"
    destination.mkdir()
    retained = artifacts.RetainedArtifactInput(report(), "b" * 64, "c" * 64)
    resolved = decision(destination)
    break_identity(monkeypatch, mode)
    with pytest.raises(AdhelmError) as error:
        artifacts.generate_artifacts(retained, resolved, key=b"k" * 32)
    assert error.value.code == "UNSAFE_ARTIFACT_DESTINATION"
    assert list(destination.iterdir()) == []


@pytest.mark.parametrize("mode", IDENTITY_FAILURES)
def test_storage_observation_without_a_trustworthy_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    (tmp_path / "file").write_bytes(b"1")
    break_identity(monkeypatch, mode)
    with pytest.raises(AdhelmError) as error:
        storage_usage.observe_local_storage(tmp_path)
    assert error.value.code == "STORAGE_ROOT_INVALID"


@pytest.mark.parametrize("mode", IDENTITY_FAILURES)
def test_a_credential_lock_without_a_trustworthy_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    break_identity(monkeypatch, mode)
    with pytest.raises(AdhelmError) as error:
        with credential_mutation_lock("reference", root_override=tmp_path / "locks"):
            pytest.fail("The lock must not be taken")
    assert error.value.code == "UNSAFE_CREDENTIAL_LOCK_PATH"


@pytest.mark.parametrize("mode", IDENTITY_FAILURES)
def test_a_google_client_file_without_a_trustworthy_identity_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    # The reader never compares identity, yet it reads state, so a client file on
    # FAT or exFAT is refused like every other disk object.
    client_id = "123456-fixture.apps.googleusercontent.com"
    path = client_file(tmp_path)
    assert read_google_installed_client(path, expected_client_ref=client_id).client_ref == client_id
    break_identity(monkeypatch, mode)
    with pytest.raises(AdhelmError) as error:
        read_google_installed_client(path, expected_client_ref=client_id)
    assert error.value.code == "GOOGLE_OAUTH_CLIENT_FILE_INVALID"
    assert "fixture" not in str(error.value)


@pytest.mark.parametrize("mode", IDENTITY_FAILURES)
def test_installed_guidance_without_a_trustworthy_identity_is_refused(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    assert guidance.guidance_document("ad-program-optimization")["section"] == "main"
    break_identity(monkeypatch, mode)
    with pytest.raises(AdhelmError) as error:
        guidance.guidance_document("ad-program-optimization")
    assert error.value.code == "GUIDANCE_INVALID"


def test_a_failing_pinned_close_makes_the_lock_report_an_unverified_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "locks"
    real_open = windows_filesystem._open_directory_handle
    opened: set[int] = set()

    def record(path: str, *, create: bool) -> int:
        handle = real_open(path, create=create)
        opened.add(handle)
        return handle

    real_close = windows_filesystem._CloseHandle
    failed: list[int] = []

    def fail_a_directory_close(handle: int) -> int:
        closed = real_close(handle)
        if handle in opened and not failed:
            failed.append(handle)
            return 0
        return int(closed)

    monkeypatch.setattr(windows_filesystem, "_open_directory_handle", record)
    monkeypatch.setattr(windows_filesystem, "_CloseHandle", fail_a_directory_close)
    with pytest.raises(AdhelmError) as cleanup:
        with credential_mutation_lock("reference", root_override=root):
            pass
    assert failed
    assert cleanup.value.code == "CREDENTIAL_LOCAL_CHANGE_UNVERIFIED"
    assert cleanup.value.exit_code == 6

    # The operation's own error is never masked by the cleanup failure.
    failed.clear()
    with pytest.raises(RuntimeError, match="operation failure"):
        with credential_mutation_lock("other-reference", root_override=root):
            raise RuntimeError("operation failure")
    assert failed
