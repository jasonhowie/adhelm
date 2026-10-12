"""Independent adversarial retention checks for the fixed OpenAI pause plan."""

from __future__ import annotations

import json
import traceback
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from adhelm.canonical import digest
from adhelm.errors import AdhelmError
from adhelm.ledger import Ledger
from adhelm.plan_records import (
    OpenAIPauseExecutionPlanRecord,
    openai_pause_execution_plan_record,
    revalidate_openai_pause_action,
)
from adhelm.providers.openai_pause_contract import (
    OpenAIPauseBinding,
    OpenAIPauseCampaignBaseline,
    OpenAIPausePlannedAction,
    _build_plan,
)
from adhelm.saved_plans import (
    load_openai_pause_plan,
    openai_pause_plan_catalog,
    retain_openai_pause_plan,
)

_EXPIRY = datetime(2030, 1, 2, tzinfo=UTC)
_CREDENTIAL_BINDING = "credential-binding:sha256:" + "a" * 64


def _action(
    *,
    account_ref: str = "local-account",
    account_client_ref: str = "account_fixture",
    profile_ref: str = "auth-profile",
    campaign_client_ref: str = "campaign_fixture",
) -> OpenAIPausePlannedAction:
    binding = OpenAIPauseBinding(
        account_ref=account_ref,
        account_client_ref=account_client_ref,
        profile_ref=profile_ref,
        credential_binding_ref=_CREDENTIAL_BINDING,
        account_timezone="UTC",
        account_currency="USD",
        account_status="active",
        campaign=OpenAIPauseCampaignBaseline(
            campaign_ref=f"openai_ads.campaign.{campaign_client_ref}",
            campaign_client_ref=campaign_client_ref,
            status="active",
            bidding_type="clicks",
            lifetime_spend_limit_micros="1000000",
            start_at=datetime(2030, 1, 1, tzinfo=UTC),
        ),
    )
    return OpenAIPausePlannedAction(
        binding=binding,
        plan=_build_plan(binding, expires_at=_EXPIRY),
    )


def _payload(action: OpenAIPausePlannedAction) -> dict[str, Any]:
    return openai_pause_execution_plan_record(action).model_dump(mode="json")


def test_contract_extraction_preserves_the_preexisting_pause_action_digests() -> None:
    action = _action()

    # These digests were calculated from the same fixed fixture against the
    # pre-extraction provider module at commit 464e662. Moving pure models into
    # the retention-safe module cannot rewrite a historical exact plan.
    assert (
        action.plan.plan_digest
        == "edcabbb50a5c0672fa62fffed68e2abcd9badf1379e9ecb5c5d3d4b0031f4250"
    )
    assert action.plan.subplan_digests == (
        "76a2c9e2faad7d740f35bf427905943a2dd4431981bfe1765b7965c32e562d89",
    )
    assert digest(action) == "23ffca944521091fd8367b4f13af454755ad7dd326cb7521aa4139ee63d4fa3e"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("plan", "schema_version"), True),
        (("plan", "schema_version"), 1.0),
        (("plan", "stop_on_non_verified"), 1),
        (("plan", "stop_on_non_verified"), 1.0),
    ],
)
def test_raw_pause_action_rejects_bool_and_float_literal_substitutions(
    path: tuple[str, ...], value: object
) -> None:
    raw = _action().model_dump(mode="json")
    target: dict[str, Any] = raw
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(AdhelmError) as raised:
        revalidate_openai_pause_action(raw)

    assert raised.value.code == "INVALID_EXECUTION_PLAN"


@pytest.mark.parametrize(
    "path",
    [
        ("binding", "credential_binding_ref"),
        ("binding", "campaign", "lifetime_spend_limit_micros"),
        ("plan", "subplans"),
    ],
)
def test_raw_pause_action_requires_every_closed_field(path: tuple[str, ...]) -> None:
    raw = _action().model_dump(mode="json")
    target: dict[str, Any] = raw
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]

    with pytest.raises(AdhelmError) as raised:
        revalidate_openai_pause_action(raw)

    assert raised.value.code == "INVALID_EXECUTION_PLAN"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("binding", "account_ref"), b"local-account"),
        (("binding", "campaign", "lifetime_spend_limit_micros"), Decimal("1000000")),
        (("binding", "campaign", "bidding_type"), object()),
    ],
)
def test_python_mapping_action_rejects_non_json_values_before_normalization(
    path: tuple[str, ...], value: object
) -> None:
    raw = _action().model_dump(mode="json")
    target: dict[str, Any] = raw
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value

    with pytest.raises(AdhelmError) as raised:
        revalidate_openai_pause_action(raw)

    assert raised.value.code == "INVALID_EXECUTION_PLAN"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("capability_status", "LIVE_VALIDATED"),
        ("action_digest", "0" * 64),
        ("plan_digest", "0" * 64),
        ("maximum_incremental_spend", {"minor_units": 1, "currency": "USD"}),
        ("execution_authority", True),
        ("execution_authority", 0),
        ("requires_current_kernel_checks", False),
        ("requires_current_kernel_checks", 1),
        ("historical_evidence_status", "CURRENT_REMOTE_EVIDENCE"),
    ],
)
def test_execution_record_rejects_false_authority_and_nonexact_json_flags(
    field: str, value: object
) -> None:
    raw = _payload(_action())
    raw[field] = value

    with pytest.raises(ValidationError):
        OpenAIPauseExecutionPlanRecord.model_validate(raw)


