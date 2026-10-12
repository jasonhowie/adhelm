"""Cooperating writers must not overwrite edits made during OAuth handoff."""

from pathlib import Path

import pytest

from adhelm.canonical import canonical_bytes, digest
from adhelm.config import (
    configuration_transaction,
    load_configuration,
    save_configuration,
    update_configuration,
)
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, OutputPreferences


def test_nested_writer_is_busy_and_retained_transaction_is_closed(tmp_path: Path) -> None:
    original = Configuration()
    save_configuration(tmp_path, original)
    with configuration_transaction(tmp_path) as transaction:
        assert transaction.read() == original
        with pytest.raises(AdhelmError, match="Another CLI") as error:
            save_configuration(tmp_path, original, replace=True)
        assert error.value.code == "CONFIGURATION_BUSY"
    with pytest.raises(AdhelmError) as closed:
        transaction.read()
    assert closed.value.code == "CONFIGURATION_TRANSACTION_CLOSED"
    save_configuration(tmp_path, original, replace=True)


def test_browser_snapshot_rejects_changed_preferences_before_write(tmp_path: Path) -> None:
    original = Configuration()
    save_configuration(tmp_path, original)
    expected = digest(original)
    changed = Configuration(output=OutputPreferences(artifact_mode="OFF"))
    save_configuration(tmp_path, changed, replace=True)
    with configuration_transaction(tmp_path) as transaction:
        with pytest.raises(AdhelmError) as error:
            transaction.require_unchanged(expected)
    assert error.value.code == "CONFIGURATION_CHANGED"
    assert load_configuration(tmp_path) == changed


def test_update_detects_uncoordinated_edit_during_transform(tmp_path: Path) -> None:
    save_configuration(tmp_path, Configuration())
    outside = Configuration(output=OutputPreferences(artifact_mode="OFF"))

    def interrupted_update(current: Configuration) -> Configuration:
        (tmp_path / "config.json").write_bytes(canonical_bytes(outside) + b"\n")
        return Configuration.model_validate(
            {**current.model_dump(), "output": OutputPreferences(artifact_mode="OFF")}
        )

    with pytest.raises(AdhelmError) as error:
        update_configuration(tmp_path, interrupted_update)
    assert error.value.code == "CONFIGURATION_CHANGED"
    assert load_configuration(tmp_path) == outside


def test_failed_transform_releases_lock_and_preserves_config(tmp_path: Path) -> None:
    original = Configuration()
    save_configuration(tmp_path, original)

    def fail(_: Configuration) -> Configuration:
        raise ValueError("synthetic transform failure")

    with pytest.raises(ValueError):
        update_configuration(tmp_path, fail)
    assert load_configuration(tmp_path) == original
    changed = update_configuration(
        tmp_path, lambda _: Configuration(output=OutputPreferences(artifact_mode="OFF"))
    )
    assert load_configuration(tmp_path) == changed


def test_distinct_configuration_roots_do_not_share_a_lock(tmp_path: Path) -> None:
    one, two = tmp_path / "one", tmp_path / "two"
    with configuration_transaction(one):
        save_configuration(two, Configuration())
    assert load_configuration(two) == Configuration()
