# ruff: noqa: E501
"""The `audit --provider-rules` route for Reddit Ads over the real read path.

Every record the audit reads is produced by this product's own Reddit adapter:
a fixed inventory read twice and a fixed campaign report, all against
`httpx.MockTransport`.  No provider request, credential-store read, or network
access is made, and the audit itself appends nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

import adhelm.credentials
import adhelm.inventory as inventory
from adhelm import workflows
from adhelm.audits import provider_rule
from adhelm.audits.reddit_ads import RULE_TABLE_VERSION, evaluate
from adhelm.cli import app
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Profile
from adhelm.saved_audit_evidence import load_audit_evidence, retained_account_key

runner = CliRunner()
ACCOUNT = "a2_fixture"
_REAL_HTTP_CLIENT = httpx.Client


def _profile() -> Profile:
    return Profile(
        profile_id="reddit_ads-read",
        provider="reddit_ads",
        account_ref=ACCOUNT,
        principal_ref="principal.fixture",
        client_ref="client.fixture",
        scopes=["adsread"],
        purpose="READ",
        currency="USD",
        timezone="UTC",
        credential_ref="fixture-read-token",
    )


class _Provider:
    """A mutable fake Reddit account; every request it serves is recorded."""

    def __init__(self) -> None:
        self.campaign_status = "ACTIVE"
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/api/v3/ad_accounts/{ACCOUNT}":
            return httpx.Response(
                200, json={"data": {"id": ACCOUNT, "currency": "USD", "time_zone_id": "UTC"}}
            )
        if path.endswith("/reports"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "metrics": [
                            {
                                "campaign_id": "c1",
                                "date": "2026-09-01",
                                "impressions": 100,
                                "clicks": 4,
                                "spend": 1_250_000,
                            }
                        ],
                        "metrics_updated_at": "2026-09-02T00:00:00+00:00",
                    }
                },
            )
        items = {
            "/campaigns": {
                "id": "c1",
                "ad_account_id": ACCOUNT,
                "effective_status": self.campaign_status,
                "objective": "TRAFFIC",
            },
            "/ad_groups": {
                "id": "g1",
                "ad_account_id": ACCOUNT,
                "campaign_id": "c1",
                "effective_status": "ACTIVE",
            },
            "/ads": {
                "id": "ad1",
                "ad_account_id": ACCOUNT,
                "campaign_id": "c1",
                "ad_group_id": "g1",
                "effective_status": "ACTIVE",
            },
        }
        for suffix, item in items.items():
            if path.endswith(suffix):
                return httpx.Response(200, json={"data": [item], "pagination": {}})
        raise AssertionError(path)


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Provider:
    fake = _Provider()
    save_configuration(tmp_path, Configuration(profiles=[_profile()]))
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: _REAL_HTTP_CLIENT(transport=httpx.MockTransport(fake), **kwargs),
    )
    for module in (inventory, workflows):
        monkeypatch.setattr(module, "credential", lambda *_a, **_k: "fixture-read-token")
        monkeypatch.setattr(module, "export_key", lambda: b"k" * 32)
    monkeypatch.setattr(adhelm.credentials, "export_key", lambda: b"p" * 32)
    return fake


def _read_inventory(directory: Path) -> None:
    inventory.run_inventory(directory, "reddit_ads", "reddit_ads-read")


def _retain_report(directory: Path) -> str:
    query = directory / "report.json"
    query.write_text(
        json.dumps(
            {
                "account_ref": ACCOUNT,
                "report_id": "report.reddit.campaign",
                "query_contract_hash": "a" * 64,
                "window_start": "2026-09-01",
                "window_end": "2026-09-01",
            }
        )
    )
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "report",
            "reddit-ads",
            "reddit_ads-read",
            "--input",
            str(query),
        ],
    )
    assert result.exit_code == 0, result.stdout
    return str(json.loads(result.stdout)["data"]["retained_record_digest"])


def _audit(directory: Path, record_digest: str, *extra: str) -> Any:
    policy = directory / "audit-policy.json"
    policy.write_text(
        json.dumps(
            {"maximum_observation_age_seconds": 604800, "require_conversion_evidence": False}
        )
    )
    return runner.invoke(
        app,
        [
            "--data-dir",
            str(directory),
            "audit",
            record_digest,
            "--input",
            str(policy),
            "--provider-rules",
            "--limit",
            "200",
            *extra,
        ],
    )


def test_the_reddit_rule_module_is_reachable_through_the_registry() -> None:
    assert provider_rule("reddit_ads") is evaluate


def test_discover_lists_the_reddit_rule_module_for_an_agent() -> None:
    data = json.loads(runner.invoke(app, ["discover"]).stdout)["data"]
    assert "reddit_ads" in data["provider_audit"]["rule_modules"]


def test_a_report_this_build_produced_reaches_its_own_retained_inventory(
    tmp_path: Path, provider: _Provider
) -> None:
    """The canonical report reference and the bare ledger key name one account."""
    _read_inventory(tmp_path)
    digest = _retain_report(tmp_path)

    result = _audit(tmp_path, digest)

    assert result.exit_code == 0, result.stdout
    envelope = json.loads(result.stdout)
    assert envelope["command"] == "audit --provider-rules"
    assert envelope["outcome"] == "INCOMPLETE_COVERAGE"
    data = envelope["data"]
    assert data["provider"] == "reddit_ads"
    assert data["rules_status"] == "EVALUATED"
    sections = data["sections"]
    assert sections["rule_table_version"] == RULE_TABLE_VERSION
    assert sections["live_provider_reads"] == "NONE_RETAINED_RECORDS_ONLY"
    assert sections["analytics_completeness"] == "BOUNDED_NON_EXHAUSTIVE"
    statuses = {item["dimension"]: item["status"] for item in sections["coverage"]}
    assert len(statuses) == 14
    assert statuses["STRUCTURE"] == "COVERED"
    assert statuses["DELIVERY"] == "PARTIAL"
    assert statuses["DRIFT"] == "NOT_READ"
    assert data["provenance"]["selected_inventory_completeness"] == "COMPLETE_BOUNDED_READ"
    assert data["provenance"]["retained_account_spellings"] == [ACCOUNT]
    # Rule identifiers are pseudonymized on the redacted route, so a rule is
    # recognized by the exact observation prose it emits.
    observations = {item["finding"]["observation"] for item in sections["findings"]}
    assert any(
        "Budget, spend-cap and run-schedule fields are not retained" in o for o in observations
    )


def test_a_second_read_after_an_out_of_band_change_reports_drift_with_an_unknown_actor(
    tmp_path: Path, provider: _Provider
) -> None:
    _read_inventory(tmp_path)
    provider.campaign_status = "PAUSED"
    _read_inventory(tmp_path)
    digest = _retain_report(tmp_path)

    result = _audit(tmp_path, digest)

    assert result.exit_code == 0, result.stdout
    data = json.loads(result.stdout)["data"]
    statuses = {item["dimension"]: item["status"] for item in data["sections"]["coverage"]}
    assert statuses["DRIFT"] == "COVERED"
    observations = data["sections"]["drift_observations"]
    assert [item["change_kind"] for item in observations] == ["STATE_CHANGED"]
    assert observations[0]["observed_actor"] == "UNKNOWN"
    assert observations[0]["changed_paths"] == ["normalized_state.status"]
    assert "PAUSED" not in json.dumps(observations)
    # The contradiction between the paused campaign and its still-active
    # children is a structure finding, never an instruction.
    texts = {item["finding"]["observation"] for item in data["sections"]["findings"]}
    assert any("declares the ACTIVE status while its retained parent does not" in t for t in texts)
    assert {item["operation_kind"] for item in data["sections"]["recommendations"]} == {"OBSERVE"}


def test_the_audit_makes_no_provider_request_and_appends_nothing(
    tmp_path: Path, provider: _Provider
) -> None:
    _read_inventory(tmp_path)
    digest = _retain_report(tmp_path)
    requests = len(provider.requests)
    before = {
        stream: len(Ledger(tmp_path).records(stream)) for stream in ("changes", "performance")
    }

    result = _audit(tmp_path, digest)

    assert result.exit_code == 0, result.stdout
    assert len(provider.requests) == requests
    assert before == {
        stream: len(Ledger(tmp_path).records(stream)) for stream in ("changes", "performance")
    }


def test_another_accounts_evidence_is_refused_not_mixed(
    tmp_path: Path, provider: _Provider
) -> None:
    _read_inventory(tmp_path)
    digest = _retain_report(tmp_path)

    result = _audit(tmp_path, digest, "--account", "a2_other")

    assert result.exit_code != 0
    assert json.loads(result.stdout)["diagnostics"][0]["code"] == "INVENTORY_NOT_RETAINED"


def test_the_loader_names_every_retained_spelling_of_the_account(
    tmp_path: Path, provider: _Provider
) -> None:
    _read_inventory(tmp_path)

    for spelling in (ACCOUNT, f"reddit_ads.account.{ACCOUNT}"):
        _, provenance = load_audit_evidence(
            Ledger(tmp_path), provider="reddit_ads", account_ref=spelling
        )
        assert provenance["retained_account_spellings"] == [ACCOUNT]


def test_the_retained_account_key_joins_exactly_one_reddit_spelling() -> None:
    assert retained_account_key("reddit_ads", "reddit_ads.account.a2_x") == "a2_x"
    assert retained_account_key("reddit_ads", "a2_x") == "a2_x"
    assert retained_account_key("reddit_ads", "a2_x") != retained_account_key("reddit_ads", "a2_y")
    # Another provider's reference is never reduced by the Reddit prefix.
    assert retained_account_key("linkedin_ads", "reddit_ads.account.1") == "reddit_ads.account.1"


@pytest.mark.parametrize("reference", ["reddit_ads.account.", ""])
def test_a_reddit_reference_that_reduces_to_nothing_is_refused(reference: str) -> None:
    with pytest.raises(AdhelmError) as error:
        retained_account_key("reddit_ads", reference)

    assert error.value.code == "INVALID_AUDIT_EVIDENCE_SELECTION"
    assert error.value.exit_code == 2
