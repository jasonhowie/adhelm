"""Google Ads paused Search hierarchy CREATE on the generic route, with fakes only.

Real here: the P04 contract, the recompile-and-bind promotion of a retained
offline candidate, the three-slot protected-credential lifecycle, the S3
retained record, the S4 route, a real signed approval receipt from an ephemeral
test key, a real ``SafetyKernel``, and a real local ledger.  Fake here: the
operating-system credential store, the HTTP transport, and the *installed*
approval bridge.  Nothing contacts Google Ads, a real credential store, a native
helper, or a network socket, and none of this is evidence of installed native
authority or live provider conformance.

Two build dependencies are deliberately visible: the additive ledger record
kinds ``GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_PLAN`` and
``GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_READBACK_OBSERVATION`` are registered by the
coordinator's own ``ledger.py`` hunk.  ``_install_record_kinds`` installs exactly
those two entries when this checkout does not already have them, so the suite
proves the whole path either side of that hunk and never hides its absence: the
planner and the adapter each refuse closed, before any credential, when a kind
is missing.
"""

from __future__ import annotations

import json
import re
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from test_generic_execution_route import _FakeCompositeBridge, _install
from test_google_ads_compiler import _brief as _compiler_brief
from test_google_ads_pause_credentials import (
    _ACCESS,
    _GENERIC_SERVICE,
    _installed,
    _MemoryStore,
)

