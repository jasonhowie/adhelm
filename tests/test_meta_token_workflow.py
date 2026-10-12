"""Configured handoff regressions for the fixed Meta token inspector."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from adhelm import config as config_module
from adhelm import meta_token_credentials as token_credentials
from adhelm import meta_token_workflow as workflow
from adhelm.config import load_configuration, save_configuration
from adhelm.errors import AdhelmError
from adhelm.meta_token_credentials import enroll_meta_token_profile
from adhelm.meta_token_inspection import MetaTokenInspectionCredentials, MetaTokenObservation
from adhelm.meta_token_profiles import MetaAccountTokenProfileDraft
from adhelm.models import Configuration, Profile


@dataclass
class _MemoryStore:
    values: dict[tuple[str, str], str] = field(default_factory=dict)

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.values.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.values[(service_name, username)] = password


def _draft() -> MetaAccountTokenProfileDraft:
    return MetaAccountTokenProfileDraft.model_validate(
        {
            "profile_id": "meta-read",
            "account_ref": "12345",
            "client_application_ref": "67890",
            "currency": "USD",
            "timezone": "UTC",
            "purpose": "READ",
            "inspector_credential_kind": "CUSTOMER_APP_ACCESS_TOKEN",
        }
    )


def _install_fake_store(monkeypatch: pytest.MonkeyPatch, store: _MemoryStore) -> None:
    monkeypatch.setattr(token_credentials, "_credential_backend", lambda: store)
    monkeypatch.setattr(
        token_credentials, "credential_mutation_lock", lambda _reference: nullcontext()
    )
    monkeypatch.setattr(config_module, "credential_mutation_lock", lambda _reference: nullcontext())


def _configured_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    store = _MemoryStore()
    _install_fake_store(monkeypatch, store)
    enroll_meta_token_profile(
        tmp_path,
        _draft(),
        subject_token_supplier=lambda: "subject-token",
        inspector_token_supplier=lambda: "inspector-token",
    )
    return tmp_path


def test_configured_workflow_passes_only_fixed_bound_credentials_to_inspector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _configured_root(tmp_path, monkeypatch)
    observed: list[MetaTokenInspectionCredentials] = []
    expected = object()

    def fake_inspect(**kwargs: object) -> object:
        supplier = kwargs["credential_supplier"]
        assert callable(supplier)
        first = supplier()
        second = supplier()
        assert isinstance(first, MetaTokenInspectionCredentials)
        assert isinstance(second, MetaTokenInspectionCredentials)
        observed.extend((first, second))
        assert kwargs["expected_client_application_ref"] == "67890"
        assert kwargs["required_scope"] == "ads_read"
        return expected

    monkeypatch.setattr(workflow, "inspect_meta_token", fake_inspect)
    assert workflow.inspect_configured_meta_token(root, "meta-read") is expected
    assert observed[0] == observed[1]
    assert observed[0].profile_ref == "meta-read"
    assert observed[0].subject_token == "subject-token"
    assert observed[0].inspector_token == "inspector-token"
    assert len(observed[0].binding_key) == 32


def test_changed_configuration_blocks_second_supplier_before_a_fake_http_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _configured_root(tmp_path, monkeypatch)
    calls = 0

    def fake_inspect(**kwargs: object) -> MetaTokenObservation:
        nonlocal calls
        supplier = kwargs["credential_supplier"]
        assert callable(supplier)
        supplier()
        calls += 1
        current = load_configuration(root)
        updated = current.model_copy(
            update={"output": current.output.model_copy(update={"max_output_bytes": 4096})}
        )
        save_configuration(root, updated, replace=True)
        supplier()
        pytest.fail("the changed configuration must stop before a fake provider response")

    monkeypatch.setattr(workflow, "inspect_meta_token", fake_inspect)
    with pytest.raises(AdhelmError) as failure:
        workflow.inspect_configured_meta_token(root, "meta-read")
    assert calls == 1
    assert failure.value.code == "CONFIGURATION_CHANGED"


def test_non_meta_profile_cannot_use_the_dedicated_inspection_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_store(monkeypatch, _MemoryStore())
    generic = Profile(
        profile_id="meta-read",
        provider="meta_ads",
        account_ref="12345",
        principal_ref="customer-declared-principal",
        client_ref="67890",
        scopes=["ads_read"],
        credential_ref="generic-meta-secret",
    )
    save_configuration(tmp_path, Configuration(profiles=[generic]), replace=True)
    # The handoff requires the exact configured Meta profile rather than a
    # generic Meta credential with caller-supplied principal/scope claims.
    with pytest.raises(AdhelmError) as absent:
        workflow.inspect_configured_meta_token(tmp_path, generic.profile_id)
    assert absent.value.code == "META_TOKEN_PROFILE_REQUIRED"
