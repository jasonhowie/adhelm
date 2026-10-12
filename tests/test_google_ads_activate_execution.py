"""Google Ads Search hierarchy ACTIVATE on the generic route, with fakes only.

Real here: the P05 activation contract, the P04 CREATE plan this activation
promotes, the three-slot protected-credential lifecycle, the S3 retained record,
the S4 route, a real signed approval receipt from an ephemeral test key, a real
``SafetyKernel``, and a real local ledger.  Fake here: the operating-system
credential store, the HTTP transport, and the *installed* approval bridge.
Nothing contacts Google Ads, a real credential store, a native helper, or a
network socket, and none of this is evidence of installed native authority or
live provider conformance.  **No activation is ever executed live and no spend
is ever incurred by this suite.**

Two build dependencies are deliberately visible: the additive ledger record
kinds ``GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_PLAN`` and
``GOOGLE_ADS_SEARCH_CAMPAIGN_ACTIVATE_READBACK_OBSERVATION`` are registered by
the coordinator's own ``ledger.py`` hunk.  ``_install_record_kinds`` installs
exactly those when this checkout does not already have them, so the suite proves
the whole path either side of that hunk and never hides its absence: the planner
and the adapter each refuse closed, before any credential, when a kind is
missing.
"""

from __future__ import annotations

import json
import re
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_generic_execution_route import _FakeCompositeBridge, _install
from test_google_ads_compiler import _brief as _compiler_brief
from test_google_ads_pause_credentials import (
    _ACCESS,
    _DEVELOPER,
    _installed,
    _MemoryStore,
)

from adhelm import credentials as credentials_module
from adhelm import google_ads_activate_execution as execution
from adhelm import google_ads_create_execution as create_execution
from adhelm import google_ads_pause_planning_session as planning
from adhelm import ledger as ledger_module
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.execution_planning_session import (
    CONFIGURED_EXECUTION_PLANNERS,
    RetainedExecutionPlan,
    compilable_execution_operations,
    configured_execution_planner,
)
from adhelm.generic_execution_workflow import (
    CONFIGURED_EXECUTION_ADAPTERS,
    ExecutionAdapterCapabilities,
    ExecutionPlanExecutionRequest,
    configured_execution_adapter,
    executable_execution_operations,
    execute_retained_execution_plan_with_installed_approval,
    register_execution_adapter,
)
from adhelm.google_ads_activate_execution import (
    GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY,
    GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER,
    _retained_action_for_plan,
)
from adhelm.google_ads_activate_readback_records import (
    GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND,
    GoogleAdsSearchCampaignActivateReadbackObservation,
    pseudonymized_activation_ref,
    validate_google_ads_activate_readback_history,
)
from adhelm.google_ads_create_readback_records import (
    GOOGLE_ADS_CREATE_READBACK_RECORD_KIND,
    GoogleAdsSearchCampaignCreateReadbackObservation,
)
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money
from adhelm.plan_cli import ExecutionPlanRequest
from adhelm.providers import google_ads_activate_adapter as adapter_module
from adhelm.providers.google_ads_activate_adapter import (
    google_ads_search_campaign_activate_mutate_body,
)
from adhelm.providers.google_ads_activate_contract import (
    GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND,
    GoogleAdsSearchCampaignActivatePlanRecord,
    GoogleAdsSearchCampaignActivateSelection,
    activated_projection,
)
from adhelm.providers.google_ads_compiler import compile_google_ads_search_campaign
from adhelm.providers.google_ads_create_contract import (
    GOOGLE_ADS_CREATE_PLAN_RECORD_KIND,
    GoogleAdsSearchCampaignCreatePlanRecord,
    GoogleAdsSearchCampaignCreateSelection,
)
from adhelm.safety import EffectStatus, OperationKind
from adhelm.saved_candidates import retain_candidate

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_CUSTOMER = "1234567890"
_PROVIDER = "google_ads"
_ACCOUNT_REF = f"google_ads.customer.{_CUSTOMER}"
_SEARCH_URL = f"https://googleads.googleapis.com/v25/customers/{_CUSTOMER}/googleAds:search"
_MUTATE_URL = f"https://googleads.googleapis.com/v25/customers/{_CUSTOMER}/googleAds:mutate"
_CAMPAIGN_ID = "5511"
_AD_GROUP_ID = "7722"
_BUDGET_ID = "3311"
_AD_ID = "6601"
_GOLDEN = (
    Path(__file__).parent / "fixtures" / "google_ads_search_campaign_activate_mutate_body.json"
)


def _brief() -> Any:
    """The compiler suite's own brief, bound to this test customer and profile."""

    return _compiler_brief().model_copy(
        update={
            "account_ref": _ACCOUNT_REF,
            "account_timezone": "UTC",
            "total_budget": Money(minor_units=25_000, currency="USD"),
            "start_date": date(2030, 3, 1),
            "end_date": date(2030, 3, 31),
        }
    )


_SPEC = compile_google_ads_search_campaign(_brief())
_CAMPAIGN_NAME = _SPEC.campaign_request.name
_CAMPAIGN_RESOURCE = f"customers/{_CUSTOMER}/campaigns/{_CAMPAIGN_ID}"
_BUDGET_RESOURCE = f"customers/{_CUSTOMER}/campaignBudgets/{_BUDGET_ID}"
_AD_GROUP_RESOURCE = f"customers/{_CUSTOMER}/adGroups/{_AD_GROUP_ID}"
_AD_RESOURCE = f"customers/{_CUSTOMER}/adGroupAds/{_AD_GROUP_ID}~{_AD_ID}"


