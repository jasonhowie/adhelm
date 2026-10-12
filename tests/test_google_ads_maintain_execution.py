"""The four guarded Google Ads Search maintenance routes, with fakes only.

Real here: the P05 maintenance contract, the three-slot protected-credential
lifecycle, the S3 retained record, the S4 route, a real signed approval receipt
from an ephemeral test key, a real ``SafetyKernel``, and a real local ledger.
Fake here: the operating-system credential store, the HTTP transport, and the
*installed* approval bridge.  Nothing contacts Google Ads, a real credential
store, a native helper, or a network socket, and none of this is evidence of
installed native authority or live provider conformance.  **No spend is ever
incurred by this suite and nothing is ever archived live.**

The additive ledger record kinds ``GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_PLAN``
and ``GOOGLE_ADS_SEARCH_CAMPAIGN_MAINTAIN_READBACK_OBSERVATION`` are registered
by the coordinator's own ``ledger.py`` hunk; ``_install_record_kinds`` installs
exactly those when this checkout does not already have them.
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
    _installed,
    _MemoryStore,
)
from typer.testing import CliRunner

import adhelm.cli as root_cli
from adhelm import credentials as credentials_module
from adhelm import execution_planning_session as planning_registry
from adhelm import generic_execution_workflow as workflow_registry
from adhelm import google_ads_maintain_execution as execution
from adhelm import google_ads_maintain_planning_session as maintain_session
from adhelm import google_ads_pause_planning_session as planning
from adhelm import ledger as ledger_module
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError, ExitCode
from adhelm.execution_planning_session import (
    CONFIGURED_EXECUTION_PLANNERS,
    RetainedExecutionPlan,
    configured_execution_planner,
)
from adhelm.generic_execution_workflow import (
    CONFIGURED_EXECUTION_ADAPTERS,
    AttemptRecoveryRequest,
    ExecutionAdapterCapabilities,
    ExecutionPlanExecutionRequest,
    configured_execution_adapter,
    execute_retained_execution_plan_with_installed_approval,
    record_retained_attempt_recovery_with_installed_approval,
    register_execution_adapter,
)
from adhelm.google_ads_maintain_execution import (
    GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY,
    GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER,
    _retained_action_for_plan,
    campaign_name_ref,
)
from adhelm.google_ads_maintain_readback_records import (
    GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND,
    GoogleAdsSearchCampaignMaintainReadbackObservation,
    validate_google_ads_maintain_readback_history,
)
from adhelm.ledger import Ledger
from adhelm.models import Configuration, Money, OutputPreferences
from adhelm.plan_cli import ExecutionPlanRequest
from adhelm.providers import google_ads_maintain_adapter as adapter_module
from adhelm.providers.google_ads_compiler import compile_google_ads_search_campaign
from adhelm.providers.google_ads_maintain_adapter import (
    google_ads_search_campaign_maintain_mutate_body,
)
from adhelm.providers.google_ads_maintain_contract import (
    CAMPAIGN_REFERENCE_SURFACES,
    GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN,
    GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND,
    GoogleAdsAdGroupBidIntent,
    GoogleAdsAdScheduleReplacementIntent,
    GoogleAdsBudgetAmountIntent,
    GoogleAdsCampaignArchiveIntent,
    GoogleAdsCampaignReferenceSurfaceReading,
    GoogleAdsMaintenanceSchedule,
    GoogleAdsSearchCampaignMaintainPlanRecord,
    GoogleAdsSearchCampaignMaintainSelection,
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
_FIXTURES = Path(__file__).parent / "fixtures"

_BID_MICROS = 1_500_000
_NEW_BID_MICROS = 2_250_000
_NEW_BUDGET_MICROS = 500_000_000


def _brief() -> Any:
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

_LOCATIONS = tuple(
    item.request for item in _SPEC.campaign_criteria if hasattr(item.request, "location")
)
_LANGUAGES = tuple(
    item.request for item in _SPEC.campaign_criteria if hasattr(item.request, "language")
)
_SCHEDULES = tuple(
    (
        item.request.ad_schedule.day_of_week,
        item.request.ad_schedule.start_hour,
        item.request.ad_schedule.start_minute,
        item.request.ad_schedule.end_hour,
        item.request.ad_schedule.end_minute,
    )
    for item in _SPEC.campaign_criteria
    if hasattr(item.request, "ad_schedule")
)

#: The replacement schedules every ad-schedule test asks for, sorted exactly as
#: the contract requires.
_REPLACEMENT = tuple(
    sorted(
        (
            GoogleAdsMaintenanceSchedule(
                day_of_week="SATURDAY",
                start_hour=10,
                start_minute="ZERO",
                end_hour=14,
                end_minute="THIRTY",
            ),
            GoogleAdsMaintenanceSchedule(
                day_of_week="SUNDAY",
                start_hour=9,
                start_minute="FIFTEEN",
                end_hour=17,
                end_minute="ZERO",
            ),
        ),
        key=lambda item: item.identity,
    )
)


def _keyword_resource(index: int) -> str:
    return f"customers/{_CUSTOMER}/adGroupCriteria/{_AD_GROUP_ID}~{8000 + index}"


# --- the fake provider ------------------------------------------------------


@dataclass
class _FakeGoogleAds:
    """One in-memory customer that actually applies the maintenance mutation."""

    clock: list[datetime]
    customer_status: str = "ENABLED"
    currency: str = "USD"
    timezone: str = "UTC"
    campaign_status: str = "PAUSED"
    budget_micros: int = _SPEC.budget.request.total_amount_micros
    cpc_bid_micros: int | None = _BID_MICROS
    schedules: tuple[tuple[Any, ...], ...] = _SCHEDULES
    next_criterion_id: int = 9500
    applies_mutation: bool = True
    mutate_error: BaseException | None = None
    mutate_response: httpx.Response | None = None
    searches: list[str] = field(default_factory=list)
    mutations: list[dict[str, Any]] = field(default_factory=list)
    criterion_ids: dict[tuple[Any, ...], int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for index, schedule in enumerate(self.schedules):
            self.criterion_ids[schedule] = 9000 + index

    # -- request routing ----------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.clock[0] += timedelta(seconds=1)
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        assert "developer-token" not in request.headers
        body = json.loads(request.content)
        if str(request.url) == _SEARCH_URL:
            assert body["searchSettings"] == {"returnTotalResultsCount": True}
            return self._search(body["query"])
        assert str(request.url) == _MUTATE_URL
        self.mutations.append(body)
        if self.mutate_error is not None:
            raise self.mutate_error
        if self.applies_mutation:
            self._apply(body)
        if self.mutate_response is not None:
            return self.mutate_response
        return httpx.Response(200, json={"mutateOperationResponses": self._results(body)})

    def _apply(self, body: dict[str, Any]) -> None:
        removed: list[tuple[Any, ...]] = []
        created: list[tuple[Any, ...]] = []
        for operation in body["mutateOperations"]:
            for name, inner in operation.items():
                if name == "campaignBudgetOperation":
                    self.budget_micros = int(inner["update"]["totalAmountMicros"])
                elif name == "adGroupOperation":
                    self.cpc_bid_micros = int(inner["update"]["cpcBidMicros"])
                elif name == "campaignOperation":
                    self.campaign_status = inner["update"]["status"]
                elif name == "campaignCriterionOperation":
                    if "remove" in inner:
                        removed.append(self._schedule_for(inner["remove"]))
                    else:
                        schedule = inner["create"]["adSchedule"]
                        created.append(
                            (
                                schedule["dayOfWeek"],
                                schedule["startHour"],
                                schedule["startMinute"],
                                schedule["endHour"],
                                schedule["endMinute"],
                            )
                        )
        if removed or created:
            remaining = [item for item in self.schedules if item not in removed]
            for item in created:
                self.criterion_ids[item] = self.next_criterion_id
                self.next_criterion_id += 1
            self.schedules = tuple(remaining + created)

    def _schedule_for(self, resource_name: str) -> tuple[Any, ...]:
        identifier = int(resource_name.split("~")[-1])
        for schedule, value in self.criterion_ids.items():
            if value == identifier:
                return schedule
        raise AssertionError(f"unknown criterion {resource_name}")

    def _search(self, query: str) -> httpx.Response:
        if "FROM customer" in query:
            self.searches.append("customer")
            return self._rows("customer", [self._identity()])
        if "campaign.resource_name, campaign.status FROM campaign" in query:
            self.searches.append("campaign_status")
            if self.campaign_status == "REMOVED":
                return self._rows("campaign.status", [])
            return self._rows(
                "campaign.status",
                [
                    {
                        "campaign": {
                            "resourceName": _CAMPAIGN_RESOURCE,
                            "status": self.campaign_status,
                        }
                    }
                ],
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
        return {
            "campaign": {
                "id": _CAMPAIGN_ID,
                "resourceName": _CAMPAIGN_RESOURCE,
                "name": campaign.name,
                "status": self.campaign_status,
                "advertisingChannelType": campaign.advertising_channel_type,
                "biddingStrategyType": "TARGET_SPEND",
                "startDateTime": campaign.start_date_time,
                "endDateTime": campaign.end_date_time,
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
        rows: list[dict[str, Any]] = []
        for index, request in enumerate(_LOCATIONS):
            rows.append(
                {
                    "campaignCriterion": {
                        "resourceName": (
                            f"customers/{_CUSTOMER}/campaignCriteria/{_CAMPAIGN_ID}~{100 + index}"
                        ),
                        "type": "LOCATION",
                        "status": self.campaign_status,
                        "negative": request.negative,
                        "location": {"geoTargetConstant": request.location.geo_target_constant},
                    }
                }
            )
        for index, request in enumerate(_LANGUAGES):
            rows.append(
                {
                    "campaignCriterion": {
                        "resourceName": (
                            f"customers/{_CUSTOMER}/campaignCriteria/{_CAMPAIGN_ID}~{200 + index}"
                        ),
                        "type": "LANGUAGE",
                        "status": self.campaign_status,
                        "negative": request.negative,
                        "language": {"languageConstant": request.language.language_constant},
                    }
                }
            )
        for schedule in self.schedules:
            day, start_hour, start_minute, end_hour, end_minute = schedule
            rows.append(
                {
                    "campaignCriterion": {
                        "resourceName": (
                            f"customers/{_CUSTOMER}/campaignCriteria/"
                            f"{_CAMPAIGN_ID}~{self.criterion_ids[schedule]}"
                        ),
                        "type": "AD_SCHEDULE",
                        "status": self.campaign_status,
                        "negative": False,
                        "adSchedule": {
                            "dayOfWeek": day,
                            "startHour": start_hour,
                            "startMinute": start_minute,
                            "endHour": end_hour,
                            "endMinute": end_minute,
                        },
                    }
                }
            )
        return rows

    def _ad_group(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "id": _AD_GROUP_ID,
            "resourceName": _AD_GROUP_RESOURCE,
            "name": _SPEC.ad_group.request.name,
            "type": _SPEC.ad_group.request.type,
            "status": self.campaign_status,
        }
        if self.cpc_bid_micros is not None:
            row["cpcBidMicros"] = str(self.cpc_bid_micros)
        return {"adGroup": row}

    def _keywords(self) -> list[dict[str, Any]]:
        return [
            {
                "adGroupCriterion": {
                    "resourceName": _keyword_resource(index),
                    "status": self.campaign_status,
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
        return {
            "adGroupAd": {
                "resourceName": _AD_RESOURCE,
                "status": self.campaign_status,
                "ad": {
                    "finalUrls": list(ad.final_urls),
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
        }

    def _results(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for index, operation in enumerate(body["mutateOperations"]):
            name = next(iter(operation))
            if name == "campaignBudgetOperation":
                results.append({"campaignBudgetResult": {"resourceName": _BUDGET_RESOURCE}})
            elif name == "adGroupOperation":
                results.append({"adGroupResult": {"resourceName": _AD_GROUP_RESOURCE}})
            elif name == "campaignOperation":
                results.append({"campaignResult": {"resourceName": _CAMPAIGN_RESOURCE}})
            else:
                results.append(
                    {
                        "campaignCriterionResult": {
                            "resourceName": (
                                f"customers/{_CUSTOMER}/campaignCriteria/"
                                f"{_CAMPAIGN_ID}~{7000 + index}"
                            )
                        }
                    }
                )
        return results


_DIGEST_RUN = re.compile(r"[0-9a-f]{64}")


def _non_digest_text(value: Any) -> str:
    return _DIGEST_RUN.sub("", json.dumps(value))


# --- harness ----------------------------------------------------------------


_KIND_MODELS = {
    GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND: GoogleAdsSearchCampaignMaintainPlanRecord,
    GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND: GoogleAdsSearchCampaignMaintainReadbackObservation,
}


def _install_record_kinds(monkeypatch: pytest.MonkeyPatch, *, omit: str | None = None) -> None:
    kinds = ledger_module._KINDS["changes"]
    for kind, model in _KIND_MODELS.items():
        if kind == omit:
            monkeypatch.delitem(kinds, kind, raising=False)
            continue
        if kinds.get(kind) is not model:
            monkeypatch.setitem(kinds, kind, model)


def _register_archive_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register ``("google_ads", ARCHIVE)`` for the fake-proof tests only.

    A production build does not register it: no cited read can supply rule 11's
    dependency proof, so the pair would be advertised and never compile.  These
    tests stand in for a *future* build that has one, exactly as
    ``_read_campaign_references`` stands in for its read, and ``monkeypatch``
    removes the entries again so nothing leaks into the global registries.
    """

    monkeypatch.setitem(
        planning_registry._CONFIGURED_EXECUTION_PLANNERS,
        (_PROVIDER, OperationKind.ARCHIVE),
        GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER,
    )
    monkeypatch.setitem(
        workflow_registry._CONFIGURED_EXECUTION_ADAPTERS,
        (_PROVIDER, OperationKind.ARCHIVE),
        GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY,
    )


