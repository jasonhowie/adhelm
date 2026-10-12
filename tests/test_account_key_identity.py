"""Account-key evidence cannot impersonate OAuth scopes or survive credential drift."""

from pathlib import Path

import pytest
from pydantic import ValidationError
from test_safety import (
    EVIDENCE_REF,
    FakeAdapter,
    FakeApprovalHost,
    direct_grant,
    kernel,
    plan,
)

from adhelm.egress_permit import EgressPermit
from adhelm.errors import AdhelmError
from adhelm.safety import (
    AdapterCapabilities,
    ApplyOutcome,
    CompositePlan,
    EffectStatus,
    OpenAIAccountKeyIdentity,
    OperationKind,
    Reconciliation,
    RemoteIdentity,
    RemoteOpenAIAccountKeyIdentity,
    RemoteState,
    SubPlan,
)

pytestmark = pytest.mark.usefixtures("simulate_released_writes")

_BINDING = "credential-binding:sha256:" + "c" * 64


def _account() -> OpenAIAccountKeyIdentity:
    return OpenAIAccountKeyIdentity(
        account_ref="alias-account",
        stable_account_id="stable-account",
        auth_profile_ref="edit-profile",
        credential_binding_ref=_BINDING,
    )


def _plan() -> CompositePlan:
    old = plan(operation=OperationKind.PAUSE, spend=0)
    child = SubPlan.model_validate(
        {
            **old.subplans[0].model_dump(mode="python"),
            "provider": "openai_ads",
            "subplan_id": "openai-pause",
            "account": _account(),
        }
    )
    return CompositePlan.model_validate({**old.model_dump(mode="python"), "subplans": (child,)})


class AccountKeyAdapter(FakeAdapter):
    provider = "openai_ads"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.identity = RemoteOpenAIAccountKeyIdentity(
            **_account().model_dump(mode="python", exclude={"account_ref"})
        )
        self.identity_reads = 0
        self.rotate_after_first_read = False
        self.reconcile_calls = 0
        self.last_permit = None

    def capabilities(self, plan: SubPlan) -> AdapterCapabilities:
        return AdapterCapabilities(
            provider_api_version="v1",
            capability_version="cap-v1",
            supported_operations=(OperationKind.PAUSE,),
        )

    def read_identity(self, account_ref: str) -> RemoteOpenAIAccountKeyIdentity:
        self.identity_reads += 1
        if self.rotate_after_first_read and self.identity_reads > 1:
            return self.identity.model_copy(
                update={"credential_binding_ref": "credential-binding:sha256:" + "d" * 64}
            )
        return self.identity

    def apply_guarded(self, plan: SubPlan, *, idempotency_key: str, permit: EgressPermit):
        self.last_permit = permit
        permit.consume(
            plan_digest=_plan().plan_digest,
            subplan_digest=plan.subplan_digest,
            attempt_ref=idempotency_key,
        )
        return super().apply(plan, idempotency_key=idempotency_key)

    def reconcile(self, plan: SubPlan) -> Reconciliation:
        self.reconcile_calls += 1
        return Reconciliation(
            state=RemoteState(fingerprint=plan.before.fingerprint, revision=plan.before.revision),
            status=EffectStatus.FAILED_NO_EFFECT,
            settled=True,
            evidence_refs=(EVIDENCE_REF,),
        )


def test_existing_scoped_identity_serialization_and_plan_digests_are_unchanged() -> None:
    legacy = plan()
    assert legacy.plan_digest == "ff6925c636448c38f4190c76a75dd85967ea90b2cea5c3b07ca00d242a6a4120"
    assert (
        legacy.subplans[0].subplan_digest
        == "6d6e1f66dc1d71632bcf8628d2c2ae4da7aae4079ecfccc5a89d3709d3792fd0"
    )
    assert set(legacy.subplans[0].account.model_dump()) == {
        "account_ref",
        "stable_account_id",
        "auth_profile_ref",
        "principal_ref",
        "client_application_ref",
        "scopes",
    }


def test_key_identity_is_a_distinct_closed_roundtripping_variant() -> None:
    compiled = _plan()
    restored = CompositePlan.model_validate_json(compiled.model_dump_json())
    assert restored == compiled
    account = restored.subplans[0].account
    assert isinstance(account, OpenAIAccountKeyIdentity)
    assert account.provider_scope_status == "NOT_DOCUMENTED"
    assert not {"scopes", "principal_ref", "client_application_ref"} & account.model_dump().keys()
    rotated = account.model_copy(
        update={"credential_binding_ref": "credential-binding:sha256:" + "d" * 64}
    )
    changed_child = compiled.subplans[0].model_copy(update={"account": rotated})
    assert changed_child.subplan_digest != compiled.subplans[0].subplan_digest


