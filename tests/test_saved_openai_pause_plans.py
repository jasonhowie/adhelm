"""Closed local retention for the fixture-only OpenAI pause action."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_openai_pause import NOW, _action, _Scenario

from adhelm.canonical import canonical_bytes, digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.plan_records import (
    OpenAIPauseExecutionPlanRecord,
    openai_pause_execution_plan_record,
    revalidate_openai_pause_action,
)
from adhelm.projections import export_verified
from adhelm.providers.openai_pause import OpenAIPausePlannedAction
from adhelm.providers.openai_pause_contract import _build_plan
from adhelm.saved_plans import (
    load_openai_pause_plan,
    openai_pause_plan_catalog,
    retain_openai_pause_plan,
)


def _payload(action: OpenAIPausePlannedAction) -> dict[str, Any]:
    return openai_pause_execution_plan_record(action).model_dump(mode="json")


def _append_kwargs(action: OpenAIPausePlannedAction) -> dict[str, Any]:
    return {
        "provider": "openai_ads",
        "account_ref": action.binding.account_ref,
        "source": "LOCAL_CLI",
    }


def test_pause_plan_retention_reconstructs_exact_action_and_keeps_digest_compatibility(
    tmp_path: Path,
) -> None:
    action, credentials = _action(_Scenario())
    assert action.plan.plan_digest == (
        "56d247c754d91f5ed418b4de56cbddca0637772fdf27f4b5ffbe6877deff9945"
    )
    assert action.plan.subplan_digests == (
        "059a101facb6bbb19d73395b2bd4ca164c4ded52fc71248598fd91eb7e161f73",
    )
    assert digest(action) == "75ea6e147c869940bf966c5139b209bdc1f9e4eb7c278a56f2ad78cbd6792f4c"

    ledger = Ledger(tmp_path, profile_ref="profile")
    appended = retain_openai_pause_plan(ledger, action)
    loaded, provenance = load_openai_pause_plan(ledger, appended["record_digest"])

    assert loaded == action
    assert provenance == {
        "record_digest": appended["record_digest"],
        "action_digest": digest(action),
        "plan_digest": action.plan.plan_digest,
        "checkpoint_digest": ledger.checkpoint()["checkpoint_digest"],
        "recorded_at": appended["recorded_at"],
        "expires_at": action.plan.expires_at.isoformat().replace("+00:00", "Z"),
        "capability_status": "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
        "historical_evidence_status": "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
        "execution_authority": False,
        "requires_current_kernel_checks": True,
    }
    assert json.loads(json.dumps(provenance))["expires_at"] == provenance["expires_at"]
    retained = [row for row in ledger.records("changes") if row["record_kind"] == "EXECUTION_PLAN"]
    assert len(retained) == 1
    row = retained[0]
    assert row["provider"] == "openai_ads"
    assert row["account_ref"] == action.binding.account_ref
    assert row["profile_ref"] == action.plan.profile_ref == action.binding.profile_ref
    assert row["source"] == "LOCAL_CLI"
    assert row["run_id"] is row["occurred_at"] is row["observed_at"] is None
    assert row["recorded_at"] == appended["recorded_at"]
    assert credentials.secret.encode() not in canonical_bytes(row)
    assert not ledger.unresolved_starts() and not ledger.unresolved_runs()


def test_pause_plan_catalog_is_bounded_and_omits_full_action_and_local_identifiers(
    tmp_path: Path,
) -> None:
    action, _ = _action(_Scenario())
    ledger = Ledger(tmp_path, profile_ref="profile")
    first = retain_openai_pause_plan(ledger, action)
    second = retain_openai_pause_plan(ledger, action)

    catalog = openai_pause_plan_catalog(ledger, limit=1)
    assert catalog["total_count"] == 2 and catalog["next_cursor"] == "1"
    entry = catalog["plans"][0]
    assert entry == {
        "record_digest": second["record_digest"],
        "action_digest": digest(action),
        "plan_digest": action.plan.plan_digest,
        "provider": "openai_ads",
        "recorded_at": second["recorded_at"],
        "expires_at": action.plan.expires_at.isoformat().replace("+00:00", "Z"),
        "capability_status": "DEVELOPMENT_FIXTURE_ONLY_NOT_LIVE_VALIDATED",
        "maximum_incremental_spend": {"minor_units": 0, "currency": "USD"},
        "historical_evidence_status": "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
        "execution_authority": False,
        "requires_current_kernel_checks": True,
    }
    next_page = openai_pause_plan_catalog(ledger, cursor=1, limit=1)
    assert next_page["plans"][0]["record_digest"] == first["record_digest"]
    for private in (
        action.binding.account_ref,
        action.binding.account_client_ref,
        action.binding.profile_ref,
        action.binding.credential_binding_ref,
        action.binding.campaign.campaign_ref,
        action.binding.campaign.campaign_client_ref,
    ):
        assert private not in str(catalog)
    assert "action" not in entry and "binding" not in entry
    assert json.loads(json.dumps(catalog))["plans"][0]["expires_at"] == entry["expires_at"]
    with pytest.raises(AdhelmError, match="pagination"):
        openai_pause_plan_catalog(ledger, cursor=True)
    with pytest.raises(AdhelmError, match="Cursor"):
        openai_pause_plan_catalog(ledger, cursor=3)


def test_expired_plan_remains_history_without_becoming_authority(tmp_path: Path) -> None:
    action, _ = _action(
        _Scenario(),
        expires_at=NOW - timedelta(seconds=1),
        planning_now=NOW - timedelta(seconds=2),
    )
    stored = retain_openai_pause_plan(Ledger(tmp_path, profile_ref="profile"), action)
    _, provenance = load_openai_pause_plan(
        Ledger(tmp_path, profile_ref="profile"), stored["record_digest"]
    )

    assert provenance["expires_at"] == action.plan.expires_at.isoformat().replace("+00:00", "Z")
    assert provenance["execution_authority"] is False
    assert provenance["requires_current_kernel_checks"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider", "meta_ads"),
        ("account_ref", "account:substituted"),
        ("source", "IMPORT"),
        ("run_id", "run:forged"),
        ("occurred_at", NOW.isoformat()),
        ("observed_at", NOW.isoformat()),
    ],
)
def test_plan_append_rejects_false_envelope_before_creating_history(
    tmp_path: Path, field: str, value: str
) -> None:
    action, _ = _action(_Scenario())
    kwargs = _append_kwargs(action)
    kwargs[field] = value
    with pytest.raises(AdhelmError) as error:
        Ledger(tmp_path / field, profile_ref="profile").append(
            "changes", "EXECUTION_PLAN", _payload(action), **kwargs
        )
    assert error.value.code == "INVALID_EXECUTION_PLAN_PROVENANCE"
    assert not (tmp_path / field / "ledger").exists()


def test_plan_append_rejects_workspace_profile_mismatch_before_creating_history(
    tmp_path: Path,
) -> None:
    action, _ = _action(_Scenario())
    with pytest.raises(AdhelmError) as error:
        Ledger(tmp_path, profile_ref="workspace-profile").append(
            "changes", "EXECUTION_PLAN", _payload(action), **_append_kwargs(action)
        )
    assert error.value.code == "INVALID_EXECUTION_PLAN_PROVENANCE"
    assert not (tmp_path / "ledger").exists()


def test_append_persists_the_prevalidated_plan_snapshot_not_a_mutated_caller_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action, _ = _action(_Scenario())
    payload = _payload(action)
    alternate_binding = action.binding.model_copy(update={"account_ref": "account:alternate"})
    alternate_action = OpenAIPausePlannedAction.model_construct(
        schema_version=1,
        status=action.status,
        binding=alternate_binding,
        plan=_build_plan(alternate_binding, expires_at=action.plan.expires_at),
    )
    alternate_payload = _payload(alternate_action)
    ledger = Ledger(tmp_path, profile_ref="profile")
    original_ensure_dirs = ledger._ensure_dirs

    def replace_after_validation(stream: str) -> None:
        payload.clear()
        payload.update(alternate_payload)
        original_ensure_dirs(stream)

    monkeypatch.setattr(ledger, "_ensure_dirs", replace_after_validation)
    appended = ledger.append("changes", "EXECUTION_PLAN", payload, **_append_kwargs(action))

    assert appended["account_ref"] == action.binding.account_ref
    assert appended["payload"]["action"]["binding"]["account_ref"] == action.binding.account_ref
    assert ledger.verify("changes")["status"] == "VERIFIED"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda row: row.update({"provider": "meta_ads"}),
        lambda row: row.update({"account_ref": "account:substituted"}),
        lambda row: row.update({"source": "IMPORT"}),
        lambda row: row.update({"run_id": "run:forged"}),
        lambda row: row.update({"occurred_at": NOW.isoformat()}),
        lambda row: row.update({"observed_at": NOW.isoformat()}),
    ],
)
def test_rehashed_false_envelope_fails_verified_checkpoint(tmp_path: Path, mutation: Any) -> None:
    action, _ = _action(_Scenario())
    stored = retain_openai_pause_plan(Ledger(tmp_path, profile_ref="profile"), action)
    path = next((tmp_path / "ledger" / "changes").glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    mutation(rows[-1])
    rows[-1]["record_digest"] = digest(
        {name: value for name, value in rows[-1].items() if name != "record_digest"}
    )
    path.write_bytes(b"".join(canonical_bytes(row) + b"\n" for row in rows))

    for operation in (
        lambda: openai_pause_plan_catalog(Ledger(tmp_path, profile_ref="profile")),
        lambda: load_openai_pause_plan(
            Ledger(tmp_path, profile_ref="profile"), stored["record_digest"]
        ),
    ):
        with pytest.raises(AdhelmError) as error:
            operation()
        assert error.value.code == "INVALID_EXECUTION_PLAN_PROVENANCE"


def test_rehashed_noncanonical_numeric_plan_control_fails_verified_checkpoint(
    tmp_path: Path,
) -> None:
    action, _ = _action(_Scenario())
    stored = retain_openai_pause_plan(Ledger(tmp_path, profile_ref="profile"), action)
    path = next((tmp_path / "ledger" / "changes").glob("*.jsonl"))
    rows = [json.loads(line) for line in path.read_bytes().splitlines()]
    rows[-1]["payload"]["action"]["schema_version"] = 1.0
    rows[-1]["record_digest"] = digest(
        {name: value for name, value in rows[-1].items() if name != "record_digest"}
    )
    # Deliberately retain the raw float token: RFC 8785 would otherwise turn
    # it into the integer 1 before the strict nested action validator sees it.
    path.write_bytes(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows).encode()
    )

    with pytest.raises(AdhelmError) as error:
        load_openai_pause_plan(Ledger(tmp_path, profile_ref="profile"), stored["record_digest"])
    assert error.value.code == "INVALID_LEDGER_PAYLOAD"


def test_constructed_or_incomplete_action_cannot_bypass_closed_reconstruction(
    tmp_path: Path,
) -> None:
    action, _ = _action(_Scenario())
    changed_plan = action.plan.model_copy(update={"profile_ref": "other-profile"})
    copied = action.model_copy(update={"plan": changed_plan})
    constructed = OpenAIPausePlannedAction.model_construct(
        schema_version=1,
        status=action.status,
        binding=action.binding,
        plan=changed_plan,
    )
    for forged in (copied, constructed):
        with pytest.raises(AdhelmError) as error:
            revalidate_openai_pause_action(forged)
        assert error.value.code == "INVALID_EXECUTION_PLAN"

    invalid_timezone_binding = action.binding.model_copy(
        update={"account_timezone": "not/a/real-timezone"}
    )
    invalid_timezone = OpenAIPausePlannedAction.model_construct(
        schema_version=1,
        status=action.status,
        binding=invalid_timezone_binding,
        plan=_build_plan(invalid_timezone_binding, expires_at=action.plan.expires_at),
    )
    with pytest.raises(AdhelmError) as timezone_error:
        revalidate_openai_pause_action(invalid_timezone)
    assert timezone_error.value.code == "INVALID_EXECUTION_PLAN"

    class SecretBearingObject:
        def __repr__(self) -> str:
            return "secret-never-exposed"

    malformed = OpenAIPausePlannedAction.model_construct(
        schema_version=1,
        status=action.status,
        binding=SecretBearingObject(),
        plan=SecretBearingObject(),
    )
    with pytest.raises(AdhelmError) as serialization_error:
        revalidate_openai_pause_action(malformed)
    assert serialization_error.value.code == "INVALID_EXECUTION_PLAN"
    assert "secret-never-exposed" not in str(serialization_error.value)

    constructed_bytes = OpenAIPausePlannedAction.model_construct(
        schema_version=1,
        status=action.status,
        binding=action.binding.model_copy(update={"account_ref": b"account:bytes"}),
        plan=action.plan,
    )
    constructed_decimal = OpenAIPausePlannedAction.model_construct(
        schema_version=1,
        status=action.status,
        binding=action.binding.model_copy(
            update={
                "campaign": action.binding.campaign.model_copy(
                    update={"lifetime_spend_limit_micros": Decimal("10000000")}
                )
            }
        ),
        plan=action.plan,
    )
    for malformed_nested_value in (constructed_bytes, constructed_decimal):
        with pytest.raises(AdhelmError) as nested_error:
            revalidate_openai_pause_action(malformed_nested_value)
        assert nested_error.value.code == "INVALID_EXECUTION_PLAN"

    for path, value in (
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("plan", "schema_version"), True),
        (("plan", "schema_version"), 1.0),
        (("plan", "stop_on_non_verified"), 1),
        (("plan", "stop_on_non_verified"), 1.0),
    ):
        raw_action = action.model_dump(mode="json")
        target: Any = raw_action
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(AdhelmError) as error:
            revalidate_openai_pause_action(raw_action)
        assert error.value.code == "INVALID_EXECUTION_PLAN"

    for path, value in (
        (("binding", "account_ref"), b"account:bytes"),
        (("binding", "campaign", "lifetime_spend_limit_micros"), Decimal("1000000")),
        (("binding", "account_client_ref"), object()),
    ):
        raw_action = action.model_dump(mode="json")
        target = raw_action
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(AdhelmError) as error:
            revalidate_openai_pause_action(raw_action)
        assert error.value.code == "INVALID_EXECUTION_PLAN"

    payload = _payload(action)
    for path in (
        ("action", "binding", "account_ref"),
        ("action", "plan", "stop_on_non_verified"),
        ("action", "plan", "subplans", 0, "account", "auth_profile_ref"),
        ("execution_authority",),
        ("requires_current_kernel_checks",),
    ):
        forged_payload = deepcopy(payload)
        target: Any = forged_payload
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]
        with pytest.raises(AdhelmError) as error:
            Ledger(tmp_path / f"missing-{len(path)}", profile_ref="profile").append(
                "changes", "EXECUTION_PLAN", forged_payload, **_append_kwargs(action)
            )
        assert error.value.code == "INVALID_LEDGER_PAYLOAD"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"action_digest": "0" * 64}),
        lambda payload: payload.update({"plan_digest": "0" * 64}),
        lambda payload: payload.update({"expires_at": NOW.isoformat()}),
        lambda payload: payload.update(
            {"maximum_incremental_spend": {"minor_units": 1, "currency": "USD"}}
        ),
        lambda payload: payload.update({"execution_authority": 0}),
        lambda payload: payload.update({"requires_current_kernel_checks": 1}),
        lambda payload: payload["action"].update({"schema_version": True}),
        lambda payload: payload["action"].update({"schema_version": 1.0}),
        lambda payload: payload["action"]["plan"].update({"schema_version": True}),
        lambda payload: payload["action"]["plan"].update({"schema_version": 1.0}),
        lambda payload: payload["action"]["plan"].update({"stop_on_non_verified": 1}),
        lambda payload: payload["action"]["plan"].update({"stop_on_non_verified": 1.0}),
        lambda payload: payload.update({"native_payload": {"arbitrary": "not allowed"}}),
    ],
)
def test_plan_record_rejects_digest_authority_and_generic_payload_tampering(
    tmp_path: Path, mutation: Any
) -> None:
    action, _ = _action(_Scenario())
    payload = _payload(action)
    mutation(payload)
    with pytest.raises(AdhelmError) as error:
        Ledger(tmp_path, profile_ref="profile").append(
            "changes", "EXECUTION_PLAN", payload, **_append_kwargs(action)
        )
    assert error.value.code == "INVALID_LEDGER_PAYLOAD"
    assert not (tmp_path / "ledger").exists()


def test_rotation_checkpoint_and_export_preserve_old_plan_bytes_and_redact_private_values(
    tmp_path: Path,
) -> None:
    action, credentials = _action(_Scenario())
    ledger = Ledger(tmp_path, profile_ref="profile", segment_bytes=1024)
    first = retain_openai_pause_plan(ledger, action)
    original_segments = {
        path.name: path.read_bytes() for path in (tmp_path / "ledger" / "changes").glob("*.jsonl")
    }
    second = retain_openai_pause_plan(ledger, action)

    assert first["record_digest"] != second["record_digest"]
    assert len(list((tmp_path / "ledger" / "changes").glob("*.jsonl"))) >= 3
    for name, original in original_segments.items():
        assert (tmp_path / "ledger" / "changes" / name).read_bytes() == original
    checkpoint = ledger.checkpoint()
    assert checkpoint["status"] == "VERIFIED_CHECKPOINT"
    assert checkpoint["streams"]["changes"]["high_watermark"] == 5

    output = tmp_path / "export"
    export_verified(ledger, output, opaque_key=b"x" * 32)
    exported = (output / "records.jsonl").read_text()
    values: set[str] = set()

    def collect_strings(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                collect_strings(item)
        elif isinstance(value, list):
            for item in value:
                collect_strings(item)
        elif isinstance(value, str):
            values.add(value)

    for line in exported.splitlines():
        collect_strings(json.loads(line))
    for private in (
        credentials.secret,
        action.binding.account_ref,
        action.binding.account_client_ref,
        action.binding.profile_ref,
        action.binding.credential_binding_ref,
        action.binding.campaign.campaign_ref,
        action.binding.campaign.campaign_client_ref,
    ):
        assert private not in values
    assert action.plan.plan_digest in exported
    assert "raw_response" not in exported and "native_payload" not in exported


def test_record_model_rejects_direct_missing_and_constructed_nested_values() -> None:
    action, _ = _action(_Scenario())
    payload = _payload(action)
    del payload["action"]["plan"]["subplans"][0]["before"]
    with pytest.raises(ValidationError):
        OpenAIPauseExecutionPlanRecord.model_validate(payload)