from adhelm import credentials as credentials_module
from adhelm import google_ads_create_execution as execution
from adhelm import google_ads_create_planning_session as create_session
from adhelm import google_ads_pause_planning_session as planning
from adhelm import ledger as ledger_module
from adhelm.canonical import canonical_bytes, digest
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.execution_planning_session import (
    CONFIGURED_EXECUTION_PLANNERS,
    RetainedExecutionPlan,
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
from adhelm.google_ads_create_execution import (
    GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY,
    GOOGLE_ADS_CREATE_EXECUTION_PLANNER,
    _retained_action_for_plan,
)
from adhelm.google_ads_create_planning_session import near_match_campaign_names
from adhelm.google_ads_create_readback_records import (
    GOOGLE_ADS_CREATE_READBACK_RECORD_KIND,
    GoogleAdsSearchCampaignCreateReadbackObservation,
    pseudonymized_resource_ref,
    validate_google_ads_create_readback_history,
)
from adhelm.ledger import Ledger
from adhelm.models import GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE, Configuration, Money
from adhelm.plan_cli import ExecutionPlanRequest
from adhelm.providers import google_ads_create_adapter as adapter_module
from adhelm.providers.google_ads_compiler import compile_google_ads_search_campaign
from adhelm.providers.google_ads_create_adapter import (
    google_ads_search_campaign_create_mutate_body,
)
from adhelm.providers.google_ads_create_contract import (
    GOOGLE_ADS_CREATE_PLAN_RECORD_KIND,
    GoogleAdsSearchCampaignCreatePlanRecord,
    GoogleAdsSearchCampaignCreateSelection,
    projection_from_compiled_spec,
    semantic_hierarchy_state,
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
_GOLDEN = Path(__file__).parent / "fixtures" / "google_ads_search_campaign_create_mutate_body.json"


def _brief(campaign_name: str | None = None) -> Any:
    """The compiler suite's own brief, bound to this test customer and profile."""

    source = _compiler_brief()
    return source.model_copy(
        update={
            "account_ref": _ACCOUNT_REF,
            "account_timezone": "UTC",
            **({} if campaign_name is None else {"campaign_name": campaign_name}),
            "total_budget": Money(minor_units=25_000, currency="USD"),
            "start_date": date(2030, 3, 1),
            "end_date": date(2030, 3, 31),
        }
    )


_SPEC = compile_google_ads_search_campaign(_brief())
_CAMPAIGN_NAME = _SPEC.campaign_request.name


# --- the fake provider ------------------------------------------------------


@dataclass
class _FakeGoogleAds:
    """One in-memory customer behind the fixed v25 search and mutate routes."""

    clock: list[datetime]
    plan_campaign_name: str = _CAMPAIGN_NAME
    customer_status: str = "ENABLED"
    currency: str = "USD"
    timezone: str = "UTC"
    created: bool = False
    campaign_status: str = "PAUSED"
    ad_group_name_override: str | None = None
    positive_geo: str = "PRESENCE_OR_INTEREST"
    asset_automation: tuple[tuple[str, str], ...] = (("TEXT_ASSET_AUTOMATION", "OPTED_OUT"),)

    name_matches_before_create: int = 0
    #: Other campaign names this customer already holds, probed by the bounded
    #: near-variant ``IN`` read at planning time.
    existing_campaign_names: tuple[str, ...] = ()
    #: A second campaign carrying the planned name, visible only at readback.
    concurrent_same_name: bool = False
    mutate_error: BaseException | None = None
    mutate_response: httpx.Response | None = None
    applies_mutation: bool = True
    searches: list[str] = field(default_factory=list)
    probes: list[tuple[str, ...]] = field(default_factory=list)
    mutations: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.clock[0] += timedelta(seconds=1)
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        assert "developer-token" not in request.headers
        body = json.loads(request.content)
        if str(request.url) == _SEARCH_URL:
            query = body["query"]
            assert body["searchSettings"] == {"returnTotalResultsCount": True}
            return self._search(query)
        assert str(request.url) == _MUTATE_URL
        self.mutations.append(body)
        if self.mutate_error is not None:
            raise self.mutate_error
        if self.applies_mutation:
            self.created = True
        if self.mutate_response is not None:
            return self.mutate_response
        return httpx.Response(200, json={"mutateOperationResponses": self._results()})

    # -- searches -----------------------------------------------------------

    def _search(self, query: str) -> httpx.Response:
        if "FROM customer" in query:
            self.searches.append("customer")
            return self._rows("customer", [self._identity()])
        if "FROM campaign_criterion" in query:
            self.searches.append("campaign_criterion")
            return self._rows("campaign_criterion", self._criteria())
        if "FROM campaign WHERE campaign.name IN (" in query:
            self.searches.append("campaign_name_probe")
            assert f"'{self.plan_campaign_name}'" in query
            probed = tuple(_probe_names(query))
            self.probes.append(probed)
            held = set(self.existing_campaign_names)
            if self.created or self.name_matches_before_create:
                held.add(self.plan_campaign_name)
            rows = [{"campaign": {"name": name}} for name in probed if name in held]
            if self.name_matches_before_create > 1:
                extra = self.name_matches_before_create - 1
                rows.extend([{"campaign": {"name": self.plan_campaign_name}}] * extra)
            return self._rows("campaign.name", rows)
        if "FROM campaign WHERE campaign.name" in query:
            self.searches.append("campaign")
            assert f"'{_CAMPAIGN_NAME}'" in query
            if not self.created:
                return self._rows("campaign", [self._campaign()] * self.name_matches_before_create)
            rows = [self._campaign()]
            if self.concurrent_same_name:
                rows.append(self._campaign())
            return self._rows("campaign", rows)
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
            json={
                "results": rows,
                "totalResultsCount": str(len(rows)),
                "fieldMask": mask,
            },
        )

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
                "geoTargetTypeSetting": {
                    "positiveGeoTargetType": self.positive_geo,
                    "negativeGeoTargetType": "PRESENCE",
                },
                "assetAutomationSettings": [
                    {"assetAutomationType": kind, "assetAutomationStatus": status}
                    for kind, status in self.asset_automation
                ],
            },
            "campaignBudget": {
                "explicitlyShared": _SPEC.budget.request.explicitly_shared,
                "period": _SPEC.budget.request.period,
                "deliveryMethod": _SPEC.budget.request.delivery_method,
                "totalAmountMicros": str(_SPEC.budget.request.total_amount_micros),
            },
        }

    @staticmethod
    def _criteria() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in _SPEC.campaign_criteria:
            request = item.request
            common: dict[str, Any] = {"status": request.status, "negative": request.negative}
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
        return {
            "adGroup": {
                "id": _AD_GROUP_ID,
                "name": self.ad_group_name_override or _SPEC.ad_group.request.name,
                "type": _SPEC.ad_group.request.type,
                "status": _SPEC.ad_group.request.status,
            }
        }

    @staticmethod
    def _keywords() -> list[dict[str, Any]]:
        return [
            {
                "adGroupCriterion": {
                    "status": item.request.status,
                    "negative": item.request.negative,
                    "keyword": {
                        "text": item.request.keyword.text,
                        "matchType": item.request.keyword.match_type,
                    },
                }
            }
            for item in _SPEC.keywords
        ]

    @staticmethod
    def _ad() -> dict[str, Any]:
        ad = _SPEC.responsive_search_ad.request.ad
        return {
            "adGroupAd": {
                "status": _SPEC.responsive_search_ad.request.status,
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

    @staticmethod
    def _results() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = [
            {
                "campaignBudgetResult": {
                    "resourceName": f"customers/{_CUSTOMER}/campaignBudgets/{_BUDGET_ID}"
                }
            },
            {"campaignResult": {"resourceName": f"customers/{_CUSTOMER}/campaigns/{_CAMPAIGN_ID}"}},
        ]
        for index, _item in enumerate(_SPEC.campaign_criteria):
            results.append(
                {
                    "campaignCriterionResult": {
                        "resourceName": (f"customers/{_CUSTOMER}/campaignCriteria/{9000 + index}")
                    }
                }
            )
        results.append(
            {"adGroupResult": {"resourceName": f"customers/{_CUSTOMER}/adGroups/{_AD_GROUP_ID}"}}
        )
        for index, _item in enumerate(_SPEC.keywords):
            results.append(
                {
                    "adGroupCriterionResult": {
                        "resourceName": (f"customers/{_CUSTOMER}/adGroupCriteria/{8000 + index}")
                    }
                }
            )
        results.append(
            {"adGroupAdResult": {"resourceName": f"customers/{_CUSTOMER}/adGroupAds/{6001}"}}
        )
        return results


_DIGEST_RUN = re.compile(r"[0-9a-f]{64}")


def _non_digest_text(value: Any) -> str:
    """Render a JSON value with every 64-hex digest run removed.

    A SHA-256 digest is a hex string, so any short decimal identifier can appear
    inside one by coincidence.  Asserting "no native identifier is retained"
    means asserting it about the values that are *not* opaque digests.
    """

    return _DIGEST_RUN.sub("", json.dumps(value))


def _probe_names(query: str) -> list[str]:
    """Read back the exact literal list one bounded ``IN`` probe asked for.

    A campaign name that reaches a GAQL literal carries no single quote, so
    this recovers each literal byte for byte -- including the surrounding
    whitespace the padded near variants deliberately carry.
    """

    inner = query.split(" IN (", 1)[1].rsplit(")", 1)[0]
    return re.findall(r"'([^']*)'", inner)


# --- harness ----------------------------------------------------------------


def _install_record_kinds(monkeypatch: pytest.MonkeyPatch, *, plan: bool, readback: bool) -> None:
    """Install the coordinator's own additive ``_KINDS`` entries when absent.

    These are the exact hunks described in the P04 handoff, applied for the test
    process only.  They are idempotent: once ``ledger.py`` carries them, this is
    a no-op and every assertion below still holds.
    """

    kinds = ledger_module._KINDS["changes"]
    if plan and kinds.get(GOOGLE_ADS_CREATE_PLAN_RECORD_KIND) is not (
        GoogleAdsSearchCampaignCreatePlanRecord
    ):
        monkeypatch.setitem(
            kinds, GOOGLE_ADS_CREATE_PLAN_RECORD_KIND, GoogleAdsSearchCampaignCreatePlanRecord
        )
    if readback and kinds.get(GOOGLE_ADS_CREATE_READBACK_RECORD_KIND) is not (
        GoogleAdsSearchCampaignCreateReadbackObservation
    ):
        monkeypatch.setitem(
            kinds,
            GOOGLE_ADS_CREATE_READBACK_RECORD_KIND,
            GoogleAdsSearchCampaignCreateReadbackObservation,
        )
    if not plan:
        monkeypatch.delitem(kinds, GOOGLE_ADS_CREATE_PLAN_RECORD_KIND, raising=False)
    if not readback:
        monkeypatch.delitem(kinds, GOOGLE_ADS_CREATE_READBACK_RECORD_KIND, raising=False)


@dataclass(frozen=True)
class _Prepared:
    directory: Path
    google: _FakeGoogleAds
    clock: list[datetime]
    profile_id: str
    candidate_record_digest: str
    store: _MemoryStore
    campaign_name: str = _CAMPAIGN_NAME


def current_expiry() -> datetime:
    """A bearer window comfortably wider than the 300 second plan lifetime."""

    return datetime(2030, 1, 2, 12, 0, tzinfo=UTC) + timedelta(minutes=30)


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    plan_kind: bool = True,
    readback_kind: bool = True,
    retain: bool = True,
    campaign_name: str | None = None,
    **google: Any,
) -> _Prepared:
    monkeypatch.delenv("CI", raising=False)
    _install_record_kinds(monkeypatch, plan=plan_kind, readback=readback_kind)
    store, profile, current = _installed(monkeypatch, expires_at=current_expiry())
    # ``export_key`` is a separate generic-store read; keep it local too.
    export_store = _MemoryStore()
    monkeypatch.setattr(credentials_module, "_backend", lambda: export_store)
    monkeypatch.setattr(
        credentials_module, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    save_configuration(tmp_path, Configuration(profiles=[profile]), replace=True)
    if campaign_name is not None:
        google["plan_campaign_name"] = campaign_name
    fake = _FakeGoogleAds(clock=current, **google)
    for module in (planning, execution, adapter_module):
        monkeypatch.setattr(module, "_now", lambda: current[0])
    monkeypatch.setattr(
        planning,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(fake.handler), trust_env=False),
    )
    record_digest = ""
    if retain:
        record_digest = retain_candidate(Ledger(tmp_path), _brief(campaign_name))["record_digest"]
    return _Prepared(
        directory=tmp_path,
        google=fake,
        clock=current,
        profile_id=profile.profile_id,
        candidate_record_digest=record_digest,
        store=store,
        campaign_name=campaign_name or _CAMPAIGN_NAME,
    )


def _selection(prepared: _Prepared, **overrides: Any) -> GoogleAdsSearchCampaignCreateSelection:
    values: dict[str, Any] = {
        "candidate_record_digest": prepared.candidate_record_digest,
        "fresh": _brief(prepared.campaign_name),
    }
    values.update(overrides)
    return GoogleAdsSearchCampaignCreateSelection(**values)


def _request(prepared: _Prepared, **overrides: Any) -> ExecutionPlanRequest:
    values: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": prepared.profile_id,
        "provider": _PROVIDER,
        "operation": "CREATE",
        "account_ref": _ACCOUNT_REF,
        "resource_ref": f"google_ads.candidate.{prepared.candidate_record_digest}",
        "selection": _selection(prepared),
    }
    values.update(overrides)
    return ExecutionPlanRequest(**values)


