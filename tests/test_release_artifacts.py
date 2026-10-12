"""The release artifact checks accept the exact wheel and sdist and refuse every deviation."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import tarfile
import tomllib
import uuid
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import adhelm

_ROOT = Path(__file__).resolve().parents[1]
# SHA-256 of the unmodified Apache License 2.0 text published at
# https://www.apache.org/licenses/LICENSE-2.0.txt (11,358 bytes).
_APACHE_2_0_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_artifacts_under_test", _ROOT / "scripts/release_artifacts.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = _script()
VERSION = "0.1.0"
WHEEL = f"adhelm-{VERSION}-py3-none-any.whl"
SDIST = f"adhelm-{VERSION}.tar.gz"
DIST_INFO = f"adhelm-{VERSION}.dist-info"


def _metadata(
    *,
    expression: str = "Apache-2.0",
    license_files: tuple[str, ...] = ("LICENSE", "NOTICE"),
    description: str = "",
) -> bytes:
    lines = ["Metadata-Version: 2.5", "Name: adhelm", f"Version: {VERSION}"]
    if expression:
        lines.append(f"License-Expression: {expression}")
    lines.extend(f"License-File: {name}" for name in license_files)
    lines.append("Requires-Python: >=3.12")
    body = f"\n{description}" if description else ""
    return ("\n".join(lines) + "\n" + body).encode()


def _repository(tmp_path: Path, *, version: str = VERSION, name: str = "adhelm") -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8"
    )
    (root / "LICENSE").write_bytes(b"license text\n")
    (root / "NOTICE").write_bytes(b"notice text\n")
    (root / "uv.lock").write_bytes(b"version = 1\n")
    return root


def _wheel_files(root: Path) -> dict[str, bytes]:
    return {
        "adhelm/__init__.py": b"",
        "adhelm/py.typed": b"",
        "adhelm/bundled_skills/one/SKILL.md": b"one",
        "adhelm/bundled_skills/two/SKILL.md": b"two",
        "adhelm/pilot_record_files/README.md": b"records",
        f"{DIST_INFO}/METADATA": _metadata(),
        f"{DIST_INFO}/licenses/LICENSE": (root / "LICENSE").read_bytes(),
        f"{DIST_INFO}/licenses/NOTICE": (root / "NOTICE").read_bytes(),
    }


def _sdist_files(root: Path) -> dict[str, bytes]:
    return {
        "pyproject.toml": (root / "pyproject.toml").read_bytes(),
        "PKG-INFO": _metadata(),
        "README.md": b"readme",
        "LICENSE": (root / "LICENSE").read_bytes(),
        "NOTICE": (root / "NOTICE").read_bytes(),
        "src/adhelm/__init__.py": b"",
    }


def _write_wheel(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def _write_sdist(path: Path, files: dict[str, bytes], root: str = f"adhelm-{VERSION}") -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


@pytest.fixture
def release_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A repository with matching license files and a valid dist directory beside it."""
    root = _repository(tmp_path)
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_wheel(dist / WHEEL, _wheel_files(root))
    _write_sdist(dist / SDIST, _sdist_files(root))
    return root, dist


def _verify(root: Path, dist: Path, tag: str | None = None) -> dict[str, str]:
    result: dict[str, str] = release.verify_artifacts(dist, root / "pyproject.toml", tag)
    return result


def test_the_exact_wheel_and_sdist_verify_and_report_their_digests(
    release_tree: tuple[Path, Path],
) -> None:
    root, dist = release_tree

    digests = _verify(root, dist, f"v{VERSION}")

    assert digests == {
        name: hashlib.sha256((dist / name).read_bytes()).hexdigest() for name in (SDIST, WHEEL)
    }


def test_the_gitignore_uv_writes_into_its_output_directory_is_not_an_artifact(
    release_tree: tuple[Path, Path],
) -> None:
    root, dist = release_tree
    (dist / ".gitignore").write_text("*\n", encoding="utf-8")

    assert set(_verify(root, dist)) == {SDIST, WHEEL}


@pytest.mark.parametrize("tag", ["v0.1.1", "0.1.0", "v0.1.0-rc1", "v0.1.0 ", "latest", ""])
def test_a_tag_that_is_not_v_plus_the_package_version_is_refused(
    release_tree: tuple[Path, Path], tag: str
) -> None:
    root, dist = release_tree

    with pytest.raises(release.ReleaseError, match="does not match package version"):
        _verify(root, dist, tag)


