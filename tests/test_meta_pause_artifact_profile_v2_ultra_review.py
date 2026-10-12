"""Independent fake-only review of the explicit Meta artifact profile."""

from __future__ import annotations

import io
import json
import stat
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest
from test_build_native_approval_artifact import (
    _EPOCH,
    _build,
    _descriptor_bytes,
    _fake_runner,
    _write_source_tree,
    _write_spec,
    builder,
)
from test_compare_native_approval_artifacts import _ARCHIVE, _write_artifact, comparator


def _context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, Path, Path, Path]:
    loaded = builder()
    repo = tmp_path / "repository"
    repo.mkdir()
    _write_source_tree(repo)
    specification = _write_spec(tmp_path)
    _fake_runner(monkeypatch, repo=repo, descriptor=_descriptor_bytes(specification))
    return loaded, repo, specification, tmp_path / "artifact-output"


def _rewrite_wheel_member(wheel: Path, member_name: str, replacement: bytes) -> None:
    """Replace one fake ZIP payload without importing any wheel module."""
    with zipfile.ZipFile(wheel) as source:
        members = [
            (member.filename, source.read(member.filename))
            for member in source.infolist()
            if not member.is_dir()
        ]
    with zipfile.ZipFile(wheel, "w") as destination:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            destination.writestr(info, replacement if name == member_name else data)


def test_v1_rechecks_the_native_absent_meta_route_after_late_swift_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V1 cannot silently carry a route changed after its first default check."""
    loaded, repo, specification, output = _context(tmp_path, monkeypatch)
    fake_run_tool = loaded._run_tool
    observed: list[tuple[str, ...]] = []

    def changing_run_tool(
        arguments: object,
        *,
        cwd: Path,
        environment: object,
        inspector: Path | None = None,
        swift_context: tuple[Path, Path] | None = None,
        archive_output: Path | None = None,
        python_build_context: tuple[Path, Path] | None = None,
        profile: object = None,
        timeout: int = 600,
    ) -> subprocess.CompletedProcess[bytes]:
        result = fake_run_tool(
            arguments,
            cwd=cwd,
            environment=environment,
            inspector=inspector,
            swift_context=swift_context,
            archive_output=archive_output,
            python_build_context=python_build_context,
            profile=profile,
            timeout=timeout,
        )
        if isinstance(arguments, list):
            observed.append(tuple(arguments))
        if (
            isinstance(arguments, list)
            and arguments[-2:] == ["--product", loaded._CLIENT_PRODUCT]
            and swift_context is not None
        ):
            package, _workspace = swift_context
            (package.parents[1] / loaded._GENERATED_META_ROUTE_RELATIVE_PATH).write_text(
                "configured route replacement\n", encoding="utf-8"
            )
        return cast(subprocess.CompletedProcess[bytes], result)

    monkeypatch.setattr(loaded, "_run_tool", changing_run_tool)
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_METADATA_INVALID"):
        loaded.build_native_approval_artifact(
            repo=repo,
            revision="HEAD",
            specification=specification,
            output=output,
            source_date_epoch=_EPOCH,
            profile="V1",
        )

    assert not output.exists()
    assert any(call[-2:] == ("--product", loaded._CLIENT_PRODUCT) for call in observed)
    assert not any(call[-1:] == ("--show-bin-path",) for call in observed)
    assert not any(call[0] == loaded._UV for call in observed)


def test_meta_profile_rejects_a_missing_generated_binding_record_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The checked wheel needs an exact RECORD row for the generated Meta binding."""
    loaded, repo, specification, output = _context(tmp_path, monkeypatch)
    fake_run_tool = loaded._run_tool

    def corrupting_run_tool(
        arguments: object,
        *,
        cwd: Path,
        environment: object,
        inspector: Path | None = None,
        swift_context: tuple[Path, Path] | None = None,
        archive_output: Path | None = None,
        python_build_context: tuple[Path, Path] | None = None,
        profile: object = None,
        timeout: int = 600,
    ) -> subprocess.CompletedProcess[bytes]:
        result = fake_run_tool(
            arguments,
            cwd=cwd,
            environment=environment,
            inspector=inspector,
            swift_context=swift_context,
            archive_output=archive_output,
            python_build_context=python_build_context,
            profile=profile,
            timeout=timeout,
        )
        if isinstance(arguments, list) and python_build_context is not None:
            _snapshot, workspace = python_build_context
            wheel = workspace / "python-wheel" / loaded._WHEEL_NAME
            record_name = "adhelm-0.1.0.dist-info/RECORD"
            record = wheel.read_bytes()
            with zipfile.ZipFile(Path(workspace / "python-wheel" / loaded._WHEEL_NAME)) as archive:
                original = archive.read(record_name).decode("ascii")
            replacement = "".join(
                line
                for line in original.splitlines(keepends=True)
                if not line.startswith(loaded._GENERATED_META_PYTHON_WHEEL_MEMBER + ",")
            ).encode("ascii")
            _rewrite_wheel_member(wheel, record_name, replacement)
            assert wheel.read_bytes() != record
        return cast(subprocess.CompletedProcess[bytes], result)

    monkeypatch.setattr(loaded, "_run_tool", corrupting_run_tool)
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_PYTHON_WHEEL_INVALID"):
        loaded.build_native_approval_artifact(
            repo=repo,
            revision="HEAD",
            specification=specification,
            output=output,
            source_date_epoch=_EPOCH,
            profile="META_CAMPAIGN_PAUSE_V2",
        )

    assert not output.exists()