def _read_campaign_references(monkeypatch: pytest.MonkeyPatch, **dispositions: str) -> None:
    """Fake the read of the campaign reference surfaces this build cannot make.

    No cited v25 resource reaches them, so the real seam reports every surface
    ``NOT_READ`` and an archive is always refused.  A *future* build that reads
    them would report ``READ_EMPTY`` or ``READ_NONEMPTY`` per surface; this
    stands in for that read, exactly as ``_FakeGoogleAds`` stands in for Google.
    Every surface is empty unless a keyword names it.
    """

    def readings(
        _observation: Any,
    ) -> tuple[GoogleAdsCampaignReferenceSurfaceReading, ...]:
        found: list[GoogleAdsCampaignReferenceSurfaceReading] = []
        for surface in CAMPAIGN_REFERENCE_SURFACES:
            disposition = dispositions.get(surface, "READ_EMPTY")
            count = None if disposition == "NOT_READ" else int(disposition == "READ_NONEMPTY")
            found.append(
                GoogleAdsCampaignReferenceSurfaceReading(
                    surface=surface,
                    disposition=disposition,  # type: ignore[arg-type]
                    referencing_row_count=count,
                    basis="FAKE_FUTURE_REFERENCE_READ",
                )
            )
        return tuple(found)

    monkeypatch.setattr(maintain_session, "campaign_reference_surfaces", readings)