def _criterion_resource(index: int) -> str:
    return f"customers/{_CUSTOMER}/campaignCriteria/{_CAMPAIGN_ID}~{9000 + index}"


def _keyword_resource(index: int) -> str:
    return f"customers/{_CUSTOMER}/adGroupCriteria/{_AD_GROUP_ID}~{8000 + index}"


# --- the fake provider ------------------------------------------------------


@dataclass
class _FakeGoogleAds:
    """One in-memory customer behind the fixed v25 search and mutate routes.

    It serves both the P04 CREATE planning probe (so a retained CREATE plan
    exists to promote) and the P05 live-hierarchy reads.
    """

    clock: list[datetime]
    customer_status: str = "ENABLED"
    currency: str = "USD"
    timezone: str = "UTC"
    campaign_status: str = "PAUSED"
    child_status: str | None = None
    budget_micros: int = _SPEC.budget.request.total_amount_micros
    start_date_time: str = _SPEC.campaign_request.start_date_time
    end_date_time: str = _SPEC.campaign_request.end_date_time
    approval_status: str | None = "APPROVED"
    bidding_strategy: str = "TARGET_SPEND"
    final_urls: tuple[str, ...] | None = None
    cpc_bid_micros: int | None = 1_500_000
    ad_group_name_override: str | None = None
    activated: bool = False
    applies_mutation: bool = True
    mutate_error: BaseException | None = None
    mutate_response: httpx.Response | None = None
    searches: list[str] = field(default_factory=list)
    mutations: list[dict[str, Any]] = field(default_factory=list)

    # -- request routing ----------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.clock[0] += timedelta(seconds=1)
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        assert request.headers["developer-token"] == _DEVELOPER
        body = json.loads(request.content)
        if str(request.url) == _SEARCH_URL:
            assert body["searchSettings"] == {"returnTotalResultsCount": True}
            return self._search(body["query"])
        assert str(request.url) == _MUTATE_URL
        self.mutations.append(body)
        if self.mutate_error is not None:
            raise self.mutate_error
        if self.applies_mutation:
            self.activated = True
        if self.mutate_response is not None:
            return self.mutate_response
        return httpx.Response(200, json={"mutateOperationResponses": self._results()})

    def _statuses(self) -> tuple[str, str]:
        if self.activated:
            return "ENABLED", "ENABLED"
        return self.campaign_status, self.child_status or self.campaign_status

    def _search(self, query: str) -> httpx.Response:
        if "FROM customer" in query:
            self.searches.append("customer")
            return self._rows("customer", [self._identity()])
        if "FROM campaign WHERE campaign.name IN (" in query:
            self.searches.append("campaign_name_probe")
            return self._rows("campaign.name", [])
        if "campaign.resource_name, campaign.status FROM campaign" in query:
            self.searches.append("campaign_status")
            campaign, _child = self._statuses()
            return self._rows(
                "campaign.status",
                [{"campaign": {"resourceName": _CAMPAIGN_RESOURCE, "status": campaign}}],
            )
        if "FROM campaign WHERE campaign.name" in query:
            self.searches.append("campaign")
            assert f"'{_CAMPAIGN_NAME}'" in query
            return self._rows("campaign", [self._campaign()])
        if "FROM campaign_criterion" in query:
            self.searches.append("campaign_criterion")
            return self._rows("campaign_criterion", self._criteria())
        if "FROM ad_group_criterion" in query:
            self.searches.append("ad_group_criterion")
            return self._rows("ad_group_criterion", self._keywords())
        if "FROM ad_group_ad" in query:
            self.searches.append("ad_group_ad")
            return self._rows("ad_group_ad", [self._ad()])
        if "FROM ad_group" in query:
            self.searches.append("ad_group")
            return self._rows("ad_group", [self._ad_group()])
        raise AssertionError(f"unexpected query: {query}")

    @staticmethod
    def _rows(mask: str, rows: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(
            200,
            json={"results": rows, "totalResultsCount": str(len(rows)), "fieldMask": mask},
        )

    # -- rows ---------------------------------------------------------------

    def _identity(self) -> dict[str, Any]:
        return {
            "customer": {
                "id": _CUSTOMER,
                "resourceName": f"customers/{_CUSTOMER}",
                "currencyCode": self.currency,
                "timeZone": self.timezone,
                "status": self.customer_status,
            }
        }

    def _campaign(self) -> dict[str, Any]:
        campaign = _SPEC.campaign_request
        network = campaign.network_settings
        status, _child = self._statuses()
        return {
            "campaign": {
                "id": _CAMPAIGN_ID,
                "resourceName": _CAMPAIGN_RESOURCE,
                "name": campaign.name,
                "status": status,
                "advertisingChannelType": campaign.advertising_channel_type,
                "biddingStrategyType": self.bidding_strategy,
                "startDateTime": self.start_date_time,
                "endDateTime": self.end_date_time,
                "networkSettings": {
                    "targetGoogleSearch": network.target_google_search,
                    "targetSearchNetwork": network.target_search_network,
                    "targetContentNetwork": network.target_content_network,
                    "targetPartnerSearchNetwork": network.target_partner_search_network,
                    "targetYoutube": network.target_youtube,
                    "targetGoogleTvNetwork": network.target_google_tv_network,
                },
            },
            "campaignBudget": {
                "resourceName": _BUDGET_RESOURCE,
                "explicitlyShared": _SPEC.budget.request.explicitly_shared,
                "period": _SPEC.budget.request.period,
                "deliveryMethod": _SPEC.budget.request.delivery_method,
                "totalAmountMicros": str(self.budget_micros),
            },
        }

    def _criteria(self) -> list[dict[str, Any]]:
        _campaign, child = self._statuses()
        rows: list[dict[str, Any]] = []
        for index, item in enumerate(_SPEC.campaign_criteria):
            request = item.request
            common: dict[str, Any] = {
                "resourceName": _criterion_resource(index),
                "status": child,
                "negative": request.negative,
            }
            if hasattr(request, "location"):
                rows.append(
                    {
                        "campaignCriterion": {
                            "type": "LOCATION",
                            **common,
                            "location": {"geoTargetConstant": request.location.geo_target_constant},
                        }
                    }
                )
            elif hasattr(request, "language"):
                rows.append(
                    {
                        "campaignCriterion": {
                            "type": "LANGUAGE",
                            **common,
                            "language": {"languageConstant": request.language.language_constant},
                        }
                    }
                )
            else:
                schedule = request.ad_schedule
                rows.append(
                    {
                        "campaignCriterion": {
                            "type": "AD_SCHEDULE",
                            **common,
                            "adSchedule": {
                                "dayOfWeek": schedule.day_of_week,
                                "startHour": schedule.start_hour,
                                "startMinute": schedule.start_minute,
                                "endHour": schedule.end_hour,
                                "endMinute": schedule.end_minute,
                            },
                        }
                    }
                )
        return rows

    def _ad_group(self) -> dict[str, Any]:
        _campaign, child = self._statuses()
        row: dict[str, Any] = {
            "id": _AD_GROUP_ID,
            "resourceName": _AD_GROUP_RESOURCE,
            "name": self.ad_group_name_override or _SPEC.ad_group.request.name,
            "type": _SPEC.ad_group.request.type,
            "status": child,
        }
        if self.cpc_bid_micros is not None:
            row["cpcBidMicros"] = str(self.cpc_bid_micros)
        return {"adGroup": row}

    def _keywords(self) -> list[dict[str, Any]]:
        _campaign, child = self._statuses()
        return [
            {
                "adGroupCriterion": {
                    "resourceName": _keyword_resource(index),
                    "status": child,
                    "negative": item.request.negative,
                    "keyword": {
                        "text": item.request.keyword.text,
                        "matchType": item.request.keyword.match_type,
                    },
                }
            }
            for index, item in enumerate(_SPEC.keywords)
        ]

    def _ad(self) -> dict[str, Any]:
        ad = _SPEC.responsive_search_ad.request.ad
        _campaign, child = self._statuses()
        row: dict[str, Any] = {
            "resourceName": _AD_RESOURCE,
            "status": child,
            "ad": {
                "finalUrls": list(ad.final_urls if self.final_urls is None else self.final_urls),
                "responsiveSearchAd": {
                    "headlines": [
                        {"text": item.text} for item in ad.responsive_search_ad.headlines
                    ],
                    "descriptions": [
                        {"text": item.text} for item in ad.responsive_search_ad.descriptions
                    ],
                },
            },
        }
        if self.approval_status is not None:
            row["policySummary"] = {"approvalStatus": self.approval_status}
        return {"adGroupAd": row}

    @staticmethod
    def _results() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = [{"campaignResult": {"resourceName": _CAMPAIGN_RESOURCE}}]
        for index, _item in enumerate(_SPEC.campaign_criteria):
            results.append(
                {"campaignCriterionResult": {"resourceName": _criterion_resource(index)}}
            )
        results.append({"adGroupResult": {"resourceName": _AD_GROUP_RESOURCE}})
        for index, _item in enumerate(_SPEC.keywords):
            results.append({"adGroupCriterionResult": {"resourceName": _keyword_resource(index)}})
        results.append({"adGroupAdResult": {"resourceName": _AD_RESOURCE}})
        return results


_DIGEST_RUN = re.compile(r"[0-9a-f]{64}")


def _non_digest_text(value: Any) -> str:
    """Render a JSON value with every 64-hex digest run removed."""

    return _DIGEST_RUN.sub("", json.dumps(value))


# --- harness ----------------------------------------------------------------


_KIND_MODELS = {
    GOOGLE_ADS_CREATE_PLAN_RECORD_KIND: GoogleAdsSearchCampaignCreatePlanRecord,
    GOOGLE_ADS_CREATE_READBACK_RECORD_KIND: GoogleAdsSearchCampaignCreateReadbackObservation,
    GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND: GoogleAdsSearchCampaignActivatePlanRecord,
    GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND: GoogleAdsSearchCampaignActivateReadbackObservation,
}


def _install_record_kinds(monkeypatch: pytest.MonkeyPatch, *, omit: str | None = None) -> None:
    """Install the coordinator's own additive ``_KINDS`` entries when absent."""

    kinds = ledger_module._KINDS["changes"]
    for kind, model in _KIND_MODELS.items():
        if kind == omit:
            monkeypatch.delitem(kinds, kind, raising=False)
            continue
        if kinds.get(kind) is not model:
            monkeypatch.setitem(kinds, kind, model)


@dataclass(frozen=True)
class _Prepared:
    directory: Path
    google: _FakeGoogleAds
    clock: list[datetime]
    profile_id: str
    create_plan_record_digest: str
    store: _MemoryStore


def current_expiry() -> datetime:
    """A bearer window comfortably wider than the 300 second plan lifetime."""

    return datetime(2030, 1, 2, 12, 0, tzinfo=UTC) + timedelta(minutes=30)


def _retain_create_plan(directory: Path, profile_id: str, *, brief: Any = None) -> str:
    """Run the P04 planner so a retained CREATE plan exists to promote."""

    source = brief if brief is not None else _brief()
    ledger = Ledger(directory)
    candidate_digest = retain_candidate(ledger, source)["record_digest"]
    create_execution.GOOGLE_ADS_CREATE_EXECUTION_PLANNER.plan_configured_execution(
        directory,
        ExecutionPlanRequest(
            schema_version=1,
            profile_id=profile_id,
            provider=_PROVIDER,
            operation="CREATE",
            account_ref=_ACCOUNT_REF,
            resource_ref=f"google_ads.candidate.{candidate_digest}",
            selection=GoogleAdsSearchCampaignCreateSelection(
                candidate_record_digest=candidate_digest, fresh=source
            ),
        ),
        expected_configuration_digest=digest(load_configuration(directory)),
    )
    plans = [
        record
        for record in Ledger(directory).records("changes")
        if record["record_kind"] == GOOGLE_ADS_CREATE_PLAN_RECORD_KIND
    ]
    assert len(plans) == 1
    return str(plans[0]["record_digest"])


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    omit_kind: str | None = None,
    retain_create_plan: bool = True,
    brief: Any = None,
    **google: Any,
) -> _Prepared:
    monkeypatch.delenv("CI", raising=False)
    _install_record_kinds(monkeypatch, omit=omit_kind)
    store, profile, current = _installed(monkeypatch, expires_at=current_expiry())
    export_store = _MemoryStore()
    monkeypatch.setattr(credentials_module, "_backend", lambda: export_store)
    monkeypatch.setattr(
        credentials_module, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    save_configuration(tmp_path, Configuration(profiles=[profile]), replace=True)
    fake = _FakeGoogleAds(clock=current, **google)
    for module in (planning, execution, create_execution, adapter_module):
        monkeypatch.setattr(module, "_now", lambda: current[0])
    monkeypatch.setattr(
        planning,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(fake.handler), trust_env=False),
    )
    record_digest = ""
    if retain_create_plan:
        record_digest = _retain_create_plan(tmp_path, profile.profile_id, brief=brief)
        fake.searches.clear()
    return _Prepared(
        directory=tmp_path,
        google=fake,
        clock=current,
        profile_id=profile.profile_id,
        create_plan_record_digest=record_digest,
        store=store,
    )


