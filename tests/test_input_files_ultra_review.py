"""Independent adversarial review of bounded local CLI input files."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from adhelm import cli, input_files, workflows
from adhelm.artifact_cli import ArtifactRenderRequest
from adhelm.cli import app as root_app
from adhelm.creative import MAX_IMAGE_BYTES
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, OutputPreferences, Profile

# A FIFO input would block an unbounded reader forever, so any finite harness
# deadline proves the boundary refuses instead of hanging. The bound is
# generous because interpreter start-up under a loaded host has exceeded two
# seconds without any product delay being involved.
_SUBPROCESS_HARNESS_DEADLINE_SECONDS = 20


def _subprocess_cli(data_directory: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "adhelm", "--data-dir", str(data_directory), *arguments],
        check=False,
        capture_output=True,
        timeout=_SUBPROCESS_HARNESS_DEADLINE_SECONDS,
    )


def _validator_test_module() -> ModuleType:
    """Load the existing wrapper extractor against the live validator source."""
    path = Path(__file__).with_name("test_validate_development.py")
    spec = importlib.util.spec_from_file_location("input_review_validator_tests", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _valid_event(required: object = False) -> bytes:
    return json.dumps(
        {
            "event_contract_ref": "contract.fixture",
            "event_name": "qualified_request",
            "semantics": {
                "semantics_ref": "semantics.fixture",
                "customer_definition": "A declared conversion event.",
                "counting_rule": "Count each qualifying request.",
            },
            "fields": [
                {
                    "name": "consent",
                    "value_type": "BOOLEAN",
                    "required": required,
                }
            ],
        }
    ).encode()


def test_installed_measurement_fifo_fails_within_deadline_before_data_directory(
    tmp_path: Path, make_pipe
) -> None:
    """The previously hanging root command must reject a FIFO before downstream setup."""
    pipe = make_pipe("private-measurement-input")
    data_directory = tmp_path / "data"

    result = _subprocess_cli(
        data_directory,
        "measurement",
        "validate-event",
        "--input",
        str(pipe),
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "INPUT_FILE_INVALID"
    assert result.stderr == b""
    assert b"private-measurement-input" not in result.stdout
    assert not data_directory.exists()


def test_installed_image_fifo_fails_before_decode_or_persistence(tmp_path: Path, make_pipe) -> None:
    """The image command shares the regular-file boundary and cannot hang on a FIFO."""
    pipe = make_pipe("private-image-input")
    data_directory = tmp_path / "data"

    result = _subprocess_cli(
        data_directory,
        "creative",
        "analyze",
        "--ephemeral",
        "--input",
        str(pipe),
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "INPUT_FILE_INVALID"
    assert result.stderr == b""
    assert b"private-image-input" not in result.stdout
    assert not data_directory.exists()


def test_image_analysis_uses_the_shared_regular_file_reader_with_its_fixed_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image boundary cannot replace safe file checks with a local open/read path."""
    selected = tmp_path / "private-image-input"
    data_directory = tmp_path / "data"
    observed: list[tuple[Path, int]] = []

    def reject(path: Path, *, maximum_bytes: int) -> bytes:
        observed.append((path, maximum_bytes))
        raise AdhelmError("INPUT_FILE_INVALID", "fixed local input failure", exit_code=2)

    monkeypatch.setattr(cli, "read_regular_input_file", reject)
    result = CliRunner().invoke(
        root_app,
        [
            "--data-dir",
            str(data_directory),
            "creative",
            "analyze",
            "--ephemeral",
            "--input",
            str(selected),
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "INPUT_FILE_INVALID"
    assert observed == [(selected, MAX_IMAGE_BYTES)]
    assert selected.name not in result.output
    assert not data_directory.exists()


def test_final_regular_file_swap_rejects_before_any_bytes_are_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A post-lstat replacement by another regular file cannot substitute its contents."""
    selected = tmp_path / "request.json"
    selected.write_bytes(b'{"minor_units":5,"currency":"USD"}')
    replacement = tmp_path / "replacement.json"
    private_replacement = b'{"minor_units":6,"currency":"USD","secret":"never-output"}'
    replacement.write_bytes(private_replacement)
    if sys.platform == "win32":
        from adhelm import windows_filesystem

        original_open = windows_filesystem.open_regular_file

        def swap_before_open(path: str, *args: object, **kwargs: object) -> int:
            replacement.replace(selected)
            return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(windows_filesystem, "open_regular_file", swap_before_open)
    else:
        original_open = os.open

        def swap_before_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
            replacement.replace(selected)
            return original_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(input_files.os, "open", swap_before_open)

    def forbidden_read(*_args: object, **_kwargs: object) -> bytes:
        pytest.fail("A replaced regular file must not be read")

    monkeypatch.setattr(input_files.os, "read", forbidden_read)
    with pytest.raises(AdhelmError) as raised:
        input_files.read_json_input_file(selected)

    assert raised.value.code == "INPUT_FILE_CHANGED"
    assert "never-output" not in str(raised.value)


def test_growth_after_initial_metadata_cannot_return_prefix_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that crosses the maximum during descriptor reads fails without a prefix return."""
    selected = tmp_path / "request.json"
    maximum = 65_536
    selected.write_bytes(b"x" * maximum)
    original_read = os.read
    grew = False

    def grow_after_first_read(descriptor: int, count: int) -> bytes:
        nonlocal grew
        chunk = original_read(descriptor, count)
        if not grew:
            grew = True
            with selected.open("ab") as handle:
                handle.write(b"y")
        return chunk

    monkeypatch.setattr(input_files.os, "read", grow_after_first_read)
    with pytest.raises(AdhelmError) as raised:
        input_files.read_regular_input_file(selected, maximum_bytes=maximum)

    assert raised.value.code == "INPUT_TOO_LARGE"
    assert "request.json" not in str(raised.value)


def test_final_path_removal_is_an_observed_change_not_a_fresh_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removal after a successful descriptor read is a TOCTOU change with no returned bytes."""
    selected = tmp_path / "request.json"
    selected.write_bytes(b'{"minor_units":5,"currency":"USD"}')
    original_read = os.read
    removed = False

    def remove_after_read(descriptor: int, count: int) -> bytes:
        nonlocal removed
        chunk = original_read(descriptor, count)
        if not removed:
            removed = True
            selected.unlink()
        return chunk

    monkeypatch.setattr(input_files.os, "read", remove_after_read)
    with pytest.raises(AdhelmError) as raised:
        input_files.read_json_input_file(selected)

    assert raised.value.code == "INPUT_FILE_CHANGED"
    assert "request.json" not in str(raised.value)


def test_primitive_read_error_is_closed_and_descriptor_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw local I/O error cannot leak its text or leave an opened descriptor behind."""
    selected = tmp_path / "request.json"
    selected.write_bytes(b'{"minor_units":5,"currency":"USD"}')
    original_close = os.close
    closed: list[int] = []

    def failing_read(*_args: object, **_kwargs: object) -> bytes:
        raise OSError("private-read-error-sentinel")

    def track_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(input_files.os, "read", failing_read)
    monkeypatch.setattr(input_files.os, "close", track_close)
    with pytest.raises(AdhelmError) as raised:
        input_files.read_json_input_file(selected)

    assert raised.value.code == "INPUT_FILE_INVALID"
    assert len(closed) == 1
    assert "private-read-error-sentinel" not in str(raised.value)


def test_installed_json_command_preserves_original_boolean_type_before_side_effects(
    tmp_path: Path,
) -> None:
    """The helper must not canonicalize a numeric boolean alias into a valid declaration."""
    source = tmp_path / "event.json"
    source.write_bytes(_valid_event(required=1))
    data_directory = tmp_path / "data"

    result = CliRunner().invoke(
        root_app,
        [
            "--data-dir",
            str(data_directory),
            "measurement",
            "validate-event",
            "--input",
            str(source),
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.output)["diagnostics"][0]["code"] == "SCHEMA_VALIDATION_FAILED"
    assert not data_directory.exists()


def test_report_fifo_rejects_before_ledger_key_credential_or_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make_pipe
) -> None:
    """A report input is screened before any credential or provider-facing work."""
    pipe = make_pipe("private-report-input")
    profile = Profile(
        schema_version=1,
        profile_id="ga4-read",
        provider="ga4",
        account_ref="123",
        principal_ref="principal",
        client_ref="client",
        scopes=["https://www.googleapis.com/auth/analytics.readonly"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_source="OS_STORE",
        credential_ref="ga4-read-credential",
    )
    monkeypatch.setattr(
        workflows,
        "_configuration",
        lambda _root: Configuration(profiles=[profile]),
    )

    def prohibited(*_args: object, **_kwargs: object) -> object:
        pytest.fail("FIFO rejection must finish before ledger, key, credential, or HTTP work")

    monkeypatch.setattr(workflows, "Ledger", prohibited)
    monkeypatch.setattr(workflows, "export_key", prohibited)
    monkeypatch.setattr(workflows, "credential", prohibited)
    monkeypatch.setattr(workflows.httpx, "Client", prohibited)
    with pytest.raises(AdhelmError) as raised:
        workflows.run_report(
            tmp_path / "data",
            "ga4",
            profile.profile_id,
            pipe,
            stream_id="456",
        )

    assert raised.value.code == "INPUT_FILE_INVALID"
    assert pipe.name not in str(raised.value)
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("alias", (True, 1.0))
def test_inherited_schema_version_guard_rejects_raw_aliases_in_request_and_nested_config(
    alias: object,
) -> None:
    """StrictModel must protect subclasses without their own version validator."""
    with pytest.raises(ValidationError):
        ArtifactRenderRequest.model_validate({"schema_version": alias})
    with pytest.raises(ValidationError):
        Configuration.model_validate({"schema_version": 1, "output": {"schema_version": alias}})

    assert ArtifactRenderRequest.model_validate({"schema_version": 1}).schema_version == 1
    assert OutputPreferences.model_validate({"schema_version": 1}).schema_version == 1


def test_installed_negative_smoke_rejects_a_float_exit_code_alias(tmp_path: Path) -> None:
    """An equality alias must not turn a non-OS exit code into expected exit two."""
    package = tmp_path / "adhelm"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "credential_lock.py").write_text("def _default_lock_root(): return None\n")
    (package / "credentials.py").write_text("def _backend(): return None\n")
    (package / "cli.py").write_text("def main(): raise SystemExit(2.0)\n")
    wrapper = _validator_test_module()._installed_smoke_wrapper()
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            wrapper,
            "--validation-expected-exit",
            "2",
            "--data-dir",
            str(tmp_path / "state"),
            "discover",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=_SUBPROCESS_HARNESS_DEADLINE_SECONDS,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip() == "Unexpected CLI exit status"