@dataclass(frozen=True)
class _Prepared:
    directory: Path
    google: _FakeGoogleAds
    clock: list[datetime]
    profile_id: str
    store: _MemoryStore


def current_expiry() -> datetime:
    return datetime(2030, 1, 2, 12, 0, tzinfo=UTC) + timedelta(minutes=30)


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    omit_kind: str | None = None,
    archive_pair: bool = True,
    **google: Any,
) -> _Prepared:
    monkeypatch.delenv("CI", raising=False)
    if archive_pair:
        _register_archive_pair(monkeypatch)
    _install_record_kinds(monkeypatch, omit=omit_kind)
    store, profile, current = _installed(monkeypatch, expires_at=current_expiry())
    export_store = _MemoryStore()
    monkeypatch.setattr(credentials_module, "_backend", lambda: export_store)
    monkeypatch.setattr(
        credentials_module, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    save_configuration(tmp_path, Configuration(profiles=[profile]), replace=True)
    fake = _FakeGoogleAds(clock=current, **google)
    for module in (planning, execution, adapter_module):
        monkeypatch.setattr(module, "_now", lambda: current[0])
    monkeypatch.setattr(
        planning,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(fake.handler), trust_env=False),
    )
    # A retained compiler candidate is deliberately *not* required: maintenance
    # addresses a hierarchy that already exists, by its configured name.
    retain_candidate(Ledger(tmp_path), _brief())
    return _Prepared(
        directory=tmp_path,
        google=fake,
        clock=current,
        profile_id=profile.profile_id,
        store=store,
    )


_INTENTS: dict[str, Any] = {
    "budget": GoogleAdsBudgetAmountIntent(new_total_amount_micros=_NEW_BUDGET_MICROS),
    "bid": GoogleAdsAdGroupBidIntent(new_cpc_bid_micros=_NEW_BID_MICROS),
    "schedule": GoogleAdsAdScheduleReplacementIntent(schedules=_REPLACEMENT),
    "archive": GoogleAdsCampaignArchiveIntent(
        disposition="IRREVERSIBLE_ARCHIVE", acknowledged_irreversible=True
    ),
}
_OPERATIONS = {"budget": "UPDATE", "bid": "UPDATE", "schedule": "UPDATE", "archive": "ARCHIVE"}
_GOLDEN_NAMES = {
    "budget": "google_ads_search_campaign_maintain_budget_mutate_body.json",
    "bid": "google_ads_search_campaign_maintain_bid_mutate_body.json",
    "schedule": "google_ads_search_campaign_maintain_schedule_mutate_body.json",
    "archive": "google_ads_search_campaign_maintain_archive_mutate_body.json",
}


def _selection(kind: str, **overrides: Any) -> GoogleAdsSearchCampaignMaintainSelection:
    values: dict[str, Any] = {"campaign_name": _CAMPAIGN_NAME, "intent": _INTENTS[kind]}
    values.update(overrides)
    return GoogleAdsSearchCampaignMaintainSelection(**values)


def _request(prepared: _Prepared, kind: str, **overrides: Any) -> ExecutionPlanRequest:
    values: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": prepared.profile_id,
        "provider": _PROVIDER,
        "operation": _OPERATIONS[kind],
        "account_ref": _ACCOUNT_REF,
        "resource_ref": campaign_name_ref(_CAMPAIGN_NAME),
        "maintain_selection": _selection(kind),
    }
    values.update(overrides)
    return ExecutionPlanRequest(**values)


def _plan(prepared: _Prepared, kind: str, **overrides: Any) -> RetainedExecutionPlan:
    configuration = load_configuration(prepared.directory)
    return GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER.plan_configured_execution(
        prepared.directory,
        _request(prepared, kind, **overrides),
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
        if record["record_kind"] == GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND
    ]