def _selection(prepared: _Prepared, **overrides: Any) -> GoogleAdsSearchCampaignActivateSelection:
    values: dict[str, Any] = {
        "create_plan_record_digest": prepared.create_plan_record_digest,
        "campaign_name": _CAMPAIGN_NAME,
    }
    values.update(overrides)
    return GoogleAdsSearchCampaignActivateSelection(**values)


def _request(prepared: _Prepared, **overrides: Any) -> ExecutionPlanRequest:
    values: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": prepared.profile_id,
        "provider": _PROVIDER,
        "operation": "ACTIVATE",
        "account_ref": _ACCOUNT_REF,
        "resource_ref": f"google_ads.create_plan.{prepared.create_plan_record_digest}",
        "activate_selection": _selection(prepared),
    }
    values.update(overrides)
    return ExecutionPlanRequest(**values)


def _plan(prepared: _Prepared, **overrides: Any) -> RetainedExecutionPlan:
    configuration = load_configuration(prepared.directory)
    return GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER.plan_configured_execution(
        prepared.directory,
        _request(prepared, **overrides),
        expected_configuration_digest=digest(configuration),
    )


def _execute(
    prepared: _Prepared,
    retained: RetainedExecutionPlan,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bridge: object | None = None,
) -> Any:
    installed = bridge if bridge is not None else _FakeCompositeBridge()
    _install(monkeypatch, installed)
    return execute_retained_execution_plan_with_installed_approval(
        prepared.directory,
        ExecutionPlanExecutionRequest(plan_record_digest=retained.record_digest),
    )