def test_constructed_action_serialization_failure_has_no_untrusted_representation() -> None:
    class UntrustedValue:
        def __repr__(self) -> str:
            return "EXECUTION_PLAN_SECRET_SENTINEL"

    valid = _action()
    forged = OpenAIPausePlannedAction.model_construct(
        binding=UntrustedValue(),
        plan=valid.plan,
    )

    with pytest.raises(AdhelmError) as raised:
        revalidate_openai_pause_action(forged)

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.code == "INVALID_EXECUTION_PLAN"
    assert "EXECUTION_PLAN_SECRET_SENTINEL" not in str(raised.value)
    assert "EXECUTION_PLAN_SECRET_SENTINEL" not in rendered


@pytest.mark.parametrize(
    "forged",
    [
        lambda action: OpenAIPausePlannedAction.model_construct(
            schema_version=True,
            binding=action.binding,
            plan=action.plan,
        ),
        lambda action: OpenAIPausePlannedAction.model_construct(
            binding=OpenAIPauseBinding.model_construct(
                **{**action.binding.model_dump(mode="python"), "account_ref": b"local-account"}
            ),
            plan=action.plan,
        ),
        lambda action: action.model_copy(
            update={
                "plan": action.plan.model_construct(
                    **{**action.plan.model_dump(mode="python"), "stop_on_non_verified": 1}
                )
            }
        ),
    ],
)
def test_deep_constructed_or_copied_action_cannot_reach_retention(
    tmp_path: Path, forged: Any
) -> None:
    action = _action()
    ledger = Ledger(tmp_path, profile_ref=action.binding.profile_ref)

    with pytest.raises(AdhelmError) as raised:
        retain_openai_pause_plan(ledger, forged(action))

    assert raised.value.code == "INVALID_EXECUTION_PLAN"
    assert not (tmp_path / "ledger").exists()


def test_retention_binds_auth_profile_account_and_local_only_provenance(tmp_path: Path) -> None:
    action = _action()
    ledger = Ledger(tmp_path, profile_ref=action.binding.profile_ref)

    receipt = retain_openai_pause_plan(ledger, action)
    stored = ledger.records("changes")[-1]

    assert stored["record_kind"] == "EXECUTION_PLAN"
    assert stored["record_digest"] == receipt["record_digest"]
    assert stored["profile_ref"] == action.plan.profile_ref == action.binding.profile_ref
    assert stored["provider"] == "openai_ads"
    assert stored["account_ref"] == action.binding.account_ref
    assert stored["source"] == "LOCAL_CLI"
    assert stored["run_id"] is None
    assert stored["occurred_at"] is None
    assert stored["observed_at"] is None
    assert stored["payload"]["action_digest"] == digest(action)
    assert stored["payload"]["plan_digest"] == action.plan.plan_digest
    assert ledger.verify("changes")["status"] == "VERIFIED"

    loaded, metadata = load_openai_pause_plan(ledger, stored["record_digest"])
    assert loaded == action
    assert metadata["execution_authority"] is False
    assert metadata["requires_current_kernel_checks"] is True
    assert metadata["historical_evidence_status"] == "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE"
    assert metadata["expires_at"] == "2030-01-02T00:00:00Z"


