"""GTM dependency graph, `PROVEN_UNREFERENCED` classification, and read-only audit (M01.2).

Every fixture here is built from the M01.1 inventory helpers in
`tests/test_gtm_inventory.py`, so the graph is always computed from an
observation the real reader could have produced.  `tests/conftest.py` blocks the
network for all of them, and no test appends a ledger record except through the
CLI's own `measurement gtm` read.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_gtm_inventory import (
    _SENTINEL,
    _default_workspace_items,
    _Fixture,
    _install_workflow_fixture,
    _read,
    _resource,
    _tag,
)
from typer.testing import CliRunner

from adhelm import measurement_cli
from adhelm.audits import COMPILABLE_OPERATION_KINDS
from adhelm.audits.gtm_web import (
    RULE_TABLE_VERSION,
    GtmWebAuditSections,
    evaluate_gtm_web_audit,
)
from adhelm.canonical import digest
from adhelm.cli import app
from adhelm.errors import AdhelmError
from adhelm.gtm_dependency_graph import (
    GtmDependencyGraph,
    build_gtm_dependency_graph,
)
from adhelm.gtm_inventory import GtmFullInventory
from adhelm.ledger import Ledger
from adhelm.measurement import (
    ReferenceStatus,
    container_inventory_from_gtm_inventory,
    validate_gtm_stage_gate,
    validate_stage_gate,
)
from adhelm.measurement_records import GtmFullInventoryMeasurementSnapshot
from adhelm.privacy import validate_data
from adhelm.saved_measurements import load_measurement

# --------------------------------------------------------------- fixtures


def _clean_items() -> dict[str, list[dict[str, Any]]]:
    """A workspace whose every documented reference resolves.

    The M01.1 default fixture deliberately contains a dangling teardown tag and
    a custom-code tag, which is the right default for a reader test but makes
    every proof of absence blocked.  This variant is the positive case.
    """

    return {
        "tags": [
            _tag(
                "11",
                firingTriggerId=["20"],
                blockingTriggerId=["21"],
                parentFolderId="30",
                consentSettings={"consentStatus": "notNeeded"},
                parameter=[{"type": "template", "key": "eventName", "value": "purchase"}],
            )
        ],
        "triggers": [
            _resource("trigger", "triggers", "20", type="pageview"),
            _resource("trigger", "triggers", "21", type="click"),
        ],
        "variables": [_resource("variable", "variables", "41", type="v")],
        "folders": [_resource("folder", "folders", "30")],
        "built_in_variables": [],
        "templates": [],
        "zones": [],
        "clients": [],
        "transformations": [],
        "gtag_config": [],
    }


def _built_in_items() -> dict[str, list[dict[str, Any]]]:
    """A clean workspace holding one built-in variable nothing references."""

    items = _clean_items()
    items["built_in_variables"] = [
        {
            "path": "accounts/1/containers/2/workspaces/3/built_in_variables",
            "accountId": "1",
            "containerId": "2",
            "workspaceId": "3",
            "type": "clickUrl",
            "name": "Click URL",
        }
    ]
    return items


def _clean_inventory() -> GtmFullInventory:
    return _read(_Fixture(items=_clean_items()))


def _audit(inventory: GtmFullInventory, graph: GtmDependencyGraph) -> GtmWebAuditSections:
    return evaluate_gtm_web_audit(
        inventory=inventory,
        graph=graph,
        source_record_digest="a" * 64,
        snapshot_digest="b" * 64,
    )


def _rules(sections: GtmWebAuditSections) -> dict[str, int]:
    return dict(sections.finding_counts_by_rule)


def _node(graph: GtmDependencyGraph, suffix: str) -> Any:
    matches = [node for node in graph.nodes if node.resource_ref.endswith(suffix)]
    assert len(matches) == 1, (suffix, [node.resource_ref for node in graph.nodes])
    return matches[0]


# ------------------------------------------------------- graph construction


def test_graph_covers_every_scope_and_preserves_every_edge_resolution() -> None:
    inventory = _read(_Fixture(workspaces=["3", "4"]))
    graph = build_gtm_dependency_graph(inventory)

    assert graph.capability_version == "gtm-dependency-graph-1"
    assert graph.inventory_digest == digest(inventory)
    assert graph.scope_composition == "SCOPES_KEPT_DISTINCT"
    assert graph.live_provider_reads == "NONE_RETAINED_OBSERVATION_ONLY"
    assert graph.deletion_authority == "NEVER_ESTABLISHED_BY_A_READ_ONLY_GRAPH"
    assert graph.union is None

    # One graph node per retained resource, one graph edge per retained edge.
    assert graph.counts.nodes == len(inventory.resources)
    assert graph.counts.edges == len(inventory.edges)
    assert {node.resource_ref for node in graph.nodes} == {
        resource.resource_ref for resource in inventory.resources
    }
    assert sorted(edge.resolution for edge in graph.edges) == sorted(
        edge.resolution for edge in inventory.edges
    )
    # Scopes stay distinct: a workspace node never appears in a version scope.
    assert {scope.scope_ref for scope in graph.scopes} == {
        item.scope_ref for item in inventory.coverage
    }
    for scope in graph.scopes:
        assert scope.node_count == sum(node.scope_ref == scope.scope_ref for node in graph.nodes)
        assert scope.edge_count == sum(edge.scope_ref == scope.scope_ref for edge in graph.edges)


def test_graph_classifies_every_documented_relation() -> None:
    items = _default_workspace_items()
    items["triggers"].append(_resource("trigger", "triggers", "22", type="custom"))
    items["zones"][0]["boundary"]["customEvaluationTriggerId"] = ["20"]
    inventory = _read(_Fixture(items=items))
    graph = build_gtm_dependency_graph(inventory)

    relations = {edge.relation for edge in graph.edges}
    assert {
        "FIRING",
        "BLOCKING",
        "SEQUENCING_SETUP",
        "SEQUENCING_TEARDOWN",
        "FOLDER_MEMBERSHIP",
        "ZONE_BOUNDARY",
        "VARIABLE_REFERENCE",
        "PARAMETER_REFERENCE",
    } <= relations
    # A zone child container is out of scope, never an unresolved gap.
    child = [edge for edge in graph.edges if edge.edge_kind == "ZONE_CHILD_CONTAINER"]
    assert child and all(
        edge.resolution == "OUT_OF_SCOPE_CHILD_CONTAINER" and not edge.disables_scope_proof
        for edge in child
    )


def test_inbound_index_names_every_referrer_of_a_node() -> None:
    inventory = _read(_Fixture(items=_clean_items()))
    graph = build_gtm_dependency_graph(inventory)

    trigger = _node(graph, "/triggers/20")
    assert trigger.inbound_edge_count == 1
    assert trigger.inbound_relations == ("FIRING",)
    assert trigger.inbound_source_refs == ("gtm.workspace.3/tags/11",)
    assert trigger.inbound_source_ref_status == "COMPLETE"
    assert [edge.source_ref for edge in graph.inbound_edges(trigger.resource_ref)] == [
        "gtm.workspace.3/tags/11"
    ]
    assert graph.scope("gtm.workspace.3") is not None
    assert graph.scope("gtm.workspace.absent") is None


# --------------------------------------------------------- classification


def test_a_complete_scope_proves_an_unreferenced_resource() -> None:
    inventory = _clean_inventory()
    graph = build_gtm_dependency_graph(inventory)

    assert graph.container_blocking_reasons == ()
    assert inventory.completeness.status == "COMPLETE_WITHIN_DECLARED_BOUNDS"
    workspace = graph.scope("gtm.workspace.3")
    assert workspace is not None and workspace.proof_status == "PROOF_AVAILABLE"

    # The variable nothing references is proven unreferenced; the trigger a tag
    # fires is referenced.
    variable = _node(graph, "/variables/41")
    assert variable.reference_status == "PROVEN_UNREFERENCED"
    assert variable.blocking_reasons == ()
    assert variable.deletion_authority == "NEVER_ESTABLISHED_BY_A_READ_ONLY_GRAPH"
    assert _node(graph, "/triggers/20").reference_status == "REFERENCED"
    assert _node(graph, "/folders/30").reference_status == "REFERENCED"
    assert graph.counts.proven_unreferenced == workspace.proven_unreferenced_count


def test_age_name_paused_and_folder_are_not_classification_inputs() -> None:
    """AGENTS rule 11: apparent inactivity is never deletion proof, either way."""

    items = _clean_items()
    items["variables"] = [
        _resource(
            "variable",
            "variables",
            "41",
            type="v",
            name="OLD legacy 2019 unused",
            paused=True,
            parentFolderId="30",
        )
    ]
    aged = build_gtm_dependency_graph(_read(_Fixture(items=items)))
    plain = build_gtm_dependency_graph(_clean_inventory())

    aged_node = _node(aged, "/variables/41")
    plain_node = _node(plain, "/variables/41")
    # A resource that looks old, is paused, is renamed and sits in a folder gets
    # exactly the classification the plain one gets.  Nothing about its name,
    # age or state moved it.
    assert aged_node.reference_status == plain_node.reference_status == "PROVEN_UNREFERENCED"
    assert aged_node.blocking_reasons == plain_node.blocking_reasons == ()

    # And a paused *tag* that other resources reference stays REFERENCED: pause
    # cannot make a referenced resource look removable either.
    paused_items = _clean_items()
    paused_items["tags"] = [
        _tag("11", firingTriggerId=["20"], paused=True, parameter=[]),
        _tag(
            "12",
            firingTriggerId=["21"],
            setupTag=[{"tagName": "Tag 11", "stopOnSetupFailure": True}],
            parameter=[],
        ),
    ]
    paused = build_gtm_dependency_graph(_read(_Fixture(items=paused_items)))
    assert _node(paused, "/tags/11").paused is True
    assert _node(paused, "/tags/11").reference_status == "REFERENCED"


def test_a_built_in_variable_is_never_proven_unreferenced() -> None:
    graph = build_gtm_dependency_graph(_read(_Fixture(items=_built_in_items())))
    node = _node(graph, "/built_in_variables/clickUrl")
    assert node.reference_status == "UNPROVABLE_BUILT_IN_RESOURCE"
    assert node.blocking_reasons == ("BUILT_IN_RESOURCE",)
    assert node.inbound_edge_count == 0


def test_a_template_with_an_unbound_instance_is_never_proven_unreferenced() -> None:
    items = _clean_items()
    items["templates"] = [_resource("template", "templates", "50")]
    # `cvt_2_99` names a template identifier this read never saw, so the
    # instance could belong to any custom template in the scope.
    items["tags"].append(_tag("13", type="cvt_2_99", firingTriggerId=["20"], parameter=[]))
    graph = build_gtm_dependency_graph(_read(_Fixture(items=items)))

    node = _node(graph, "/templates/50")
    assert node.reference_status == "UNPROVABLE_TEMPLATE_WITH_UNBOUND_INSTANCES"
    assert node.blocking_reasons[0] == "TEMPLATE_WITH_UNBOUND_INSTANCES"
    # The unresolved instance edge blocks the rest of the scope as well.
    assert "SCOPE_UNRESOLVED_EDGE_PRESENT" in node.blocking_reasons


def test_a_bound_template_instance_makes_its_template_referenced() -> None:
    items = _clean_items()
    items["templates"] = [_resource("template", "templates", "50")]
    items["tags"].append(_tag("13", type="cvt_2_50", firingTriggerId=["20"], parameter=[]))
    graph = build_gtm_dependency_graph(_read(_Fixture(items=items)))

    node = _node(graph, "/templates/50")
    assert node.reference_status == "REFERENCED"
    assert node.inbound_relations == ("TEMPLATE_INSTANCE",)


def test_one_unresolved_edge_blocks_the_whole_scope() -> None:
    items = _clean_items()
    items["tags"][0]["teardownTag"] = [
        {"tagName": "Missing Teardown", "stopTeardownOnFailure": False}
    ]
    graph = build_gtm_dependency_graph(_read(_Fixture(items=items)))

    scope = graph.scope("gtm.workspace.3")
    assert scope is not None and scope.proof_status == "PROOF_BLOCKED"
    assert graph.counts.proven_unreferenced == 0
    node = _node(graph, "/variables/41")
    assert node.reference_status == "UNPROVABLE_SCOPE_UNRESOLVED_EDGE_PRESENT"
    assert node.blocking_reasons == (
        "SCOPE_UNRESOLVED_EDGE_PRESENT",
        "CONTAINER_UNRESOLVED_EDGE_PRESENT",
    )


def test_an_ambiguous_name_reference_blocks_the_scope() -> None:
    items = _clean_items()
    items["tags"] = [
        _tag("11", firingTriggerId=["20"], parameter=[]),
        _tag("12", name="Tag 11", firingTriggerId=["21"], parameter=[]),
        _tag(
            "13",
            firingTriggerId=["20"],
            setupTag=[{"tagName": "Tag 11", "stopOnSetupFailure": True}],
            parameter=[],
        ),
    ]
    inventory = _read(_Fixture(items=items))
    graph = build_gtm_dependency_graph(inventory)

    assert any(edge.resolution == "UNRESOLVED_AMBIGUOUS_NAME" for edge in graph.edges)
    assert graph.counts.proven_unreferenced == 0
    assert _rules(_audit(inventory, graph))["GTM_AMBIGUOUS_NAME_REFERENCE"] == 1
    assert _rules(_audit(inventory, graph))["GTM_DUPLICATE_RESOURCE_NAME"] == 1


def test_an_incomplete_collection_blocks_every_scope() -> None:
    inventory = _read(_Fixture(items=_clean_items(), endless={"variables"}))
    graph = build_gtm_dependency_graph(inventory)

    assert "CONTAINER_COVERAGE_INCOMPLETE" in graph.container_blocking_reasons
    assert graph.counts.proven_unreferenced == 0
    assert all(
        node.reference_status.startswith("UNPROVABLE_") or node.reference_status == "REFERENCED"
        for node in graph.nodes
    )
    workspace = graph.scope("gtm.workspace.3")
    assert workspace is not None and "variables" in workspace.incomplete_collections


def test_an_uninspected_workspace_blocks_every_scope() -> None:
    inventory = _read(_Fixture(items=_clean_items(), workspaces=[str(i) for i in range(3, 32)]))
    graph = build_gtm_dependency_graph(inventory)

    assert inventory.completeness.uninspected_workspaces > 0
    assert "UNINSPECTED_WORKSPACE_PRESENT" in graph.container_blocking_reasons
    assert graph.counts.proven_unreferenced == 0


def test_a_bounded_reference_set_blocks_every_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    import adhelm.gtm_inventory as module

    monkeypatch.setattr(module, "_MAX_EDGES", 0)
    inventory = _read(_Fixture(items=_clean_items()))
    assert inventory.reference_coverage == "BOUNDED_INCOMPLETE"

    graph = build_gtm_dependency_graph(inventory)
    assert "REFERENCE_COVERAGE_BOUNDED_INCOMPLETE" in graph.container_blocking_reasons
    assert graph.counts.proven_unreferenced == 0


def test_an_undeterminable_live_latest_identity_blocks_every_scope() -> None:
    # Same version identifier, different embedded bodies: one identity says the
    # versions are equal and the other says they differ, so the reader refuses
    # to guess and the graph refuses to prove anything.
    inventory = _read(_Fixture(items=_clean_items(), live_version="8", latest_version="8"))
    diverged = inventory.model_copy(
        update={
            "live_latest": inventory.live_latest.model_copy(update={"status": "UNDETERMINABLE"})
        }
    )
    graph = build_gtm_dependency_graph(diverged)
    assert graph.container_blocking_reasons == ("LIVE_LATEST_UNDETERMINABLE",)
    assert graph.counts.proven_unreferenced == 0
    node = _node(graph, "/variables/41")
    assert node.reference_status == "UNPROVABLE_LIVE_LATEST_UNDETERMINABLE"


def test_every_classification_branch_is_reachable() -> None:
    """One assertion that the vocabulary is exercised, not merely declared."""

    observed: set[str] = set()
    for graph in (
        build_gtm_dependency_graph(_clean_inventory()),
        build_gtm_dependency_graph(_read(_Fixture())),
        build_gtm_dependency_graph(_read(_Fixture(items=_clean_items(), endless={"variables"}))),
        build_gtm_dependency_graph(_read(_Fixture(items=_built_in_items()))),
    ):
        observed.update(node.reference_status for node in graph.nodes)
    assert {
        "PROVEN_UNREFERENCED",
        "REFERENCED",
        "UNPROVABLE_BUILT_IN_RESOURCE",
        "UNPROVABLE_SCOPE_UNRESOLVED_EDGE_PRESENT",
        "UNPROVABLE_SCOPE_COVERAGE_INCOMPLETE",
    } <= observed


def test_sequencing_cycles_are_found_inside_one_scope() -> None:
    items = _clean_items()
    items["tags"] = [
        _tag(
            "11",
            firingTriggerId=["20"],
            setupTag=[{"tagName": "Tag 12", "stopOnSetupFailure": True}],
            parameter=[],
        ),
        _tag(
            "12",
            firingTriggerId=["21"],
            setupTag=[{"tagName": "Tag 11", "stopOnSetupFailure": True}],
            parameter=[],
        ),
    ]
    inventory = _read(_Fixture(items=items))
    graph = build_gtm_dependency_graph(inventory)

    scope = graph.scope("gtm.workspace.3")
    assert scope is not None
    assert len(scope.sequencing_cycles) == 1
    cycle = scope.sequencing_cycles[0]
    assert cycle.member_refs == ("gtm.workspace.3/tags/11", "gtm.workspace.3/tags/12")
    assert cycle.member_ref_status == "COMPLETE"
    assert _rules(_audit(inventory, graph))["GTM_SEQUENCING_CYCLE"] == 1


def test_an_acyclic_sequence_reports_no_cycle() -> None:
    items = _clean_items()
    items["tags"] = [
        _tag("11", firingTriggerId=["20"], parameter=[]),
        _tag(
            "12",
            firingTriggerId=["21"],
            setupTag=[{"tagName": "Tag 11", "stopOnSetupFailure": True}],
            parameter=[],
        ),
    ]
    inventory = _read(_Fixture(items=items))
    graph = build_gtm_dependency_graph(inventory)
    assert graph.counts.sequencing_cycles == 0
    assert "GTM_SEQUENCING_CYCLE" not in _rules(_audit(inventory, graph))


# ------------------------------------------------------------------- union


def test_the_union_view_is_opt_in_and_takes_the_most_conservative_member() -> None:
    inventory = _read(_Fixture(items=_clean_items()))
    default = build_gtm_dependency_graph(inventory)
    joined = build_gtm_dependency_graph(inventory, include_union=True)

    assert default.union is None
    assert joined.union is not None
    entries = {(item.collection, item.resource_client_ref): item for item in joined.union}
    assert entries[("variables", "41")].reference_status == "PROVEN_UNREFERENCED"
    assert entries[("triggers", "20")].reference_status == "REFERENCED"
    assert entries[("triggers", "20")].scope_ref_status == "COMPLETE"

    # A blocked scope makes the container-wide entry unprovable even though the
    # per-scope classification elsewhere might have been clean.
    blocked_items = _clean_items()
    blocked_items["tags"][0]["teardownTag"] = [
        {"tagName": "Missing", "stopTeardownOnFailure": False}
    ]
    blocked = build_gtm_dependency_graph(_read(_Fixture(items=blocked_items)), include_union=True)
    assert blocked.union is not None
    assert all(item.reference_status != "PROVEN_UNREFERENCED" for item in blocked.union)


# ------------------------------------------------------------------- audits


def test_every_audit_dimension_has_an_explicit_coverage_status() -> None:
    inventory = _read(_Fixture())
    sections = _audit(inventory, build_gtm_dependency_graph(inventory))

    assert sections.rule_table_version == RULE_TABLE_VERSION
    assert sections.live_provider_reads == "NONE_RETAINED_RECORDS_ONLY"
    assert sections.ledger_appends == "NONE"
    assert len(sections.coverage) == 12
    assert all(
        item.status in {"COVERED", "PARTIAL", "NOT_READ", "NOT_APPLICABLE"}
        for item in sections.coverage
    )
    for finding in sections.findings:
        assert finding.automatic_write_allowed is False
        assert finding.deletion_recommended is False
        assert finding.evidence_refs[0] == "a" * 64


def test_broken_reference_and_coverage_rules_fire_and_do_not_fire() -> None:
    dirty = _read(_Fixture())
    assert _rules(_audit(dirty, build_gtm_dependency_graph(dirty)))["GTM_BROKEN_REFERENCE"] == 1

    clean = _clean_inventory()
    fired = _rules(_audit(clean, build_gtm_dependency_graph(clean)))
    assert "GTM_BROKEN_REFERENCE" not in fired
    assert "GTM_COVERAGE_GAP" not in fired
    assert "GTM_UNINSPECTED_WORKSPACE" not in fired
    assert "GTM_LIVE_LATEST_DIVERGENCE" not in fired
    assert "GTM_PAUSED_TAG" not in fired
    assert "GTM_CUSTOM_CODE_RESOURCE" not in fired

    bounded = _read(_Fixture(items=_clean_items(), endless={"variables"}))
    assert _rules(_audit(bounded, build_gtm_dependency_graph(bounded)))["GTM_COVERAGE_GAP"] >= 1


def test_tag_firing_consent_paused_and_custom_code_rules() -> None:
    items = _clean_items()
    items["tags"] = [
        # A reviewed GA4 event tag with no consent declaration and no trigger.
        _tag("11", type="gaawe", parameter=[]),
        # A Google tag: consent absent, but its firing is not trigger-driven.
        _tag("12", type="googtag", parameter=[]),
        # An unreviewed vendor type with custom HTML and a paused state.
        _tag(
            "13",
            type="html",
            paused=True,
            firingTriggerId=["20"],
            parameter=[{"type": "template", "key": "html", "value": "<script>x()</script>"}],
        ),
    ]
    inventory = _read(_Fixture(items=items))
    fired = _rules(_audit(inventory, build_gtm_dependency_graph(inventory)))

    assert fired["GTM_TAG_WITHOUT_FIRING_TRIGGER"] == 1  # tag 11 only; googtag exempt
    assert fired["GTM_CONSENT_SETTING_ABSENT"] == 2  # gaawe and googtag are reviewed types
    assert fired["GTM_CONSENT_TYPE_NOT_REVIEWED"] == 1  # the html tag
    assert fired["GTM_PAUSED_TAG"] == 1
    assert fired["GTM_CUSTOM_CODE_RESOURCE"] == 1

    declared = _clean_inventory()
    clean_rules = _rules(_audit(declared, build_gtm_dependency_graph(declared)))
    assert "GTM_CONSENT_SETTING_ABSENT" not in clean_rules
    assert "GTM_CONSENT_TYPE_NOT_REVIEWED" not in clean_rules
    assert "GTM_TAG_WITHOUT_FIRING_TRIGGER" not in clean_rules


def test_unreferenced_resources_are_reference_status_never_a_delete_recommendation() -> None:
    inventory = _clean_inventory()
    sections = _audit(inventory, build_gtm_dependency_graph(inventory))

    unreferenced = [
        finding
        for finding in sections.findings
        if finding.rule_id == "GTM_VARIABLE_WITHOUT_REFERRER"
    ]
    assert len(unreferenced) == 1
    assert unreferenced[0].dimension == "REFERENCE_STATUS"
    assert unreferenced[0].severity == "INFO"
    assert unreferenced[0].deletion_recommended is False

    declined = {item.considered_operation_kind: item for item in sections.no_recommendations}
    assert declined["DELETE"].reason == "OPERATION_NOT_COMPILABLE_FOR_PROVIDER"
    assert "deletion" in declined["DELETE"].detail.lower()
    assert sections.recommendation_status == (
        "NO_COMPILABLE_GTM_OPERATION_EVERY_CANDIDATE_DECLINED"
    )
    # There is no recommendation field at all; every candidate is declined.
    assert "recommendations" not in set(GtmWebAuditSections.model_fields) - {
        "no_recommendations",
        "recommendation_status",
    }


def test_no_gtm_operation_is_compilable() -> None:
    assert COMPILABLE_OPERATION_KINDS["gtm"] == frozenset()


def test_zone_diagnostics_report_what_a_single_container_read_cannot_see() -> None:
    inventory = _read(_Fixture())
    sections = _audit(inventory, build_gtm_dependency_graph(inventory))
    coverage = {item.dimension: item for item in sections.coverage}

    assert coverage["ZONE_BOUNDARY"].status == "PARTIAL"
    assert "child container" in coverage["ZONE_BOUNDARY"].rationale
    assert _rules(sections)["GTM_ZONE_RESTRICTION_UNVERIFIABLE_OUTSIDE_CONTAINER"] == 1

    no_zones = _clean_inventory()
    empty = _audit(no_zones, build_gtm_dependency_graph(no_zones))
    zone_coverage = {item.dimension: item for item in empty.coverage}["ZONE_BOUNDARY"]
    assert zone_coverage.status == "NOT_APPLICABLE"
    assert zone_coverage.evidence_source == "NONE"


def test_live_latest_divergence_and_workspace_hygiene_rules() -> None:
    diverged = _read(_Fixture(items=_clean_items(), live_version="8", latest_version="9"))
    assert (
        _rules(_audit(diverged, build_gtm_dependency_graph(diverged)))["GTM_LIVE_LATEST_DIVERGENCE"]
        == 1
    )

    dirty = _read(
        _Fixture(
            items=_clean_items(),
            workspace_changes=[{"changeStatus": "added"}, {"changeStatus": "updated"}],
        )
    )
    fired = _rules(_audit(dirty, build_gtm_dependency_graph(dirty)))
    assert fired["GTM_WORKSPACE_PENDING_CHANGES"] == 1


def test_the_audit_refuses_a_graph_built_from_another_inventory() -> None:
    graph = build_gtm_dependency_graph(_clean_inventory())
    with pytest.raises(AdhelmError) as error:
        evaluate_gtm_web_audit(
            inventory=_read(_Fixture()),
            graph=graph,
            source_record_digest="a" * 64,
            snapshot_digest="b" * 64,
        )
    assert error.value.code == "GTM_GRAPH_INVENTORY_MISMATCH"


# ------------------------------------------------------------- determinism


def test_graph_and_audit_are_deterministic_and_privacy_clean() -> None:
    first = _read(_Fixture())
    second = _read(_Fixture())
    graph_a = build_gtm_dependency_graph(first, include_union=True)
    graph_b = build_gtm_dependency_graph(second, include_union=True)

    assert digest(graph_a) == digest(graph_b)
    assert digest(_audit(first, graph_a)) == digest(_audit(second, graph_b))

    payload = {
        "graph": graph_a.model_dump(mode="json"),
        "audit": _audit(first, graph_a).model_dump(mode="json"),
    }
    validate_data(payload)
    serialized = json.dumps(payload)
    assert _SENTINEL not in serialized
    assert "customer container" not in serialized


def test_the_graph_refuses_a_structure_that_is_not_a_retained_inventory() -> None:
    with pytest.raises(AdhelmError) as error:
        build_gtm_dependency_graph({"resources": []})  # type: ignore[arg-type]
    assert error.value.code == "INVALID_GTM_GRAPH_INPUT"


# ------------------------------------------------------------- stage gate


def test_the_stage_gate_runs_on_the_retained_inventory() -> None:
    inventory = _clean_inventory()
    container = container_inventory_from_gtm_inventory(inventory, workspace_limit=3)

    assert container.account_ref == inventory.identity.account_ref
    assert container.container_ref == inventory.identity.container_ref
    assert set(container.inventoried_context_refs) == {"live", "latest", "gtm.workspace.3"}
    assert container.full_dependency_inventory is True
    gate = validate_gtm_stage_gate(inventory, workspace_limit=3)
    assert gate.base_live_version_ref == inventory.live_latest.live_version_ref
    assert gate.dedicated_workspace_required is True
    assert validate_stage_gate(container).base_live_version_ref == gate.base_live_version_ref


def test_the_stage_gate_fails_closed_on_incomplete_coverage() -> None:
    inventory = _read(_Fixture(items=_clean_items(), endless={"variables"}))
    container = container_inventory_from_gtm_inventory(inventory, workspace_limit=3)
    assert container.full_dependency_inventory is False
    with pytest.raises(AdhelmError) as error:
        validate_gtm_stage_gate(inventory, workspace_limit=3)
    assert error.value.code == "INVENTORY_INCOMPLETE"


def test_the_stage_gate_fails_closed_on_an_unresolved_reference() -> None:
    with pytest.raises(AdhelmError) as error:
        validate_gtm_stage_gate(_read(_Fixture()), workspace_limit=3)
    assert error.value.code == "INVENTORY_INCOMPLETE"


def test_the_stage_gate_fails_closed_on_live_latest_drift_and_a_full_slot() -> None:
    diverged = _read(_Fixture(items=_clean_items(), live_version="8", latest_version="9"))
    with pytest.raises(AdhelmError) as error:
        validate_gtm_stage_gate(diverged, workspace_limit=3)
    assert error.value.code == "WORKSPACE_CONFLICT"

    inventory = _clean_inventory()
    with pytest.raises(AdhelmError) as full:
        validate_gtm_stage_gate(inventory, workspace_limit=1)
    assert full.value.code == "WORKSPACE_SLOT_REQUIRED"


def test_the_stage_gate_requires_a_declared_workspace_limit() -> None:
    with pytest.raises(AdhelmError) as error:
        container_inventory_from_gtm_inventory(_clean_inventory(), workspace_limit=0)
    assert error.value.code == "WORKSPACE_LIMIT_REQUIRED"
    with pytest.raises(AdhelmError):
        container_inventory_from_gtm_inventory({}, workspace_limit=3)  # type: ignore[arg-type]


def test_the_projection_never_claims_a_reference_proof_of_its_own() -> None:
    """`reference_status` on the projection is fail-closed, not a second engine."""

    inventory = _read(_Fixture())
    container = container_inventory_from_gtm_inventory(inventory, workspace_limit=3)
    assert all(edge.status.value != "BROKEN" for edge in container.edges)
    assert (
        container.reference_status("gtm.workspace.3/variables/41")
        is ReferenceStatus.DYNAMIC_OR_UNKNOWN
    )


# -------------------------------------------------------------------- CLI


def _configure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixture: _Fixture) -> CliRunner:
    _install_workflow_fixture(monkeypatch, tmp_path, fixture)
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)
    monkeypatch.setattr("adhelm.credentials.export_key", lambda: b"k" * 32)
    return CliRunner()


def _retain(tmp_path: Path, runner: CliRunner) -> str:
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "gtm",
            "gtm-read",
            "2",
            "--ci-enabled",
            "--full-inventory",
        ],
    )
    assert result.exit_code == 0, result.output
    return str(json.loads(result.output)["data"]["record_digest"])


def _graph(runner: CliRunner, tmp_path: Path, *arguments: str) -> Any:
    result = runner.invoke(
        app, ["--data-dir", str(tmp_path), "measurement", "gtm-graph", *arguments]
    )
    return result


def test_cli_builds_the_graph_from_verified_history_without_rereading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _Fixture(items=_clean_items())
    runner = _configure(tmp_path, monkeypatch, fixture)
    record_digest = _retain(tmp_path, runner)
    requests_after_read = len(fixture.requests)
    ledger_files = sorted((tmp_path / "ledger/changes").glob("*.jsonl"))
    before = [path.read_bytes() for path in ledger_files]

    result = _graph(runner, tmp_path, record_digest, "--limit", "5")
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    data = envelope["data"]

    assert data["capability_version"] == "gtm-dependency-graph-1"
    assert data["rule_table_version"] == RULE_TABLE_VERSION
    assert data["live_provider_reads"] == "NONE_RETAINED_OBSERVATION_ONLY"
    assert data["ledger_appends"] == "NONE"
    assert data["deletion_authority"] == "NEVER_ESTABLISHED_BY_A_READ_ONLY_GRAPH"
    assert data["cleanup_operations"] == "NOT_IMPLEMENTED_SEPARATE_AUTHORIZED_WORK"
    assert data["compilable_operation_kinds"] == []
    assert data["retention"]["graph_retained"] is False
    assert data["source"]["record_digest"] == record_digest
    assert data["section"] == "NODES"
    assert data["rows"] and data["rows"][0]["row_type"] == "NODES"
    assert data["stage_gate"]["outcome"] == "NOT_EVALUATED"
    assert data["stage_gate"]["code"] == "WORKSPACE_LIMIT_REQUIRED"
    assert "union" not in data

    # Neither a provider request nor a ledger append happened.
    assert len(fixture.requests) == requests_after_read
    assert [path.read_bytes() for path in ledger_files] == before
    assert _SENTINEL not in result.output


def test_cli_pages_every_section_and_reports_the_stage_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _configure(tmp_path, monkeypatch, _Fixture(items=_clean_items()))
    record_digest = _retain(tmp_path, runner)

    for section in ("NODES", "EDGES", "FINDINGS", "COVERAGE"):
        result = _graph(runner, tmp_path, record_digest, "--section", section, "--limit", "1")
        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["data"]["section"] == section
        assert envelope["total_count"] >= 1
        assert len(envelope["data"]["rows"]) == 1
        assert envelope["next_cursor"] in (None, "1")

    gated = _graph(
        runner, tmp_path, record_digest, "--workspace-limit", "3", "--union", "--limit", "2"
    )
    assert gated.exit_code == 0, gated.output
    data = json.loads(gated.output)["data"]
    assert data["stage_gate"]["outcome"] == "PRECONDITIONS_MET"
    assert data["stage_gate"]["dedicated_workspace_required"] is True
    assert data["stage_gate"]["remote_write_authorized"] is False
    # `--union` selects the paged container-wide section instead of embedding
    # the whole classification list in the summary.
    assert data["section"] == "UNION"
    assert data["rows"] and all(row["row_type"] == "UNION" for row in data["rows"])

    # The envelope pseudonymizes references, so the exact scope reference is
    # read back from verified history rather than from the redacted output.
    observation, _ = load_measurement(Ledger(tmp_path), record_digest)
    snapshot = observation.snapshot
    assert isinstance(snapshot, GtmFullInventoryMeasurementSnapshot)
    workspace_ref = snapshot.inventory.base.workspaces[0].workspace_ref
    scoped = _graph(runner, tmp_path, record_digest, "--scope-ref", workspace_ref)
    assert scoped.exit_code == 0, scoped.output
    scoped_envelope = json.loads(scoped.output)
    assert scoped_envelope["data"]["rows"]
    assert len({row["scope_ref"] for row in scoped_envelope["data"]["rows"]}) == 1
    unfiltered = _graph(runner, tmp_path, record_digest, "--section", "COVERAGE")
    assert unfiltered.exit_code == 0, unfiltered.output
    # Filtering narrows the node rows to one scope; the whole graph has more.
    assert scoped_envelope["total_count"] < len(snapshot.inventory.resources) + 1
    assert scoped_envelope["total_count"] == sum(
        resource.scope_ref == workspace_ref for resource in snapshot.inventory.resources
    )


def test_cli_reports_a_blocked_stage_gate_rather_than_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _configure(tmp_path, monkeypatch, _Fixture())
    record_digest = _retain(tmp_path, runner)
    result = _graph(runner, tmp_path, record_digest, "--workspace-limit", "3", "--limit", "1")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["stage_gate"]["outcome"] == "BLOCKED"
    assert data["stage_gate"]["code"] == "INVENTORY_INCOMPLETE"
    assert data["container_blocking_reasons"]
    assert data["counts"]["proven_unreferenced"] == 0


def test_cli_refuses_an_unretained_or_wrong_kind_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _configure(tmp_path, monkeypatch, _Fixture())
    missing = _graph(runner, tmp_path, "0" * 64)
    assert missing.exit_code == 3
    assert json.loads(missing.output)["diagnostics"][0]["code"] == "MEASUREMENT_NOT_RETAINED"

    invalid = _graph(runner, tmp_path, "not-a-digest")
    assert invalid.exit_code == 2

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path),
            "measurement",
            "gtm",
            "gtm-read",
            "2",
            "--ci-enabled",
        ],
    )
    assert result.exit_code == 0, result.output
    partial = str(json.loads(result.output)["data"]["record_digest"])
    wrong_kind = _graph(runner, tmp_path, partial)
    assert wrong_kind.exit_code == 2
    assert json.loads(wrong_kind.output)["diagnostics"][0]["code"] == "MEASUREMENT_KIND_UNSUPPORTED"

    bad_section = _graph(runner, tmp_path, "0" * 64, "--section", "EVERYTHING")
    assert bad_section.exit_code == 2


def test_measurement_show_now_surfaces_reference_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = _configure(tmp_path, monkeypatch, _Fixture())
    record_digest = _retain(tmp_path, runner)
    shown = runner.invoke(
        app,
        ["--data-dir", str(tmp_path), "measurement", "show", record_digest, "--limit", "1"],
    )
    assert shown.exit_code == 0, shown.output
    data = json.loads(shown.output)["data"]
    assert data["reference_coverage"] == "COMPLETE_WITHIN_DECLARED_BOUNDS"
    assert data["dependency_completeness"] == "NOT_PROVEN"


def test_existing_check_stage_output_is_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller-supplied `measurement check-stage` route is untouched.

    `validate_stage_gate` gained a retained-observation adapter, not a new
    behaviour.  This pins the exact envelope the existing command emits for an
    existing input, so an additive change to the model or the validator that
    altered one byte of it would fail here.
    """

    from test_measurement import inventory as caller_supplied_inventory

    runner = CliRunner()
    request = tmp_path / "check-stage.json"
    request.write_text(
        json.dumps(caller_supplied_inventory().model_dump(mode="json")), encoding="utf-8"
    )
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / "data"),
            "measurement",
            "check-stage",
            "--input",
            str(request),
        ],
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["command"] == "measurement check-stage"
    assert envelope["outcome"] == "OK"
    assert envelope["data"] == {
        "inventory_digest": digest(caller_supplied_inventory()),
        "validation_scope": "LOCAL_DECLARATION_ONLY",
        "remote_write_authorized": False,
        "live_evidence_verified": False,
        "dedicated_workspace_required": True,
    }
    # The exact recorded digest of the exact model that existed before this
    # ticket.  A change to `ContainerInventory` would move it.
    assert envelope["data"]["inventory_digest"] == _GOLDEN_CHECK_STAGE_DIGEST


_GOLDEN_CHECK_STAGE_DIGEST = "238bd745440ad812f2d4d1325854c37a385a20dcbe7f9138343066004323c974"
