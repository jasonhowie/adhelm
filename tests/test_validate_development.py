"""Regression coverage for exact-snapshot validator environment isolation."""

import ast
import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType

import pytest


def _validator_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "validate_development.py"
    spec = importlib.util.spec_from_file_location("validate_development_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validator_ignores_inherited_test_and_tool_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "PYTEST_ADDOPTS": "-k selected_only",
        "PYTEST_PLUGINS": "outside_plugin",
        "MYPYPATH": "/outside/mypy",
        "MYPY_CONFIG_FILE": "/outside/mypy.ini",
        "PYTHONOPTIMIZE": "2",
        "PYTHONPATH": "/outside/python",
        "RUFF_CACHE_DIR": "/outside/ruff",
        "PIP_CONFIG_FILE": "/outside/pip.conf",
        "UV_PROJECT": "/outside/project",
        "UV_CACHE_DIR": "/safe/offline-cache",
        "VIRTUAL_ENV": "/outside/venv",
        "GIT_DIR": "/outside/git",
        "HATCH_BUILD_HOOKS_ONLY": "outside",
    }.items():
        monkeypatch.setenv(name, value)

    module = _validator_module()
    environment = module.validation_environment()

    for name in (
        "PYTEST_ADDOPTS",
        "PYTEST_PLUGINS",
        "MYPYPATH",
        "MYPY_CONFIG_FILE",
        "PYTHONOPTIMIZE",
        "PYTHONPATH",
        "RUFF_CACHE_DIR",
        "PIP_CONFIG_FILE",
        "UV_PROJECT",
        "VIRTUAL_ENV",
        "GIT_DIR",
        "HATCH_BUILD_HOOKS_ONLY",
    ):
        assert name not in environment
    assert environment["UV_CACHE_DIR"] == "/safe/offline-cache"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert environment["PYTHONHASHSEED"] == "0"
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"]
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"


def test_validator_extracts_each_archive_copy_without_prior_test_mutations(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    source = b"from archived_commit import value\n"
    with tarfile.open(archive, "w") as bundle:
        member = tarfile.TarInfo("src/adhelm/sample.py")
        member.size = len(source)
        bundle.addfile(member, io.BytesIO(source))

    module = _validator_module()
    first = tmp_path / "first"
    second = tmp_path / "second"
    module.extract_archive(archive, first)
    (first / "src/adhelm/sample.py").write_text("mutated by a check\n")
    module.extract_archive(archive, second)

    assert (second / "src/adhelm/sample.py").read_bytes() == source


def _installed_smoke_wrapper() -> str:
    path = Path(__file__).resolve().parents[1] / "scripts" / "validate_development.py"
    tree = ast.parse(path.read_text())
    installed_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "installed_call"
    ]
    assert len(installed_calls) == 1
    candidates = [
        node.value
        for node in ast.walk(installed_calls[0])
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "from adhelm.cli import main" in node.value
        and "external access forbidden" in node.value
    ]
    assert len(candidates) == 1
    assert any(
        isinstance(node, ast.List)
        and len(node.elts) >= 3
        and isinstance(node.elts[1], ast.Constant)
        and node.elts[1].value == "-c"
        and any(
            isinstance(element, ast.Constant) and element.value == candidates[0]
            for element in node.elts
        )
        for node in ast.walk(installed_calls[0])
    )
    return candidates[0]


