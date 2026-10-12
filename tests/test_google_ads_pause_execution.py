"""Google Ads campaign PAUSE on the generic execution route, with fakes only.

Real here: the M55 contract, the F01 planning workflow, the two-slot
protected-credential lifecycle, the S3 retained record, the S4 route, a real
signed approval receipt from an ephemeral test key, a real ``SafetyKernel``,
and a real local ledger.  Fake here: the operating-system credential store, the
HTTP transport, and the *installed* approval bridge.  Nothing contacts Google
Ads, a real credential store, a native helper, or a network socket, and none of
this is evidence of installed native authority or live provider conformance.

One build dependency is deliberately visible: the additive ledger record kind
``GOOGLE_ADS_CAMPAIGN_PAUSE_READBACK_OBSERVATION`` is registered by the
coordinator's own ``ledger.py`` hunk.  ``_install_readback_record_kind``
installs exactly that entry when this checkout does not already have it, so the
suite proves the whole path either side of that hunk and never hides its
absence: the adapter itself refuses closed, before any credential, when the
kind is missing (``test_unregistered_readback_kind_refuses_before_credentials``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_generic_execution_route import (
    _FakeCompositeBridge,
    _install,
    _no_credentials,
)
from test_google_ads_pause_credentials import (
    _ACCESS,
    _installed,
)

from adhelm import execution_cli
from adhelm import generic_execution_workflow as route
from adhelm import google_ads_pause_execution as execution
from adhelm import google_ads_pause_planning_session as planning
from adhelm import google_ads_pause_workflow as workflow
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
from adhelm.google_ads_pause_execution import (
    GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY,
    GOOGLE_ADS_PAUSE_EXECUTION_PLANNER,
    _retained_action_for_plan,
)
from adhelm.google_ads_pause_readback_records import (
    GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND,
    GoogleAdsCampaignPauseReadbackObservation,
    validate_google_ads_pause_readback_history,
)
from adhelm.ledger import Ledger
from adhelm.models import MAX_CHILD_EGRESS_COUNT, Configuration
from adhelm.plan_cli import ExecutionPlanRequest
from adhelm.providers import google_ads_pause_adapter as adapter_module
from adhelm.safety import EffectStatus, OperationKind

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_CUSTOMER = "1234567890"
_CAMPAIGN = "42"
_PROVIDER = "google_ads"
_SEARCH_URL = f"https://googleads.googleapis.com/v25/customers/{_CUSTOMER}/googleAds:search"
_MUTATE_URL = f"https://googleads.googleapis.com/v25/customers/{_CUSTOMER}/campaigns:mutate"
_RESOURCE_NAME = f"customers/{_CUSTOMER}/campaigns/{_CAMPAIGN}"


# --- the fake provider ------------------------------------------------------


@dataclass
class _FakeGoogleAds:
    """One in-memory customer and campaign behind the two fixed v25 routes."""

    clock: list[datetime]
    customer_status: str = "CLOSED"
    currency: str = "USD"
    timezone: str = "UTC"
    campaign_status: str = "ENABLED"
    campaign_id: str = _CAMPAIGN
    mutate_error: BaseException | None = None
    mutate_response: httpx.Response | None = None
    applies_mutation: bool = True
    searches: list[str] = field(default_factory=list)
    mutations: list[dict[str, Any]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.clock[0] += timedelta(seconds=1)
        if str(request.url) == _SEARCH_URL:
            body = json.loads(request.content)
            assert request.method == "POST"
            assert request.headers["authorization"] == f"Bearer {_ACCESS}"
            assert "developer-token" not in request.headers
            assert body["searchSettings"] == {"returnTotalResultsCount": True}
            if "FROM customer" in body["query"]:
                self.searches.append("customer")
                return httpx.Response(200, json=self._identity())
            self.searches.append("campaign")
            return httpx.Response(200, json=self._campaign())
        assert str(request.url) == _MUTATE_URL
        assert request.method == "POST"
        assert request.headers["authorization"] == f"Bearer {_ACCESS}"
        assert "developer-token" not in request.headers
        body = json.loads(request.content)
        self.mutations.append(body)
        if self.mutate_error is not None:
            raise self.mutate_error
        if self.applies_mutation:
            self.campaign_status = "PAUSED"
        if self.mutate_response is not None:
            return self.mutate_response
        return httpx.Response(200, json={"results": [{"resourceName": _RESOURCE_NAME}]})

    def _identity(self) -> dict[str, Any]:
        return {
            "results": [
                {
                    "customer": {
                        "id": _CUSTOMER,
                        "resourceName": f"customers/{_CUSTOMER}",
                        "currencyCode": self.currency,
                        "timeZone": self.timezone,
                        "status": self.customer_status,
                    }
                }
            ],
            "totalResultsCount": "1",
            "fieldMask": "customer.id,customer.currency_code,customer.time_zone,customer.status",
        }

    def _campaign(self) -> dict[str, Any]:
        return {
            "results": [
                {
                    "campaign": {
                        "id": self.campaign_id,
                        "resourceName": f"customers/{_CUSTOMER}/campaigns/{self.campaign_id}",
                        "status": self.campaign_status,
                    }
                }
            ],
            "totalResultsCount": "1",
            "fieldMask": "campaign.id,campaign.resource_name,campaign.status",
        }


# --- harness ----------------------------------------------------------------


def _install_readback_record_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install the coordinator's own additive ``_KINDS`` entry when absent.

    This is the exact hunk described in the P03 handoff, applied for the test
    process only.  It is idempotent: once ``ledger.py`` carries it, this is a
    no-op and every assertion below still holds.
    """

    kinds = ledger_module._KINDS["changes"]
    if kinds.get(GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND) is (
        GoogleAdsCampaignPauseReadbackObservation
    ):
        return
    monkeypatch.setitem(
        kinds, GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND, GoogleAdsCampaignPauseReadbackObservation
    )