def _plan(prepared: _Prepared, **overrides: Any) -> RetainedExecutionPlan:
    configuration = load_configuration(prepared.directory)
    return GOOGLE_ADS_CREATE_EXECUTION_PLANNER.plan_configured_execution(
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
        if record["record_kind"] == GOOGLE_ADS_CREATE_READBACK_RECORD_KIND
    ]


# --- registration -----------------------------------------------------------


def test_google_ads_create_is_a_registered_pair_on_both_registries() -> None:
    assert configured_execution_planner(_PROVIDER, OperationKind.CREATE) is (
        GOOGLE_ADS_CREATE_EXECUTION_PLANNER
    )
    assert configured_execution_adapter(_PROVIDER, OperationKind.CREATE) is (
        GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY
    )
    assert (_PROVIDER, OperationKind.CREATE) in dict(CONFIGURED_EXECUTION_ADAPTERS)
    assert (_PROVIDER, OperationKind.CREATE) in dict(CONFIGURED_EXECUTION_PLANNERS)
    assert {"provider": _PROVIDER, "operation": "CREATE"} in executable_execution_operations()


def test_the_factory_declares_no_child_egress_without_reading_anything() -> None:
    """Google creates the whole hierarchy in one atomic mutate."""

    assert GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY.declared_capabilities() == (
        ExecutionAdapterCapabilities(child_egress=False)
    )