def test_validator_installed_smokes_fail_closed_on_network_dns_and_key_store_access(
    tmp_path: Path,
) -> None:
    package = tmp_path / "adhelm"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "credential_lock.py").write_text(
        "from pathlib import Path\n"
        "def _default_lock_root() -> Path:\n"
        "    return Path('/not-the-smoke-root')\n"
    )
    (package / "credentials.py").write_text("def _backend() -> object:\n    return object()\n")
    (package / "cli.py").write_text(
        "import sys\n"
        "import socket\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        "from adhelm import credential_lock, credentials\n"
        "def main() -> None:\n"
        "    guarded = (\n"
        "        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo,\n"
        "        socket.gethostbyname, socket.gethostbyname_ex, socket.gethostbyaddr,\n"
        "        socket.getnameinfo, credentials._backend, subprocess.Popen,\n"
        "    )\n"
        "    if not all(\n"
        "        isinstance(getattr(value, 'side_effect', None), AssertionError)\n"
        "        for value in guarded\n"
        "    ):\n"
        "        raise SystemExit('external access guard missing')\n"
        "    if credential_lock._default_lock_root() != Path(sys.argv[2]) / 'credential-locks':\n"
        "        raise SystemExit('credential lock root is not isolated')\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(tmp_path)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _installed_smoke_wrapper(),
            "--data-dir",
            str(tmp_path / "smoke-state"),
            "discover",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("target", [0, 1])
@pytest.mark.parametrize("selection", ["changed", "missing"])
def test_validator_bridge_source_guard_requires_exact_reviewed_default(
    tmp_path: Path, target: int, selection: str
) -> None:
    module = _validator_module()
    for member, _digest in module._UNCONFIGURED_BRIDGE_SOURCES:
        destination = tmp_path / "src" / member
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = Path(__file__).resolve().parents[1] / "src" / member
        destination.write_bytes(source.read_bytes())
    module.require_unconfigured_bridge_source(tmp_path)
    destination = tmp_path / "src" / module._UNCONFIGURED_BRIDGE_SOURCES[target][0]
    if selection == "missing":
        destination.unlink()
    else:
        destination.write_text("raise AssertionError('configured source must not be imported')\n")
    with pytest.raises(RuntimeError, match="exact unconfigured bridge source"):
        module.require_unconfigured_bridge_source(tmp_path)


@pytest.mark.parametrize("selection", ["default", "changed", "missing", "duplicate"])
@pytest.mark.parametrize("target", [0, 1])
def test_validator_bridge_wheel_guard_never_imports_configured_payload(
    tmp_path: Path, selection: str, target: int
) -> None:
    module = _validator_module()
    wheel = tmp_path / "synthetic.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for index, (member, _digest) in enumerate(module._UNCONFIGURED_BRIDGE_SOURCES):
            default = (Path(__file__).resolve().parents[1] / "src" / member).read_bytes()
            selected = selection if index == target else "default"
            if selected != "missing":
                archive.writestr(
                    member,
                    b"raise AssertionError('must not be executed')\n"
                    if selected == "changed"
                    else default,
                )
            if selected == "duplicate":
                with pytest.warns(UserWarning, match="Duplicate name"):
                    archive.writestr(member, default)
    if selection == "default":
        module.require_unconfigured_bridge_wheel(wheel)
    else:
        with pytest.raises(RuntimeError, match="must never be installed"):
            module.require_unconfigured_bridge_wheel(wheel)


@pytest.mark.parametrize(
    "expected,actual,passes",
    [
        (2, 2, True),
        (2, 0, False),
        (2, 3, False),
        (0, 2, False),
        (0, 0, True),
        (2, 2.0, False),
        (0, 0.0, False),
        (0, False, False),
        (3, "ExitCode.PRECONDITION", True),
        (2, "ExitCode.PRECONDITION", False),
    ],
)
def test_installed_negative_smoke_requires_the_exact_exit_status(
    tmp_path: Path, expected: int, actual: int | float | bool | str, passes: bool
) -> None:
    package = tmp_path / "adhelm"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "credential_lock.py").write_text("def _default_lock_root(): return None\n")
    (package / "credentials.py").write_text("def _backend(): return None\n")
    (package / "cli.py").write_text(
        "from enum import IntEnum\n"
        "class ExitCode(IntEnum):\n"
        "    PRECONDITION = 3\n"
        f"def main(): raise SystemExit({actual})\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _installed_smoke_wrapper(),
            "--validation-expected-exit",
            str(expected),
            "--data-dir",
            str(tmp_path / "state"),
            "discover",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert (result.returncode == 0) is passes
    assert result.stdout == ""
    if not passes:
        assert result.stderr.strip() == "Unexpected CLI exit status"


def _main_node() -> ast.FunctionDef:
    path = Path(__file__).resolve().parents[1] / "scripts" / "validate_development.py"
    tree = ast.parse(path.read_text())
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    ]
    assert len(functions) == 1
    return functions[0]


