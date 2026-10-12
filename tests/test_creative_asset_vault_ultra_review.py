"""Independent adversarial checks for the private local creative-image vault."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

import adhelm.assets as assets
from adhelm.assets import read_image, store_image
from adhelm.errors import AdhelmError


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _temporary_names(root: Path) -> list[str]:
    directory = root / "assets" / "sha256"
    return sorted(item.name for item in directory.iterdir() if item.name.startswith(".asset-"))


def test_invalid_request_fails_before_creating_a_private_vault(tmp_path: Path) -> None:
    root = tmp_path / "SENTINEL-PRIVATE-ROOT"
    expected = _digest(b"expected")

    with pytest.raises(AdhelmError) as write_error:
        store_image(root, b"different", expected)
    with pytest.raises(AdhelmError) as read_error:
        read_image(root, "not-a-digest")

    assert write_error.value.code == "ASSET_DIGEST_MISMATCH"
    assert read_error.value.code == "ASSET_DIGEST_MISMATCH"
    assert not root.exists()
    for error in (write_error.value, read_error.value):
        assert "SENTINEL-PRIVATE-ROOT" not in str(error)
        assert expected not in str(error)


def test_symlinked_vault_root_is_rejected_without_writing_the_target(
    tmp_path: Path, make_link
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "SENTINEL-PRIVATE-SYMLINK"
    make_link(root, target)
    data = b"private-image"
    expected = _digest(data)

    with pytest.raises(AdhelmError) as raised:
        store_image(root, data, expected)

    assert raised.value.code == "UNSAFE_ASSET_PATH"
    assert "SENTINEL-PRIVATE-SYMLINK" not in str(raised.value)
    assert expected not in str(raised.value)
    assert not (target / "assets").exists()


def test_failed_publish_removes_its_owned_temp_file_and_redacts_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    data = b"private-image"
    expected = _digest(data)

    def fail_link(*_args: object, **_kwargs: object) -> None:
        raise OSError("SENTINEL-RAW-LINK-ERROR")

    monkeypatch.setattr(assets.os, "link", fail_link)

    with pytest.raises(AdhelmError) as raised:
        store_image(root, data, expected)

    assert raised.value.code == "ASSET_STORAGE_FAILED"
    assert "SENTINEL-RAW-LINK-ERROR" not in str(raised.value)
    assert _temporary_names(root) == []
    assert not (root / "assets" / "sha256" / expected).exists()


def test_competing_corrupt_publish_is_not_overwritten_or_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    data = b"private-image"
    expected = _digest(data)

    def corrupt_competitor(destination: str, directory: int | None = None) -> None:
        """Publish a corrupt object under the final name, as a competing process would."""
        if directory is None:
            (root / "assets" / "sha256" / destination).write_bytes(b"corrupt competing object")
            return
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            os.write(descriptor, b"corrupt competing object")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    if sys.platform == "win32":

        def competing_publish(_directory: object, _source: str, destination: str) -> None:
            corrupt_competitor(destination)
            raise FileExistsError

        monkeypatch.setattr(assets, "link_child", competing_publish)
    else:

        def competing_publish(
            _source: str,
            destination: str,
            *,
            src_dir_fd: int,
            dst_dir_fd: int,
            follow_symlinks: bool,
        ) -> None:
            del src_dir_fd, follow_symlinks
            corrupt_competitor(destination, dst_dir_fd)
            raise FileExistsError

        monkeypatch.setattr(assets.os, "link", competing_publish)

    with pytest.raises(AdhelmError) as raised:
        store_image(root, data, expected)

    path = root / "assets" / "sha256" / expected
    assert raised.value.code == "ASSET_CORRUPT"
    assert path.read_bytes() == b"corrupt competing object"
    assert _temporary_names(root) == []


def test_read_detects_a_current_object_rewrite_before_returning_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "private"
    data = b"private-image"
    expected = _digest(data)
    path = store_image(root, data, expected)
    original_read = assets.os.read
    rewritten = False

    def rewrite_after_first_read(descriptor: int, size: int) -> bytes:
        nonlocal rewritten
        chunk = original_read(descriptor, size)
        if not rewritten:
            rewritten = True
            path.write_bytes(data + b"-rewritten")
            os.chmod(path, 0o600)
        return chunk

    monkeypatch.setattr(assets.os, "read", rewrite_after_first_read)

    with pytest.raises(AdhelmError) as raised:
        read_image(root, expected)

    assert raised.value.code == "ASSET_CHANGED"
    assert "private" not in str(raised.value)
    assert expected not in str(raised.value)
