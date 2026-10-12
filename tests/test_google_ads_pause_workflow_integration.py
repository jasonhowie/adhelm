"""Real private planning composition with only fake protected-store and HTTP edges."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_google_ads_campaign_state import _campaign_response, _identity_response
from test_google_ads_pause_credentials import (
    _ACCESS,
    _GENERIC_SERVICE,
    _installed,
)

from adhelm import google_ads_pause_planning_session as sessions
from adhelm import google_ads_pause_workflow as workflow
from adhelm.config import save_configuration
from adhelm.errors import AdhelmError
from adhelm.google_ads_pause_plan_views import review_google_ads_campaign_pause_plan
from adhelm.google_oauth_credentials import GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE
from adhelm.ledger import Ledger
from adhelm.models import Configuration


@pytest.mark.parametrize("change", ["none", "key-after-pair", "config-at-append"])
def test_complete_planning_composition_with_final_drift_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    monkeypatch.delenv("CI", raising=False)
    store, profile, current = _installed(monkeypatch)
    save_configuration(tmp_path, Configuration(profiles=[profile]))
    monkeypatch.setattr(sessions, "_now", lambda: current[0])
    monkeypatch.setattr(workflow, "_now", lambda: current[0])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            f"https://googleads.googleapis.com/v25/customers/{profile.account_ref}/googleAds:search"
        )
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        assert "developer-token" not in request.headers
        assert request.headers["login-customer-id"] == profile.login_customer_ref
        body = json.loads(request.content)
        assert body["searchSettings"] == {"returnTotalResultsCount": True}
        current[0] += timedelta(seconds=1)
        if "FROM customer" in body["query"]:
            calls.append("customer")
            return httpx.Response(200, json=_identity_response())
        assert "WHERE campaign.id = 42" in body["query"]
        calls.append("campaign")
        if change == "key-after-pair":
            store.values[(_GENERIC_SERVICE, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)] = "c" * 64
        return httpx.Response(200, json=_campaign_response())

    monkeypatch.setattr(
        sessions,
        "_google_ads_http_client",
        lambda: httpx.Client(
            transport=httpx.MockTransport(handler),
            trust_env=False,
        ),
    )
    if change == "config-at-append":
        original_append = Ledger.append

        def append(self: Ledger, *args: Any, **kwargs: Any) -> dict[str, Any]:
            if len(args) > 1 and args[1] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN":
                final_check = kwargs["_before_durable_append"]

                def after_manual_edit() -> None:
                    # A manual local editor does not take the CLI config lock.
                    path = tmp_path / "config.json"
                    data = json.loads(path.read_text())
                    data["output"]["artifact_mode"] = "OFF"
                    path.write_text(json.dumps(data))
                    final_check()

                kwargs["_before_durable_append"] = after_manual_edit
            return original_append(self, *args, **kwargs)

        monkeypatch.setattr(Ledger, "append", append)

    request = workflow.GoogleAdsCampaignPausePlanRequest(
        profile_id=profile.profile_id, campaign_id="42"
    )
    if change == "none":
        retained = workflow.plan_configured_google_ads_campaign_pause(tmp_path, request)
        plans = [
            r
            for r in Ledger(tmp_path).records("changes")
            if r["record_kind"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
        ]
        assert len(plans) == 1
        assert plans[0]["record_digest"] == retained.plan_record_digest
        assert retained.action.observation_binding.read_finished_at == current[0]
        assert retained.action.execution_authority is False
        assert retained.action.plan.subplans[0].max_spend.minor_units == 0
        raw = json.dumps(plans[0])
        for secret in (
            _ACCESS,
            "m56-google-ads-refresh-token",
            "m56-google-client-secret",
        ):
            assert secret not in raw
        review = review_google_ads_campaign_pause_plan(
            plans[0]["payload"],
            opaque_key=b"g" * 32,
            reviewed_at=current[0],
        ).model_dump_json()
        assert "credential-binding:sha256:" not in review
        assert profile.principal_ref not in review
        assert profile.client_ref not in review
    else:
        with pytest.raises(AdhelmError) as raised:
            workflow.plan_configured_google_ads_campaign_pause(tmp_path, request)
        assert (
            raised.value.code
            == {
                "key-after-pair": "GOOGLE_ADS_PAUSE_CREDENTIAL_CHANGED",
                "config-at-append": "CONFIGURATION_CHANGED",
            }[change]
        )
        assert [
            r
            for r in Ledger(tmp_path).records("changes")
            if r["record_kind"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
        ] == []
    assert calls == ["customer", "campaign"]
    assert Ledger(tmp_path).unresolved_starts() == []