@dataclass(frozen=True)
class _Prepared:
    directory: Path
    google: _FakeGoogleAds
    clock: list[datetime]
    profile_id: str


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    readback_kind: bool = True,
    **google: Any,
) -> _Prepared:
    monkeypatch.delenv("CI", raising=False)
    if readback_kind:
        _install_readback_record_kind(monkeypatch)
    _store, profile, current = _installed(monkeypatch, expires_at=current_expiry())
    save_configuration(tmp_path, Configuration(profiles=[profile]), replace=True)
    fake = _FakeGoogleAds(clock=current, **google)
    for module in (planning, workflow, execution):
        monkeypatch.setattr(module, "_now", lambda: current[0])
    monkeypatch.setattr(
        planning,
        "_google_ads_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(fake.handler), trust_env=False),
    )
    return _Prepared(directory=tmp_path, google=fake, clock=current, profile_id=profile.profile_id)


def current_expiry() -> datetime:
    """A bearer window comfortably wider than the 300 second plan lifetime."""

    return datetime(2030, 1, 2, 12, 0, tzinfo=UTC) + timedelta(minutes=30)


def _request(prepared: _Prepared, **overrides: Any) -> ExecutionPlanRequest:
    values: dict[str, Any] = {
        "schema_version": 1,
        "profile_id": prepared.profile_id,
        "provider": _PROVIDER,
        "operation": "PAUSE",
        "account_ref": f"google_ads.customer.{_CUSTOMER}",
        "resource_ref": f"google_ads.campaign.{_CUSTOMER}.{_CAMPAIGN}",
    }
    values.update(overrides)
    return ExecutionPlanRequest(**values)


def _plan(prepared: _Prepared, **overrides: Any) -> RetainedExecutionPlan:
    configuration = load_configuration(prepared.directory)
    return GOOGLE_ADS_PAUSE_EXECUTION_PLANNER.plan_configured_execution(
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
        if record["record_kind"] == GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND
    ]


# --- registration -----------------------------------------------------------


def test_google_ads_pause_is_the_registered_pair_on_both_registries() -> None:
    """P03's pair is registered on both registries and resolves to its own singletons.

    The exact whole-registry set is pinned by
    ``tests/test_generic_execution_route.py`` and
    ``tests/test_execution_plan_records.py``, which P04 extended when it
    registered ``("google_ads", CREATE)`` and P04-OA extended again when it
    registered ``("openai_ads", CREATE)``.
    """

    assert configured_execution_planner(_PROVIDER, OperationKind.PAUSE) is (
        GOOGLE_ADS_PAUSE_EXECUTION_PLANNER
    )
    assert configured_execution_adapter(_PROVIDER, OperationKind.PAUSE) is (
        GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY
    )
    assert dict(CONFIGURED_EXECUTION_ADAPTERS)[(_PROVIDER, OperationKind.PAUSE)] is (
        GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY
    )
    assert dict(CONFIGURED_EXECUTION_PLANNERS)[(_PROVIDER, OperationKind.PAUSE)] is (
        GOOGLE_ADS_PAUSE_EXECUTION_PLANNER
    )
    assert {"provider": _PROVIDER, "operation": "PAUSE"} in executable_execution_operations()