@pytest.mark.parametrize(
    "extra",
    [
        {"scopes": ()},
        {"scopes": ("ads.write",)},
        {"principal_ref": "invented-principal"},
        {"client_application_ref": "invented-app"},
        {"provider_scope_status": "VERIFIED"},
        {"credential_binding_ref": "raw-token-must-not-be-accepted"},
    ],
)
def test_key_identity_does_not_accept_scoped_claims_or_unbounded_credentials(extra) -> None:
    with pytest.raises(ValidationError):
        OpenAIAccountKeyIdentity.model_validate({**_account().model_dump(), **extra})


def test_key_identity_cannot_be_used_for_another_provider() -> None:
    child = _plan().subplans[0]
    with pytest.raises(ValidationError):
        SubPlan.model_validate({**child.model_dump(mode="python"), "provider": "google_ads"})


def test_key_identity_still_requires_durable_one_use_authority_and_start(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    adapter = AccountKeyAdapter()
    adapter.ledger = ledger
    with pytest.raises(AdhelmError) as error:
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert error.value.code == "USER_APPROVAL_HOST_REQUIRED"
    assert adapter.apply_calls == adapter.identity_reads == 0
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    result = instance.execute(compiled, grant, {adapter.provider: adapter})
    assert result.results[0].status == EffectStatus.APPLIED_VERIFIED
    assert adapter.apply_calls == 1
    assert adapter.identity_reads >= 2
    with pytest.raises(AdhelmError):
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert adapter.apply_calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("stable_account_id", "other-account"),
        ("auth_profile_ref", "other-profile"),
        ("credential_binding_ref", "credential-binding:sha256:" + "d" * 64),
    ],
)
def test_changed_account_profile_or_key_rejects_before_start(
    tmp_path: Path, field: str, value: str
) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = AccountKeyAdapter()
    adapter.ledger = ledger
    adapter.identity = adapter.identity.model_copy(update={field: value})
    with pytest.raises(AdhelmError) as error:
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert error.value.code == "ACCOUNT_IDENTITY_DRIFT"
    assert adapter.apply_calls == 0
    assert ledger.unresolved_starts() == []


def test_rotation_between_preflight_and_immediate_reread_prevents_egress(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = AccountKeyAdapter()
    adapter.ledger = ledger
    adapter.rotate_after_first_read = True
    with pytest.raises(AdhelmError) as error:
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert error.value.code == "ACCOUNT_IDENTITY_DRIFT"
    assert adapter.apply_calls == 0
    assert ledger.unresolved_starts() == []


def test_scope_identity_cannot_satisfy_a_key_identity(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = AccountKeyAdapter()
    adapter.ledger = ledger
    adapter.identity = RemoteIdentity(
        stable_account_id="stable-account",
        auth_profile_ref="edit-profile",
        principal_ref="principal-1",
        client_application_ref="client-1",
        scopes=("ads.write",),
    )
    with pytest.raises(AdhelmError) as error:
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert error.value.code == "ACCOUNT_IDENTITY_DRIFT"
    assert adapter.apply_calls == 0


def test_model_construct_cannot_forge_remote_scope_evidence(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = AccountKeyAdapter()
    adapter.ledger = ledger
    adapter.identity = adapter.identity.model_copy(update={"provider_scope_status": "VERIFIED"})
    with pytest.raises(AdhelmError) as error:
        instance.execute(compiled, grant, {adapter.provider: adapter})
    assert error.value.code == "INVALID_SAFETY_INPUT"
    assert adapter.apply_calls == 0


def test_reconciliation_rejects_a_changed_key_without_claiming_settlement(tmp_path: Path) -> None:
    instance, ledger = kernel(tmp_path)
    compiled = _plan()
    grant = direct_grant(compiled)
    instance.record_direct_grant(compiled, grant, FakeApprovalHost())
    adapter = AccountKeyAdapter(apply_outcome=ApplyOutcome.AMBIGUOUS)
    adapter.ledger = ledger
    result = instance.execute(compiled, grant, {adapter.provider: adapter})
    assert result.results[0].status == EffectStatus.AMBIGUOUS_REMOTE_STATE
    adapter.identity = adapter.identity.model_copy(
        update={"credential_binding_ref": "credential-binding:sha256:" + "d" * 64}
    )
    reconciled = instance.reconcile_run(compiled, grant, {adapter.provider: adapter})
    assert reconciled.results[0].status == EffectStatus.AMBIGUOUS_REMOTE_STATE
    assert adapter.reconcile_calls == 0
    assert ledger.unresolved_starts()