def _observation(prepared: _Prepared) -> Any:
    from adhelm.google_ads_maintain_planning_session import GoogleAdsLiveHierarchyReader

    configuration = load_configuration(prepared.directory)
    with planning.configured_google_ads_pause_planning_session(
        prepared.directory,
        prepared.profile_id,
        expected_configuration_digest=digest(configuration),
    ) as session:
        return GoogleAdsLiveHierarchyReader(session).observe_live_hierarchy(_CAMPAIGN_NAME)


# --- registration -----------------------------------------------------------


def test_only_the_update_pair_is_registered_in_a_production_build() -> None:
    """``ARCHIVE`` is delete-class and no cited read supplies rule 11's proof.

    ``discover`` renders these registries, so registering the pair would tell an
    agent a guaranteed-refused operation is compilable.
    """

    assert execution.GOOGLE_ADS_MAINTAIN_REGISTERED_OPERATIONS == (OperationKind.UPDATE,)
    assert configured_execution_planner(_PROVIDER, OperationKind.UPDATE) is (
        GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER
    )
    assert configured_execution_adapter(_PROVIDER, OperationKind.UPDATE) is (
        GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY
    )
    assert (_PROVIDER, OperationKind.ARCHIVE) not in dict(CONFIGURED_EXECUTION_PLANNERS)
    assert (_PROVIDER, OperationKind.ARCHIVE) not in dict(CONFIGURED_EXECUTION_ADAPTERS)
    assert {"provider": _PROVIDER, "operation": "ARCHIVE"} not in (
        workflow_registry.executable_execution_operations()
    )
    assert {"provider": _PROVIDER, "operation": "ARCHIVE"} not in (
        planning_registry.compilable_execution_operations()
    )


def test_the_fake_proof_tests_register_the_archive_pair_only_through_monkeypatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path, monkeypatch)
    assert configured_execution_planner(_PROVIDER, OperationKind.ARCHIVE) is (
        GOOGLE_ADS_MAINTAIN_EXECUTION_PLANNER
    )
    monkeypatch.undo()
    assert configured_execution_planner(_PROVIDER, OperationKind.ARCHIVE) is None
    assert configured_execution_adapter(_PROVIDER, OperationKind.ARCHIVE) is None


def _invoke_unregistered_archive_create(tmp_path: Path, prepared: _Prepared) -> Any:
    """Run the real ``plan execution create`` CLI for a well-formed ARCHIVE request."""

    assert configured_execution_planner(_PROVIDER, OperationKind.ARCHIVE) is None
    request = tmp_path / "archive-request.json"
    request.write_text(
        _request(prepared, "archive").model_dump_json(exclude_none=True), encoding="utf-8"
    )
    prepared.store.reads.clear()
    return CliRunner().invoke(
        root_cli.app,
        [
            "--data-dir",
            str(tmp_path),
            "plan",
            "execution",
            "create",
            "--provider",
            _PROVIDER,
            "--operation",
            "ARCHIVE",
            "--input",
            str(request),
        ],
    )


def _assert_no_plan_retained(tmp_path: Path) -> None:
    catalog = CliRunner().invoke(
        root_cli.app, ["--data-dir", str(tmp_path), "plan", "execution", "catalog"]
    )
    assert json.loads(catalog.output)["data"]["plans"] == []