def test_the_factory_declares_no_child_egress_without_reading_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declaration is answered from the compiled contract, not from state."""

    store = _no_credentials(monkeypatch)
    declared = GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY.declared_capabilities()
    assert declared == ExecutionAdapterCapabilities(child_egress=False)
    assert store.calls == 0


def test_registering_a_second_google_pause_adapter_is_refused() -> None:
    with pytest.raises(AdhelmError) as refused:
        register_execution_adapter(
            _PROVIDER, OperationKind.PAUSE, GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY
        )
    assert refused.value.code == "EXECUTION_ADAPTER_ALREADY_REGISTERED"


def test_repeated_registration_of_the_exact_singletons_is_a_no_operation() -> None:
    execution.register_google_ads_pause_execution()
    assert configured_execution_adapter(_PROVIDER, OperationKind.PAUSE) is (
        GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY
    )


# --- the planner ------------------------------------------------------------


def test_the_planner_retains_both_records_for_one_plan_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One compiled plan, two retained families, joined by ``plan_digest``."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    records = Ledger(tmp_path).records("changes")
    provider_records = [
        record for record in records if record["record_kind"] == "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
    ]
    neutral_records = [
        record for record in records if record["record_kind"] == "EXECUTION_PLAN_RETAINED"
    ]
    assert len(provider_records) == 1
    assert len(neutral_records) == 1
    assert provider_records[0]["payload"]["plan_digest"] == retained.plan_digest
    assert neutral_records[0]["record_digest"] == retained.record_digest
    assert retained.record.provider == _PROVIDER
    assert retained.record.operation is OperationKind.PAUSE
    assert retained.record.expected_child_egress_count == 1
    assert retained.record.spend_bounds_by_currency == ()
    assert retained.record.provenance.live_validated is False
    # The join the adapter factory performs, proven on verified history.
    action = _retained_action_for_plan(Ledger(tmp_path), retained.record.plan)
    assert canonical_bytes(action.plan.model_dump(mode="json")) == canonical_bytes(
        retained.record.plan.model_dump(mode="json")
    )
    assert prepared.google.searches == ["customer", "campaign"]
    assert prepared.google.mutations == []


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"resource_ref": None}, "INVALID_GOOGLE_ADS_PAUSE_EXECUTION_SELECTION"),
        ({"resource_ref": "google_ads.campaign.9999999999.42"}, None),
        ({"operation": "ARCHIVE"}, "INVALID_GOOGLE_ADS_PAUSE_EXECUTION_SELECTION"),
    ],
)
def test_the_planner_refuses_malformed_selections_before_any_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, Any],
    code: str | None,
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    expected = code or "INVALID_GOOGLE_ADS_PAUSE_EXECUTION_SELECTION"
    with pytest.raises(AdhelmError) as refused:
        _plan(prepared, **overrides)
    assert refused.value.code == expected
    assert prepared.google.searches == []
    assert Ledger(tmp_path).records("changes") == []


def test_the_planner_refuses_a_customer_the_configured_profile_does_not_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    with pytest.raises(AdhelmError) as refused:
        _plan(
            prepared,
            account_ref="google_ads.customer.5555555555",
            resource_ref="google_ads.campaign.5555555555.42",
        )
    assert refused.value.code == "GOOGLE_ADS_EDIT_PROFILE_REQUIRED"
    assert prepared.google.searches == []
    assert Ledger(tmp_path).records("changes") == []


# --- the happy path ---------------------------------------------------------


def test_full_route_from_retained_plan_to_applied_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retained plan -> receipt -> grant -> adapter -> mutate -> readback."""

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

    # Exactly one fixed mutation, with the fixed field mask and no other field.
    assert len(prepared.google.mutations) == 1
    assert prepared.google.mutations[0] == {
        "operations": [
            {
                "updateMask": "status",
                "update": {"resourceName": _RESOURCE_NAME, "status": "PAUSED"},
            }
        ],
        "partialFailure": False,
        "validateOnly": False,
        "responseContentType": "RESOURCE_NAME_ONLY",
    }

    readbacks = _readbacks(tmp_path)
    assert len(readbacks) == 1
    payload = readbacks[0]["payload"]
    assert payload["before_configured_status"] == "ENABLED"
    assert payload["after_configured_status"] == "PAUSED"
    assert payload["acknowledged_resource_name"] == _RESOURCE_NAME
    assert payload["conditional_write_proof"] is False
    assert payload["idempotency_proof"] is False
    assert readbacks[0]["provider"] == _PROVIDER
    assert readbacks[0]["source"] == "LOCAL_CLI"
    assert readbacks[0]["observed_at"] == readbacks[0]["occurred_at"]
    # The record the ledger holds is the record the history validator accepts.
    validate_google_ads_pause_readback_history(Ledger(tmp_path).records("changes"))

    # The readback record digest is the adapter's second evidence reference.
    assert f"evidence:sha256:{readbacks[0]['record_digest']}" in result.results[0].evidence_refs
    assert Ledger(tmp_path).unresolved_starts() == []


