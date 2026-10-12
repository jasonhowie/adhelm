"""Create local state directories privately regardless of the process umask.

``Path.mkdir(mode=0o700, parents=True)`` applies the mode only to the final
component; intermediate directories receive the umask default. On hosts whose
umask permits group writes (Ubuntu's ``0002`` for private user groups) that
leaves a group-writable data directory, which the credential lock rejects as an
untrusted ancestor. Every missing component is therefore created explicitly.
"""

from __future__ import annotations

from pathlib import Path


def ensure_private_directory(path: Path) -> None:
    """Create ``path`` and any missing ancestors with mode ``0o700``.

    Existing directories are left untouched: silently changing the mode of a
    directory the user already owns is not this product's decision to make.
    """

    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        parent = current.parent
        if parent == current:
            break
        current = parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