@pytest.mark.parametrize(
    "unexpected",
    ["adhelm-0.1.0-py3-none-macosx_11_0_arm64.whl", "adhelm-0.1.0.zip", "other-0.1.0.tar.gz"],
)
def test_an_extra_artifact_is_refused(release_tree: tuple[Path, Path], unexpected: str) -> None:
    root, dist = release_tree
    (dist / unexpected).write_bytes(b"x")

    with pytest.raises(release.ReleaseError, match="must hold exactly"):
        _verify(root, dist)


@pytest.mark.parametrize("missing", [WHEEL, SDIST])
def test_a_missing_artifact_is_refused(release_tree: tuple[Path, Path], missing: str) -> None:
    root, dist = release_tree
    (dist / missing).unlink()

    with pytest.raises(release.ReleaseError, match="must hold exactly"):
        _verify(root, dist)


def test_artifacts_for_another_version_than_pyproject_are_refused(
    release_tree: tuple[Path, Path],
) -> None:
    root, dist = release_tree
    (root / "pyproject.toml").write_text(
        '[project]\nname = "adhelm"\nversion = "0.2.0"\n', encoding="utf-8"
    )

    with pytest.raises(release.ReleaseError, match="must hold exactly"):
        _verify(root, dist)


def test_a_project_not_named_adhelm_is_refused(tmp_path: Path) -> None:
    root = _repository(tmp_path, name="another-project")

    with pytest.raises(release.ReleaseError, match="must name the project adhelm"):
        release.project_version(root / "pyproject.toml")


@pytest.mark.parametrize(
    ("expression", "message"),
    [("MIT", "License-Expression"), ("Apache-2.0 OR MIT", "License-Expression"), ("", "License")],
)
def test_a_wheel_not_declaring_apache_2_0_is_refused(
    release_tree: tuple[Path, Path], expression: str, message: str
) -> None:
    root, dist = release_tree
    files = _wheel_files(root)
    files[f"{DIST_INFO}/METADATA"] = _metadata(expression=expression)
    _write_wheel(dist / WHEEL, files)

    with pytest.raises(release.ReleaseError, match=message):
        _verify(root, dist)


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_a_wheel_without_a_license_file_is_refused(
    release_tree: tuple[Path, Path], name: str
) -> None:
    root, dist = release_tree
    files = _wheel_files(root)
    del files[f"{DIST_INFO}/licenses/{name}"]
    _write_wheel(dist / WHEEL, files)

    with pytest.raises(release.ReleaseError, match=f"does not carry {name}"):
        _verify(root, dist)


@pytest.mark.parametrize("name", ["LICENSE", "NOTICE"])
def test_a_wheel_license_file_that_differs_from_the_repository_is_refused(
    release_tree: tuple[Path, Path], name: str
) -> None:
    root, dist = release_tree
    files = _wheel_files(root)
    files[f"{DIST_INFO}/licenses/{name}"] = b"altered\n"
    _write_wheel(dist / WHEEL, files)

    with pytest.raises(release.ReleaseError, match=f"{name} differs"):
        _verify(root, dist)


def test_a_wheel_that_declares_extra_license_files_is_refused(
    release_tree: tuple[Path, Path],
) -> None:
    root, dist = release_tree
    files = _wheel_files(root)
    files[f"{DIST_INFO}/METADATA"] = _metadata(license_files=("LICENSE",))
    _write_wheel(dist / WHEEL, files)

    with pytest.raises(release.ReleaseError, match="must declare exactly"):
        _verify(root, dist)


@pytest.mark.parametrize("stray", ["tests/test_x.py", "docs/notes.md", ".env", "setup.py"])
def test_a_wheel_carrying_files_outside_the_package_is_refused(
    release_tree: tuple[Path, Path], stray: str
) -> None:
    root, dist = release_tree
    files = _wheel_files(root)
    files[stray] = b"x"
    _write_wheel(dist / WHEEL, files)

    with pytest.raises(release.ReleaseError, match="top-level entries outside the package"):
        _verify(root, dist)


@pytest.mark.parametrize(
    "removed",
    [
        "adhelm/py.typed",
        "adhelm/bundled_skills/two/SKILL.md",
        "adhelm/pilot_record_files/README.md",
    ],
)
def test_a_wheel_missing_a_shipped_resource_is_refused(
    release_tree: tuple[Path, Path], removed: str
) -> None:
    root, dist = release_tree
    files = _wheel_files(root)
    del files[removed]
    _write_wheel(dist / WHEEL, files)

    with pytest.raises(release.ReleaseError, match="missing"):
        _verify(root, dist)