def test_a_production_archive_request_is_refused_as_not_compilable_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real CLI path, with the ARCHIVE pair deliberately left unregistered.

    Every other archive test registers the pair through ``_register_archive_pair``
    and so exercises the *future-build* rule 11 planner gate.  This one is the
    production behaviour: a complete, well-formed ``CAMPAIGN_ARCHIVE`` request is
    refused by the registry as ``EXECUTION_PLAN_NOT_COMPILABLE`` (not as
    ``GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN``, which is only reachable once the
    pair is registered), after the local configuration and output-bound check but
    before any planner, credential, or provider request, with nothing retained and
    neither the campaign nor the customer named.
    """

    prepared = _prepare(tmp_path, monkeypatch, archive_pair=False)
    result = _invoke_unregistered_archive_create(tmp_path, prepared)

    assert result.exit_code == ExitCode.PRECONDITION
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert [item["code"] for item in envelope["diagnostics"]] == ["EXECUTION_PLAN_NOT_COMPILABLE"]
    assert envelope["data"] == {}
    assert "GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN" not in result.output
    assert _CAMPAIGN_NAME not in result.output
    assert _CUSTOMER not in result.output
    # Neither the credential store nor the fake Google Ads transport was touched.
    assert prepared.store.reads == []
    assert prepared.google.searches == []
    assert prepared.google.mutations == []
    _assert_no_plan_retained(tmp_path)


@pytest.mark.parametrize(
    ("failure", "expected", "expected_exit"),
    [
        ("INVALID_CONFIGURATION", "CONFIGURATION_INVALID", ExitCode.INVALID_INPUT),
        ("OUTPUT_BOUND_TOO_SMALL", "OUTPUT_LIMIT_EXCEEDED", ExitCode.PRECONDITION),
    ],
)
def test_a_local_configuration_or_output_bound_refusal_precedes_not_compilable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected: str,
    expected_exit: int,
) -> None:
    """Pins the documented order: configuration and output bound, then the registry.

    The same unregistered ARCHIVE request is refused by the local check that
    runs first, not as ``EXECUTION_PLAN_NOT_COMPILABLE``, so an operator with a
    broken configuration or too small an output bound is told that and not that
    the operation does not exist.
    """

    prepared = _prepare(tmp_path, monkeypatch, archive_pair=False)
    if failure == "INVALID_CONFIGURATION":
        (tmp_path / "config.json").write_text('{"output": {"max_output_bytes": 1}}')
    else:
        configuration = load_configuration(tmp_path)
        save_configuration(
            tmp_path,
            configuration.model_copy(update={"output": OutputPreferences(max_output_bytes=1024)}),
            replace=True,
        )

    result = _invoke_unregistered_archive_create(tmp_path, prepared)

    assert result.exit_code == expected_exit, result.output
    envelope = json.loads(result.output)
    assert envelope["outcome"] == "FAILED_CLOSED"
    assert [item["code"] for item in envelope["diagnostics"]] == [expected]
    assert "NOT_COMPILABLE" not in result.output
    assert prepared.store.reads == []
    assert prepared.google.searches == []
    assert prepared.google.mutations == []
    if failure == "INVALID_CONFIGURATION":
        # The catalog needs a valid configuration too; remove the invalid one so the
        # default applies.
        (tmp_path / "config.json").unlink()
    _assert_no_plan_retained(tmp_path)


def test_the_factory_declares_no_child_egress_without_reading_anything() -> None:
    assert GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY.declared_capabilities() == (
        ExecutionAdapterCapabilities(child_egress=False)
    )


def test_registering_a_second_maintenance_adapter_is_refused() -> None:
    with pytest.raises(AdhelmError) as refused:
        register_execution_adapter(
            _PROVIDER, OperationKind.UPDATE, GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY
        )
    assert refused.value.code == "EXECUTION_ADAPTER_ALREADY_REGISTERED"


def test_repeated_registration_of_the_exact_singletons_is_a_no_operation() -> None:
    execution.register_google_ads_maintain_execution()
    assert configured_execution_adapter(_PROVIDER, OperationKind.UPDATE) is (
        GOOGLE_ADS_MAINTAIN_EXECUTION_ADAPTER_FACTORY
    )
    assert configured_execution_adapter(_PROVIDER, OperationKind.ARCHIVE) is None


# --- the four golden wire bodies --------------------------------------------


@pytest.mark.parametrize("kind", ["budget", "bid", "schedule", "archive"])
def test_each_mutate_body_is_the_documented_golden_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    observation = _observation(prepared)
    body = google_ads_search_campaign_maintain_mutate_body(
        observation, _INTENTS[kind], customer_id=_CUSTOMER
    )
    assert json.loads((_FIXTURES / _GOLDEN_NAMES[kind]).read_text()) == body
    assert body["partialFailure"] is False
    assert body["validateOnly"] is False
    assert canonical_bytes(body) == canonical_bytes(
        google_ads_search_campaign_maintain_mutate_body(
            observation, _INTENTS[kind], customer_id=_CUSTOMER
        )
    )


def test_the_budget_body_is_one_update_with_the_total_amount_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    body = google_ads_search_campaign_maintain_mutate_body(
        _observation(prepared), _INTENTS["budget"], customer_id=_CUSTOMER
    )
    operations = body["mutateOperations"]
    assert len(operations) == 1
    update = operations[0]["campaignBudgetOperation"]
    assert update["updateMask"] == "totalAmountMicros"
    assert update["update"] == {
        "resourceName": _BUDGET_RESOURCE,
        "totalAmountMicros": str(_NEW_BUDGET_MICROS),
    }


def test_the_bid_body_is_one_update_with_the_cpc_bid_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    body = google_ads_search_campaign_maintain_mutate_body(
        _observation(prepared), _INTENTS["bid"], customer_id=_CUSTOMER
    )
    operations = body["mutateOperations"]
    assert len(operations) == 1
    update = operations[0]["adGroupOperation"]
    assert update["updateMask"] == "cpcBidMicros"
    assert update["update"] == {
        "resourceName": _AD_GROUP_RESOURCE,
        "cpcBidMicros": str(_NEW_BID_MICROS),
    }


def test_the_schedule_body_removes_then_creates_in_one_atomic_mutate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criteria are immutable in v25, so a replacement is remove-then-create."""

    prepared = _prepare(tmp_path, monkeypatch)
    body = google_ads_search_campaign_maintain_mutate_body(
        _observation(prepared), _INTENTS["schedule"], customer_id=_CUSTOMER
    )
    operations = body["mutateOperations"]
    removes = [item for item in operations if "remove" in item["campaignCriterionOperation"]]
    creates = [item for item in operations if "create" in item["campaignCriterionOperation"]]
    assert len(removes) == len(_SCHEDULES)
    assert len(creates) == len(_REPLACEMENT)
    assert operations[: len(removes)] == removes
    assert all("campaignCriterionOperation" in item for item in operations)
    # No temporary resource ID is used: no created criterion is referenced by a
    # later operation in the same request.
    assert "-1" not in json.dumps(body)
    for item in creates:
        create = item["campaignCriterionOperation"]["create"]
        assert create["campaign"] == _CAMPAIGN_RESOURCE
        assert create["negative"] is False
        assert create["status"] == "PAUSED"


def test_the_archive_body_is_one_status_update_to_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    body = google_ads_search_campaign_maintain_mutate_body(
        _observation(prepared), _INTENTS["archive"], customer_id=_CUSTOMER
    )
    operations = body["mutateOperations"]
    assert len(operations) == 1
    update = operations[0]["campaignOperation"]
    assert update["updateMask"] == "status"
    assert update["update"] == {"resourceName": _CAMPAIGN_RESOURCE, "status": "REMOVED"}


# --- the planner ------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "operation", "minor_units"),
    [
        ("budget", OperationKind.UPDATE, 50_000),
        ("bid", OperationKind.UPDATE, 25_000),
        ("schedule", OperationKind.UPDATE, 25_000),
    ],
)
def test_each_kind_retains_both_records_with_its_declared_spend_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    operation: OperationKind,
    minor_units: int,
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, kind)
    records = Ledger(tmp_path).records("changes")
    provider_records = [
        record
        for record in records
        if record["record_kind"] == GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND
    ]
    assert len(provider_records) == 1
    assert retained.record.operation is operation
    assert retained.record.expected_child_egress_count == 1
    assert retained.record.review_source == "COMPILER_CANDIDATE_REVIEW"
    assert retained.record.plan.subplans[0].max_spend.minor_units == minor_units
    assert len(retained.record.spend_bounds_by_currency) == 1
    assert retained.record.spend_bounds_by_currency[0].maximum_minor_units == minor_units
    assert retained.record.review_lines[0] == (
        f"Maximum incremental spend {minor_units} minor units USD"
    )
    assert "dependency proof: NOT_APPLICABLE_NOT_DELETE_CLASS" in retained.record.review_lines


def test_a_bid_change_is_spend_affecting_and_bounded_by_the_campaign_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bid change is never NOT_SPEND_CAPABLE."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "bid")
    assert retained.record.spend_bounds_by_currency != ()
    assert retained.record.spend_bounds_by_currency[0].maximum_minor_units == 25_000
    assert any(
        line == "maintenance effect: AD_GROUP_MAX_CPC_BID_MICROS_CHANGED_SPEND_AFFECTING"
        for line in retained.record.review_lines
    )


