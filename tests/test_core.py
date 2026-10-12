import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from adhelm.canonical import canonical_bytes, digest, parse_json
from adhelm.config import effective_output, load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.models import Configuration, Money, OutputPreferences
from adhelm.privacy import opaque_ref, validate_data, validate_destination


def test_canonical_jcs_and_input_precision() -> None:
    assert canonical_bytes({"z": "α", "a": 1}) == '{"a":1,"z":"α"}'.encode()
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    for invalid in [b'{"a":1,"a":2}', b'{"n":NaN}', b"\xff"]:
        with pytest.raises(AdhelmError):
            parse_json(invalid)
    for invalid_value in [float("nan"), 2**54]:
        with pytest.raises(AdhelmError):
            canonical_bytes(invalid_value)
    for invalid_money in [True, 1.5, "10", -1, 2**54]:
        with pytest.raises(ValidationError):
            Money(minor_units=invalid_money, currency="USD")


@pytest.mark.parametrize(
    "value",
    [
        {"nested": {"refresh_token": "not-for-output"}},
        {"text": "contact person@example.test"},
        {"text": "Bearer abcdef"},
        {"text": "(312) 555-1234"},
        {"user_data": {}},
        {"raw_response": {}},
        {"page_text": "text"},
    ],
)
def test_privacy_fails_without_echo(value: object) -> None:
    with pytest.raises(AdhelmError) as error:
        validate_data(value)
    assert error.value.code == "SENSITIVE_DATA_RISK"
    assert "not-for-output" not in str(error.value)
    assert "person@example.test" not in str(error.value)


def test_destination_boundaries() -> None:
    def validate(url: str) -> str:
        return validate_destination(url, allowed_hosts={"example.test"}, allowed_paths={"/demo"})

    assert validate("https://example.test/demo?utm_campaign=test")
    for url in [
        "http://example.test/demo",
        "https://example.test.attacker.test/demo",
        "https://example.test/demo?gclid=abc",
        "https://example.test/demo#private",
        "https://example.test/%252e%252e/demo",
        "https://example.test/patient",
        "https://name:secret@example.test/demo",
        "https://example.test:444/demo",
        "https://example.test/demo?utm_campaign=x&utm_campaign=y",
    ]:
        with pytest.raises(AdhelmError):
            validate(url)


def test_keyed_references() -> None:
    assert opaque_ref(b"a" * 32, "account-123") == opaque_ref(b"a" * 32, "account-123")
    assert opaque_ref(b"a" * 32, "account-123") != opaque_ref(b"b" * 32, "account-123")
    with pytest.raises(AdhelmError):
        opaque_ref(b"short", "account")


def test_defaults_no_writes_and_overrides_not_persisted(tmp_path: Path) -> None:
    root = tmp_path / "data"
    config = load_configuration(root)
    assert not root.exists()
    assert config.profiles == []
    assert config.output.artifact_mode == "ON_REQUEST"
    save_configuration(root, config)
    prior = (root / "config.json").read_bytes()
    changed = effective_output(config.output, {"artifact_mode": "OFF"})
    assert changed.artifact_mode == "OFF"
    assert (root / "config.json").read_bytes() == prior
    with pytest.raises(AdhelmError):
        save_configuration(root, Configuration())
    assert (root / "config.json").read_bytes() == prior
    with pytest.raises(ValidationError):
        OutputPreferences(formats=["unknown"])


def test_cli_unknown_secret_argument_never_echoed(tmp_path: Path) -> None:
    secret_marker = "private-input-must-not-echo"
    result = subprocess.run(
        [sys.executable, "-m", "adhelm", "--data-dir", str(tmp_path), "invented-" + secret_marker],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert secret_marker not in result.stdout + result.stderr
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "INVALID_COMMAND"


def test_cli_invalid_input_has_failing_exit(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "adhelm", "--data-dir", str(tmp_path), "schema", "unknown"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stdout)["outcome"] == "FAILED_CLOSED"


def test_cli_discovery_independent_of_optional_accounts(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "adhelm", "--data-dir", str(tmp_path / "empty"), "discover"],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)["data"]
    assert data["release_state"] == "READY_FOR_USER_AUTHORIZATION"
    assert len(data["providers"]) == 11
    assert not (tmp_path / "empty").exists()
