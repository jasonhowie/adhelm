from __future__ import annotations

import pytest
from pydantic import ValidationError

from adhelm.errors import AdhelmError
from adhelm.measurement import (
    ConsentContract,
    ConsentState,
    ContainerInventory,
    ConversionSemantics,
    DependencyEdge,
    EdgeStatus,
    EventContract,
    EventField,
    EvidenceItem,
    EvidenceKind,
    InventoryCoverage,
    PublishAuthorization,
    PublishState,
    ReferenceStatus,
    ResourceNode,
    ResourceSurface,
    WorkspaceInventory,
    validate_measurement_evidence,
    validate_publish_gate,
    validate_stage_gate,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def inventory(**updates: object) -> ContainerInventory:
    data: dict[str, object] = {
        "account_ref": "account/1",
        "container_ref": "container/1",
        "live_version_ref": "version/1",
        "latest_version_ref": "version/1",
        "accessible_workspace_refs": ("workspace/customer",),
        "workspaces": (
            WorkspaceInventory(
                workspace_ref="workspace/customer",
                workspace_fingerprint=DIGEST_A,
                accessible=True,
                pending_change_count=0,
            ),
        ),
        "inventoried_context_refs": ("live", "latest", "workspace/customer"),
        "inventory_coverage": (
            InventoryCoverage(
                context_ref="live", surface=ResourceSurface.TAG, inventory_digest=DIGEST_A
            ),
            InventoryCoverage(
                context_ref="live", surface=ResourceSurface.TRIGGER, inventory_digest=DIGEST_A
            ),
            InventoryCoverage(
                context_ref="latest", surface=ResourceSurface.TAG, inventory_digest=DIGEST_A
            ),
            InventoryCoverage(
                context_ref="latest", surface=ResourceSurface.TRIGGER, inventory_digest=DIGEST_A
            ),
            InventoryCoverage(
                context_ref="workspace/customer",
                surface=ResourceSurface.TAG,
                inventory_digest=DIGEST_A,
            ),
            InventoryCoverage(
                context_ref="workspace/customer",
                surface=ResourceSurface.TRIGGER,
                inventory_digest=DIGEST_A,
            ),
        ),
        "applicable_surfaces": (ResourceSurface.TAG, ResourceSurface.TRIGGER),
        "inventoried_surfaces": (ResourceSurface.TAG, ResourceSurface.TRIGGER),
        "nodes": (
            ResourceNode(
                resource_ref="tag/remove",
                surface=ResourceSurface.TAG,
                fingerprint=DIGEST_A,
                context_ref="live",
            ),
        ),
        "edges": (),
        "live_inventory_complete": True,
        "latest_inventory_complete": True,
        "workspace_inventory_complete": True,
        "workspace_limit": 2,
    }
    data.update(updates)
    return ContainerInventory.model_validate(data)


def event() -> EventContract:
    return EventContract(
        event_contract_ref="event/demo-request",
        event_name="demo_request",
        semantics=ConversionSemantics(
            semantics_ref="semantics/demo",
            customer_definition="A visitor explicitly requests a sales demonstration.",
            counting_rule="One successful request per browser action.",
        ),
        fields=(EventField(name="form_state", value_type="LOW_CARDINALITY_STRING"),),
    )


def consent() -> ConsentContract:
    return ConsentContract(
        consent_contract_ref="consent/public-marketing",
        allowed_public_hosts=("marketing.example.test",),
        allowed_public_paths=("/demo",),
        approved_outbound_domains=("collect.example.test",),
        tags_allowed_when_granted=("tag/ga4",),
    )


def evidence(
    kind: EvidenceKind, *, consent_state: ConsentState | None = None, count: int | None = None
) -> EvidenceItem:
    return EvidenceItem(
        kind=kind,
        evidence_digest=DIGEST_A,
        event_contract_ref=event().event_contract_ref,
        consent_contract_ref=consent().consent_contract_ref,
        consent_state=consent_state,
        occurrence_count=count,
        outbound_domains=("collect.example.test",) if consent_state is ConsentState.GRANTED else (),
        request_field_names=("form_state",) if consent_state is ConsentState.GRANTED else (),
        browser_host="marketing.example.test" if consent_state is not None else None,
        browser_path="/demo" if consent_state is not None else None,
    )


def complete_evidence() -> tuple[EvidenceItem, ...]:
    return (
        evidence(EvidenceKind.CONFIGURATION),
        evidence(EvidenceKind.QUICK_PREVIEW),
        evidence(EvidenceKind.BROWSER_NETWORK, consent_state=ConsentState.GRANTED, count=1),
        evidence(EvidenceKind.BROWSER_NETWORK, consent_state=ConsentState.DENIED, count=0),
        evidence(EvidenceKind.NATIVE_RECEIPT),
        evidence(EvidenceKind.PROCESSED_REPORT),
        evidence(EvidenceKind.OPTIMIZATION_ASSOCIATION),
    )


def test_only_complete_unknown_free_inventory_proves_unreferenced() -> None:
    assert inventory().reference_status("tag/remove") is ReferenceStatus.PROVEN_UNREFERENCED
    dynamic = DependencyEdge(
        source_ref="tag/custom", status=EdgeStatus.DYNAMIC_OR_UNKNOWN, context_ref="live"
    )
    assert (
        inventory(edges=(dynamic,)).reference_status("tag/remove")
        is ReferenceStatus.DYNAMIC_OR_UNKNOWN
    )
    broken = DependencyEdge(
        source_ref="tag/a",
        target_ref="variable/missing",
        status=EdgeStatus.BROKEN,
        context_ref="latest",
    )
    assert inventory(edges=(broken,)).reference_status("tag/remove") is ReferenceStatus.BROKEN
    assert (
        inventory(workspace_inventory_complete=False).reference_status("tag/remove")
        is ReferenceStatus.INVENTORY_INCOMPLETE
    )
    assert (
        inventory(inventory_coverage=()).reference_status("tag/remove")
        is ReferenceStatus.INVENTORY_INCOMPLETE
    )


def test_stage_requires_all_workspaces_clean_versions_and_a_new_slot() -> None:
    assert validate_stage_gate(inventory()).existing_workspace_reuse_allowed is False
    with pytest.raises(AdhelmError, match="Live and latest"):
        validate_stage_gate(inventory(latest_version_ref="version/2"))
    with pytest.raises(AdhelmError, match="slot"):
        validate_stage_gate(inventory(workspace_limit=1))
    with pytest.raises(AdhelmError, match="Complete"):
        validate_stage_gate(inventory(inventoried_surfaces=(ResourceSurface.TAG,)))


def test_publish_requires_same_browser_tested_immutable_version_and_publish_scope() -> None:
    authorization = PublishAuthorization(
        publish_scope_granted=True,
        immutable_version_digest=DIGEST_B,
        base_live_version_ref="version/1",
        event_contract_ref=event().event_contract_ref,
        consent_contract_ref=consent().consent_contract_ref,
        browser_host="marketing.example.test",
        browser_path="/demo",
    )
    state = PublishState(
        live_version_ref="version/1",
        latest_version_ref="version/2",
        created_version_ref="version/2",
        created_version_digest=DIGEST_B,
        browser_tested_digest=DIGEST_B,
    )
    validate_publish_gate(
        authorization=authorization,
        state=state,
        event=event(),
        consent=consent(),
        evidence=complete_evidence(),
    )
    with pytest.raises(AdhelmError, match="browser-tested"):
        validate_publish_gate(
            authorization=authorization,
            state=state.model_copy(update={"browser_tested_digest": DIGEST_A}),
            event=event(),
            consent=consent(),
            evidence=complete_evidence(),
        )
    with pytest.raises(AdhelmError, match="publish authority"):
        validate_publish_gate(
            authorization=authorization.model_copy(update={"publish_scope_granted": False}),
            state=state,
            event=event(),
            consent=consent(),
            evidence=complete_evidence(),
        )


def test_quick_preview_never_satisfies_runtime_evidence() -> None:
    with pytest.raises(AdhelmError, match="missing"):
        validate_measurement_evidence(
            event=event(), consent=consent(), evidence=(evidence(EvidenceKind.QUICK_PREVIEW),)
        )


def test_denied_consent_rejects_event_and_unapproved_egress() -> None:
    denied_event = evidence(
        EvidenceKind.BROWSER_NETWORK, consent_state=ConsentState.DENIED, count=1
    )
    all_evidence = complete_evidence()
    with pytest.raises(AdhelmError, match="denied-consent"):
        validate_measurement_evidence(
            event=event(),
            consent=consent(),
            evidence=(all_evidence[0], denied_event, *all_evidence[3:]),
        )
    denied_egress = EvidenceItem(
        kind=EvidenceKind.BROWSER_NETWORK,
        evidence_digest=DIGEST_A,
        event_contract_ref=event().event_contract_ref,
        consent_contract_ref=consent().consent_contract_ref,
        consent_state=ConsentState.DENIED,
        occurrence_count=0,
        outbound_domains=("collect.example.test",),
        browser_host="marketing.example.test",
        browser_path="/demo",
    )
    with pytest.raises(AdhelmError, match="egress"):
        validate_measurement_evidence(
            event=event(),
            consent=consent(),
            evidence=(all_evidence[0], denied_egress, *all_evidence[3:]),
        )


def test_customer_semantics_and_strict_fields_are_required() -> None:
    with pytest.raises(ValidationError, match="sensitive"):
        EventField(name="email", value_type="LOW_CARDINALITY_STRING")
    with pytest.raises(ValidationError):
        EventContract(
            event_contract_ref="event/unsafe",
            event_name="unsafe_event",
            semantics=ConversionSemantics(
                semantics_ref="s", customer_definition="", counting_rule="once"
            ),
        )
    undeclared = evidence(
        EvidenceKind.BROWSER_NETWORK, consent_state=ConsentState.GRANTED, count=1
    ).model_copy(update={"request_field_names": ("new_field",)})
    all_evidence = complete_evidence()
    with pytest.raises(AdhelmError, match="undeclared"):
        validate_measurement_evidence(
            event=event(),
            consent=consent(),
            evidence=(all_evidence[0], undeclared, *all_evidence[3:]),
        )