def test_meta_profile_rejects_a_non_macho_helper_before_output_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An executable-looking Meta helper cannot bypass the fixed Mach-O validation."""
    loaded, repo, specification, output = _context(tmp_path, monkeypatch)
    fake_run_tool = loaded._run_tool

    def corrupting_run_tool(
        arguments: object,
        *,
        cwd: Path,
        environment: object,
        inspector: Path | None = None,
        swift_context: tuple[Path, Path] | None = None,
        archive_output: Path | None = None,
        python_build_context: tuple[Path, Path] | None = None,
        profile: object = None,
        timeout: int = 600,
    ) -> subprocess.CompletedProcess[bytes]:
        result = fake_run_tool(
            arguments,
            cwd=cwd,
            environment=environment,
            inspector=inspector,
            swift_context=swift_context,
            archive_output=archive_output,
            python_build_context=python_build_context,
            profile=profile,
            timeout=timeout,
        )
        if (
            isinstance(arguments, list)
            and arguments[-1:] == ["--show-bin-path"]
            and swift_context is not None
        ):
            _package, workspace = swift_context
            helper = workspace / "swift-build/release" / loaded._META_CAMPAIGN_PAUSE_CLIENT_PRODUCT
            helper.write_bytes(b"not a Mach-O helper")
            helper.chmod(0o755)
        return cast(subprocess.CompletedProcess[bytes], result)

    monkeypatch.setattr(loaded, "_run_tool", corrupting_run_tool)
    with pytest.raises(loaded.ArtifactBuildError, match="NATIVE_ARTIFACT_HOST_BINARY_INVALID"):
        loaded.build_native_approval_artifact(
            repo=repo,
            revision="HEAD",
            specification=specification,
            output=output,
            source_date_epoch=_EPOCH,
            profile="META_CAMPAIGN_PAUSE_V2",
        )

    assert not output.exists()


def test_meta_profile_reads_a_generated_binding_as_wheel_data_without_importing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build may inspect configured source bytes but must never execute them."""
    loaded, repo, specification, output = _context(tmp_path, monkeypatch)
    original_load_contract = loaded._load_contract
    trap = 'raise SystemExit("configured wheel import is forbidden")\n'

    def trapping_load_contract(path: Path) -> tuple[object, object, object]:
        contract, descriptor, api = original_load_contract(path)
        monkeypatch.setattr(
            api,
            "render_generated_meta_campaign_pause_approval_bridge_binding_v2",
            lambda _contract: trap,
        )
        return contract, descriptor, api

    monkeypatch.setattr(loaded, "_load_contract", trapping_load_contract)
    result = loaded.build_native_approval_artifact(
        repo=repo,
        revision="HEAD",
        specification=specification,
        output=output,
        source_date_epoch=_EPOCH,
        profile="META_CAMPAIGN_PAUSE_V2",
    )

    assert result.output_directory == output
    with zipfile.ZipFile(output / loaded._WHEEL_NAME) as wheel:
        assert wheel.read(loaded._GENERATED_META_PYTHON_WHEEL_MEMBER) == trap.encode("utf-8")


def test_pair_comparator_rejects_cross_profile_fake_builder_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A V1 artifact cannot be presented as the paired Meta profile artifact."""
    _v1_result, _v1_calls, _v1_repo, _v1_spec, v1 = _build(tmp_path / "v1", monkeypatch)
    _meta_result, _meta_calls, _meta_repo, _meta_spec, meta = _build(
        tmp_path / "meta",
        monkeypatch,
        profile="META_CAMPAIGN_PAUSE_V2",
    )

    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="MISMATCH"):
        comparator().compare_native_approval_artifacts(v1, meta)


def test_pair_comparator_recomputes_metadata_and_descriptor_digests(
    tmp_path: Path,
) -> None:
    """Matching hexadecimal placeholders cannot replace the full18 digest binding."""
    first, second = tmp_path / "first", tmp_path / "second"
    _write_artifact(first)
    _write_artifact(second)
    manifest_path = first / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["metadata"]["metadata_digest"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")))
    manifest_path.chmod(0o644)
    archive = first / _ARCHIVE
    archive.unlink()
    builder().create_deterministic_archive(first, archive=archive, epoch=_EPOCH)

    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
        comparator().compare_native_approval_artifacts(first, second)


def test_pair_comparator_streams_and_rejects_extra_tar_headers_without_getmembers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded archive check neither materializes a tar member list nor permits surplus rows."""
    first, second = tmp_path / "first", tmp_path / "second"
    _write_artifact(first)
    _write_artifact(second)

    def forbidden_getmembers(_self: tarfile.TarFile) -> list[tarfile.TarInfo]:
        raise AssertionError("archive member enumeration is unbounded")

    monkeypatch.setattr(tarfile.TarFile, "getmembers", forbidden_getmembers)
    assert comparator().compare_native_approval_artifacts(first, second)["profile"] == "V1"

    archive_path = first / _ARCHIVE
    with tarfile.open(archive_path, "a", format=tarfile.GNU_FORMAT) as archive:
        for index in range(17):
            extra = tarfile.TarInfo(f"surplus-{index:02d}")
            extra.size = 0
            extra.mode = 0o644
            extra.uid = 0
            extra.gid = 0
            extra.mtime = _EPOCH
            archive.addfile(extra, io.BytesIO())
    archive_path.chmod(0o600)

    with pytest.raises(comparator().NativeApprovalArtifactComparisonError, match="INPUT_INVALID"):
        comparator().compare_native_approval_artifacts(first, second)