def _readbacks(directory: Path) -> list[dict[str, Any]]:
    return [
        record
        for record in Ledger(directory).records("changes")
        if record["record_kind"] == GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND
    ]


def _observation(prepared: _Prepared) -> Any:
    """One live observation, read through the same sealed session the route uses."""

    from adhelm.google_ads_activate_planning_session import GoogleAdsLiveHierarchyReader

    configuration = load_configuration(prepared.directory)
    with planning.configured_google_ads_pause_planning_session(
        prepared.directory,
        prepared.profile_id,
        expected_configuration_digest=digest(configuration),
    ) as session:
        return GoogleAdsLiveHierarchyReader(session).observe_live_hierarchy(_CAMPAIGN_NAME)


# --- registration -----------------------------------------------------------


def test_google_ads_activate_is_a_registered_pair_on_both_registries() -> None:
    assert configured_execution_planner(_PROVIDER, OperationKind.ACTIVATE) is (
        GOOGLE_ADS_ACTIVATE_EXECUTION_PLANNER
    )
    assert configured_execution_adapter(_PROVIDER, OperationKind.ACTIVATE) is (
        GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY
    )
    assert (_PROVIDER, OperationKind.ACTIVATE) in dict(CONFIGURED_EXECUTION_ADAPTERS)
    assert (_PROVIDER, OperationKind.ACTIVATE) in dict(CONFIGURED_EXECUTION_PLANNERS)
    assert {"provider": _PROVIDER, "operation": "ACTIVATE"} in executable_execution_operations()