@pytest.mark.parametrize("member", ["../escape.py", "/etc/passwd", "adhelm/../../escape.py"])
def test_a_wheel_member_that_escapes_the_archive_is_refused(
    release_tree: tuple[Path, Path], member: str
) -> None:
    root, dist = release_tree
    files = _wheel_files(root)
    files[member] = b"x"
    _write_wheel(dist / WHEEL, files)

    with pytest.raises(release.ReleaseError, match="escapes the archive root"):
        _verify(root, dist)


def test_a_file_that_is_not_a_wheel_archive_is_refused(release_tree: tuple[Path, Path]) -> None:
    root, dist = release_tree
    (dist / WHEEL).write_bytes(b"not a zip")

    with pytest.raises(release.ReleaseError, match="not a valid wheel"):
        _verify(root, dist)


@pytest.mark.parametrize("name", ["pyproject.toml", "PKG-INFO", "README.md", "LICENSE", "NOTICE"])
def test_an_sdist_missing_a_required_file_is_refused(
    release_tree: tuple[Path, Path], name: str
) -> None:
    root, dist = release_tree
    files = _sdist_files(root)
    del files[name]
    _write_sdist(dist / SDIST, files)

    with pytest.raises(release.ReleaseError, match=name):
        _verify(root, dist)


def test_an_sdist_license_file_that_differs_from_the_repository_is_refused(
    release_tree: tuple[Path, Path],
) -> None:
    root, dist = release_tree
    files = _sdist_files(root)
    files["LICENSE"] = b"altered\n"
    _write_sdist(dist / SDIST, files)

    with pytest.raises(release.ReleaseError, match="LICENSE differs"):
        _verify(root, dist)


def test_an_sdist_with_members_outside_its_root_directory_is_refused(
    release_tree: tuple[Path, Path],
) -> None:
    root, dist = release_tree
    _write_sdist(dist / SDIST, _sdist_files(root), root="adhelm-9.9.9")

    with pytest.raises(release.ReleaseError, match="outside adhelm-0.1.0"):
        _verify(root, dist)


def test_an_sdist_carrying_a_link_is_refused(release_tree: tuple[Path, Path]) -> None:
    root, dist = release_tree
    with tarfile.open(dist / SDIST, "w:gz") as archive:
        for name, content in _sdist_files(root).items():
            info = tarfile.TarInfo(f"adhelm-{VERSION}/{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        link = tarfile.TarInfo(f"adhelm-{VERSION}/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)

    with pytest.raises(release.ReleaseError, match="not a regular file"):
        _verify(root, dist)


def test_a_symlinked_artifact_is_refused(release_tree: tuple[Path, Path], tmp_path: Path) -> None:
    root, dist = release_tree
    elsewhere = tmp_path / "elsewhere.whl"
    (dist / WHEEL).rename(elsewhere)
    try:
        (dist / WHEEL).symlink_to(elsewhere)
    except OSError:
        pytest.skip("symbolic links need extra privilege on this platform")

    with pytest.raises(release.ReleaseError, match="regular file"):
        _verify(root, dist)


