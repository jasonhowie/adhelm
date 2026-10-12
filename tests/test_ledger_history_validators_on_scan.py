"""Every history validator the ledger imports must run on scan, not only on append."""

from __future__ import annotations

import ast
import pathlib
import re

_LEDGER = pathlib.Path(__file__).resolve().parents[1] / "src" / "adhelm" / "ledger.py"


def _imported_history_validators(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("adhelm."):
            for alias in node.names:
                name = alias.asname or alias.name
                if re.fullmatch(r"validate_[a-z0-9_]+_history", name):
                    names.add(name)
    return names


def _calls_in(function: ast.FunctionDef) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_every_imported_history_validator_runs_on_scan_and_on_append() -> None:
    source = _LEDGER.read_text()
    tree = ast.parse(source)
    validators = _imported_history_validators(tree)
    assert validators, "the ledger imports no history validators"
    functions = {
        node.name: node
        for cls in tree.body
        if isinstance(cls, ast.ClassDef) and cls.name == "Ledger"
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
    }
    scan_calls = _calls_in(functions["_scan_stream"])
    append_calls = _calls_in(functions["append"])
    missing_on_scan = sorted(validators - scan_calls)
    missing_on_append = sorted(validators - append_calls)
    assert not missing_on_scan, f"history validators absent from the scan block: {missing_on_scan}"
    assert not missing_on_append, (
        f"history validators absent from the append path: {missing_on_append}"
    )
