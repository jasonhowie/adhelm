"""Fake-only coverage for the bounded native artifact pair comparator."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tarfile
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

_EPOCH = 1_725_000_000
_COMMIT = "a" * 40
_ARCHIVE = "adhelm-macos-installed-approval-host-unsigned.tar"
_APP = "AdHelmApprovalHost.app"
_HOST = "adhelm-macos-installed-approval-host"
_CLIENT = "adhelm-macos-approval-client"
_META = "adhelm-macos-meta-campaign-pause-approval-client"
_WHEEL = "adhelm-0.1.0-py3-none-any.whl"


@cache
def comparator() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/compare_native_approval_artifacts.py"
    spec = importlib.util.spec_from_file_location("compare_native_approval_artifacts_test", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
    )


def _contract(access_group: str | None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "host_ref": "host:synthetic",
        "signing_key_ref": "key:synthetic",
        "mach_service_name": "com.aptsonic.adhelm.approvalhost",
        "client_bundle_identifier": "com.aptsonic.adhelm.client",
        "client_team_identifier": "ABCDE12345",
        "host_bundle_identifier": "com.aptsonic.adhelm.host",
        "host_team_identifier": "ABCDE12345",
        "signing_key_application_tag_hex": "00616263ff",
        "signing_key_access_group": access_group,
        "key_storage": "KEYCHAIN_P256",
        "provenance_service": "com.aptsonic.adhelm.provenance",
        "provenance_account": "approval-host-v1",
        "maximum_challenge_bytes": 65_536,
        "maximum_challenge_lifetime_seconds": 300,
        "maximum_issued_at_skew_seconds": 60,
        "maximum_subplans": 6,
        "maximum_policy_items": 64,
    }


def _layout(*, profile: str, access_group: str | None) -> tuple[tuple[str, ...], ...]:
    files: set[tuple[str, ...]] = {
        (_APP, "Contents", "Info.plist"),
        (_APP, "Contents", "Library", "LaunchAgents", "com.aptsonic.adhelm.approvalhost.plist"),
        (_APP, "Contents", "MacOS", _HOST),
        (_APP, "Contents", "MacOS", _CLIENT),
        (_WHEEL,),
    }
    if access_group is not None:
        files.add(("AdHelmApprovalHost.entitlements",))
    if profile == "META_CAMPAIGN_PAUSE_V2":
        files.add((_APP, "Contents", "MacOS", _META))
    return tuple(sorted(files))


def _archive_members(files: tuple[tuple[str, ...], ...]) -> tuple[tuple[str, ...], ...]:
    directories = (
        (_APP,),
        (_APP, "Contents"),
        (_APP, "Contents", "Library"),
        (_APP, "Contents", "Library", "LaunchAgents"),
        (_APP, "Contents", "MacOS"),
    )
    return tuple(sorted((*directories, *files, ("manifest.json",))))


def _write_file(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    path.chmod(mode)


def _write_artifact(
    output: Path,
    *,
    profile: str = "V1",
    access_group: str | None = None,
    salt: bytes = b"",
) -> None:
    output.mkdir(mode=0o700)
    contract = _contract(access_group)
    files = _layout(profile=profile, access_group=access_group)
    payloads = {
        (_APP, "Contents", "Info.plist"): b"plist" + salt,
        (
            _APP,
            "Contents",
            "Library",
            "LaunchAgents",
            "com.aptsonic.adhelm.approvalhost.plist",
        ): b"launch" + salt,
        (_APP, "Contents", "MacOS", _HOST): b"host" + salt,
        (_APP, "Contents", "MacOS", _CLIENT): b"client" + salt,
        (_WHEEL,): b"wheel" + salt,
        ("AdHelmApprovalHost.entitlements",): b"entitlements" + salt,
        (_APP, "Contents", "MacOS", _META): b"meta-client" + salt,
    }
    for relative in files:
        _write_file(
            output.joinpath(*relative),
            payloads[relative],
            0o755 if relative[-1] in {_HOST, _CLIENT, _META} else 0o644,
        )
    components = {"/".join(relative): _sha256(payloads[relative]) for relative in files}
    contract_module = comparator()._CONTRACT
    checked_contract = contract_module.parse_native_approval_artifact_contract(contract)
    descriptor = contract_module.native_approval_artifact_descriptor(checked_contract)
    metadata_digest = descriptor.metadata_digest
    protocol_digest = descriptor.protocol_configuration_digest
    descriptor_digest = descriptor.digest
    metadata: dict[str, object] = {
        "contract": checked_contract.to_data(),
        "canonical_contract_sha256": _sha256(
            contract_module.canonical_native_approval_artifact_contract(checked_contract)
        ),
        "generated_source_sha256": "e" * 64,
        "generated_python_source_sha256": "f" * 64,
        "native_descriptor": descriptor.to_data(),
        "native_descriptor_sha256": descriptor_digest,
        "native_descriptor_digest": descriptor_digest,
        "metadata_digest": metadata_digest,
        "protocol_configuration_digest": protocol_digest,
    }
    target = {
        "architecture": "arm64",
        "platform": "macos",
        "minimum_os_version": "13.0.0",
        "sdk_version": "26.2.0",
    }
    manifest: dict[str, object] = {
        "schema_version": 1,
        "artifact_status": "UNSIGNED_UNVERIFIED",
        "scope": "UNSIGNED_DEVELOPMENT_ARTIFACT_NOT_INSTALLED_OR_APPROVAL_AUTHORITY",
        "source": {
            "commit": _COMMIT,
            "git_archive_sha256": "1" * 64,
            "source_date_epoch": _EPOCH,
            "development_version": "0.1.0",
            "bundle_short_version": "0.1.0",
            "bundle_version": "1",
        },
        "toolchain": {"effective_client_binary_target": target},
        "python_build": {"profile": "synthetic"},
        "metadata": metadata,
        "components": dict(sorted(components.items())),
        "developer_identity_signed": False,
        "developer_identity_signing_status": "NOT_VERIFIED",
        "compiler_signature_status": "NOT_INSPECTED",
        "notarized": False,
        "registered": False,
        "registration_status": "NOT_VERIFIED",
        "identity_authorized": False,
        "identity_authorization_status": "NOT_VERIFIED",
        "host_started": False,
        "client_started": False,
        "keychain_access_group_template_only": access_group is not None,
    }
    if profile == "META_CAMPAIGN_PAUSE_V2":
        helper = "/".join((_APP, "Contents", "MacOS", _META))
        manifest["meta_campaign_pause_approval_bridge_v2"] = {
            "profile": profile,
            "scope": "EXPLICIT_BUILD_PROFILE_STATIC_BINDING_NOT_INSTALLED_OR_APPROVAL_AUTHORITY",
            "generated_python_binding_sha256": "2" * 64,
            "generated_native_route_binding_sha256": "3" * 64,
            "metadata_digest": metadata_digest,
            "protocol_configuration_digest": protocol_digest,
            "helper_relative_path": helper,
            "helper_sha256": components[helper],
            "helper_binary_target": target,
        }
    manifest_bytes = _canonical(manifest)
    _write_file(output / "manifest.json", manifest_bytes, 0o644)
    with tarfile.open(output / _ARCHIVE, "w", format=tarfile.GNU_FORMAT) as archive:
        for relative in _archive_members(files):
            name = "/".join(relative)
            directory = relative[-1] in {
                "AdHelmApprovalHost.app",
                "Contents",
                "Library",
                "LaunchAgents",
                "MacOS",
            }
            entry = tarfile.TarInfo(name + ("/" if directory else ""))
            entry.uid = 0
            entry.gid = 0
            entry.uname = ""
            entry.gname = ""
            entry.mtime = _EPOCH
            entry.mode = 0o755 if directory or relative[-1] in {_HOST, _CLIENT, _META} else 0o644
            if directory:
                entry.type = tarfile.DIRTYPE
                archive.addfile(entry)
            else:
                data = (
                    (output / "manifest.json").read_bytes()
                    if relative == ("manifest.json",)
                    else payloads[relative]
                )
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
    (output / _ARCHIVE).chmod(0o600)


@pytest.mark.parametrize(
    ("profile", "access_group", "expected_files"),
    [
        ("V1", None, 7),
        ("V1", "ABCDE12345.com.aptsonic.adhelm.host", 8),
        ("META_CAMPAIGN_PAUSE_V2", None, 8),
        ("META_CAMPAIGN_PAUSE_V2", "ABCDE12345.com.aptsonic.adhelm.host", 9),
    ],
)
def test_compares_closed_profile_trees_and_writes_new_report(
    tmp_path: Path, profile: str, access_group: str | None, expected_files: int
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_artifact(first, profile=profile, access_group=access_group)
    _write_artifact(second, profile=profile, access_group=access_group)
    report = tmp_path / "report.json"

    result = comparator().compare_native_approval_artifacts(first, second, report_path=report)

    assert result["profile"] == profile
    assert result["source"]["commit"] == _COMMIT
    assert len(result["files"]) == expected_files
    assert result["archive"]["identical"] is True
    assert result["archive"]["member_count"] == expected_files + 4
    assert json.loads(report.read_bytes()) == result
    assert comparator().compare_native_approval_artifacts(first, second) == result
    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="OUTPUT_EXISTS"):
        comparator().compare_native_approval_artifacts(first, second, report_path=report)


def test_rejects_profile_substitution_and_different_valid_component_bytes(tmp_path: Path) -> None:
    first, meta = tmp_path / "first", tmp_path / "meta"
    _write_artifact(first)
    _write_artifact(meta, profile="META_CAMPAIGN_PAUSE_V2")
    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="MISMATCH"):
        comparator().compare_native_approval_artifacts(first, meta)

    changed = tmp_path / "changed"
    _write_artifact(changed, salt=b"different")
    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="MISMATCH"):
        comparator().compare_native_approval_artifacts(first, changed)


def test_rejects_symlink_hardlink_unexpected_file_and_bad_tar_header(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_artifact(first)
    _write_artifact(second)
    target = first / _APP / "Contents" / "MacOS" / _HOST
    target.unlink()
    target.symlink_to("missing")
    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
        comparator().compare_native_approval_artifacts(first, second)

    first.unlink(missing_ok=True) if first.is_file() else None
    first = tmp_path / "hardlinked"
    _write_artifact(first)
    host = first / _APP / "Contents" / "MacOS" / _HOST
    os.link(host, first / "outside-link")
    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
        comparator().compare_native_approval_artifacts(first, second)

    first = tmp_path / "bad-tar"
    _write_artifact(first)
    original = first / _ARCHIVE
    replacement = first / "replacement.tar"
    with tarfile.open(replacement, "w", format=tarfile.GNU_FORMAT) as archive:
        entry = tarfile.TarInfo(f"{_APP}/")
        entry.type = tarfile.DIRTYPE
        entry.mode = 0o755
        entry.uid = 1
        entry.gid = 0
        entry.mtime = _EPOCH
        archive.addfile(entry)
    original.unlink()
    replacement.rename(original)
    original.chmod(0o600)
    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
        comparator().compare_native_approval_artifacts(first, second)


def test_cli_returns_fixed_error_without_echoing_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_artifact(first)
    _write_artifact(second)
    (first / "unexpected").write_bytes(b"no")

    assert comparator().main(["--first", str(first), "--second", str(second)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "NATIVE_ARTIFACT_PAIR_INPUT_INVALID\n"
    assert str(first) not in captured.err


def test_rejects_noncanonical_file_modes(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_artifact(first)
    _write_artifact(second)
    client = first / _APP / "Contents" / "MacOS" / _CLIENT
    client.chmod(stat.S_IRUSR | stat.S_IWUSR)

    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
        comparator().compare_native_approval_artifacts(first, second)


def test_rebuilds_full_contract_and_descriptor_digests_before_archive_access(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_artifact(first)
    _write_artifact(second)
    manifest_path = first / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    contract = manifest["metadata"]["contract"]
    descriptor = manifest["metadata"]["native_descriptor"]
    assert isinstance(contract, dict) and isinstance(descriptor, dict)
    contract["maximum_policy_items"] = 63
    descriptor_metadata = descriptor["metadata"]
    descriptor_protocol = descriptor["protocol_configuration"]
    assert isinstance(descriptor_metadata, dict) and isinstance(descriptor_protocol, dict)
    descriptor_metadata["maximum_policy_items"] = 63
    descriptor_protocol["maximum_policy_items"] = 63
    manifest_path.write_bytes(_canonical(manifest))
    manifest_path.chmod(0o644)

    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
        comparator().compare_native_approval_artifacts(first, second)


def test_streams_tar_members_without_getmembers_and_stops_at_the_first_extra_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_artifact(first)
    _write_artifact(second)

    def forbidden_getmembers(_: tarfile.TarFile) -> list[tarfile.TarInfo]:
        raise AssertionError("pair comparator must not materialize every tar member")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", forbidden_getmembers)
    assert (
        comparator().compare_native_approval_artifacts(first, second)["archive"]["member_count"]
        == 11
    )

    original = first / _ARCHIVE
    replacement = first / "replacement.tar"
    with (
        tarfile.open(original, "r:") as source,
        tarfile.open(replacement, "w", format=tarfile.GNU_FORMAT) as target,
    ):
        for member in source:
            copied = tarfile.TarInfo(member.name + ("/" if member.isdir() else ""))
            copied.type = member.type
            copied.mode = member.mode
            copied.uid = member.uid
            copied.gid = member.gid
            copied.uname = member.uname
            copied.gname = member.gname
            copied.mtime = member.mtime
            copied.size = member.size
            payload = source.extractfile(member) if member.isreg() else None
            try:
                target.addfile(copied, payload)
            finally:
                if payload is not None:
                    payload.close()
        for number in range(17):
            extra = tarfile.TarInfo(f"extra-{number}")
            extra.uid = 0
            extra.gid = 0
            extra.mtime = _EPOCH
            extra.mode = 0o644
            extra.size = 0
            target.addfile(extra, io.BytesIO())
    original.unlink()
    replacement.rename(original)
    original.chmod(0o600)
    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
        comparator().compare_native_approval_artifacts(first, second)


def test_rejects_nonzero_tar_trailer_outside_the_fixed_record_boundary(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    _write_artifact(first)
    _write_artifact(second)
    with (first / _ARCHIVE).open("ab") as archive:
        archive.write(b"unexpected-tail")

    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
        comparator().compare_native_approval_artifacts(first, second)


def test_closes_acquired_descriptors_and_tar_streams_when_next_acquisition_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_path, second_path = tmp_path / "first", tmp_path / "second"
    _write_artifact(first_path)
    _write_artifact(second_path)
    loaded = comparator()
    first = loaded._inspect_artifact(first_path)
    second = loaded._inspect_artifact(second_path)
    original_open = loaded._open_relative_regular
    opened: list[int] = []
    calls = 0

    def fail_second_open(*args: object, **kwargs: object) -> tuple[int, os.stat_result]:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise loaded.NativeApprovalArtifactComparisonError("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
        descriptor, value = original_open(*args, **kwargs)
        opened.append(descriptor)
        return descriptor, value

    try:
        monkeypatch.setattr(loaded, "_open_relative_regular", fail_second_open)
        with pytest.raises(loaded.NativeApprovalArtifactComparisonError, match="INPUT_CHANGED"):
            loaded._compare_regular_files(first, second, ("manifest.json",))
        assert opened
        with pytest.raises(OSError):
            os.fstat(opened[0])

        class _TrackedStream:
            closed = False

            def read(self, _: int = -1) -> bytes:
                return b""

            def close(self) -> None:
                self.closed = True

        class _Archive:
            def __init__(self, stream: _TrackedStream) -> None:
                self.stream = stream

            def extractfile(self, _: tarfile.TarInfo) -> _TrackedStream:
                return self.stream

        stream = _TrackedStream()
        monkeypatch.setattr(
            loaded,
            "_open_relative_regular",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                loaded.NativeApprovalArtifactComparisonError("NATIVE_ARTIFACT_PAIR_INPUT_CHANGED")
            ),
        )
        with pytest.raises(loaded.NativeApprovalArtifactComparisonError, match="INPUT_CHANGED"):
            loaded._compare_tar_member_to_staged(
                _Archive(stream),
                tarfile.TarInfo("synthetic"),
                first.root_descriptor,
                ("manifest.json",),
                first.files[("manifest.json",)],
            )
        assert stream.closed is True
    finally:
        first.close()
        second.close()


def test_rejects_two_open_handles_for_the_same_artifact_root(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    _write_artifact(artifact)
    loaded = comparator()
    first = loaded._inspect_artifact(artifact)
    second = loaded._inspect_artifact(artifact)
    try:
        with pytest.raises(loaded.NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
            loaded._checked_pair(first, second)
    finally:
        first.close()
        second.close()
