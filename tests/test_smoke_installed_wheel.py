"""The installed-wheel smoke accepts the real CLI's output and rejects each broken contract."""

from __future__ import annotations

import copy
import importlib.util
import json
import socket
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from typer.testing import CliRunner

import adhelm
from adhelm.cli import app

_ROOT = Path(__file__).resolve().parents[1]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "smoke_installed_wheel_under_test", _ROOT / "scripts/smoke_installed_wheel.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _script()


def _envelope(data_dir: Path, *arguments: str) -> dict[str, Any]:
    result = CliRunner().invoke(app, ["--data-dir", str(data_dir), *arguments])
    envelope: dict[str, Any] = json.loads(result.stdout)
    return envelope


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "state"


def test_the_real_cli_output_satisfies_every_check(state: Path, tmp_path: Path) -> None:
    smoke.check_version(_envelope(state, "version"), adhelm.__version__)
    smoke.check_discover(_envelope(state, "discover"))
    smoke.check_doctor(_envelope(state, "doctor"))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"schema_version": 1, "plan_record_digest": "0" * 64}))
    smoke.check_write_refusal(_envelope(state, "execute", "openai-pause", "--input", str(request)))


def test_the_offline_fixture_report_runs_against_the_cli_and_leaves_a_verified_ledger(
    state: Path,
) -> None:
    smoke.fixture_report(state)

    verified = _envelope(state, "history", "verify")
    assert verified["data"]["status"] == "VERIFIED"
    assert verified["data"]["streams"]["performance"]["records"] > 0


def test_the_offline_guard_refuses_name_resolution_and_connections() -> None:
    with smoke._no_network():
        with pytest.raises(AssertionError, match="must not open a network"):
            socket.getaddrinfo("example.test", 443)
        with socket.socket() as client, pytest.raises(AssertionError, match="must not open"):
            client.connect(("192.0.2.1", 443))


def test_the_credential_store_guard_refuses_every_keyring_entry_point_and_records_the_attempt() -> (
    None
):
    import keyring

    with smoke._no_credential_store() as attempts:
        for call in (
            lambda: keyring.get_keyring(),
            lambda: keyring.get_password("adhelm", "reference"),
            lambda: keyring.get_credential("adhelm", None),
            lambda: keyring.set_password("adhelm", "reference", "value"),
            lambda: keyring.delete_password("adhelm", "reference"),
        ):
            with pytest.raises(AssertionError, match="must not use the credential store"):
                call()

    assert attempts == [
        "get_keyring",
        "get_password",
        "get_credential",
        "set_password",
        "delete_password",
    ]
    # The guard is scoped: the real entry points are back afterwards.
    assert keyring.get_password.__module__ != smoke.__name__


def test_a_fixture_report_that_reaches_the_credential_store_fails_even_if_the_error_is_swallowed(
    state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import keyring

    from adhelm import workflows

    def swallowing_credential(*_: object, **__: object) -> str:
        try:
            keyring.get_password("adhelm", "reference")
        except AssertionError:
            pass
        return "oauth-fixture"

    real_patch = smoke.patch.object

    def keep_ours(target: object, name: str, *args: object, **kwargs: object) -> Any:
        if target is workflows and name == "credential":
            return real_patch(target, name, swallowing_credential)
        return real_patch(target, name, *args, **kwargs)

    monkeypatch.setattr(smoke.patch, "object", keep_ours)

    with pytest.raises(smoke.SmokeError, match="reached the credential store"):
        smoke.fixture_report(state)


def test_a_version_other_than_the_expected_one_is_refused(state: Path) -> None:
    with pytest.raises(smoke.SmokeError, match="AdHelm 9.9.9"):
        smoke.check_version(_envelope(state, "version"), "9.9.9")


def test_a_discover_that_lists_a_released_write_is_refused(state: Path) -> None:
    envelope = _envelope(state, "discover")
    envelope["data"]["release_policy"]["unreleased_commands"][0]["status"] = "AVAILABLE"

    with pytest.raises(smoke.SmokeError, match="UNRELEASED"):
        smoke.check_discover(envelope)


def test_the_installed_catalog_holds_the_write_commands_the_smoke_checks_against(
    state: Path,
) -> None:
    catalog = _envelope(state, "discover")["data"]["commands"]
    writes = {c for c in catalog if c.startswith("execute ") or c == "recover attempt"}

    assert {
        "execute openai-pause",
        "execute ga4-key-event-create",
        "execute meta-campaign-pause",
        "execute plan",
        "recover attempt",
    } <= writes


@pytest.mark.parametrize(
    "dropped",
    [
        "execute openai-pause",
        "execute ga4-key-event-create",
        "execute meta-campaign-pause",
        "execute plan",
        "recover attempt",
    ],
)
def test_a_write_command_dropped_from_the_unreleased_list_is_refused(
    state: Path, dropped: str
) -> None:
    envelope = _envelope(state, "discover")
    policy = envelope["data"]["release_policy"]
    policy["unreleased_commands"] = [
        entry for entry in policy["unreleased_commands"] if entry["command"] != dropped
    ]

    with pytest.raises(smoke.SmokeError, match=f"missing .*{dropped}"):
        smoke.check_discover(envelope)


def test_a_new_write_command_in_the_catalog_that_is_not_listed_as_unreleased_is_refused(
    state: Path,
) -> None:
    envelope = _envelope(state, "discover")
    envelope["data"]["commands"].append("execute brand-new-write")

    with pytest.raises(smoke.SmokeError, match="brand-new-write"):
        smoke.check_discover(envelope)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda data: data["release_policy"].update(remote_writes="RELEASED"),
        lambda data: data["release_policy"].update(enabled_by="ADHELM_ENABLE_WRITES"),
        lambda data: data["release_policy"].update(unreleased_commands=[]),
    ],
)
def test_a_discover_that_does_not_withhold_writes_is_refused(state: Path, mutation: Any) -> None:
    envelope = _envelope(state, "discover")
    mutation(envelope["data"])

    with pytest.raises(smoke.SmokeError, match="UNRELEASED"):
        smoke.check_discover(envelope)