def test_discover_reports_every_registered_google_pair() -> None:
    """P03, P04 and P05 together: four Google pairs, from the registries.

    The assertion is scoped to ``google_ads`` on purpose.  Other provider
    tickets register their own pairs on the same two registries, and this test
    is about P05 not losing one of Google's, not about the global catalogue.
    """

    google = [
        item for item in executable_execution_operations() if item["provider"] == "google_ads"
    ]
    assert google == [
        {"provider": "google_ads", "operation": operation}
        for operation in ("ACTIVATE", "CREATE", "PAUSE", "UPDATE")
    ]
    # ``discover`` renders the same tuple the planner registry holds.
    assert [
        item for item in compilable_execution_operations() if item["provider"] == "google_ads"
    ] == google


def test_the_factory_declares_no_child_egress_without_reading_anything() -> None:
    """One atomic mutate enables the whole hierarchy."""

    assert GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY.declared_capabilities() == (
        ExecutionAdapterCapabilities(child_egress=False)
    )


def test_registering_a_second_google_activate_adapter_is_refused() -> None:
    with pytest.raises(AdhelmError) as refused:
        register_execution_adapter(
            _PROVIDER, OperationKind.ACTIVATE, GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY
        )
    assert refused.value.code == "EXECUTION_ADAPTER_ALREADY_REGISTERED"


def test_repeated_registration_of_the_exact_singletons_is_a_no_operation() -> None:
    execution.register_google_ads_activate_execution()
    assert configured_execution_adapter(_PROVIDER, OperationKind.ACTIVATE) is (
        GOOGLE_ADS_ACTIVATE_EXECUTION_ADAPTER_FACTORY
    )


# --- the golden wire body ---------------------------------------------------


def test_the_mutate_body_is_the_documented_golden_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One atomic mutate, update_mask=status per operation, no temporary IDs."""

    prepared = _prepare(tmp_path, monkeypatch)
    body = google_ads_search_campaign_activate_mutate_body(
        _observation(prepared), customer_id=_CUSTOMER
    )
    assert json.loads(_GOLDEN.read_text()) == body
    assert body["partialFailure"] is False
    assert body["validateOnly"] is False
    assert body["responseContentType"] == "RESOURCE_NAME_ONLY"
    operations = body["mutateOperations"]
    assert operations[0]["campaignOperation"]["update"]["resourceName"] == _CAMPAIGN_RESOURCE
    for operation in operations:
        inner = next(iter(operation.values()))
        assert inner["updateMask"] == "status"
        assert inner["update"]["status"] == "ENABLED"
        assert set(inner) == {"update", "updateMask"}
        assert set(inner["update"]) == {"resourceName", "status"}
    # The campaign budget is never addressed: its status is output-only in v25.
    assert not any("campaignBudgetOperation" in operation for operation in operations)
    assert "-1" not in json.dumps(body)


def test_the_mutate_body_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    observation = _observation(prepared)
    first = google_ads_search_campaign_activate_mutate_body(observation, customer_id=_CUSTOMER)
    second = google_ads_search_campaign_activate_mutate_body(observation, customer_id=_CUSTOMER)
    assert canonical_bytes(first) == canonical_bytes(second)


# --- the planner ------------------------------------------------------------


def test_the_planner_retains_both_records_for_one_plan_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    records = Ledger(tmp_path).records("changes")
    provider_records = [
        record
        for record in records
        if record["record_kind"] == GOOGLE_ADS_ACTIVATE_PLAN_RECORD_KIND
    ]
    neutral_records = [
        record
        for record in records
        if record["record_kind"] == "EXECUTION_PLAN_RETAINED"
        and record["payload"]["operation"] == "ACTIVATE"
    ]
    assert len(provider_records) == 1
    assert len(neutral_records) == 1
    assert provider_records[0]["payload"]["plan_digest"] == retained.plan_digest
    assert retained.record.provider == _PROVIDER
    assert retained.record.operation is OperationKind.ACTIVATE
    assert retained.record.expected_child_egress_count == 1
    assert retained.record.review_source == "COMPILER_CANDIDATE_REVIEW"
    assert retained.record.provenance.live_validated is False
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert canonical_bytes(action.plan.model_dump(mode="json")) == canonical_bytes(
        retained.record.plan.model_dump(mode="json")
    )
    preconditions = action.observation_binding.preconditions
    assert preconditions.observed_hierarchy_status == "EVERY_ENTITY_PAUSED"
    assert preconditions.destination == "FINAL_URLS_PRESENT_ON_EVERY_AD"
    assert preconditions.policy_approval_status == "APPROVED"
    assert preconditions.budget_and_dates == "EXACT_MATCH_WITH_RETAINED_CREATE_PLAN"
    assert preconditions.daily_overdelivery == "NOT_APPLICABLE_NO_AVERAGE_DAILY_BUDGET"
    assert preconditions.conversion_dependency == (
        "NOT_APPLICABLE_TARGET_SPEND_NON_CONVERSION_OBJECTIVE"
    )
    assert action.postcondition.every_entity_configured_status == "ENABLED"
    assert action.postcondition.effective_delivery == "BEGINS_ON_ACTIVATION_VOLUME_NOT_PREDICTED"


def test_the_spend_bound_is_the_budget_total_through_the_s1_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The COMPOSITE subject's per-currency bound is the campaign budget total."""

    from test_execution_plan_records import approval_configuration

    from adhelm.approval_protocol import (
        ApprovalChallengeKind,
        expected_composite_approval_subject,
    )
    from adhelm.execution_plan_records import execution_plan_spend_bounds

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    bounds = retained.record.spend_bounds_by_currency
    assert len(bounds) == 1
    assert bounds[0].currency == "USD"
    assert bounds[0].maximum_minor_units == 25_000
    assert bounds == execution_plan_spend_bounds(retained.record.plan)
    # ACTIVATE is never NOT_SPEND_CAPABLE, and the reviewer's first line says so.
    subject = expected_composite_approval_subject(
        retained.record.plan,
        kind=ApprovalChallengeKind.COMPOSITE_EXECUTION_PLAN,
        grantor_ref="customer-grantor-1",
        review_lines=retained.record.review_lines,
        configuration=approval_configuration(),
    )
    assert [
        {"currency": bound.currency, "maximum_minor_units": bound.maximum_minor_units}
        for bound in subject.spend_bounds_by_currency
    ] == [bound.model_dump(mode="json") for bound in bounds]
    assert retained.record.review_lines[0] == "Maximum incremental spend 25000 minor units USD"
    assert any(
        line == "activation effect: ACTIVATION_BEGINS_DELIVERY_AND_SPEND"
        for line in retained.record.review_lines
    )
    assert any("policy approval status: APPROVED" == line for line in retained.record.review_lines)


