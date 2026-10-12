#!/usr/bin/env python3
"""Smoke-test an installed AdHelm wheel offline, as a fresh machine would run it.

Run it with the interpreter of an environment that holds only the built wheel and its
dependencies, from outside the repository. It checks the `adhelm` console script
(`version`, `discover`, `doctor`), that a write command is refused by the release policy,
and an offline fixture report whose provider transport and credential lookup are fakes.
It never opens a socket to a provider or reads the operating-system credential store.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

PROVIDERS = frozenset(
    {
        "google_ads",
        "meta_ads",
        "linkedin_ads",
        "tiktok_ads",
        "reddit_ads",
        "openai_ads",
        "ga4",
        "gtm",
    }
)
_ACCOUNT = "1234567890"


class SmokeError(RuntimeError):
    """The installed wheel does not behave as the release definition requires."""


def require_installed(module_file: str, purelib: str) -> None:
    """The package under test must come from the environment's site-packages, not a checkout."""
    if not Path(module_file).resolve().is_relative_to(Path(purelib).resolve()):
        raise SmokeError(f"adhelm was imported from {module_file}, not from {purelib}.")


def check_version(envelope: dict[str, Any], expected: str) -> None:
    if envelope.get("outcome") != "OK" or envelope.get("tool_version") != expected:
        raise SmokeError(f"version did not report AdHelm {expected}.")
    if envelope.get("data", {}).get("version") != expected:
        raise SmokeError(f"version data did not report AdHelm {expected}.")
    if envelope.get("data", {}).get("release_state") != "RELEASED_0_1_0":
        raise SmokeError("the installed wheel does not report the released read-only milestone.")


def check_discover(envelope: dict[str, Any]) -> None:
    if envelope.get("outcome") != "OK":
        raise SmokeError("discover did not succeed.")
    data = envelope["data"]
    policy = data.get("release_policy", {})
    unreleased = policy.get("unreleased_commands", [])
    if (
        policy.get("remote_writes") != "UNRELEASED"
        or policy.get("enabled_by") != "NOTHING"
        or not unreleased
        or any(command.get("status") != "UNRELEASED" for command in unreleased)
    ):
        raise SmokeError("discover does not list every write command as UNRELEASED.")
    # The policy list is checked against the installed command catalog, so dropping a write
    # command from the list cannot pass: every `execute *` command and `recover attempt` in the
    # catalog must be named there.
    catalog = data.get("commands", [])
    writes = {
        command
        for command in catalog
        if isinstance(command, str)
        and (command.startswith("execute ") or command == "recover attempt")
    }
    listed = {command.get("command") for command in unreleased}
    if not writes or not writes <= listed:
        raise SmokeError(
            "discover does not list every write command as UNRELEASED: "
            f"missing {sorted(writes - listed)}."
        )
    labels = {entry.get("provider"): entry.get("label") for entry in envelope["provider_labels"]}
    if set(labels) != PROVIDERS or not set(labels.values()) <= {"LIVE_PILOTED", "EXPERIMENTAL"}:
        raise SmokeError("discover does not label each of the eight providers.")


def check_doctor(envelope: dict[str, Any]) -> None:
    """A fresh data directory has no configured provider, so doctor asks for attention."""
    data = envelope.get("data", {})
    if (
        envelope.get("outcome") != "ATTENTION_REQUIRED"
        or data.get("remote_checks_performed") is not False
        or data.get("local_history", {}).get("status") != "VERIFIED"
    ):
        raise SmokeError("doctor did not report a verified local history without remote checks.")


def check_write_refusal(envelope: dict[str, Any]) -> None:
    diagnostics = envelope.get("diagnostics") or [{}]
    if (
        envelope.get("outcome") != "FAILED_CLOSED"
        or diagnostics[0].get("code") != "WRITES_NOT_RELEASED"
    ):
        raise SmokeError("a write command was not refused by the release policy.")


def check_report(envelope: dict[str, Any], output: str) -> None:
    data = envelope.get("data", {})
    rows = data.get("performance_rows", [])
    if (
        envelope.get("outcome") != "OK"
        or len(rows) != 1
        or rows[0].get("spend") != {"currency": "USD", "minor_units": 1}
        or not data.get("retained_record_digest")
    ):
        raise SmokeError("the fixture report did not retain the expected daily row.")
    if _ACCOUNT in output:
        raise SmokeError("the fixture report disclosed its account reference.")


def _decode(output: str, what: str) -> dict[str, Any]:
    try:
        value = json.loads(output)
    except ValueError as error:
        raise SmokeError(f"{what} did not print a JSON result envelope.") from error
    if not isinstance(value, dict):
        raise SmokeError(f"{what} did not print a JSON result envelope.")
    return value