def test_the_emitted_result_carries_no_identifier_or_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bounded envelope is digests, statuses, and opaque references only."""

    prepared = _prepare(tmp_path, monkeypatch)
    result = _execute(prepared, _plan(prepared), monkeypatch)
    emitted = json.dumps(
        execution_cli._execution_result_data(
            result, expected_provider=None, maximum_opaque_refs=MAX_CHILD_EGRESS_COUNT
        )
    )
    for secret in (
        _ACCESS,
        "m56-google-ads-refresh-token",
        "m56-google-client-secret",
        _CUSTOMER,
        _RESOURCE_NAME,
        "Bearer ",
    ):
        assert secret not in emitted
    history = json.dumps(Ledger(tmp_path).records("changes"))
    for secret in (
        _ACCESS,
        "m56-google-ads-refresh-token",
        "m56-google-client-secret",
        "campaigns:mutate",
        "updateMask",
    ):
        assert secret not in history


def test_the_readback_record_is_deterministic_for_one_observation() -> None:
    """Two constructions of the same observation are byte identical."""

    started = datetime(2026, 5, 4, 3, 2, 1, tzinfo=UTC)
    values: dict[str, Any] = {
        "action_id": "a" * 64,
        "plan_digest": "b" * 64,
        "subplan_digest": "c" * 64,
        "attempt_ref": "d" * 64,
        "account_ref": f"google_ads.customer.{_CUSTOMER}",
        "campaign_ref": f"google_ads.campaign.{_CUSTOMER}.{_CAMPAIGN}",
        "observation_interval_start": started,
        "observation_interval_end": started + timedelta(seconds=2),
        "observed_account_status": "CLOSED",
        "acknowledged_resource_name": _RESOURCE_NAME,
    }
    first = GoogleAdsCampaignPauseReadbackObservation(**values).model_dump(mode="json")
    second = GoogleAdsCampaignPauseReadbackObservation(**values).model_dump(mode="json")
    assert canonical_bytes(first) == canonical_bytes(second)
    assert digest(first) == digest(second)
    with pytest.raises(ValueError):
        GoogleAdsCampaignPauseReadbackObservation(
            **{**values, "acknowledged_resource_name": f"customers/{_CUSTOMER}/campaigns/77"}
        )


# --- drift, before any egress ----------------------------------------------


@pytest.mark.parametrize(
    ("drift", "code"),
    [
        ({"campaign_status": "PAUSED"}, "REMOTE_DRIFT"),
        ({"campaign_status": "REMOVED"}, "REMOTE_DRIFT"),
        ({"customer_status": "SUSPENDED"}, "IDENTITY_MISMATCH"),
        ({"currency": "EUR"}, "IDENTITY_MISMATCH"),
        ({"campaign_id": "77"}, "IDENTITY_MISMATCH"),
    ],
)
def test_drift_after_planning_is_refused_before_the_single_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: dict[str, Any], code: str
) -> None:
    """Status, currency, and customer drift all stop before ``campaigns:mutate``."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    for name, value in drift.items():
        setattr(prepared.google, name, value)
    with pytest.raises(AdhelmError) as refused:
        _execute(prepared, retained, monkeypatch)
    assert refused.value.code == code
    assert prepared.google.mutations == []
    assert _readbacks(tmp_path) == []
    # The refusal is in the kernel's preflight, before the durable start.
    assert Ledger(tmp_path).unresolved_starts() == []