def test_the_plan_before_is_the_paused_hierarchy_and_after_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    subplan = retained.record.plan.subplans[0]
    assert subplan.before != subplan.after
    assert subplan.before.revision == "GOOGLE_ADS_SEARCH_CAMPAIGN_LIVE_HIERARCHY_V1"
    assert subplan.after.revision == subplan.before.revision
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert set(action.observation_binding.projection.statuses) == {"PAUSED"}
    assert set(activated_projection(action.observation_binding.projection).statuses) == {"ENABLED"}
    rendered = _non_digest_text(retained.record.plan.model_dump(mode="json"))
    assert "customers/" not in rendered
    assert "resourceName" not in rendered
    for native in (_CAMPAIGN_ID, _AD_GROUP_ID, _BUDGET_ID):
        assert native not in rendered


def test_the_recovery_disposition_records_a_precomputed_pause_plan_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact pause plan that would undo this activation, as a proposal only."""

    from adhelm.providers.google_ads_pause_contract import (
        GoogleAdsCampaignPauseCampaignBaseline,
        GoogleAdsCampaignPauseObservationBinding,
        build_google_ads_campaign_pause_plan,
    )

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    recovery = action.recovery_disposition
    assert recovery.disposition_kind == "RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL"
    assert recovery.recovery_pause_plan_authorization == "NOT_AUTHORIZED_PROPOSAL_ONLY"
    assert recovery.automatic_revert is False
    observation = action.observation_binding
    rebuilt = build_google_ads_campaign_pause_plan(
        workspace_profile_ref=action.workspace_binding.workspace_profile_ref,
        account_binding=action.account_binding,
        execution_binding=action.execution_binding,
        observation_binding=GoogleAdsCampaignPauseObservationBinding(
            campaign=GoogleAdsCampaignPauseCampaignBaseline(
                campaign_ref=f"google_ads.campaign.{_CUSTOMER}.{_CAMPAIGN_ID}",
                campaign_client_ref=_CAMPAIGN_ID,
                account_ref=action.account_binding.account_ref,
                account_client_ref=_CUSTOMER,
            ),
            read_started_at=observation.read_started_at,
            read_finished_at=observation.read_finished_at,
        ),
        expires_at=action.plan.expires_at,
    )
    assert recovery.recovery_pause_plan_digest == rebuilt.plan.plan_digest
    # The proposal is not retained and nothing pauses automatically.
    assert not [
        record
        for record in Ledger(tmp_path).records("changes")
        if record["record_kind"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    ]
    assert retained.record.plan.subplans[0].recovery_actions == (OperationKind.PAUSE,)


def test_the_planner_is_deterministic_for_one_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    first = _plan(prepared)
    action = _retained_action_for_plan(Ledger(tmp_path), first.record.plan)
    assert canonical_bytes(action.to_data()) == canonical_bytes(action.to_data())
    assert action.plan.plan_digest == first.plan_digest


@pytest.mark.parametrize(
    "overrides",
    [
        {"resource_ref": None},
        {"activate_selection": None},
        {"operation": "PAUSE"},
        {"resource_ref": "google_ads.create_plan." + "b" * 64},
    ],
)
def test_the_planner_refuses_malformed_selections_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, **overrides)
    assert refused.value.code == "INVALID_GOOGLE_ADS_ACTIVATE_EXECUTION_SELECTION"
    assert prepared.google.searches == []


def test_the_planner_refuses_a_customer_the_configured_profile_does_not_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, account_ref="google_ads.customer.5555555555")
    assert refused.value.code == "GOOGLE_ADS_EDIT_PROFILE_REQUIRED"
    assert prepared.google.searches == []


def test_a_request_naming_two_provider_selections_is_refused() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ExecutionPlanRequest(
            schema_version=1,
            profile_id="google-ads-pause-edit",
            provider=_PROVIDER,
            operation="ACTIVATE",
            account_ref=_ACCOUNT_REF,
            resource_ref="google_ads.create_plan." + "a" * 64,
            activate_selection=GoogleAdsSearchCampaignActivateSelection(
                create_plan_record_digest="a" * 64, campaign_name=_CAMPAIGN_NAME
            ),
            selection=GoogleAdsSearchCampaignCreateSelection(
                candidate_record_digest="a" * 64, fresh=_brief()
            ),
        )


def test_the_planner_refuses_when_no_create_plan_is_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, retain_create_plan=False)
    prepared = _Prepared(
        directory=prepared.directory,
        google=prepared.google,
        clock=prepared.clock,
        profile_id=prepared.profile_id,
        create_plan_record_digest="c" * 64,
        store=prepared.store,
    )
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACTIVATE_CREATE_PLAN_NOT_RETAINED"
    assert prepared.google.searches == []


def test_the_planner_refuses_when_a_record_kind_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    monkeypatch.delitem(
        ledger_module._KINDS["changes"], GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND, raising=False
    )
    prepared.google.searches.clear()
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACTIVATE_RECORD_KIND_NOT_INSTALLED"
    assert prepared.google.searches == []


@pytest.mark.parametrize("status", ["CANCELED", "CLOSED", "SUSPENDED"])
def test_the_planner_refuses_a_customer_that_is_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    prepared.google.customer_status = status
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACCOUNT_NOT_ENABLED"
    assert prepared.google.mutations == []


def test_the_planner_refuses_an_already_enabled_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-operation is not a plan."""

    prepared = _prepare(tmp_path, monkeypatch, campaign_status="ENABLED")
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACTIVATE_ALREADY_ENABLED"
    assert prepared.google.mutations == []