def test_registering_a_second_google_create_adapter_is_refused() -> None:
    with pytest.raises(AdhelmError) as refused:
        register_execution_adapter(
            _PROVIDER, OperationKind.CREATE, GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY
        )
    assert refused.value.code == "EXECUTION_ADAPTER_ALREADY_REGISTERED"


def test_repeated_registration_of_the_exact_singletons_is_a_no_operation() -> None:
    execution.register_google_ads_create_execution()
    assert configured_execution_adapter(_PROVIDER, OperationKind.CREATE) is (
        GOOGLE_ADS_CREATE_EXECUTION_ADAPTER_FACTORY
    )


# --- the golden wire body ---------------------------------------------------


def test_the_mutate_body_is_the_documented_golden_json() -> None:
    """One atomic mutate, ordered temporary IDs, no partial failure."""

    body = google_ads_search_campaign_create_mutate_body(_SPEC, customer_id=_CUSTOMER)
    assert json.loads(_GOLDEN.read_text()) == body
    assert body["partialFailure"] is False
    assert body["validateOnly"] is False
    assert body["responseContentType"] == "RESOURCE_NAME_ONLY"
    operations = body["mutateOperations"]
    budget = f"customers/{_CUSTOMER}/campaignBudgets/-1"
    campaign = f"customers/{_CUSTOMER}/campaigns/-2"
    ad_group = f"customers/{_CUSTOMER}/adGroups/-3"
    assert operations[0]["campaignBudgetOperation"]["create"]["resourceName"] == budget
    assert operations[1]["campaignOperation"]["create"]["resourceName"] == campaign
    assert operations[1]["campaignOperation"]["create"]["campaignBudget"] == budget
    assert operations[1]["campaignOperation"]["create"]["status"] == "PAUSED"
    assert list(operations[-1]["adGroupAdOperation"]["create"]) == ["adGroup", "status", "ad"]
    assert operations[-1]["adGroupAdOperation"]["create"]["adGroup"] == ad_group
    # Every created entity is PAUSED, at every level of the hierarchy.
    for operation in operations[1:]:
        create = next(iter(operation.values()))["create"]
        assert create["status"] == "PAUSED"


def test_the_mutate_body_is_deterministic() -> None:
    first = google_ads_search_campaign_create_mutate_body(_SPEC, customer_id=_CUSTOMER)
    second = google_ads_search_campaign_create_mutate_body(
        compile_google_ads_search_campaign(_brief()), customer_id=_CUSTOMER
    )
    assert canonical_bytes(first) == canonical_bytes(second)


# --- the planner ------------------------------------------------------------


def test_the_planner_retains_both_records_for_one_plan_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    records = Ledger(tmp_path).records("changes")
    provider_records = [
        record for record in records if record["record_kind"] == GOOGLE_ADS_CREATE_PLAN_RECORD_KIND
    ]
    neutral_records = [
        record for record in records if record["record_kind"] == "EXECUTION_PLAN_RETAINED"
    ]
    assert len(provider_records) == 1
    assert len(neutral_records) == 1
    assert provider_records[0]["payload"]["plan_digest"] == retained.plan_digest
    assert retained.record.provider == _PROVIDER
    assert retained.record.operation is OperationKind.CREATE
    assert retained.record.expected_child_egress_count == 1
    assert retained.record.review_source == "COMPILER_CANDIDATE_REVIEW"
    assert retained.record.spend_bounds_by_currency[0].currency == "USD"
    assert retained.record.spend_bounds_by_currency[0].maximum_minor_units == 25_000
    assert retained.record.provenance.live_validated is False
    # The join the adapter factory performs, proven on verified history.
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert canonical_bytes(action.plan.model_dump(mode="json")) == canonical_bytes(
        retained.record.plan.model_dump(mode="json")
    )
    # The bounded promotion checks the compiler contract requires.
    preconditions = action.observation_binding.preconditions
    assert preconditions.campaign_name_collision == "NO_CAMPAIGN_WITH_THIS_NAME_OBSERVED"
    assert preconditions.geo_target_type_setting == (
        "NOT_REQUESTED_PROVIDER_DEFAULT_READ_BACK_AFTER_CREATE"
    )
    assert preconditions.campaign_budget_quota == "NOT_READABLE_NO_PROVIDER_SURFACE_IN_THIS_BUILD"
    assert preconditions.asset_automation_account_setting == (
        "NOT_READABLE_WEB_INTERFACE_ONLY_IN_V25"
    )
    assert preconditions.asset_automation_campaign_setting == (
        "NOT_REQUESTED_PROVIDER_DEFAULT_READ_BACK_AFTER_CREATE"
    )
    assert preconditions.campaign_name_near_match == "NO_NEAR_MATCH_CAMPAIGN_NAME_OBSERVED"
    assert preconditions.name_comparison == "BYTE_EXACT_NO_NORMALIZATION_ON_EITHER_SIDE"
    assert preconditions.provider_name_comparison_semantics == (
        "UNVERIFIED_NOT_DOCUMENTED_BY_PROVIDER"
    )
    assert preconditions.probed_near_match_names == len(near_match_campaign_names(_CAMPAIGN_NAME))
    assert prepared.google.searches == ["customer", "campaign_name_probe"]
    assert prepared.google.mutations == []