# --- ambiguity --------------------------------------------------------------


def test_an_ambiguous_transport_is_ambiguous_and_blocks_the_next_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumed permit and a lost acknowledgement never become a retry."""

    prepared = _prepare(
        tmp_path, monkeypatch, mutate_error=httpx.ConnectError("transport"), applies_mutation=False
    )
    retained = _plan(prepared)
    result = _execute(prepared, retained, monkeypatch)
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert result.recovery_required is True
    assert len(prepared.google.mutations) == 1
    assert _readbacks(tmp_path) == []
    unresolved = Ledger(tmp_path).unresolved_starts()
    assert len(unresolved) == 1

    # The unresolved start refuses every later guarded write, on any provider.
    with pytest.raises(AdhelmError) as blocked:
        _plan(prepared)
    assert blocked.value.code == "RECOVERY_REQUIRED"
    with pytest.raises(AdhelmError) as retried:
        _execute(prepared, retained, monkeypatch)
    assert retried.value.code == "RECOVERY_REQUIRED"


def test_an_acknowledgement_naming_another_resource_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The closed parser refuses a resource name that is not this campaign."""

    prepared = _prepare(
        tmp_path,
        monkeypatch,
        mutate_response=httpx.Response(
            200, json={"results": [{"resourceName": f"customers/{_CUSTOMER}/campaigns/77"}]}
        ),
    )
    retained = _plan(prepared)
    result = _execute(prepared, retained, monkeypatch)
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert _readbacks(tmp_path) == []
    assert len(Ledger(tmp_path).unresolved_starts()) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"results": []}),
        httpx.Response(
            200,
            json={
                "results": [{"resourceName": _RESOURCE_NAME}],
                "partialFailureError": {"code": 3},
            },
        ),
        httpx.Response(200, json={"results": [{"resourceName": _RESOURCE_NAME, "campaign": {}}]}),
        httpx.Response(201, json={"results": [{"resourceName": _RESOURCE_NAME}]}),
        httpx.Response(200, content=b"not json"),
    ],
)
def test_every_other_acknowledgement_shape_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    prepared = _prepare(tmp_path, monkeypatch, mutate_response=response)
    result = _execute(prepared, _plan(prepared), monkeypatch)
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert _readbacks(tmp_path) == []