def test_the_archive_review_lines_say_irreversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch)
    retained = _plan(prepared, "archive")
    assert "reversibility: IRREVERSIBLE_ARCHIVE" in retained.record.review_lines
    assert "campaign status after: REMOVED" in retained.record.review_lines
    assert "dependency proof: PROVEN_UNREFERENCED" in retained.record.review_lines
    assert (
        "maintenance effect: CAMPAIGN_STATUS_SET_TO_REMOVED_IRREVERSIBLE"
        in retained.record.review_lines
    )
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert action.postcondition.reversibility == "IRREVERSIBLE_ARCHIVE"
    assert action.postcondition.configured_status == "REMOVED"
    assert action.postcondition.predicted_hierarchy_digest is None
    proof = action.observation_binding.preconditions.dependency_proof
    assert proof is not None
    assert proof.status == "PROVEN_UNREFERENCED"
    assert [item.surface for item in proof.surfaces] == list(CAMPAIGN_REFERENCE_SURFACES)


def test_an_archive_retains_a_zero_spend_bound_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch)
    retained = _plan(prepared, "archive")
    assert retained.record.operation is OperationKind.ARCHIVE
    assert retained.record.expected_child_egress_count == 1
    assert retained.record.plan.subplans[0].max_spend.minor_units == 0
    assert retained.record.spend_bounds_by_currency == ()


def test_an_archive_is_refused_without_a_dependency_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Paused status and an irreversibility acknowledgement are not rule 11's proof.

    No cited v25 resource reaches any campaign reference surface, so the real
    seam reports each one ``NOT_READ``.  A campaign that is already paused,
    asked for with the exact acknowledgement, is still refused: AGENTS rule 11
    names paused status as never enough, and nothing is retained or sent.
    """

    prepared = _prepare(tmp_path, monkeypatch, campaign_status="PAUSED")
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, "archive")
    assert refused.value.code == GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN
    for surface in CAMPAIGN_REFERENCE_SURFACES:
        assert f"{surface} (NOT_READ)" in refused.value.message
    assert prepared.google.mutations == []
    assert [
        record
        for record in Ledger(tmp_path).records("changes")
        if record["record_kind"] == GOOGLE_ADS_MAINTAIN_PLAN_RECORD_KIND
    ] == []


@pytest.mark.parametrize("surface", CAMPAIGN_REFERENCE_SURFACES)
@pytest.mark.parametrize("disposition", ["READ_NONEMPTY", "NOT_READ"])
def test_one_unproven_reference_surface_refuses_the_archive_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str, disposition: str
) -> None:
    """A single referenced or unreadable surface withholds the whole proof."""

    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch, **{surface: disposition})
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, "archive")
    assert refused.value.code == GOOGLE_ADS_ARCHIVE_DEPENDENCY_NOT_PROVEN
    assert f"{surface} ({disposition})" in refused.value.message
    assert refused.value.message.count("(") == 1
    assert prepared.google.mutations == []


def test_no_other_maintenance_kind_needs_or_carries_a_dependency_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the delete-class kind is gated; the unreadable surfaces do not block an update."""

    prepared = _prepare(tmp_path, monkeypatch)
    for kind in ("budget", "bid", "schedule"):
        retained = _plan(prepared, kind)
        action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
        assert action.observation_binding.preconditions.dependency_proof is None


