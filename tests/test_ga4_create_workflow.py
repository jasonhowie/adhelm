from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_ga4_admin_execution_observation import (
    _ACCESS,
    _KEY,
    _NOW,
    _REFRESH,
    _SUBJECT,
    _install_fixed_inventory_http,
    _install_oauth,
    _profile,
)
from test_ga4_create_selection import _history
from test_saved_ga4_key_event_candidates import _definition

from adhelm import ga4_admin_execution_observation as observations
from adhelm import ga4_create_workflow as workflow
from adhelm.canonical import canonical_bytes
from adhelm.errors import AdhelmError
from adhelm.ga4_create_plan_records import ga4_key_event_create_plan_record
from adhelm.ga4_create_plan_views import preview_ga4_key_event_create_plan
from adhelm.ga4_create_workflow import (
    Ga4KeyEventCreatePlanRequest,
    prepare_and_retain_ga4_key_event_create_plan,
)
from adhelm.projections import export_verified
from adhelm.saved_conversion_contracts import retain_conversion_contract
from adhelm.saved_ga4_create_plans import (
    ga4_key_event_create_plan_catalog,
    load_ga4_key_event_create_plan,
)


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on_list=None):
    store, profile, config, current = _install_oauth(tmp_path, monkeypatch)
    ledger, _, contract, fresh, candidate = _history(tmp_path)
    monkeypatch.setattr(workflow, "_now", lambda: current[0])
    monkeypatch.setattr(observations, "_utc_now", lambda: current[0])
    monkeypatch.setattr(workflow, "export_key", lambda: _KEY)
    calls = _install_fixed_inventory_http(monkeypatch, on_list=on_list)
    request = Ga4KeyEventCreatePlanRequest(
        profile_id=profile.profile_id,
        candidate_record_digest=candidate["record_digest"],
        fresh=fresh,
    )
    return request, ledger, store, profile, config, current, calls, contract


def test_configured_create_plan_retains_exact_sources_without_remote_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, store, profile, config, _, calls, _ = _setup(tmp_path, monkeypatch)
    result = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert result.configuration == config
    assert len(calls) == 3 and all(call.method == "GET" for call in calls)
    assert store.reads == 1
    action, provenance = load_ga4_key_event_create_plan(ledger, result.plan_record_digest)
    assert canonical_bytes(action) == canonical_bytes(result.action)
    assert action.plan.profile_ref == ledger.profile_ref
    assert action.plan.subplans[0].account.auth_profile_ref == profile.profile_id
    assert action.plan.subplans[0].max_spend.minor_units == 0
    assert action.plan.subplans[0].recovery_actions == ()
    assert action.execution_authority is False
    assert action.destination_to_execution_profile_mapping == "NOT_EVIDENCED"
    assert provenance["execution_authority"] is False
    records = ledger.checkpoint()["records_by_stream"]["changes"]
    assert [r["record_kind"] for r in records][-2:] == [
        "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION",
        "GA4_KEY_EVENT_CREATE_PLAN",
    ]
    assert records[-1]["source"] == "LOCAL_CLI"
    assert records[-1]["occurred_at"] is None and records[-1]["observed_at"] is None
    persisted = canonical_bytes(records)
    for private in (_ACCESS, _REFRESH, _SUBJECT, "A customer-defined qualified request event."):
        assert private.encode() not in persisted
    assert ga4_key_event_create_plan_catalog(ledger)["total_count"] == 1


def test_retained_review_and_exports_keep_request_and_identifiers_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, profile, _, _, _, _ = _setup(tmp_path, monkeypatch)
    result = prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    record = ga4_key_event_create_plan_record(result.action)
    review = preview_ga4_key_event_create_plan(record, opaque_key=_KEY, reviewed_at=_NOW)
    text = json.dumps(review)
    for private in (
        "qualified_request",
        "properties/123",
        "ga4_admin.property.123",
        "ga4_admin.web_stream.456",
        profile.profile_id,
        profile.principal_ref,
        profile.client_ref,
        "eventName",
        "fingerprint_key_marker",
        "inventory_content_marker",
    ):
        assert private not in text
    assert review["operation"] == "CREATE"
    assert review["configuration_effect"] == "IMMEDIATE_LIVE_GA4_KEY_EVENT_NO_DRAFT"
    assert review["execution_authority"] is False
    assert review["before"]["member_count"] == 2 and review["after"]["member_count"] == 3
    export_verified(ledger, tmp_path / "export", opaque_key=_KEY, include_marketing_text=True)
    exported = (tmp_path / "export" / "records.jsonl").read_text()
    for private in ("qualified_request", "eventName", "properties/123/keyEvents"):
        assert private not in exported


def test_static_profile_rejects_before_sharing_key_or_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm.config import save_configuration
    from adhelm.models import Configuration

    ledger, _, _, fresh, candidate = _history(tmp_path)
    profile = _profile(credential_source="OS_STORE")
    save_configuration(tmp_path, Configuration(profiles=[profile]))

    def unexpected():
        pytest.fail("static profile must fail before private key lookup")

    monkeypatch.setattr(workflow, "export_key", unexpected)
    request = Ga4KeyEventCreatePlanRequest(
        profile_id=profile.profile_id,
        candidate_record_digest=candidate["record_digest"],
        fresh=fresh,
    )
    before = ledger.checkpoint()["checkpoint_digest"]
    with pytest.raises(AdhelmError) as error:
        prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert error.value.code == "GA4_ADMIN_EDIT_OAUTH_PROFILE_REQUIRED"
    assert ledger.checkpoint()["checkpoint_digest"] == before


