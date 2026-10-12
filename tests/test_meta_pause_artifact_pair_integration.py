"""Fake builds must pass independent artifact comparison without executing payloads."""

from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest
from test_build_native_approval_artifact import _COMMIT, _build, builder


@cache
def _comparator() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/compare_native_approval_artifacts.py"
    spec = importlib.util.spec_from_file_location("artifact_pair_integration_comparator", path)
    assert spec is not None and spec.loader is not None
    loaded = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = loaded
    spec.loader.exec_module(loaded)
    return loaded


@pytest.mark.parametrize("profile", ["V1", "META_CAMPAIGN_PAUSE_V2"])
@pytest.mark.parametrize("access_group", [None, "ABCDE12345.com.aptsonic.adhelm.host"])
def test_fake_builds_pass_independent_pair_comparison_for_each_closed_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    access_group: str | None,
) -> None:
    first_result, _first_calls, _first_repo, _first_spec, first = _build(
        tmp_path / "first", monkeypatch, profile=profile, access_group=access_group
    )
    second_result, _second_calls, _second_repo, _second_spec, second = _build(
        tmp_path / "second", monkeypatch, profile=profile, access_group=access_group
    )
    report = tmp_path / "comparison.json"
    comparison = _comparator().compare_native_approval_artifacts(first, second, report_path=report)

    assert comparison["profile"] == profile
    assert comparison["source"]["commit"] == _COMMIT
    assert comparison == json.loads(report.read_bytes())
    assert comparison == _comparator().compare_native_approval_artifacts(first, second)
    assert first_result.archive_sha256 == second_result.archive_sha256
    assert len(comparison["files"]) == (
        7 + int(access_group is not None) + int(profile == "META_CAMPAIGN_PAUSE_V2")
    )
    helper = (
        first
        / "AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-meta-campaign-pause-approval-client"
    )
    assert helper.exists() is (profile == "META_CAMPAIGN_PAUSE_V2")
    default = (
        Path(__file__).resolve().parents[1]
        / "src/adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"
    ).read_bytes()
    with zipfile.ZipFile(first / builder()._WHEEL_NAME) as archive:
        binding = archive.read(
            "adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py"
        )
    assert (binding == default) is (profile == "V1")