def test_an_archive_is_refused_when_the_campaign_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Delivery is stopped by its own authorized plan before anything is archived."""

    prepared = _prepare(tmp_path, monkeypatch, campaign_status="ENABLED")
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, "archive")
    assert refused.value.code == "GOOGLE_ADS_ARCHIVE_REQUIRES_PAUSED_CAMPAIGN"
    assert prepared.google.mutations == []


def test_an_archive_intent_requires_an_explicit_irreversibility_acknowledgement() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GoogleAdsCampaignArchiveIntent(
            disposition="IRREVERSIBLE_ARCHIVE", acknowledged_irreversible=False
        )
    with pytest.raises(ValidationError):
        GoogleAdsCampaignArchiveIntent(disposition="REVERSIBLE", acknowledged_irreversible=True)


@pytest.mark.parametrize("kind", ["budget", "bid", "schedule", "archive"])
def test_the_planner_refuses_an_operation_that_is_not_the_intents_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    other = "ARCHIVE" if _OPERATIONS[kind] == "UPDATE" else "UPDATE"
    prepared = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, kind, operation=other)
    assert refused.value.code == "INVALID_GOOGLE_ADS_MAINTAIN_EXECUTION_SELECTION"
    assert prepared.google.searches == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"maintain_selection": None},
        {"resource_ref": None},
        {"resource_ref": "google_ads.campaign_name." + "b" * 64},
        {"account_ref": "google-ads-not-a-customer"},
    ],
)
def test_the_planner_refuses_malformed_selections_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any]
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, "budget", **overrides)
    assert refused.value.code == "INVALID_GOOGLE_ADS_MAINTAIN_EXECUTION_SELECTION"
    assert prepared.google.searches == []


@pytest.mark.parametrize(
    ("kind", "attribute", "value"),
    [
        ("budget", "budget_micros", _NEW_BUDGET_MICROS),
        ("bid", "cpc_bid_micros", _NEW_BID_MICROS),
    ],
)
def test_a_no_operation_maintenance_request_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, attribute: str, value: int
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, **{attribute: value})
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, kind)
    assert refused.value.code == "GOOGLE_ADS_MAINTAIN_NO_CHANGE"
    assert prepared.google.mutations == []


def test_a_schedule_replacement_matching_the_current_schedules_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    current = tuple(
        sorted(
            (
                GoogleAdsMaintenanceSchedule(
                    day_of_week=day,
                    start_hour=start_hour,
                    start_minute=start_minute,
                    end_hour=end_hour,
                    end_minute=end_minute,
                )
                for day, start_hour, start_minute, end_hour, end_minute in _SCHEDULES
            ),
            key=lambda item: item.identity,
        )
    )
    with pytest.raises(AdhelmError) as refused:
        _plan(
            prepared,
            "schedule",
            maintain_selection=_selection(
                "schedule", intent=GoogleAdsAdScheduleReplacementIntent(schedules=current)
            ),
        )
    assert refused.value.code == "GOOGLE_ADS_MAINTAIN_NO_CHANGE"


@pytest.mark.parametrize("status", ["CANCELED", "CLOSED", "SUSPENDED"])
def test_the_planner_refuses_a_customer_that_is_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, customer_status=status)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, "budget")
    assert refused.value.code == "GOOGLE_ADS_ACCOUNT_NOT_ENABLED"


def test_the_planner_refuses_a_removed_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, campaign_status="REMOVED")
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, "budget")
    assert refused.value.code == "GOOGLE_ADS_MAINTAIN_MEMBER_REMOVED"


def test_the_planner_refuses_when_a_record_kind_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, omit_kind=GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, "budget")
    assert refused.value.code == "GOOGLE_ADS_MAINTAIN_RECORD_KIND_NOT_INSTALLED"
    assert prepared.google.searches == []


def test_no_native_identifier_reaches_a_retained_maintenance_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "schedule")
    rendered = _non_digest_text(retained.record.plan.model_dump(mode="json"))
    assert "customers/" not in rendered
    assert "resourceName" not in rendered
    for native in (_CAMPAIGN_ID, _AD_GROUP_ID, _BUDGET_ID):
        assert native not in rendered


def test_the_planner_is_deterministic_for_one_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "bid")
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert canonical_bytes(action.to_data()) == canonical_bytes(action.to_data())
    assert action.plan.plan_digest == retained.plan_digest


# --- the happy paths --------------------------------------------------------


@pytest.mark.parametrize("kind", ["budget", "bid", "schedule"])
def test_full_route_from_retained_plan_to_applied_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, kind)
    bridge = _FakeCompositeBridge()
    result = _execute(prepared, retained, monkeypatch, bridge=bridge)

    assert bridge.calls == 1
    assert result.consumed is True
    assert result.recovery_required is False
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert len(prepared.google.mutations) == 1
    assert prepared.google.mutations[0] == json.loads((_FIXTURES / _GOLDEN_NAMES[kind]).read_text())

    readbacks = _readbacks(tmp_path)
    assert len(readbacks) == 1
    payload = readbacks[0]["payload"]
    assert payload["plan_digest"] == retained.plan_digest
    assert payload["maintenance_kind"] == _INTENTS[kind].maintenance_kind
    assert payload["operation"] == _OPERATIONS[kind]
    assert payload["automatic_revert"] is False
    assert payload["recovery_disposition"] == (
        "RETAIN_REFS_PREVIOUS_VALUES_RECORDED_NO_AUTOMATIC_REVERT"
    )
    assert payload["observed_after"] == "LIVE_HIERARCHY_MATCHES_PREDICTED_SEMANTICS"
    assert payload["reversibility"] == "REVERSIBLE_BY_A_NEW_PLAN"
    validate_google_ads_maintain_readback_history(Ledger(tmp_path).records("changes"))


def test_full_route_for_a_proven_archive_ends_removed_and_irreversible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch)
    retained = _plan(prepared, "archive")
    bridge = _FakeCompositeBridge()
    result = _execute(prepared, retained, monkeypatch, bridge=bridge)

    assert bridge.calls == 1
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert prepared.google.mutations == [
        json.loads((_FIXTURES / _GOLDEN_NAMES["archive"]).read_text())
    ]
    assert prepared.google.campaign_status == "REMOVED"
    payload = _readbacks(tmp_path)[0]["payload"]
    assert payload["maintenance_kind"] == "CAMPAIGN_ARCHIVE"
    assert payload["operation"] == "ARCHIVE"
    assert payload["reversibility"] == "IRREVERSIBLE_ARCHIVE"
    assert payload["previous_value"] == "PAUSED"
    assert payload["new_value"] == "REMOVED"
    validate_google_ads_maintain_readback_history(Ledger(tmp_path).records("changes"))


@pytest.mark.parametrize(
    ("kind", "previous", "new"),
    [
        ("budget", str(_SPEC.budget.request.total_amount_micros), str(_NEW_BUDGET_MICROS)),
        ("bid", str(_BID_MICROS), str(_NEW_BID_MICROS)),
        ("schedule", str(len(_SCHEDULES)), str(len(_REPLACEMENT))),
    ],
)
def test_the_readback_records_the_previous_value_for_a_later_corrective_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, previous: str, new: str
) -> None:
    """Recovery for a maintenance operation is the recorded previous value."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, kind)
    _execute(prepared, retained, monkeypatch)
    payload = _readbacks(tmp_path)[0]["payload"]
    assert payload["previous_value"] == previous
    assert payload["new_value"] == new


def test_the_permit_is_claimed_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "budget")
    claims: list[str] = []
    original = adapter_module.EgressPermit.consume

    def recording(self: Any, **kwargs: Any) -> Any:
        claims.append(kwargs["plan_digest"])
        return original(self, **kwargs)

    monkeypatch.setattr(adapter_module.EgressPermit, "consume", recording)
    _execute(prepared, retained, monkeypatch)
    assert claims == [retained.plan_digest]
    assert len(prepared.google.mutations) == 1


def test_no_token_customer_id_or_native_identifier_reaches_the_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "budget")
    _execute(prepared, retained, monkeypatch)
    payload = _readbacks(tmp_path)[0]["payload"]
    rendered = _non_digest_text(payload)
    for secret in (_ACCESS, _CAMPAIGN_ID, _AD_GROUP_ID, _BUDGET_ID):
        assert secret not in rendered
    assert "customers/" not in rendered
    assert payload["account_ref"] == _ACCOUNT_REF


# --- refusals ---------------------------------------------------------------


def _ambiguous(prepared: _Prepared, retained: RetainedExecutionPlan, monkeypatch: Any) -> Any:
    result = _execute(prepared, retained, monkeypatch)
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.results[0].evidence_refs == ()
    return result


def test_a_partial_failure_envelope_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "budget")
    prepared.google.mutate_response = httpx.Response(
        200,
        json={
            "mutateOperationResponses": [
                {"campaignBudgetResult": {"resourceName": _BUDGET_RESOURCE}}
            ],
            "partialFailureError": {"code": 3, "message": "one operation failed"},
        },
    )
    _ambiguous(prepared, retained, monkeypatch)
    assert _readbacks(tmp_path) == []


def test_an_ambiguous_transport_leaves_the_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "bid")
    prepared.google.mutate_error = httpx.ReadTimeout("no answer")
    result = _ambiguous(prepared, retained, monkeypatch)
    assert result.recovery_required is True
    assert Ledger(tmp_path).unresolved_starts()


def test_a_readback_mismatch_is_ambiguous_and_retains_no_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, applies_mutation=False)
    retained = _plan(prepared, "budget")
    _ambiguous(prepared, retained, monkeypatch)
    assert _readbacks(tmp_path) == []
    assert len(prepared.google.mutations) == 1


def test_hierarchy_drift_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "bid")
    prepared.google.budget_micros = 900_000_000
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "REMOTE_DRIFT"
    assert prepared.google.mutations == []
    assert Ledger(tmp_path).unresolved_starts() == []