def test_supersession_during_read_retains_observation_without_stale_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = {}

    def supersede():
        retain_conversion_contract(
            holder["ledger"],
            _definition(
                version=2,
                supersedes=holder["contract"].content_digest,
                effective_from=_NOW - timedelta(days=1),
            ),
        )

    request, ledger, _, _, _, _, calls, contract = _setup(tmp_path, monkeypatch, on_list=supersede)
    holder.update(ledger=ledger, contract=contract)
    with pytest.raises(AdhelmError) as error:
        prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert error.value.code == "GA4_CREATE_CONTRACT_SUPERSEDED"
    assert len(calls) == 3
    kinds = [item["record_kind"] for item in ledger.checkpoint()["records_by_stream"]["changes"]]
    assert kinds[-1] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    assert "GA4_KEY_EVENT_CREATE_PLAN" not in kinds


def test_configuration_drift_during_read_prevents_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from adhelm.config import save_configuration

    holder = {}

    def drift():
        changed = holder["config"].model_copy(
            update={"output": holder["config"].output.model_copy(update={"max_rows": 7})}
        )
        save_configuration(tmp_path, changed, replace=True)

    request, ledger, _, _, config, _, _, _ = _setup(tmp_path, monkeypatch, on_list=drift)
    holder["config"] = config
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    with pytest.raises(AdhelmError) as error:
        prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert error.value.code == "CONFIGURATION_DRIFT"
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


def test_key_change_before_retention_rejects_completed_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    keys = iter([_KEY, b"x" * 32])
    monkeypatch.setattr(workflow, "export_key", lambda: next(keys))
    checkpoint = ledger.checkpoint()["checkpoint_digest"]
    with pytest.raises(AdhelmError) as error:
        prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert error.value.code == "FINGERPRINT_KEY_CHANGED"
    assert len(calls) == 3
    assert ledger.checkpoint()["checkpoint_digest"] == checkpoint


def test_expiry_at_durable_append_blocks_plan_and_preserves_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, _, _, _, _ = _setup(tmp_path, monkeypatch)
    instants = iter([_NOW, _NOW, _NOW + timedelta(seconds=300)])
    monkeypatch.setattr(workflow, "_now", lambda: next(instants))
    with pytest.raises(AdhelmError) as error:
        prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert error.value.code == "PLAN_EXPIRED"
    kinds = [item["record_kind"] for item in ledger.checkpoint()["records_by_stream"]["changes"]]
    assert kinds[-1] == "GA4_ADMIN_EDIT_INVENTORY_OBSERVATION"
    assert "GA4_KEY_EVENT_CREATE_PLAN" not in kinds


def test_local_failure_after_append_reports_unverified_retention_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request, ledger, _, _, _, _, calls, _ = _setup(tmp_path, monkeypatch)
    retain = workflow.retain_ga4_key_event_create_plan

    def append_then_fail(*args, **kwargs):
        retain(*args, **kwargs)
        raise OSError("untrusted-storage-detail")

    monkeypatch.setattr(workflow, "retain_ga4_key_event_create_plan", append_then_fail)
    with pytest.raises(AdhelmError) as error:
        prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert error.value.code == "GA4_CREATE_RETENTION_UNVERIFIED"
    assert "untrusted-storage-detail" not in str(error.value)
    assert len(calls) == 3
    assert ga4_key_event_create_plan_catalog(ledger)["total_count"] == 1


def test_provider_read_runs_outside_configuration_retention_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import contextmanager

    depth = 0

    def unlocked():
        assert depth == 0

    request, _, _, _, _, _, _, _ = _setup(tmp_path, monkeypatch, on_list=unlocked)
    original = workflow.configuration_transaction

    @contextmanager
    def tracking_transaction(path):
        nonlocal depth
        with original(path) as transaction:
            depth += 1
            try:
                yield transaction
            finally:
                depth -= 1

    monkeypatch.setattr(workflow, "configuration_transaction", tracking_transaction)
    prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert depth == 0


def test_retained_oauth_proof_schema_allows_omission_but_not_null() -> None:
    from adhelm.ga4_admin_edit_records import Ga4AdminEditInventoryObservation

    schema = Ga4AdminEditInventoryObservation.model_json_schema()
    field = schema["properties"]["execution_observation"]
    assert "execution_observation" not in schema["required"]
    assert field["$ref"].endswith("/Ga4AdminEditExecutionInventoryObservation")
    assert "anyOf" not in field
    assert "default" not in field


def test_start_before_final_selection_cannot_be_adopted_into_plan(tmp_path, monkeypatch) -> None:
    from test_ledger import change

    request, ledger, _, _, _, _, _, _ = _setup(tmp_path, monkeypatch)
    original = workflow.select_ga4_key_event_create_candidate
    calls = 0

    def select(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            ledger.append("changes", "CHANGE_EVENT", change(), provider="google_ads")
        return original(*args, **kwargs)

    monkeypatch.setattr(workflow, "select_ga4_key_event_create_candidate", select)
    with pytest.raises(AdhelmError) as raised:
        prepare_and_retain_ga4_key_event_create_plan(tmp_path, request)
    assert raised.value.code == "RECOVERY_REQUIRED"
    assert not any(
        item["record_kind"] == "GA4_KEY_EVENT_CREATE_PLAN" for item in ledger.records("changes")
    )