def test_the_plan_before_is_the_absence_of_this_campaign_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``before`` is the empty hierarchy; ``after`` is the predicted members."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    subplan = retained.record.plan.subplans[0]
    assert subplan.before != subplan.after
    assert subplan.before.revision == "GOOGLE_ADS_SEARCH_CAMPAIGN_CREATE_SEMANTIC_HIERARCHY_V1"
    assert subplan.after.revision == subplan.before.revision
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    projection = projection_from_compiled_spec(action.candidate_binding.specification)
    assert action.postcondition.member_count == len(projection.members)
    assert action.postcondition.every_entity_configured_status == "PAUSED"
    # No native identifier and no provider resource path is in the retained plan.
    rendered = _non_digest_text(retained.record.plan.model_dump(mode="json"))
    assert "customers/" not in rendered
    assert "resourceName" not in rendered
    for native in (_CAMPAIGN_ID, _AD_GROUP_ID, _BUDGET_ID):
        assert native not in rendered


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"resource_ref": None}, "INVALID_GOOGLE_ADS_CREATE_EXECUTION_SELECTION"),
        ({"selection": None}, "INVALID_GOOGLE_ADS_CREATE_EXECUTION_SELECTION"),
        ({"operation": "PAUSE"}, "INVALID_GOOGLE_ADS_CREATE_EXECUTION_SELECTION"),
        (
            {"resource_ref": "google_ads.candidate." + "b" * 64},
            "INVALID_GOOGLE_ADS_CREATE_EXECUTION_SELECTION",
        ),
    ],
)
def test_the_planner_refuses_malformed_selections_before_any_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    code: str,
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, **overrides)
    assert refused.value.code == code
    assert prepared.google.searches == []


def test_the_planner_refuses_a_customer_the_configured_profile_does_not_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, account_ref="google_ads.customer.5555555555")
    assert refused.value.code == "INVALID_GOOGLE_ADS_CREATE_EXECUTION_SELECTION"
    assert prepared.google.searches == []


def test_the_planner_refuses_a_brief_that_does_not_recompile_to_the_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    altered = _brief().model_copy(update={"campaign_name": "A different campaign"})
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, selection=_selection(prepared, fresh=altered))
    assert refused.value.code == "GOOGLE_ADS_CREATE_CANDIDATE_DIGEST_MISMATCH"
    assert prepared.google.searches == []


def test_the_planner_refuses_a_name_collision_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, name_matches_before_create=1)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_CREATE_NAME_COLLISION"
    assert prepared.google.mutations == []
    assert not [
        record
        for record in Ledger(tmp_path).records("changes")
        if record["record_kind"] == "EXECUTION_PLAN_RETAINED"
    ]


@pytest.mark.parametrize("status", ["CANCELED", "CLOSED", "SUSPENDED"])
def test_the_planner_refuses_a_customer_that_is_not_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, customer_status=status)
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_ACCOUNT_NOT_ENABLED"
    assert prepared.google.mutations == []