def test_validator_test_bounds_default_to_the_recorded_serial_behavior() -> None:
    module = _validator_module()
    args = module.build_parser().parse_args([])

    assert args.pytest_workers == 1
    assert args.pytest_timeout == 900
    assert args.ultra_timeout == 450
    assert module.pytest_configuration_record(args) == {
        "workers": 1,
        "dist": "serial",
        "timeout_seconds": 900,
        "ultra_timeout_seconds": 450,
    }
    for cache_name, targets in (("pytest", ["tests"]), ("ultra-pytest", ["tests/a.py"])):
        assert module.pytest_command(
            "python", cache_dir=Path("/cache") / cache_name, targets=targets, workers=1
        ) == [
            "python",
            "-m",
            "pytest",
            "-q",
            "--override-ini",
            "addopts=",
            "--override-ini",
            f"cache_dir={Path('/cache') / cache_name}",
            *targets,
        ]


def test_validator_selected_workers_distribute_both_pytest_commands_by_file() -> None:
    module = _validator_module()
    args = module.build_parser().parse_args(["--pytest-workers", "8"])

    assert args.pytest_workers == 8
    assert module.pytest_configuration_record(args) == {
        "workers": 8,
        "dist": "loadfile",
        "timeout_seconds": 900,
        "ultra_timeout_seconds": 450,
    }
    for cache_name, targets in (("pytest", ["tests"]), ("ultra-pytest", ["tests/a.py"])):
        command = module.pytest_command(
            "python",
            cache_dir=Path("/cache") / cache_name,
            targets=targets,
            workers=args.pytest_workers,
        )
        assert command[-len(targets) - 5 : -len(targets)] == [
            "-p",
            "xdist",
            "-n",
            "8",
            "--dist=loadfile",
        ]
        assert command[-len(targets) :] == targets

    calls = [
        node
        for node in ast.walk(_main_node())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "pytest_command"
    ]
    assert len(calls) == 2
    assert all(
        {keyword.arg for keyword in call.keywords} == {"cache_dir", "targets", "workers"}
        for call in calls
    )


@pytest.mark.parametrize("option", ["--pytest-workers", "--pytest-timeout", "--ultra-timeout"])
@pytest.mark.parametrize("value", ["0", "-1", "-900"])
def test_validator_rejects_nonpositive_worker_counts_and_deadlines(option: str, value: str) -> None:
    module = _validator_module()
    parser = module.build_parser()
    args = parser.parse_args([option, value])

    with pytest.raises(SystemExit) as rejected:
        module.require_positive_bounds(parser, args)

    assert rejected.value.code == 2


@pytest.mark.parametrize(
    "output,expected",
    [
        (
            "3911 passed, 6 skipped in 540.11s",
            {"passed": 3911, "skipped": 6, "failed": 0, "errors": 0},
        ),
        (
            "1 failed, 2 passed, 3 errors in 1.0s",
            {"passed": 2, "skipped": 0, "failed": 1, "errors": 3},
        ),
        (
            "....\n\n3911 passed, 6 skipped in 540.11s (0:09:00)\n",
            {"passed": 3911, "skipped": 6, "failed": 0, "errors": 0},
        ),
        ("=== 1 error in 0.50s ===", {"passed": 0, "skipped": 0, "failed": 0, "errors": 1}),
        ("", None),
        ("collected 0 items\nno tests ran in 0.01s\n", None),
        ("....\nan interrupted run without any summary line\n", None),
    ],
)
def test_validator_pytest_summary_parser_never_guesses_absent_counts(
    output: str, expected: dict[str, int] | None
) -> None:
    module = _validator_module()

    assert module.parse_pytest_summary(output) == expected


def test_validator_record_keeps_existing_fields_and_adds_the_test_configuration() -> None:
    records = [
        node.value
        for node in ast.walk(_main_node())
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "result"
        and isinstance(node.value, ast.Dict)
    ]
    assert len(records) == 1
    keys = [key.value for key in records[0].keys if isinstance(key, ast.Constant)]

    assert len(keys) == len(records[0].keys)
    assert {"pytest_configuration", "check_seconds", "pytest_summary"} <= set(keys)
    assert {
        "source_commit",
        "validated_at",
        "workspace",
        "source_date_epoch",
        "checks",
        "artifacts",
        "runtime_lock_sbom",
        "dependency_license_inventory",
        "release_state",
        "signed_or_published",
    } <= set(keys)