@pytest.mark.parametrize(
    "append_kwargs",
    [
        {"provider": "meta_ads"},
        {"account_ref": "other-account"},
        {"source": "PROVIDER_UI"},
        {"run_id": "run:forbidden"},
        {"occurred_at": "2030-01-01T00:00:00Z"},
        {"observed_at": "2030-01-01T00:00:00Z"},
    ],
)
def test_execution_plan_envelope_mismatch_fails_before_persistence(
    tmp_path: Path, append_kwargs: dict[str, str]
) -> None:
    action = _action()
    ledger = Ledger(tmp_path, profile_ref=action.binding.profile_ref)
    kwargs: dict[str, Any] = {
        "provider": "openai_ads",
        "account_ref": action.binding.account_ref,
        "source": "LOCAL_CLI",
    }
    kwargs.update(append_kwargs)

    with pytest.raises(AdhelmError) as raised:
        ledger.append("changes", "EXECUTION_PLAN", _payload(action), **kwargs)

    assert raised.value.code == "INVALID_EXECUTION_PLAN_PROVENANCE"
    assert not (tmp_path / "ledger").exists()


def test_ledger_profile_mismatch_fails_before_persistence(tmp_path: Path) -> None:
    action = _action()
    ledger = Ledger(tmp_path, profile_ref="different-local-profile")

    with pytest.raises(AdhelmError) as raised:
        retain_openai_pause_plan(ledger, action)

    assert raised.value.code == "INVALID_EXECUTION_PLAN_PROVENANCE"
    assert not (tmp_path / "ledger").exists()


def test_mutating_caller_payload_cannot_leave_a_provenance_inconsistent_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _action(account_ref="local-one", account_client_ref="account_one")
    replacement = _action(
        account_ref="local-two",
        account_client_ref="account_two",
        campaign_client_ref="campaign_two",
    )
    payload = _payload(original)
    replacement_payload = _payload(replacement)
    ledger = Ledger(tmp_path, profile_ref=original.binding.profile_ref)
    original_ensure_dirs = ledger._ensure_dirs

    def mutate_after_preflight(stream: str) -> None:
        payload.clear()
        payload.update(deepcopy(replacement_payload))
        original_ensure_dirs(stream)

    monkeypatch.setattr(ledger, "_ensure_dirs", mutate_after_preflight)

    try:
        stored = ledger.append(
            "changes",
            "EXECUTION_PLAN",
            payload,
            provider="openai_ads",
            account_ref=original.binding.account_ref,
            source="LOCAL_CLI",
        )
    except AdhelmError:
        assert not (tmp_path / "ledger" / "changes").exists()
    else:
        assert stored["account_ref"] == original.binding.account_ref
        assert stored["payload"]["action"]["binding"]["account_ref"] == original.binding.account_ref
        assert ledger.verify("changes")["status"] == "VERIFIED"


def test_verified_checkpoint_rejects_rehashed_action_account_tampering(tmp_path: Path) -> None:
    original = _action(account_ref="local-one", account_client_ref="account_one")
    replacement = _action(
        account_ref="local-two",
        account_client_ref="account_two",
        campaign_client_ref="campaign_two",
    )
    ledger = Ledger(tmp_path, profile_ref=original.binding.profile_ref)
    retain_openai_pause_plan(ledger, original)

    segment = next((tmp_path / "ledger" / "changes").glob("*.jsonl"))
    rows = [json.loads(line) for line in segment.read_text().splitlines()]
    rows[-1]["payload"] = _payload(replacement)
    record = dict(rows[-1])
    record.pop("record_digest")
    rows[-1]["record_digest"] = digest(record)
    segment.write_bytes(
        "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows).encode()
    )

    with pytest.raises(AdhelmError) as raised:
        ledger.checkpoint()

    assert raised.value.code == "INVALID_EXECUTION_PLAN_PROVENANCE"


def test_catalog_is_newest_first_paged_and_never_returns_action_bindings(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path, profile_ref="auth-profile")
    actions = [_action(campaign_client_ref=f"campaign_{index}") for index in range(1, 4)]
    receipts = [retain_openai_pause_plan(ledger, action) for action in actions]

    first = openai_pause_plan_catalog(ledger, cursor=0, limit=2)
    second = openai_pause_plan_catalog(ledger, cursor=2, limit=2)

    assert first["total_count"] == 3
    assert first["next_cursor"] == "2"
    assert [item["record_digest"] for item in first["plans"]] == [
        receipts[2]["record_digest"],
        receipts[1]["record_digest"],
    ]
    assert second["next_cursor"] is None
    assert [item["record_digest"] for item in second["plans"]] == [receipts[0]["record_digest"]]
    rendered = json.dumps({"first": first, "second": second})
    for value in (
        "local-account",
        "auth-profile",
        "campaign_1",
        "campaign_2",
        "campaign_3",
        _CREDENTIAL_BINDING,
    ):
        assert value not in rendered