def run_installed(
    executable: str, data_dir: Path, *arguments: str, expected_exit: int = 0
) -> dict[str, Any]:
    """Run the console script with a private data directory and return its envelope."""
    result = subprocess.run(
        [executable, "--data-dir", str(data_dir), *arguments],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    what = f"adhelm {' '.join(arguments)}"
    if result.returncode != expected_exit:
        raise SmokeError(f"{what} exited {result.returncode}, expected {expected_exit}.")
    return _decode(result.stdout, what)


@contextmanager
def _no_network() -> Iterator[None]:
    blocked = AssertionError("the offline smoke must not open a network connection")
    with (
        patch("socket.socket.connect", side_effect=blocked),
        patch("socket.socket.connect_ex", side_effect=blocked),
        patch("socket.getaddrinfo", side_effect=blocked),
    ):
        yield


@contextmanager
def _no_credential_store() -> Iterator[list[str]]:
    """Fail loudly, and record the attempt, if anything reaches the OS credential store.

    The fixture report fakes the credential lookup, so nothing should get this far. Every
    entry point of the `keyring` package, including the backend lookup the CLI uses, raises
    here, so a developer machine with a real backend cannot be read by this smoke unnoticed.
    """
    import keyring

    attempts: list[str] = []

    def refuse(name: str) -> Any:
        def guard(*_: object, **__: object) -> Any:
            attempts.append(name)
            raise AssertionError(f"the offline smoke must not use the credential store ({name})")

        return guard

    names = ("get_keyring", "get_password", "get_credential", "set_password", "delete_password")
    with ExitStack() as stack:
        for name in names:
            stack.enter_context(patch.object(keyring, name, refuse(name)))
        yield attempts


def fixture_report(data_dir: Path) -> None:
    """Run a Google Ads daily report through the installed CLI against a fake transport."""
    import httpx
    from typer.testing import CliRunner

    from adhelm import workflows
    from adhelm.cli import app
    from adhelm.config import save_configuration
    from adhelm.models import Configuration, Profile

    save_configuration(
        data_dir,
        Configuration(
            profiles=[
                Profile(
                    profile_id="google_ads.read",
                    provider="google_ads",
                    account_ref=_ACCOUNT,
                    principal_ref="principal.fixture",
                    client_ref="client.fixture",
                    scopes=["read"],
                    purpose="READ",
                    currency="USD",
                    timezone="UTC",
                    credential_ref="oauth.fixture",
                    login_customer_ref="0987654321",
                )
            ]
        ),
    )
    query = data_dir / "fixture-report.json"
    query.write_text(
        json.dumps(
            {
                "account_ref": _ACCOUNT,
                "report_id": "report.google.ad",
                "query_contract_hash": "a" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
                "source_timezone": "UTC",
                "currency": "USD",
                "level": "AD",
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "customer.currency_code" in json.loads(request.content)["query"]:
            customer = {"id": _ACCOUNT, "currencyCode": "USD", "timeZone": "UTC"}
            return httpx.Response(200, json={"results": [{"customer": customer}]})
        row = {
            "campaign": {"id": "11"},
            "adGroup": {"id": "21"},
            "adGroupAd": {"ad": {"id": "31"}},
            "segments": {"date": "2026-09-01"},
            "metrics": {"impressions": "3", "clicks": "1", "costMicros": "5001"},
        }
        return httpx.Response(200, json={"results": [row]})

    actual_client = httpx.Client

    def fake_client(**kwargs: Any) -> httpx.Client:
        return actual_client(transport=httpx.MockTransport(handler), **kwargs)

    def fake_credential(*_: object, **__: object) -> str:
        return "oauth-fixture"

    with (
        _no_network(),
        _no_credential_store() as credential_store_attempts,
        patch.object(httpx, "Client", fake_client),
        patch.object(workflows, "credential", fake_credential),
        patch.object(workflows, "export_key", lambda: b"r" * 32),
    ):
        result = CliRunner().invoke(
            app,
            [
                "--data-dir",
                str(data_dir),
                "report",
                "google-ads",
                "google_ads.read",
                "--input",
                str(query),
            ],
        )
    if credential_store_attempts:
        raise SmokeError(
            f"the fixture report reached the credential store ({credential_store_attempts})."
        )
    if result.exit_code != 0:
        raise SmokeError("the fixture report command failed.")
    check_report(_decode(result.stdout, "the fixture report"), result.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)
    try:
        import adhelm

        require_installed(adhelm.__file__ or "", sysconfig.get_path("purelib"))
        executable = shutil.which("adhelm", path=sysconfig.get_path("scripts"))
        if executable is None:
            raise SmokeError("the adhelm console script is not installed.")
        with tempfile.TemporaryDirectory() as scratch:
            data_dir = Path(scratch) / "state"
            check_version(run_installed(executable, data_dir, "version"), args.expected_version)
            check_discover(run_installed(executable, data_dir, "discover"))
            check_doctor(run_installed(executable, data_dir, "doctor", expected_exit=3))
            refused = Path(scratch) / "execution-request.json"
            refused.write_text(
                json.dumps({"schema_version": 1, "plan_record_digest": "0" * 64}), encoding="utf-8"
            )
            check_write_refusal(
                run_installed(
                    executable,
                    data_dir,
                    "execute",
                    "openai-pause",
                    "--input",
                    str(refused),
                    expected_exit=3,
                )
            )
            fixture_report(data_dir)
            history = run_installed(executable, data_dir, "history", "verify")
            if history.get("data", {}).get("status") != "VERIFIED":
                raise SmokeError("history verify did not verify the ledger the report wrote.")
    except SmokeError as error:
        print(f"installed wheel smoke failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "PASSED", "version": args.expected_version}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