def test_the_planner_refuses_a_removed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, child_status="REMOVED")
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACTIVATE_MEMBER_REMOVED"
    assert prepared.google.mutations == []


@pytest.mark.parametrize(
    "drift",
    [
        {"budget_micros": 500_000_000},
        {"start_date_time": "2030-02-01 00:00:00"},
        {"end_date_time": "2030-04-30 23:59:59"},
    ],
)
def test_the_planner_refuses_budget_or_date_drift_against_the_create_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: dict[str, Any]
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    for name, value in drift.items():
        setattr(prepared.google, name, value)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACTIVATE_BASELINE_DRIFT"
    assert prepared.google.mutations == []


def test_the_planner_refuses_a_campaign_whose_end_date_time_has_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended = _brief().model_copy(
        update={"start_date": date(2029, 11, 1), "end_date": date(2029, 12, 31)}
    )
    spec = compile_google_ads_search_campaign(ended)
    prepared = _prepare(
        tmp_path,
        monkeypatch,
        brief=ended,
        start_date_time=spec.campaign_request.start_date_time,
        end_date_time=spec.campaign_request.end_date_time,
    )
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACTIVATE_CAMPAIGN_ENDED"
    assert prepared.google.mutations == []


def test_the_planner_refuses_an_ad_with_no_final_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No activation without a destination."""

    prepared = _prepare(tmp_path, monkeypatch, final_urls=())
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code in {"PROVIDER_RESPONSE_INVALID", "GOOGLE_ADS_LIVE_READ_INVALID"}
    assert prepared.google.mutations == []


def test_the_planner_refuses_a_conversion_bidding_strategy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conversion-dependent activation is a separate capability, not a default."""

    prepared = _prepare(tmp_path, monkeypatch, bidding_strategy="MAXIMIZE_CONVERSIONS")
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACTIVATE_BIDDING_STRATEGY_UNSUPPORTED"
    assert prepared.google.mutations == []


def test_the_planner_refuses_a_disapproved_ad(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, approval_status="DISAPPROVED")
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACTIVATE_AD_DISAPPROVED"
    assert prepared.google.mutations == []


def test_an_unread_policy_summary_is_disclosed_rather_than_assumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, approval_status=None)
    retained = _plan(prepared)
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert action.observation_binding.preconditions.policy_approval_status == (
        "NOT_READ_NO_POLICY_SUMMARY_RETURNED"
    )
    assert any(
        line == "policy approval status: NOT_READ_NO_POLICY_SUMMARY_RETURNED"
        for line in retained.record.review_lines
    )


# --- the happy path ---------------------------------------------------------


def test_full_route_from_retained_plan_to_applied_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retained plan -> receipt -> grant -> adapter -> one mutate -> readback."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    bridge = _FakeCompositeBridge()
    result = _execute(prepared, retained, monkeypatch, bridge=bridge)

    assert bridge.calls == 1
    assert result.plan_digest == retained.plan_digest
    assert result.consumed is True
    assert result.recovery_required is False
    assert len(result.results) == 1
    assert result.results[0].provider == _PROVIDER
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED

    assert len(prepared.google.mutations) == 1
    assert prepared.google.mutations[0] == json.loads(_GOLDEN.read_text())

    readbacks = _readbacks(tmp_path)
    assert len(readbacks) == 1
    payload = readbacks[0]["payload"]
    assert payload["plan_digest"] == retained.plan_digest
    assert payload["after_observed_hierarchy"] == (
        "ENABLED_SEARCH_HIERARCHY_MATCHES_PREDICTED_SEMANTICS"
    )
    assert payload["every_entity_configured_status"] == "ENABLED"
    assert payload["recovery_disposition"] == "RETAIN_REFS_PRECOMPUTED_PAUSE_PROPOSAL"
    assert payload["recovery_pause_plan_authorization"] == "NOT_AUTHORIZED_PROPOSAL_ONLY"
    assert payload["authorized_maximum_spend_minor_units"] == 25_000
    assert payload["authorized_currency"] == "USD"
    assert payload["observed_spend"] == "NOT_OBSERVABLE_FROM_THIS_ROUTE"
    assert payload["acknowledged_result_kinds"][0] == "campaignResult"
    assert payload["acknowledged_result_kinds"][-1] == "adGroupAdResult"
    assert payload["campaign_resource_ref"] == pseudonymized_activation_ref(
        "campaign", _CAMPAIGN_RESOURCE
    )
    validate_google_ads_activate_readback_history(Ledger(tmp_path).records("changes"))