def test_the_planner_refuses_a_candidate_whose_timezone_differs_from_the_customer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The compiled account calendar must match the customer this plan reads."""

    prepared = _prepare(tmp_path, monkeypatch, retain=False)
    drifted = _compiler_brief().model_copy(
        update={"account_ref": _ACCOUNT_REF, "account_timezone": "America/Chicago"}
    )
    ledger = Ledger(tmp_path)
    record_digest = retain_candidate(ledger, drifted)["record_digest"]
    prepared = _Prepared(
        directory=prepared.directory,
        google=prepared.google,
        clock=prepared.clock,
        profile_id=prepared.profile_id,
        candidate_record_digest=record_digest,
        store=prepared.store,
    )
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, selection=_selection(prepared, fresh=drifted))
    assert refused.value.code == "GOOGLE_ADS_CREATE_ACCOUNT_MISMATCH"
    assert prepared.google.mutations == []


def test_the_planner_refuses_when_a_record_kind_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, plan_kind=True, readback_kind=False, retain=False)
    ledger = Ledger(tmp_path)
    record_digest = retain_candidate(ledger, _brief())["record_digest"]
    prepared = _Prepared(
        directory=prepared.directory,
        google=prepared.google,
        clock=prepared.clock,
        profile_id=prepared.profile_id,
        candidate_record_digest=record_digest,
        store=prepared.store,
    )
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_CREATE_RECORD_KIND_NOT_INSTALLED"
    assert prepared.google.searches == []


def test_the_planner_is_deterministic_for_one_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    first = _plan(prepared)
    action = _retained_action_for_plan(Ledger(tmp_path), first.record.plan)
    rebuilt = projection_from_compiled_spec(action.candidate_binding.specification)
    assert canonical_bytes(rebuilt.model_dump(mode="json")) == canonical_bytes(
        projection_from_compiled_spec(_SPEC).model_dump(mode="json")
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

    # Exactly one atomic mutation, byte-identical to the golden body.
    assert len(prepared.google.mutations) == 1
    assert prepared.google.mutations[0] == json.loads(_GOLDEN.read_text())

    readbacks = _readbacks(tmp_path)
    assert len(readbacks) == 1
    payload = readbacks[0]["payload"]
    assert payload["plan_digest"] == retained.plan_digest
    assert payload["after_observed_hierarchy"] == (
        "PAUSED_SEARCH_HIERARCHY_MATCHES_PREDICTED_SEMANTICS"
    )
    assert payload["every_entity_configured_status"] == "PAUSED"
    assert payload["recovery_disposition"] == "RETAIN_REFS_LEAVE_PAUSED_NO_DELETE"
    assert payload["observed_positive_geo_target_type"] == "PRESENCE_OR_INTEREST"
    assert payload["geo_target_type_authorization"] == ("OBSERVED_PROVIDER_DEFAULT_NOT_AUTHORIZED")
    assert payload["acknowledged_result_kinds"][0] == "campaignBudgetResult"
    assert payload["acknowledged_result_kinds"][-1] == "adGroupAdResult"
    assert payload["campaign_resource_ref"] == pseudonymized_resource_ref(
        "campaign", f"customers/{_CUSTOMER}/campaigns/{_CAMPAIGN_ID}"
    )
    validate_google_ads_create_readback_history(Ledger(tmp_path).records("changes"))


def test_no_token_customer_id_or_native_identifier_reaches_the_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    _execute(prepared, retained, monkeypatch)
    payload = _readbacks(tmp_path)[0]["payload"]
    rendered = _non_digest_text(payload)
    for secret in (_ACCESS, _CAMPAIGN_ID, _AD_GROUP_ID, _BUDGET_ID):
        assert secret not in rendered
    assert "customers/" not in rendered
    # The pseudonymized references are the only thing standing in for the
    # native resource paths, and they are keyless digests of them.
    assert payload["campaign_resource_ref"].endswith(
        digest(
            {
                "domain": "adhelm.google-ads.search-campaign-create.resource.v1",
                "kind": "campaign",
                "resource_name": f"customers/{_CUSTOMER}/campaigns/{_CAMPAIGN_ID}",
            }
        )
    )
    # The ledger envelope keeps the ordinary account reference; the record's own
    # resource references are pseudonymized.
    assert _readbacks(tmp_path)[0]["payload"]["account_ref"] == _ACCOUNT_REF


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


def test_a_short_acknowledgement_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    prepared.google.mutate_response = httpx.Response(
        200, json={"mutateOperationResponses": _FakeGoogleAds._results()[:-1]}
    )
    _ambiguous(prepared, retained, monkeypatch)


def test_a_resource_name_outside_the_customer_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    results = _FakeGoogleAds._results()
    results[1] = {"campaignResult": {"resourceName": "customers/9999999999/campaigns/5511"}}
    prepared.google.mutate_response = httpx.Response(
        200, json={"mutateOperationResponses": results}
    )
    _ambiguous(prepared, retained, monkeypatch)


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

    prepared = _prepare(tmp_path, monkeypatch, ad_group_name_override="Something else")
    retained = _plan(prepared)
    _ambiguous(prepared, retained, monkeypatch)
    assert _readbacks(tmp_path) == []
    assert len(prepared.google.mutations) == 1


def test_a_mutation_that_does_not_take_effect_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, applies_mutation=False)
    retained = _plan(prepared)
    _ambiguous(prepared, retained, monkeypatch)
    assert _readbacks(tmp_path) == []


# --- preconditions before egress --------------------------------------------


def test_a_name_collision_appearing_after_planning_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    prepared.google.name_matches_before_create = 1
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code in {"REMOTE_DRIFT", "PROVIDER_RESPONSE_INVALID"}
    assert prepared.google.mutations == []
    assert _readbacks(tmp_path) == []
    assert Ledger(tmp_path).unresolved_starts() == []


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


def test_an_unregistered_readback_kind_refuses_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    monkeypatch.delitem(
        ledger_module._KINDS["changes"], GOOGLE_ADS_CREATE_READBACK_RECORD_KIND, raising=False
    )
    calls = len(prepared.google.searches)
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "GOOGLE_ADS_CREATE_READBACK_RECORD_KIND_NOT_INSTALLED"
    assert prepared.google.mutations == []
    assert len(prepared.google.searches) == calls


def test_an_unconfigured_build_refuses_at_the_bridge_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary build has no installed composite bridge at all."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    calls = len(prepared.google.searches)
    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            prepared.directory,
            ExecutionPlanExecutionRequest(plan_record_digest=retained.record_digest),
        )
    assert refused.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert prepared.google.mutations == []
    assert len(prepared.google.searches) == calls


# --- the readback history validator -----------------------------------------


def test_a_second_readback_for_one_attempt_is_refused() -> None:
    records = [
        {
            "record_kind": GOOGLE_ADS_CREATE_READBACK_RECORD_KIND,
            "payload": {"action_id": "a" * 64},
        }
    ]
    with pytest.raises(AdhelmError) as refused:
        validate_google_ads_create_readback_history(records)
    assert refused.value.code == "INVALID_GOOGLE_ADS_CREATE_READBACK_HISTORY"


def test_the_readback_history_validator_accepts_the_retained_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    _execute(prepared, retained, monkeypatch)
    records = Ledger(tmp_path).records("changes")
    validate_google_ads_create_readback_history(records)
    readback = next(
        record
        for record in records
        if record["record_kind"] == GOOGLE_ADS_CREATE_READBACK_RECORD_KIND
    )
    with pytest.raises(AdhelmError):
        validate_google_ads_create_readback_history([*records, readback])


# --- the paused-only contract -----------------------------------------------


def test_a_projection_with_a_non_paused_member_is_refused() -> None:
    """The plan enforces paused status for every entity, not just the campaign."""

    projection = projection_from_compiled_spec(_SPEC)
    enabled = [
        member.model_copy(
            update={
                "configuration": (
                    ("status", "ENABLED"),
                    *(pair for pair in member.configuration if pair[0] != "status"),
                )
            }
        )
        if member.member_kind == "AD_GROUP"
        else member
        for member in projection.members
    ]
    with pytest.raises(ValidationError):
        projection.model_copy(update={"members": tuple(enabled)}).model_validate(
            {
                "campaign_name": projection.campaign_name,
                "members": [item.model_dump(mode="json") for item in enabled],
            }
        )


def test_every_compiled_member_is_paused() -> None:
    projection = projection_from_compiled_spec(_SPEC)
    for member in projection.members:
        if member.member_kind == "CAMPAIGN_BUDGET":
            continue
        assert dict(member.configuration)["status"] == "PAUSED"


# --- credential and key continuity ------------------------------------------


def test_a_changed_local_configuration_refuses_before_any_provider_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    configuration = load_configuration(tmp_path)
    changed = configuration.profiles[0].model_copy(update={"timezone": "Etc/GMT-3"})
    save_configuration(tmp_path, Configuration(profiles=[changed]), replace=True)
    searches = len(prepared.google.searches)
    with pytest.raises(AdhelmError):
        _execute(prepared, retained, monkeypatch)
    assert prepared.google.mutations == []
    assert len(prepared.google.searches) == searches


def test_a_changed_local_fingerprint_key_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A projection this workspace can no longer reproduce is never executed."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    rotated = _MemoryStore()
    monkeypatch.setattr(credentials_module, "_backend", lambda: rotated)
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "GOOGLE_ADS_CREATE_FINGERPRINT_KEY_CHANGED"
    assert prepared.google.mutations == []


# --- campaign-name normalization --------------------------------------------


def test_the_probe_asks_for_the_exact_name_and_its_bounded_near_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One rule governs both sides of every name comparison: no normalization.

    The GAQL probe asks for byte-exact literals -- the planned name first, then
    the closed near-variant set -- and the returned names are classified locally
    with the same byte-exact comparison the ``before`` projection uses.
    """

    prepared = _prepare(tmp_path, monkeypatch)
    _plan(prepared)
    assert len(prepared.google.probes) == 1
    probed = prepared.google.probes[0]
    assert probed[0] == _CAMPAIGN_NAME
    assert tuple(sorted(probed[1:])) == near_match_campaign_names(_CAMPAIGN_NAME)
    assert len(set(probed)) == len(probed)
    # Padding and case variants really are asked for verbatim, spaces included.
    assert f" {_CAMPAIGN_NAME.upper()} " in probed
    assert _CAMPAIGN_NAME.casefold() in probed