@pytest.mark.parametrize(
    "drift", [{"customer_status": "SUSPENDED"}, {"currency": "EUR"}, {"timezone": "Etc/GMT-5"}]
)
def test_account_drift_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: dict[str, Any]
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "budget")
    for name, value in drift.items():
        setattr(prepared.google, name, value)
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "IDENTITY_MISMATCH"
    assert prepared.google.mutations == []


def test_an_unregistered_readback_kind_refuses_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "budget")
    monkeypatch.delitem(
        ledger_module._KINDS["changes"], GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND, raising=False
    )
    calls = len(prepared.google.searches)
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "GOOGLE_ADS_MAINTAIN_READBACK_RECORD_KIND_NOT_INSTALLED"
    assert prepared.google.mutations == []
    assert len(prepared.google.searches) == calls


def test_an_unconfigured_build_refuses_at_the_bridge_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared, "budget")
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


def test_an_archive_appearing_enabled_at_execution_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The paused-first rule is rechecked against the fresh read, not the plan.

    Campaign status is part of the semantic fingerprint, so the kernel's
    pre-start read always sees drift first.  The adapter's own ENABLED check in
    ``apply_guarded`` is defense in depth behind it and must never be what a
    clean refusal depends on: a raise after the durable start is ambiguous.
    """

    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch)
    retained = _plan(prepared, "archive")
    prepared.google.campaign_status = "ENABLED"
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "REMOTE_DRIFT"
    assert prepared.google.mutations == []
    assert Ledger(tmp_path).unresolved_starts() == []
    assert _readbacks(tmp_path) == []


def test_an_archive_whose_references_reappear_at_execution_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan-time proof is re-derived against the fresh read, not trusted.

    The re-proof reports state rather than raising, so the kernel refuses the
    UNPROVEN state as drift before the durable start, and the refusal still
    names the surface.
    """

    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch)
    retained = _plan(prepared, "archive")
    _read_campaign_references(monkeypatch, CAMPAIGN_SHARED_SET_LINKS="READ_NONEMPTY")
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "REMOTE_DRIFT"
    assert "CAMPAIGN_SHARED_SET_LINKS (READ_NONEMPTY)" in refused.value.next_action
    assert "nothing was changed" not in refused.value.next_action
    assert prepared.google.mutations == []
    assert prepared.google.campaign_status == "PAUSED"
    assert Ledger(tmp_path).unresolved_starts() == []
    assert _readbacks(tmp_path) == []


def _interrupt_a_proven_archive(
    prepared: _Prepared, monkeypatch: pytest.MonkeyPatch
) -> RetainedExecutionPlan:
    """Plan a proven archive, then die inside the write path of its attempt."""

    _read_campaign_references(monkeypatch)
    retained = _plan(prepared, "archive")

    def die(*_args: Any, **_kwargs: Any) -> Any:
        raise KeyboardInterrupt("process death")

    monkeypatch.setattr(adapter_module._FixedGoogleAdsSearchCampaignMaintainPost, "_post", die)
    with pytest.raises(KeyboardInterrupt):
        _execute(prepared, retained, monkeypatch)
    assert len(Ledger(prepared.directory).unresolved_starts()) == 1
    assert prepared.google.campaign_status == "PAUSED"
    assert prepared.google.mutations == []
    return retained


def _recover(prepared: _Prepared, monkeypatch: pytest.MonkeyPatch) -> Any:
    ledger = Ledger(prepared.directory)
    started = str(ledger.unresolved_starts()[0]["record_digest"])
    _install(monkeypatch, _FakeCompositeBridge())
    return record_retained_attempt_recovery_with_installed_approval(
        prepared.directory, AttemptRecoveryRequest(started_record_digest=started)
    )


def test_recovery_of_an_interrupted_proven_archive_still_records_while_the_proof_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    _interrupt_a_proven_archive(prepared, monkeypatch)
    outcome = _recover(prepared, monkeypatch)
    assert outcome.observed_relation == "AT_BASELINE"
    assert Ledger(tmp_path).unresolved_starts() == []


@pytest.mark.parametrize("disposition", ["READ_NONEMPTY", "NOT_READ"])
def test_recovery_records_an_honest_observation_when_a_surface_stops_reading_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposition: str
) -> None:
    """Rule 11 gates the archive's precondition; it never gates observing it.

    Once an archive attempt is unresolved, a later reference surface that is
    ``READ_NONEMPTY`` or ``NOT_READ`` must not stop the kernel from recording
    what it observed, or the write barrier would stay closed for every plan on
    every provider (AGENTS rules 6 and 22).
    """

    prepared = _prepare(tmp_path, monkeypatch)
    _interrupt_a_proven_archive(prepared, monkeypatch)
    _read_campaign_references(monkeypatch, CAMPAIGN_LABELS=disposition)

    outcome = _recover(prepared, monkeypatch)

    # The observation is recorded and is not claimed to be the baseline: the
    # campaign is unchanged but no longer proven unreferenced.
    assert outcome.observed_relation == "OTHER"
    assert outcome.disposition == "RECOVERED_DIVERGED"
    assert outcome.attempt_effect_status == "AMBIGUOUS_REMOTE_STATE"
    assert Ledger(tmp_path).unresolved_starts() == []
    assert Ledger(tmp_path).unresolved_runs() == []
    assert prepared.google.mutations == []
    assert prepared.google.campaign_status == "PAUSED"


def test_the_unproven_state_is_distinct_from_the_plan_and_from_the_archived_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    _read_campaign_references(monkeypatch)
    retained = _plan(prepared, "archive")
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    subplan = action.plan.subplans[0]
    with adapter_module._configured_google_ads_search_campaign_maintain_adapter(
        tmp_path, action, Ledger(tmp_path)
    ) as adapter:
        proven = adapter.read_state(subplan)
        assert proven.fingerprint == subplan.before.fingerprint
        assert adapter.unproven_reference_surfaces == ()
        _read_campaign_references(monkeypatch, CAMPAIGN_LABELS="READ_NONEMPTY")
        unproven = adapter.read_state(subplan)
        assert unproven.fingerprint not in {
            subplan.before.fingerprint,
            subplan.after.fingerprint,
        }
        assert unproven.revision != subplan.before.revision
        assert [item.surface for item in adapter.unproven_reference_surfaces] == ["CAMPAIGN_LABELS"]
        # A later proven read clears the diagnostic and returns the baseline.
        _read_campaign_references(monkeypatch)
        assert adapter.read_state(subplan).fingerprint == subplan.before.fingerprint
        assert adapter.unproven_reference_surfaces == ()