def test_a_discover_missing_a_provider_label_or_with_an_unknown_one_is_refused(
    state: Path,
) -> None:
    envelope = _envelope(state, "discover")
    missing = copy.deepcopy(envelope)
    missing["provider_labels"].pop()
    unknown = copy.deepcopy(envelope)
    unknown["provider_labels"][0]["label"] = "PRODUCTION"

    for broken in (missing, unknown):
        with pytest.raises(smoke.SmokeError, match="label each of the eight providers"):
            smoke.check_discover(broken)


def test_a_doctor_that_probed_a_provider_is_refused(state: Path) -> None:
    envelope = _envelope(state, "doctor")
    envelope["data"]["remote_checks_performed"] = True

    with pytest.raises(smoke.SmokeError, match="without remote checks"):
        smoke.check_doctor(envelope)


def test_a_write_command_that_was_not_refused_is_refused(state: Path) -> None:
    envelope = _envelope(state, "version")

    with pytest.raises(smoke.SmokeError, match="not refused by the release policy"):
        smoke.check_write_refusal(envelope)


def test_a_fixture_report_with_the_wrong_row_or_a_disclosed_account_is_refused() -> None:
    row = {"spend": {"currency": "USD", "minor_units": 1}}
    good = {"outcome": "OK", "data": {"performance_rows": [row], "retained_record_digest": "d"}}
    smoke.check_report(good, "{}")

    wrong_spend = copy.deepcopy(good)
    wrong_spend["data"]["performance_rows"][0]["spend"]["minor_units"] = 2
    no_digest = copy.deepcopy(good)
    no_digest["data"]["retained_record_digest"] = ""
    for broken in (wrong_spend, no_digest, {"outcome": "OK", "data": {}}):
        with pytest.raises(smoke.SmokeError, match="did not retain the expected daily row"):
            smoke.check_report(broken, "{}")
    with pytest.raises(smoke.SmokeError, match="disclosed its account reference"):
        smoke.check_report(good, '{"account": "1234567890"}')


def test_a_package_imported_from_a_checkout_instead_of_site_packages_is_refused(
    tmp_path: Path,
) -> None:
    site_packages = tmp_path / "venv" / "lib" / "site-packages"
    (site_packages / "adhelm").mkdir(parents=True)

    smoke.require_installed(str(site_packages / "adhelm" / "__init__.py"), str(site_packages))
    with pytest.raises(smoke.SmokeError, match="not from"):
        smoke.require_installed(str(tmp_path / "checkout" / "src" / "adhelm"), str(site_packages))
    # A sibling directory sharing the prefix is not inside site-packages.
    with pytest.raises(smoke.SmokeError, match="not from"):
        smoke.require_installed(
            str(tmp_path / "venv" / "lib" / "site-packages-x"), str(site_packages)
        )


def test_output_that_is_not_a_json_object_is_refused() -> None:
    for output in ("", "not json", "[1, 2]"):
        with pytest.raises(smoke.SmokeError, match="JSON result envelope"):
            smoke._decode(output, "adhelm version")