def test_no_token_customer_id_or_native_identifier_reaches_the_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    _execute(prepared, retained, monkeypatch)
    payload = _readbacks(tmp_path)[0]["payload"]
    rendered = _non_digest_text(payload)
    for secret in (_ACCESS, _DEVELOPER, _CAMPAIGN_ID, _AD_GROUP_ID, _BUDGET_ID):
        assert secret not in rendered
    assert "customers/" not in rendered
    assert payload["account_ref"] == _ACCOUNT_REF


def test_the_permit_is_claimed_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    claims: list[tuple[str, str, str]] = []
    original = adapter_module.EgressPermit.consume

    def recording(self: Any, **kwargs: Any) -> Any:
        claims.append((kwargs["plan_digest"], kwargs["subplan_digest"], kwargs["attempt_ref"]))
        return original(self, **kwargs)

    monkeypatch.setattr(adapter_module.EgressPermit, "consume", recording)
    _execute(prepared, retained, monkeypatch)
    assert len(claims) == 1
    assert claims[0][0] == retained.plan_digest
    assert len(prepared.google.mutations) == 1


# --- refusals after the permit ----------------------------------------------


def _ambiguous(prepared: _Prepared, retained: RetainedExecutionPlan, monkeypatch: Any) -> Any:
    result = _execute(prepared, retained, monkeypatch)
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.results[0].evidence_refs == ()
    return result


def test_a_partial_failure_envelope_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    prepared.google.mutate_response = httpx.Response(
        200,
        json={
            "mutateOperationResponses": _FakeGoogleAds._results(),
            "partialFailureError": {"code": 3, "message": "one operation failed"},
        },
    )
    _ambiguous(prepared, retained, monkeypatch)
    assert _readbacks(tmp_path) == []


def test_out_of_order_operation_responses_are_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    results = _FakeGoogleAds._results()
    results[0], results[1] = results[1], results[0]
    prepared.google.mutate_response = httpx.Response(
        200, json={"mutateOperationResponses": results}
    )
    _ambiguous(prepared, retained, monkeypatch)
    assert _readbacks(tmp_path) == []


def test_an_ambiguous_transport_leaves_the_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    prepared.google.mutate_error = httpx.ReadTimeout("no answer")
    result = _ambiguous(prepared, retained, monkeypatch)
    assert result.recovery_required is True
    assert Ledger(tmp_path).unresolved_starts()


def test_a_readback_mismatch_is_ambiguous_and_retains_no_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hierarchy that differs from the prediction is never verified."""

    prepared = _prepare(tmp_path, monkeypatch, applies_mutation=False)
    retained = _plan(prepared)
    _ambiguous(prepared, retained, monkeypatch)
    assert _readbacks(tmp_path) == []
    assert len(prepared.google.mutations) == 1


# --- preconditions before egress --------------------------------------------


@pytest.mark.parametrize(
    "drift", [{"customer_status": "SUSPENDED"}, {"currency": "EUR"}, {"timezone": "Etc/GMT-5"}]
)
def test_account_drift_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: dict[str, Any]
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    for name, value in drift.items():
        setattr(prepared.google, name, value)
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "IDENTITY_MISMATCH"
    assert prepared.google.mutations == []
    assert _readbacks(tmp_path) == []
    assert Ledger(tmp_path).unresolved_starts() == []


@pytest.mark.parametrize("drift", [{"budget_micros": 500_000_000}, {"cpc_bid_micros": 2_000_000}])
def test_hierarchy_drift_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: dict[str, Any]
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    for name, value in drift.items():
        setattr(prepared.google, name, value)
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "REMOTE_DRIFT"
    assert prepared.google.mutations == []
    assert _readbacks(tmp_path) == []
    assert Ledger(tmp_path).unresolved_starts() == []


def test_an_unregistered_readback_kind_refuses_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    monkeypatch.delitem(
        ledger_module._KINDS["changes"], GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND, raising=False
    )
    calls = len(prepared.google.searches)
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "GOOGLE_ADS_ACTIVATE_READBACK_RECORD_KIND_NOT_INSTALLED"
    assert prepared.google.mutations == []
    assert len(prepared.google.searches) == calls


def test_an_unconfigured_build_refuses_at_the_bridge_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary build has no installed composite bridge at all."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    calls = len(prepared.google.searches)
    prepared.store.reads.clear()
    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            prepared.directory,
            ExecutionPlanExecutionRequest(plan_record_digest=retained.record_digest),
        )
    assert refused.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert prepared.google.mutations == []
    assert len(prepared.google.searches) == calls
    assert prepared.store.reads == []


def test_an_ambiguous_acknowledgement_is_never_settled_by_a_later_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reconcile reports the current state and never settles."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    prepared.google.mutate_response = httpx.Response(
        200, json={"mutateOperationResponses": _FakeGoogleAds._results()[:-1]}
    )
    result = _ambiguous(prepared, retained, monkeypatch)
    assert result.recovery_required is True
