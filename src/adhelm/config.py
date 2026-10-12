"""Versioned local config; unknown fields and embedded credentials fail closed."""

import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from pydantic import ValidationError

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.credential_lock import credential_mutation_lock
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, OutputPreferences, validate_configuration
from adhelm.private_directories import ensure_private_directory

if sys.platform == "win32":
    from adhelm import windows_filesystem

_DATA_DIRECTORY_NAME = "adhelm"


def default_data_dir() -> Path:
    """Return the platform-native per-user data root used when `--data-dir` is absent."""
    try:
        if sys.platform == "win32":
            return windows_filesystem.local_app_data() / _DATA_DIRECTORY_NAME
        home = Path.home()
    except (OSError, RuntimeError):
        raise AdhelmError(
            "DATA_DIRECTORY_UNAVAILABLE",
            "The operating-system user directory is unavailable for local data.",
            exit_code=2,
            next_action="Pass an explicit --data-dir.",
        ) from None
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / _DATA_DIRECTORY_NAME
    return home / ".local" / "share" / _DATA_DIRECTORY_NAME


def load_configuration(root: Path) -> Configuration:
    path = root / "config.json"
    if not path.exists():
        return Configuration()
    try:
        with path.open("rb") as file:
            data = parse_json(file.read(1_048_577))
        return Configuration.model_validate(data)
    except (OSError, ValidationError) as exc:
        raise AdhelmError(
            "CONFIGURATION_INVALID",
            "Local configuration failed validation.",
            exit_code=2,
            next_action="Compare configuration with `adhelm schema config`.",
        ) from exc


def save_configuration(root: Path, config: Configuration, *, replace: bool = False) -> None:
    config = validate_configuration(config)
    with configuration_transaction(root) as transaction:
        transaction.save(config, replace=replace)


class ConfigurationTransaction:
    """One cooperating CLI writer, held only around local configuration changes."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._active = True

    def read(self) -> Configuration:
        self._require_active()
        return load_configuration(self._root)

    def require_unchanged(self, expected_digest: str) -> Configuration:
        current = self.read()
        if digest(current) != expected_digest:
            raise AdhelmError(
                "CONFIGURATION_CHANGED",
                "Local configuration changed during the requested operation.",
                next_action="Review the current configuration before a deliberate new action.",
            )
        return current

    def save(self, config: Configuration, *, replace: bool = False) -> None:
        self._require_active()
        _save_configuration(self._root, validate_configuration(config), replace=replace)

    def _require_active(self) -> None:
        if not self._active:
            raise AdhelmError("CONFIGURATION_TRANSACTION_CLOSED", "Configuration lock is closed.")


@contextmanager
def configuration_transaction(root: Path) -> Iterator[ConfigurationTransaction]:
    """Serialize local writers across browser waits using a later digest recheck.

    The lock is per resolved configuration root and OS user. It coordinates this
    CLI's writers, not manual edits or arbitrary local applications. Never hold
    it while waiting for a browser callback. Lock ordering is configuration first,
    credential slot second whenever both are needed.
    """
    normalized = root.resolve()
    # NUL cannot occur in a provider credential reference, so this internal
    # namespace cannot collide with a customer-selected credential slot.
    reference = "configuration\x00" + str(normalized)
    with ExitStack() as stack:
        try:
            stack.enter_context(credential_mutation_lock(reference))
        except AdhelmError as error:
            if error.code == "CREDENTIAL_SETUP_BUSY":
                raise AdhelmError(
                    "CONFIGURATION_BUSY",
                    "Another CLI operation is updating local configuration.",
                    next_action="Wait for that local operation to finish and retry deliberately.",
                ) from None
            raise
        transaction = ConfigurationTransaction(normalized)
        try:
            yield transaction
        finally:
            transaction._active = False


def update_configuration(
    root: Path, update: Callable[[Configuration], Configuration]
) -> Configuration:
    """Read, transform, validate and replace configuration under one writer lock."""
    with configuration_transaction(root) as transaction:
        current = transaction.read()
        updated = validate_configuration(update(current))
        # A callback must not silently overwrite an uncoordinated external edit.
        transaction.require_unchanged(digest(current))
        transaction.save(updated, replace=True)
        return updated


def _save_configuration(root: Path, config: Configuration, *, replace: bool = False) -> None:
    ensure_private_directory(root)
    destination = root / "config.json"
    if destination.exists() and not replace:
        raise AdhelmError("CONFIGURATION_EXISTS", "Configuration already exists; it was preserved.")
    descriptor, name = tempfile.mkstemp(prefix=".config-", dir=root)
    temp = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(canonical_bytes(config) + b"\n")
            file.flush()
            os.fsync(file.fileno())
        if not replace:
            try:
                os.link(temp, destination)
            except FileExistsError as exc:
                raise AdhelmError("CONFIGURATION_EXISTS", "Configuration already exists.") from exc
        else:
            os.replace(temp, destination)
        if os.name != "nt":
            directory = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temp.unlink(missing_ok=True)


def effective_output(
    saved: OutputPreferences, override: dict[str, object] | None = None
) -> OutputPreferences:
    """A one-task override returns a value; it never persists preferences."""
    return OutputPreferences.model_validate({**saved.model_dump(), **(override or {})})