def test_the_command_line_reports_a_refusal_with_exit_one_and_no_traceback(
    release_tree: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    root, dist = release_tree

    status = release.main(
        ["verify", "--dist", str(dist), "--pyproject", str(root / "pyproject.toml"), "--tag", "v9"]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "release check failed" in captured.err
    assert captured.out == ""


def test_the_command_line_prints_the_version_and_the_digests(
    release_tree: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    root, dist = release_tree
    pyproject = str(root / "pyproject.toml")

    assert release.main(["version", "--pyproject", pyproject]) == 0
    assert capsys.readouterr().out == f"{VERSION}\n"
    assert release.main(["verify", "--dist", str(dist), "--pyproject", pyproject]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED"


def _release_directory(tmp_path: Path) -> Path:
    directory = tmp_path / "release"
    directory.mkdir()
    (directory / WHEEL).write_bytes(b"wheel")
    (directory / SDIST).write_bytes(b"sdist")
    (directory / "adhelm-0.1.0.cdx.json").write_bytes(b"{}")
    return directory


def test_checksums_use_the_sha256sum_format_and_round_trip(tmp_path: Path) -> None:
    directory = _release_directory(tmp_path)

    text = release.write_checksums(directory)

    assert text == (directory / "SHA256SUMS").read_text(encoding="utf-8")
    assert text.splitlines() == [
        f"{hashlib.sha256((directory / name).read_bytes()).hexdigest()}  {name}"
        for name in sorted(path.name for path in directory.iterdir() if path.name != "SHA256SUMS")
    ]
    assert "\r" not in text
    release.check_checksums(directory)


def test_writing_checksums_twice_does_not_list_the_checksum_file(tmp_path: Path) -> None:
    directory = _release_directory(tmp_path)
    first = release.write_checksums(directory)

    assert release.write_checksums(directory) == first
    assert "SHA256SUMS" not in first


@pytest.mark.parametrize("name", [WHEEL, SDIST, "adhelm-0.1.0.cdx.json"])
def test_a_file_changed_after_checksumming_is_detected(tmp_path: Path, name: str) -> None:
    directory = _release_directory(tmp_path)
    release.write_checksums(directory)
    (directory / name).write_bytes(b"tampered")

    with pytest.raises(release.ReleaseError, match="does not match its recorded SHA-256"):
        release.check_checksums(directory)


def test_a_file_added_after_checksumming_is_detected(tmp_path: Path) -> None:
    directory = _release_directory(tmp_path)
    release.write_checksums(directory)
    (directory / "extra.whl").write_bytes(b"x")

    with pytest.raises(release.ReleaseError, match="does not list exactly"):
        release.check_checksums(directory)


def test_a_file_removed_after_checksumming_is_detected(tmp_path: Path) -> None:
    directory = _release_directory(tmp_path)
    release.write_checksums(directory)
    (directory / SDIST).unlink()

    with pytest.raises(release.ReleaseError, match="does not list exactly"):
        release.check_checksums(directory)


@pytest.mark.parametrize(
    "listing",
    [
        "not a checksum line\n",
        f"{'A' * 64}  {WHEEL}\n",
        f"{'a' * 64} {WHEEL}\n",
        f"{'a' * 64}  ../{WHEEL}\n",
        f"{'a' * 64}  {WHEEL}\n{'a' * 64}  {WHEEL}\n",
    ],
)
def test_a_malformed_or_duplicate_checksum_line_is_refused(tmp_path: Path, listing: str) -> None:
    directory = _release_directory(tmp_path)
    (directory / "SHA256SUMS").write_text(listing, encoding="utf-8", newline="\n")

    with pytest.raises(release.ReleaseError, match="malformed or duplicate"):
        release.check_checksums(directory)


def test_a_directory_without_a_checksum_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(release.ReleaseError, match="no readable SHA256SUMS"):
        release.check_checksums(_release_directory(tmp_path))


def test_a_file_name_that_cannot_be_a_checksum_line_is_refused(tmp_path: Path) -> None:
    directory = _release_directory(tmp_path)
    (directory / "has space.txt").write_bytes(b"x")

    with pytest.raises(release.ReleaseError, match="Unsupported file name"):
        release.write_checksums(directory)


_COMMIT = "a" * 40
_EPOCH = 1_790_000_000


def _sbom(**overrides: object) -> dict[str, Any]:
    document: dict[str, Any] = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": "urn:uuid:00000000-0000-4000-8000-000000000000",
        "metadata": {
            "timestamp": "2026-10-11T16:48:55.491370361Z",
            "component": {"name": "adhelm", "version": VERSION},
        },
        "components": [{"name": "httpx", "purl": "pkg:pypi/httpx@0.28.1"}],
    }
    document.update(overrides)
    return document


def _bound(root: Path, document: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    """The document as the release workflow's normalise step leaves it."""
    settings: dict[str, Any] = {"commit": _COMMIT, "epoch": _EPOCH}
    settings.update(kwargs)
    bound: dict[str, Any] = release.normalise_sbom(
        document or _sbom(), digest=release.lock_digest(root / "pyproject.toml"), **settings
    )
    return bound


def _check_sbom(tmp_path: Path, document: object, **kwargs: Any) -> None:
    root = tmp_path / "repository"
    if not root.exists():
        root = _repository(tmp_path)
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps(document), encoding="utf-8")
    settings: dict[str, Any] = {"commit": _COMMIT, "epoch": _EPOCH}
    settings.update(kwargs)
    release.verify_sbom(sbom, root / "pyproject.toml", **settings)


def test_a_cyclonedx_sbom_bound_to_this_checkout_is_accepted(tmp_path: Path) -> None:
    _check_sbom(tmp_path, _bound(_repository(tmp_path)))


@pytest.mark.parametrize(
    "document",
    [
        _sbom(bomFormat="SPDX"),
        _sbom(specVersion="1.4"),
        _sbom(metadata={"component": {"name": "adhelm", "version": "9.9.9"}}),
        _sbom(metadata={"component": {"name": "other", "version": VERSION}}),
        _sbom(metadata={}),
        _sbom(components=[]),
        _sbom(components="none"),
        _sbom(components=[{"name": "x"}]),
        _sbom(components=[{"name": "x", "purl": "pkg:npm/x@1"}]),
        ["not", "an", "object"],
    ],
)
def test_an_sbom_that_is_not_cyclonedx_1_5_for_this_release_is_refused(
    tmp_path: Path, document: object
) -> None:
    with pytest.raises(release.ReleaseError):
        _check_sbom(tmp_path, document)


def test_an_sbom_that_is_not_json_is_refused(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    sbom = tmp_path / "sbom.json"
    sbom.write_text("{", encoding="utf-8")

    with pytest.raises(release.ReleaseError, match="not valid JSON"):
        release.verify_sbom(sbom, root / "pyproject.toml", commit=_COMMIT, epoch=_EPOCH)


def test_a_raw_uv_export_with_a_random_serial_and_no_binding_is_refused(tmp_path: Path) -> None:
    with pytest.raises(release.ReleaseError, match=r"adhelm:source_commit is None"):
        _check_sbom(tmp_path, _sbom())


def _mutate(root: Path, change: Any) -> dict[str, Any]:
    document = _bound(root)
    change(document)
    return document


def _drop(name: str) -> Any:
    def change(document: dict[str, Any]) -> None:
        properties = document["metadata"]["properties"]
        document["metadata"]["properties"] = [p for p in properties if p["name"] != name]

    return change


def _set_property(name: str, value: str) -> Any:
    def change(document: dict[str, Any]) -> None:
        for entry in document["metadata"]["properties"]:
            if entry["name"] == name:
                entry["value"] = value

    return change


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (_drop("adhelm:source_commit"), "adhelm:source_commit is None"),
        (_set_property("adhelm:source_commit", "b" * 40), "adhelm:source_commit"),
        (_drop("adhelm:uv_lock_sha256"), "adhelm:uv_lock_sha256 is None"),
        (_set_property("adhelm:uv_lock_sha256", "0" * 64), "adhelm:uv_lock_sha256"),
        (_set_property("adhelm:source_date_epoch", "1"), "adhelm:source_date_epoch"),
        (_set_property("adhelm:scope", "EVERYTHING"), "adhelm:scope"),
        (
            lambda document: document.update(
                serialNumber="urn:uuid:" + "1" * 8 + "-0000-4000-8000-0" + "0" * 11
            ),
            "serial number is not derived",
        ),
        (
            lambda document: document["metadata"].update(timestamp="2026-10-11T16:48:55Z"),
            "timestamp is not SOURCE_DATE_EPOCH",
        ),
        (lambda document: document["metadata"].pop("properties"), "adhelm:source_commit is None"),
        (lambda document: document["metadata"].update(properties="none"), "is None"),
    ],
)
def test_an_sbom_not_bound_to_the_commit_and_lock_file_is_refused(
    tmp_path: Path, change: Any, message: str
) -> None:
    document = _mutate(_repository(tmp_path), change)

    with pytest.raises(release.ReleaseError, match=message):
        _check_sbom(tmp_path, document)


def test_an_sbom_bound_to_another_commit_or_epoch_than_the_checkout_is_refused(
    tmp_path: Path,
) -> None:
    document = _bound(_repository(tmp_path))

    with pytest.raises(release.ReleaseError, match="adhelm:source_commit"):
        _check_sbom(tmp_path, document, commit="c" * 40)
    with pytest.raises(release.ReleaseError, match="adhelm:source_date_epoch"):
        _check_sbom(tmp_path, document, epoch=_EPOCH + 1)
    with pytest.raises(release.ReleaseError, match="full lowercase commit hash"):
        _check_sbom(tmp_path, document, commit="HEAD")


def test_an_sbom_whose_lock_file_changed_after_it_was_written_is_refused(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    document = _bound(root)
    (root / "uv.lock").write_bytes(b"version = 1\n# a dependency moved\n")

    with pytest.raises(release.ReleaseError, match="adhelm:uv_lock_sha256"):
        _check_sbom(tmp_path, document)


def test_normalising_is_reproducible_whatever_serial_and_time_uv_stamped(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    first = _sbom()
    second = _sbom(serialNumber="urn:uuid:11111111-1111-4111-8111-111111111111")
    second["metadata"]["timestamp"] = "2030-01-01T00:00:00Z"

    one = release.serialise_sbom(_bound(root, first))
    two = release.serialise_sbom(_bound(root, second))

    assert one == two and one.endswith("}\n")
    other_commit = release.serialise_sbom(_bound(root, _sbom(), commit="b" * 40))
    assert other_commit != one


def test_the_release_serial_differs_from_the_development_runtime_graph_serial() -> None:
    commit, digest, epoch = "a" * 40, "b" * 64, 1_700_000_000
    development_style = "urn:uuid:" + str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"adhelm:{commit}:{digest}:{epoch}")
    )

    release_serial = release._sbom_serial(commit, digest, epoch)

    assert release_serial != development_style
    assert release_serial == release._sbom_serial(commit, digest, epoch)


def test_normalising_keeps_the_components_and_other_properties_and_never_doubles_ours(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    document = _sbom()
    document["metadata"]["properties"] = [{"name": "uv:keep", "value": "1"}]

    once = _bound(root, document)
    twice = _bound(root, once)

    names = [entry["name"] for entry in twice["metadata"]["properties"]]
    assert names == [
        "uv:keep",
        "adhelm:source_commit",
        "adhelm:uv_lock_sha256",
        "adhelm:source_date_epoch",
        "adhelm:scope",
    ]
    assert twice["components"] == _sbom()["components"]
    assert twice["metadata"]["timestamp"] == "2026-09-21T14:13:20Z"
    assert twice["serialNumber"].startswith("urn:uuid:") and "-5" in twice["serialNumber"]


@pytest.mark.parametrize(
    "broken",
    [{"metadata": "none"}, {"components": "none"}, {"metadata": {"properties": "none"}}],
)
def test_a_document_uv_did_not_shape_as_expected_is_not_normalised(
    tmp_path: Path, broken: dict[str, Any]
) -> None:
    root = _repository(tmp_path)

    with pytest.raises(release.ReleaseError):
        _bound(root, _sbom(**broken))


def _git_repository(root: Path) -> str:
    env = {
        **{name: value for name, value in os.environ.items() if not name.startswith("GIT_")},
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.test",
        "GIT_COMMITTER_DATE": f"{_EPOCH} +0000",
        "GIT_AUTHOR_DATE": f"{_EPOCH} +0000",
    }
    for arguments in (
        ["init", "--quiet", "--initial-branch=main"],
        ["add", "pyproject.toml", "uv.lock"],
        ["commit", "--quiet", "--no-gpg-sign", "-m", "base"],
    ):
        subprocess.run(["git", "-C", str(root), *arguments], check=True, env=env)
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout.strip()


def test_the_sbom_commands_normalise_and_verify_against_the_checkout_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    root = _repository(tmp_path)
    commit = _git_repository(root)
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_text(json.dumps(_sbom()), encoding="utf-8")
    second.write_text(
        json.dumps(_sbom(serialNumber="urn:uuid:" + "2" * 8 + "-2222-4222-8222-" + "2" * 12)),
        encoding="utf-8",
    )
    pyproject = str(root / "pyproject.toml")

    for sbom in (first, second):
        assert release.main(["verify-sbom", "--sbom", str(sbom), "--pyproject", pyproject]) == 1
        assert release.main(["normalise-sbom", "--sbom", str(sbom), "--pyproject", pyproject]) == 0
        assert release.main(["verify-sbom", "--sbom", str(sbom), "--pyproject", pyproject]) == 0

    assert first.read_bytes() == second.read_bytes()
    recorded = json.loads(first.read_text(encoding="utf-8"))["metadata"]["properties"]
    assert {"name": "adhelm:source_commit", "value": commit} in recorded
    assert {"name": "adhelm:source_date_epoch", "value": str(_EPOCH)} in recorded


def test_the_source_date_epoch_in_the_environment_sets_the_sbom_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    _git_repository(root)
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps(_sbom()), encoding="utf-8")
    pyproject = str(root / "pyproject.toml")

    monkeypatch.setenv("SOURCE_DATE_EPOCH", "86400")
    assert release.main(["normalise-sbom", "--sbom", str(sbom), "--pyproject", pyproject]) == 0
    assert json.loads(sbom.read_text(encoding="utf-8"))["metadata"]["timestamp"] == (
        "1970-01-02T00:00:00Z"
    )
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "tomorrow")
    assert release.main(["verify-sbom", "--sbom", str(sbom), "--pyproject", pyproject]) == 1


def test_the_sbom_commands_fail_outside_a_git_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    root = _repository(tmp_path)
    sbom = tmp_path / "sbom.json"
    sbom.write_text(json.dumps(_sbom()), encoding="utf-8")

    code = release.main(
        ["normalise-sbom", "--sbom", str(sbom), "--pyproject", str(root / "pyproject.toml")]
    )

    assert code == 1 and "release check failed" in capsys.readouterr().err


def test_the_license_is_the_unmodified_apache_2_0_text() -> None:
    assert hashlib.sha256((_ROOT / "LICENSE").read_bytes()).hexdigest() == _APACHE_2_0_SHA256
    assert b"\r" not in (_ROOT / "LICENSE").read_bytes()


def test_the_notice_names_the_product_and_carries_no_personal_name_or_path() -> None:
    notice = (_ROOT / "NOTICE").read_text(encoding="utf-8")

    assert notice.startswith("AdHelm\nCopyright 2026 The AdHelm authors\n")
    # The only copyright line names the project authors, never a person; a person's name or a
    # local path would fail this without the test having to spell the name out.
    assert [line for line in notice.splitlines() if "Copyright" in line] == [
        "Copyright 2026 The AdHelm authors"
    ]
    assert "/home/" not in notice and "/Users/" not in notice


def test_the_project_metadata_matches_the_release_definition() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert project["name"] == "adhelm"
    assert project["version"] == adhelm.__version__
    assert project["license"] == "Apache-2.0"
    assert project["license-files"] == ["LICENSE", "NOTICE"]
    assert project["requires-python"] == ">=3.12"
    assert project["authors"] == [{"name": "The AdHelm authors"}]
    assert project["urls"].keys() >= {"Homepage", "Source", "Issues", "Changelog"}
    # The home page is the owner's portfolio site (ADR 0013, 2026-10-12); the rest are GitHub.
    assert project["urls"]["Homepage"] == "https://aptsonic.com/adhelm"
    assert all(
        url.startswith("https://github.com/jasonhowie/adhelm")
        for name, url in project["urls"].items()
        if name != "Homepage"
    )
    # PEP 639: the SPDX expression replaces license classifiers, which are deprecated beside it.
    assert not [name for name in project["classifiers"] if name.startswith("License ::")]
    assert "Operating System :: Microsoft :: Windows" in project["classifiers"]
    assert "Operating System :: MacOS" in project["classifiers"]
    assert "Operating System :: POSIX :: Linux" in project["classifiers"]
    assert "Programming Language :: Python :: 3.12" in project["classifiers"]
    assert "Development Status :: 3 - Alpha" in project["classifiers"]


def test_the_sdist_carries_what_the_wheel_is_built_from() -> None:
    """The wheel is built from the sdist, so the force-included directories must be in it."""
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build = pyproject["tool"]["hatch"]["build"]["targets"]
    included = {entry.lstrip("/") for entry in build["sdist"]["include"]}

    assert set(build["wheel"]["force-include"]) <= included
    assert "src" in included


def test_the_checksum_commands_run_from_the_command_line(tmp_path: Path) -> None:
    directory = tmp_path / "meta"
    directory.mkdir()
    sbom = directory / "sbom.json"
    sbom.write_text(json.dumps(_sbom()), encoding="utf-8")

    assert release.main(["write-checksums", "--dir", str(directory)]) == 0
    assert release.main(["check-checksums", "--dir", str(directory)]) == 0
    sbom.write_text("{}", encoding="utf-8")
    assert release.main(["check-checksums", "--dir", str(directory)]) == 1


def _with_description(root: Path, dist: Path, description: str) -> None:
    files = _wheel_files(root)
    files[f"{DIST_INFO}/METADATA"] = _metadata(description=description)
    _write_wheel(dist / WHEEL, files)


@pytest.mark.parametrize(
    "description",
    [
        "Read the [setup guide](docs/setup/google-ads.md) first.",
        "See [the policy](SECURITY.md).",
        "![diagram](docs/diagram.png)",
        "Read [LICENSE](./LICENSE).",
        "[guide]: docs/setup/ga4.md",
        '<a href="docs/setup/ga4.md">guide</a>',
        "<img src='docs/diagram.png'>",
        "A [link](../CONTRIBUTING.md) and an [absolute](https://example.test/ok).",
    ],
)
def test_a_wheel_whose_long_description_has_a_relative_link_is_refused(
    release_tree: tuple[Path, Path], description: str
) -> None:
    root, dist = release_tree
    _with_description(root, dist, description)

    with pytest.raises(release.ReleaseError, match="relative links that break on PyPI"):
        _verify(root, dist)


def test_absolute_anchor_and_mail_links_in_the_long_description_are_accepted(
    release_tree: tuple[Path, Path],
) -> None:
    root, dist = release_tree
    _with_description(
        root,
        dist,
        "[guide](https://github.com/jasonhowie/adhelm/blob/v0.1.0/docs/setup/ga4.md) "
        "[top](#install) [mail](mailto:a@example.test)\n"
        "[ref]: https://github.com/jasonhowie/adhelm\n"
        '<a href="https://example.test">x</a>\n'
        "`values[0](1)` is not a link target, nor is a [bare] bracket.",
    )

    assert set(_verify(root, dist)) == {SDIST, WHEEL}


def test_a_wheel_whose_long_description_links_to_another_releases_tag_is_refused(
    release_tree: tuple[Path, Path],
) -> None:
    root, dist = release_tree
    _with_description(
        root, dist, "[guide](https://github.com/jasonhowie/adhelm/blob/v0.0.9/docs/setup/ga4.md)"
    )

    with pytest.raises(release.ReleaseError, match=r"links to \['v0.0.9'\], not v0.1.0"):
        _verify(root, dist)


def test_an_sdist_whose_package_info_has_a_relative_link_is_refused(
    release_tree: tuple[Path, Path],
) -> None:
    root, dist = release_tree
    files = _sdist_files(root)
    files["PKG-INFO"] = _metadata(description="[guide](docs/setup/ga4.md)")
    _write_sdist(dist / SDIST, files)

    with pytest.raises(release.ReleaseError, match=r"adhelm-0.1.0.tar.gz long description"):
        _verify(root, dist)


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_readme_the_package_publishes_is_rewritten_to_links_that_work_on_pypi() -> None:
    """Apply pyproject.toml's own substitution to README.md, as the build backend does."""
    pyproject = _pyproject()
    hook = pyproject["tool"]["hatch"]["metadata"]["hooks"]["fancy-pypi-readme"]
    version = pyproject["project"]["version"]
    text = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert release._RELATIVE_LINK.search(release._long_description(b"\n\n" + text.encode()))

    for substitution in hook["substitutions"]:
        text = re.sub(substitution["pattern"], substitution["replacement"], text)

    assert pyproject["project"]["dynamic"] == ["readme"] and "readme" not in pyproject["project"]
    assert hook["content-type"] == "text/markdown"
    assert [fragment["path"] for fragment in hook["fragments"]] == ["README.md"]
    assert not release._RELATIVE_LINK.search(release._long_description(b"\n\n" + text.encode()))
    assert f"(https://github.com/jasonhowie/adhelm/blob/v{version}/docs/release-0.1.0.md)" in text
    assert set(release._TAGGED_LINK.findall(text)) == {f"v{version}"}


def test_the_build_backend_and_its_dependencies_are_pinned_exactly_for_builds() -> None:
    pyproject = _pyproject()
    constraints = pyproject["tool"]["uv"]["build-constraint-dependencies"]
    pinned = dict(entry.split("==", 1) for entry in constraints)
    requires = pyproject["build-system"]["requires"]

    assert all("==" in entry for entry in constraints) and len(pinned) == len(constraints)
    # Every backend the build asks for, and the packages hatchling pulls in, has an exact pin.
    assert {"hatchling", "hatch-fancy-pypi-readme"} <= pinned.keys()
    assert {"packaging", "pathspec", "pluggy", "tomlkit", "trove-classifiers"} <= pinned.keys()
    for requirement in requires:
        name = re.split(r"[<>=!~ ]", requirement, maxsplit=1)[0]
        assert name in pinned, requirement
    # The lock records the same constraints, so `uv lock --check` fails if they drift apart.
    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert sorted((c["name"], c["specifier"]) for c in lock["manifest"]["build-constraints"]) == (
        sorted((name, f"=={version}") for name, version in pinned.items())
    )


def test_the_sdist_includes_the_readme_the_long_description_is_made_from() -> None:
    included = _pyproject()["tool"]["hatch"]["build"]["targets"]["sdist"]["include"]

    assert "/README.md" in included