def test_the_before_projection_is_byte_exact_in_the_campaign_name() -> None:
    """A differently cased or padded name is a different semantic target."""

    key = b"p04-create-fingerprint-key-fixture"
    base = semantic_hierarchy_state(
        None,
        account_client_ref=_CUSTOMER,
        campaign_name=_CAMPAIGN_NAME,
        fingerprint_key=key,
    )
    for variant in (
        _CAMPAIGN_NAME.upper(),
        _CAMPAIGN_NAME.casefold(),
        f" {_CAMPAIGN_NAME}",
        f"{_CAMPAIGN_NAME} ",
    ):
        other = semantic_hierarchy_state(
            None,
            account_client_ref=_CUSTOMER,
            campaign_name=variant,
            fingerprint_key=key,
        )
        assert other != base


@pytest.mark.parametrize(
    "existing",
    [
        _CAMPAIGN_NAME.upper(),
        _CAMPAIGN_NAME.casefold(),
        _CAMPAIGN_NAME.title(),
        f" {_CAMPAIGN_NAME}",
        f"{_CAMPAIGN_NAME} ",
        f" {_CAMPAIGN_NAME} ",
    ],
)
def test_a_near_match_campaign_name_is_refused_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: str
) -> None:
    """Case, surrounding whitespace, and normalization near matches all refuse.

    Google does not document whether it compares campaign names case
    sensitively, and some Google Ads surfaces treat them case-insensitively, so
    a near match is refused rather than assumed distinct.
    """

    prepared = _prepare(tmp_path, monkeypatch, existing_campaign_names=(existing,))
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_CREATE_NAME_COLLISION_NEAR_MATCH"
    assert prepared.google.mutations == []
    assert not [
        record
        for record in Ledger(tmp_path).records("changes")
        if record["record_kind"] == "EXECUTION_PLAN_RETAINED"
    ]


#: An accented composed (NFC) campaign name whose decomposed (NFD) form is a
#: different byte string but the same text. The default fixture name is pure
#: ASCII, so this is the only way to exercise the normalization branch.
_ACCENTED_NAME = "Caf\u00e9 Search campaign"


