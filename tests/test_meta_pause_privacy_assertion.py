"""Regression coverage for structural redaction checks in Meta CLI fixtures."""

from __future__ import annotations

import json

import pytest
from test_meta_pause_planning_integration import _redacted_cli_data


def _digest_with_fixture_digits() -> str:
    value = "a" * 17 + "99999" + "b" * 42
    assert len(value) == 64
    return value


def test_redaction_assertion_allows_fixture_digits_inside_derived_references() -> None:
    """A digest substring is not a semantic disclosure of the fake user ID."""

    digest = _digest_with_fixture_digits()
    output = json.dumps(
        {
            "data": {
                "plan_digest": digest,
                "account_ref": "opaque:" + digest,
                "evidence_ref": "evidence:sha256:" + digest,
                "path": "/safe-output/artifact-" + digest + ".json",
                "recorded_at": "2026-09-06T23:00:00.999990Z",
            }
        }
    )

    # This is the exact predicate that made the former blanket substring
    # assertion fail, despite every occurrence being inside a derived hash.
    assert "99999" in output
    assert _redacted_cli_data(output)["data"]["plan_digest"] == digest


@pytest.mark.parametrize(
    "data",
    [
        {"account_ref": "meta_ads.account.act_99999"},
        {"detail": "provider user 99999"},
        {"principal_ref": "meta_token_principal_v1." + "a" * 64},
        {"path": "/safe-output/99999/artifact-" + _digest_with_fixture_digits() + ".json"},
        {"detail": 99999},
        {"detail": "2026-09-06T23:00:00.999990Z"},
        {"recorded_at": "2026-09-06T23:00:00.999990"},
    ],
)
def test_redaction_assertion_rejects_raw_or_sensitive_semantic_values(
    data: dict[str, object],
) -> None:
    with pytest.raises(AssertionError):
        _redacted_cli_data(json.dumps({"data": data}))