def test_a_readback_that_never_left_the_baseline_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed acknowledgement whose readback stays ENABLED contradicts itself."""

    prepared = _prepare(tmp_path, monkeypatch, applies_mutation=False)
    result = _execute(prepared, _plan(prepared), monkeypatch)
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert _readbacks(tmp_path) == []


# --- the permit -------------------------------------------------------------


def test_the_permit_is_consumed_exactly_once_immediately_before_the_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One claim, after the credential callback, before the only mutation."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    order: list[str] = []
    original_post = adapter_module._FixedGoogleAdsCampaignPausePost._post

    def recording_post(self: Any, *, permit: Any, attempt_ref: str) -> httpx.Response:
        original_consume = type(permit).consume

        def consume(inner: Any, **kwargs: Any) -> None:
            order.append("consume")
            original_consume(inner, **kwargs)

        monkeypatch.setattr(type(permit), "consume", consume)
        try:
            response = original_post(self, permit=permit, attempt_ref=attempt_ref)
        finally:
            monkeypatch.setattr(type(permit), "consume", original_consume)
        order.append("post")
        return response

    monkeypatch.setattr(adapter_module._FixedGoogleAdsCampaignPausePost, "_post", recording_post)
    result = _execute(prepared, retained, monkeypatch)
    assert result.results[0].status is EffectStatus.APPLIED_VERIFIED
    assert order == ["consume", "post"]
    assert len(prepared.google.mutations) == 1


def test_a_guarded_adapter_that_never_claims_its_permit_is_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``GUARDED_EGRESS_NOT_CLAIMED``: an unclaimed permit proves nothing."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    original_post = adapter_module._FixedGoogleAdsCampaignPausePost._post

    def unclaimed(self: Any, *, permit: Any, attempt_ref: str) -> httpx.Response:
        del permit
        return original_post(self, permit=_NeverClaims(), attempt_ref=attempt_ref)

    class _NeverClaims:
        def consume(self, **kwargs: Any) -> None:
            return None

    monkeypatch.setattr(adapter_module, "EgressPermit", _NeverClaims)
    monkeypatch.setattr(adapter_module._FixedGoogleAdsCampaignPausePost, "_post", unclaimed)
    result = _execute(prepared, retained, monkeypatch)
    assert result.results[0].status is EffectStatus.AMBIGUOUS_REMOTE_STATE


# --- verify and reconcile ---------------------------------------------------


def test_verify_reports_unverified_without_the_adapter_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A readback the plan does not predict never reaches ``verify`` as true."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    with _opened(prepared, retained) as adapter:
        subplan = adapter.action.plan.subplans[0]
        assert adapter.verify(subplan).verified is False
        assert adapter.verify(subplan).evidence_refs == ()


def test_reconcile_never_attributes_and_never_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    with _opened(prepared, retained) as adapter:
        subplan = adapter.action.plan.subplans[0]
        reconciliation = adapter.reconcile(subplan)
    assert reconciliation.status is EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert reconciliation.settled is False
    assert reconciliation.evidence_refs == ()
    # Even after the campaign really is PAUSED, the read cannot attribute it.
    prepared.google.campaign_status = "PAUSED"
    with _opened(prepared, retained) as adapter:
        again = adapter.reconcile(adapter.action.plan.subplans[0])
    assert again.settled is False
    assert again.status is EffectStatus.AMBIGUOUS_REMOTE_STATE


def _real_clock() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _opened(prepared: _Prepared, retained: RetainedExecutionPlan) -> Iterator[Any]:
    with GOOGLE_ADS_PAUSE_EXECUTION_ADAPTER_FACTORY.open_configured_adapter(
        prepared.directory, retained.record, Ledger(prepared.directory), clock=_real_clock
    ) as adapter:
        yield adapter


# --- unconfigured builds ----------------------------------------------------


def test_execute_plan_refuses_at_the_bridge_before_any_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary build has no COMPOSITE_EXECUTION_PLAN kind; A01 owns that."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    store = _no_credentials(monkeypatch)
    monkeypatch.setattr(
        route, "_resolve_composite_approval_bridge", route._UninstalledCompositeApprovalBridge
    )
    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path, ExecutionPlanExecutionRequest(plan_record_digest=retained.record_digest)
        )
    assert refused.value.code == "NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED"
    assert store.calls == 0
    assert prepared.google.mutations == []
    assert Ledger(tmp_path).unresolved_starts() == []


def test_a_plan_that_is_not_retained_is_refused_before_the_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(tmp_path, monkeypatch)
    with pytest.raises(AdhelmError) as refused:
        execute_retained_execution_plan_with_installed_approval(
            tmp_path, ExecutionPlanExecutionRequest(plan_record_digest="0" * 64)
        )
    assert refused.value.code == "EXECUTION_PLAN_NOT_RETAINED"


def test_unregistered_readback_kind_refuses_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that cannot retain the observation refuses the whole execution."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    monkeypatch.delitem(
        ledger_module._KINDS["changes"], GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND, raising=False
    )
    store = _no_credentials(monkeypatch)
    with pytest.raises(AdhelmError) as refused, _opened(prepared, retained):
        pass  # pragma: no cover - the context manager refuses on entry
    assert refused.value.code == "GOOGLE_ADS_PAUSE_READBACK_RECORD_KIND_NOT_INSTALLED"
    assert store.calls == 0
    assert prepared.google.searches == ["customer", "campaign"]


def test_the_factory_refuses_a_plan_with_no_retained_google_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provider-neutral record alone is never enough to reach a campaign."""

    prepared = _prepare(tmp_path, monkeypatch)
    retained = _plan(prepared)
    ledger = Ledger(tmp_path)
    monkeypatch.setattr(
        Ledger,
        "checkpoint",
        lambda self: {
            "records_by_stream": {
                "changes": [
                    record
                    for record in ledger_module.Ledger.records(self, "changes")
                    if record["record_kind"] != "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"
                ]
            }
        },
    )
    with pytest.raises(AdhelmError) as refused:
        _retained_action_for_plan(ledger, retained.record.plan)
    assert refused.value.code == "GOOGLE_ADS_PAUSE_PLAN_NOT_RETAINED"