def test_a_normalization_equal_campaign_name_is_refused_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name equal only after Unicode normalization is a near match too."""

    decomposed = unicodedata.normalize("NFD", _ACCENTED_NAME)
    assert decomposed != _ACCENTED_NAME
    assert unicodedata.normalize("NFC", decomposed) == _ACCENTED_NAME
    assert decomposed in near_match_campaign_names(_ACCENTED_NAME)

    prepared = _prepare(
        tmp_path,
        monkeypatch,
        campaign_name=_ACCENTED_NAME,
        existing_campaign_names=(decomposed,),
    )
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_CREATE_NAME_COLLISION_NEAR_MATCH"
    assert prepared.google.mutations == []


def test_a_normalization_distinct_campaign_name_still_plans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The accented plan itself is fine when the customer holds nothing near it."""

    prepared = _prepare(tmp_path, monkeypatch, campaign_name=_ACCENTED_NAME)
    retained = _plan(prepared)
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert action.observation_binding.campaign_name == _ACCENTED_NAME
    assert action.observation_binding.preconditions.observed_near_match_names == 0


def test_an_unrelated_campaign_name_is_not_a_near_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(
        tmp_path, monkeypatch, existing_campaign_names=("A completely different campaign",)
    )
    retained = _plan(prepared)
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert action.observation_binding.preconditions.observed_near_match_names == 0


def test_a_near_match_appearing_after_planning_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    prepared.google.existing_campaign_names = (_CAMPAIGN_NAME.upper(),)
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "REMOTE_DRIFT"
    assert prepared.google.mutations == []
    assert Ledger(tmp_path).unresolved_starts() == []


# --- a candidate that is not fully paused -----------------------------------


def _enabled_ad_group_spec() -> Any:
    """One tampered candidate whose ad group is ENABLED, built past validation."""

    request = _SPEC.ad_group.request
    enabled = type(request).model_construct(name=request.name, type=request.type, status="ENABLED")
    return _SPEC.model_copy(
        update={"ad_group": _SPEC.ad_group.model_copy(update={"request": enabled})}
    )


def test_a_candidate_with_an_enabled_entity_is_refused_at_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 8 is enforced before configuration, credential, or provider work."""

    prepared = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(
        execution, "compile_google_ads_search_campaign", lambda _brief: _enabled_ad_group_spec()
    )
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared)
    assert refused.value.code == "GOOGLE_ADS_CREATE_CANDIDATE_NOT_PAUSED"
    assert prepared.google.searches == []
    assert prepared.google.mutations == []


def test_the_projection_refuses_the_same_tampered_candidate() -> None:
    """The contract refuses it directly, not only through the planner."""

    with pytest.raises(ValueError):
        projection_from_compiled_spec(_enabled_ad_group_spec())


# --- credential continuity between plan and execute -------------------------


def test_rotated_google_ads_binding_key_refuses_before_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rotated binding key breaks the approved association."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    searches = len(prepared.google.searches)
    prepared.store.values[(_GENERIC_SERVICE, GOOGLE_ADS_PAUSE_BINDING_KEY_REFERENCE)] = "d" * 64

    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == "GOOGLE_ADS_CREATE_EXECUTION_BINDING_DRIFT"
    assert prepared.google.mutations == []
    assert len(prepared.google.searches) == searches
    assert _readbacks(tmp_path) == []
    assert Ledger(tmp_path).unresolved_starts() == []


# --- a concurrent same-name campaign at readback ----------------------------


def test_a_concurrent_same_name_campaign_at_readback_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two campaigns carrying the planned name make the readback unprovable.

    The mutation was dispatched and the acknowledgement parsed, so the outcome
    is ambiguous rather than failed. Nothing is deleted: rule 11 permits a
    delete only for a resource proven unreferenced, and everything this route
    created is paused.
    """

    prepared = _prepare(tmp_path, monkeypatch, concurrent_same_name=True)
    retained = _plan(prepared)
    result = _ambiguous(prepared, retained, monkeypatch)
    assert result.recovery_required is True
    assert len(prepared.google.mutations) == 1
    assert _readbacks(tmp_path) == []
    assert len(Ledger(tmp_path).unresolved_starts()) == 1


# --- the non-shared budget has no independent status ------------------------


def test_no_projection_or_readback_expects_an_independent_budget_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-shared budget's name and status stay in sync with its campaign.

    v25 documents `CampaignBudget.status` as output-only and states that a
    non-shared budget's name and status track the campaign's, so a paused
    campaign implies a paused-tracking budget. Nothing here may compare a
    budget status independently, or the projection would depend on a value this
    route neither sends nor reads.
    """

    projection = projection_from_compiled_spec(_SPEC)
    budget = next(
        member for member in projection.members if member.member_kind == "CAMPAIGN_BUDGET"
    )
    assert "status" not in dict(budget.configuration)
    assert dict(budget.configuration) == {
        "delivery_method": "STANDARD",
        "explicitly_shared": "false",
        "period": "CUSTOM_PERIOD",
        "total_amount_micros": str(_SPEC.budget.request.total_amount_micros),
    }
    # Neither the readback query nor the mutate body names a budget status.
    assert "campaign_budget.status" not in create_session._CAMPAIGN_FIELDS
    body = google_ads_search_campaign_create_mutate_body(_SPEC, customer_id=_CUSTOMER)
    assert "status" not in body["mutateOperations"][0]["campaignBudgetOperation"]["create"]
    assert "name" not in body["mutateOperations"][0]["campaignBudgetOperation"]["create"]

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    _execute(prepared, retained, monkeypatch)
    payload = _readbacks(tmp_path)[0]["payload"]
    assert not [key for key in payload if "budget" in key]
