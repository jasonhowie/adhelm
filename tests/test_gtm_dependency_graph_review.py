"""Independent review of M01.2: the GTM dependency graph, its classification, and its audit.

Every test here is a *finding*.  Each one states the behaviour the contract
requires -- `AGENTS.md` rules 9, 11 and 20, `docs/measurement-control.md`'s
container-wide `PROVEN_UNREFERENCED` definition, and the provider-audit seam
invariants in `src/adhelm/audits/__init__.py` -- and fails against the shipped
implementation.  Nothing here fixes product code.

Fixtures are built from the M01.1 reader helpers in `tests/test_gtm_inventory.py`
so that every observation is one the real reader could have produced.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_gtm_inventory import (
    _Fixture,
    _install_workflow_fixture,
    _read,
    _resource,
    _tag,
    _version,
)
from typer.testing import CliRunner

from adhelm import measurement_cli
from adhelm.audits.gtm_web import evaluate_gtm_web_audit
from adhelm.cli import app
from adhelm.gtm_dependency_graph import build_gtm_dependency_graph
from adhelm.gtm_inventory import GtmFullInventory
from adhelm.measurement import validate_gtm_stage_gate

_PARENT = "accounts/1/containers/2"
_EMPTY: dict[str, list[dict[str, Any]]] = {
    "tags": [],
    "triggers": [],
    "variables": [],
    "folders": [],
    "built_in_variables": [],
    "templates": [],
    "zones": [],
    "clients": [],
    "transformations": [],
    "gtag_config": [],
}


def _items(**changes: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    items = {key: list(value) for key, value in _EMPTY.items()}
    items.update({key: list(value) for key, value in changes.items()})
    return items


def _clean_items() -> dict[str, list[dict[str, Any]]]:
    """One workspace whose every documented reference resolves."""

    return _items(
        tags=[
            _tag(
                "11",
                firingTriggerId=["20"],
                consentSettings={"consentStatus": "notNeeded"},
                parameter=[{"type": "template", "key": "eventName", "value": "purchase"}],
            )
        ],
        triggers=[_resource("trigger", "triggers", "20", type="pageview")],
        variables=[_resource("variable", "variables", "41", type="v")],
    )


def _audit(inventory: GtmFullInventory) -> Any:
    graph = build_gtm_dependency_graph(inventory)
    return graph, evaluate_gtm_web_audit(
        inventory=inventory,
        graph=graph,
        source_record_digest="a" * 64,
        snapshot_digest="b" * 64,
    )


# ------------------------------------------------------------------ finding 1


class _UndeterminableFixture(_Fixture):
    """Live and latest name one version reference but two configurations.

    `GtmLiveLatestStatus` calls that contradiction `UNDETERMINABLE` rather than
    guessing, which is exactly the state AGENTS rule 9 forbids staging into.
    """

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/tagmanager/v2/")
        if path == f"{_PARENT}/versions/8":
            self.requests.append(request)
            body = _version("8")
            body["name"] = "a latest body that differs from the live one"
            return httpx.Response(200, json=body)
        return super().handler(request)


def test_the_stage_gate_fails_closed_on_undeterminable_live_latest_identity() -> None:
    """FINDING 1 (BLOCKING). `UNDETERMINABLE` live/latest passes the stage gate.

    AGENTS rule 9: staging is allowed only after "proving GTM's live and latest
    version IDs match. A mismatch or unavailable workspace slot fails closed."
    `UNDETERMINABLE` is the explicit refusal to decide whether they match, so it
    is not a proof and must fail closed exactly as `LIVE_DIFFERS_FROM_LATEST`
    does.  `validate_gtm_stage_gate` only compares the two version *references*,
    which are equal in this state, so the gate returns `PRECONDITIONS_MET`.
    """

    inventory = _read(_UndeterminableFixture(items=_clean_items()))
    assert inventory.live_latest.status == "UNDETERMINABLE"
    assert inventory.live_latest.live_version_ref == inventory.live_latest.latest_version_ref
    # The graph refuses every proof of absence for this container.
    assert build_gtm_dependency_graph(inventory).container_blocking_reasons == (
        "LIVE_LATEST_UNDETERMINABLE",
    )

    with pytest.raises(Exception) as error:
        validate_gtm_stage_gate(inventory, workspace_limit=3)
    assert getattr(error.value, "code", None) == "WORKSPACE_CONFLICT"


def test_the_graph_command_never_reports_preconditions_met_while_live_latest_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FINDING 1 (BLOCKING), through the CLI.

    `measurement gtm-graph --workspace-limit` prints
    `stage_gate.outcome = PRECONDITIONS_MET` next to
    `live_latest_status = UNDETERMINABLE`, which is the fail-open reading an
    agent will act on.
    """

    runner = _configure(tmp_path, monkeypatch, _UndeterminableFixture(items=_clean_items()))
    record_digest = _retain(tmp_path, runner)
    result = _graph(runner, tmp_path, record_digest, "--workspace-limit", "3", "--limit", "1")
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["live_latest_status"] == "UNDETERMINABLE"
    assert data["stage_gate"]["outcome"] == "BLOCKED"


# ------------------------------------------------------------------ finding 2


class _VersionVariableFixture(_Fixture):
    """The live version holds the same variable the workspace holds."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/tagmanager/v2/")
        if path.endswith("/versions:live"):
            self.requests.append(request)
            body = _version("8")
            body["variable"] = [
                {
                    "path": f"{_PARENT}/versions/8/variables/41",
                    "accountId": "1",
                    "containerId": "2",
                    "containerVersionId": "8",
                    "variableId": "41",
                    "name": "variable 41",
                    "type": "v",
                    "fingerprint": "provider-value",
                }
            ]
            return httpx.Response(200, json=body)
        return super().handler(request)


def _cross_scope_inventory() -> GtmFullInventory:
    items = _items(
        tags=[
            _tag(
                "11",
                firingTriggerId=["20"],
                consentSettings={"consentStatus": "notNeeded"},
                parameter=[
                    {"type": "template", "key": "eventName", "value": "buy {{variable 41}}"}
                ],
            )
        ],
        triggers=[_resource("trigger", "triggers", "20", type="pageview")],
        variables=[_resource("variable", "variables", "41", type="v")],
    )
    return _read(_VersionVariableFixture(items=items, live_version="8", latest_version="9"))


def test_a_resource_referenced_in_another_scope_is_never_proven_unreferenced() -> None:
    """FINDING 2 (BLOCKING). The default graph proves a referenced resource unreferenced.

    `docs/measurement-control.md`: "A resource is `PROVEN_UNREFERENCED` only when
    every applicable resource surface, supported reference form, and every
    accessible pending workspace has been inventoried across live and latest
    versions and no edge reaches it."  That is a container-wide statement, and
    AGENTS rule 11 makes it the sole deletion authority.

    Here variable `41` exists in the live version and in workspace `3`, and a
    workspace tag references it.  The container-wide view (`include_union=True`)
    gets this right and says `REFERENCED`.  The default view -- the one
    `measurement gtm-graph` prints, since `--union` is opt-in -- publishes a
    node whose `reference_status` is the container-wide term
    `PROVEN_UNREFERENCED` for a resource the container does reference.
    """

    inventory = _cross_scope_inventory()
    graph = build_gtm_dependency_graph(inventory, include_union=True)
    assert graph.container_blocking_reasons == ()

    union = {item.resource_client_ref: item.reference_status for item in graph.union or ()}
    assert union["41"] == "REFERENCED"

    proven = {
        node.resource_ref for node in graph.nodes if node.reference_status == "PROVEN_UNREFERENCED"
    }
    referenced_client_refs = {
        node.resource_client_ref for node in graph.nodes if node.reference_status == "REFERENCED"
    }
    # No node may carry the container-wide verdict while another scope of the
    # same container references the same resource.
    assert not {ref for ref in proven if ref.endswith("/variables/41")}, (
        f"{proven} contradicts referenced client refs {referenced_client_refs}"
    )


# ------------------------------------------------------------------ finding 3


def test_an_unreviewed_tag_type_is_never_reported_healthy_when_it_declares_consent() -> None:
    """FINDING 3 (BLOCKING). An unreviewed tag type that declares consent is silently healthy.

    `src/adhelm/audits/gtm_web.py` states of `_CONSENT_REVIEWED_TAG_TYPES`: "An
    unlisted type is never reported healthy: it gets
    `GTM_CONSENT_TYPE_NOT_REVIEWED` instead, so an absent consent declaration is
    always visible, with an honest statement of how much this build knows about
    it."  The `audits` package invariant 2 says the same: a dimension the
    reviewed read does not cover "is never quietly omitted and never reported as
    healthy."

    `_consent` returns early on `declared`, so the unreviewed-type branch is
    reachable only when the tag declares *no* consent.  A vendor tag whose type
    this build has never reviewed, carrying any `consentStatus`, produces no
    finding at all and a `COVERED` consent dimension: the audit reports it clean
    on the strength of a field whose meaning for that type it has not reviewed.
    """

    inventory = _read(
        _Fixture(
            items=_items(
                tags=[
                    _tag(
                        "11",
                        type="vendorUnreviewedTagType",
                        firingTriggerId=["20"],
                        consentSettings={
                            "consentStatus": "needed",
                            "consentType": {
                                "type": "list",
                                "key": "consentType",
                                "list": [{"type": "template", "value": "ad_storage"}],
                            },
                        },
                    )
                ],
                triggers=[_resource("trigger", "triggers", "20", type="pageview")],
            )
        )
    )
    _, sections = _audit(inventory)
    counts = dict(sections.finding_counts_by_rule)
    assert counts.get("GTM_CONSENT_TYPE_NOT_REVIEWED") == 1, counts


# ------------------------------------------------------------------ finding 4


def test_reference_coverage_is_not_covered_when_a_referrer_collection_was_bounded() -> None:
    """FINDING 4 (SHOULD_FIX). A bounded `tags` collection still reports covered references.

    `REFERENCE_INTEGRITY` and `REFERENCE_STATUS` take their coverage from the
    `reference_edges` entries alone.  Those entries describe how many edges the
    reader emitted, not whether it saw every resource that could emit one.  With
    the workspace `tags` collection at `INCOMPLETE_PAGE_BOUND` -- the only place
    a referrer to a trigger or a variable could come from -- both dimensions
    still report `COVERED`, and every `GTM_TRIGGER_WITHOUT_REFERRER` /
    `GTM_VARIABLE_WITHOUT_REFERRER` finding restates `COVERED` as its own
    coverage status.  That is a dimension reported healthy over an unread
    surface (`audits` invariant 2).
    """

    inventory = _read(
        _Fixture(
            items=_items(
                triggers=[_resource("trigger", "triggers", "20", type="pageview")],
                variables=[_resource("variable", "variables", "41", type="v")],
            ),
            endless={"tags"},
        )
    )
    assert any(
        item.collection == "tags" and item.status == "INCOMPLETE_PAGE_BOUND"
        for item in inventory.coverage
    )
    _, sections = _audit(inventory)
    coverage = {item.dimension: item.status for item in sections.coverage}
    assert coverage["REFERENCE_STATUS"] == "PARTIAL", coverage
    assert coverage["REFERENCE_INTEGRITY"] == "PARTIAL", coverage


# ------------------------------------------------------------------ finding 5


def test_a_declined_outcome_that_cannot_inline_every_finding_records_the_bound() -> None:
    """FINDING 5 (SHOULD_FIX). The GTM audit truncates declined finding ids silently.

    `src/adhelm/audits/__init__.py`: "a row that could not inline every identifier
    records an explicit `BoundExceeded` entry saying so."  `ProviderAuditSections`
    carries `bound_exceeded` and `adhelm audit --provider-rules` pages it.
    `GtmWebAuditSections` has no such field, and `_declined` slices
    `identifiers[:MAXIMUM_INLINE_FINDING_IDS]` with nothing recording that it
    did.  AGENTS rule 20 forbids the silent fallback.
    """

    inventory = _read(
        _Fixture(
            items=_items(
                tags=[
                    _tag(
                        str(100 + index),
                        consentSettings={"consentStatus": "notNeeded"},
                        teardownTag=[
                            {"tagName": "Absent Teardown", "stopTeardownOnFailure": False}
                        ],
                    )
                    for index in range(40)
                ]
            )
        )
    )
    _, sections = _audit(inventory)
    declined = [
        item
        for item in sections.no_recommendations
        if "GTM_BROKEN_REFERENCE" in item.bound_rule_ids
    ]
    assert declined and declined[0].bound_finding_count > len(declined[0].finding_ids)
    assert getattr(sections, "bound_exceeded", ()), (
        "A declined GTM outcome that inlined only 32 of "
        f"{declined[0].bound_finding_count} findings records no explicit bound."
    )


# ------------------------------------------------------------------ finding 6


def _many_resource_items(count: int) -> dict[str, list[dict[str, Any]]]:
    return _items(
        tags=[
            _tag(str(100_000 + index), consentSettings={"consentStatus": "notNeeded"})
            for index in range(count)
        ],
        triggers=[
            _resource("trigger", "triggers", str(200_000 + index), type="pageview")
            for index in range(count)
        ],
        variables=[
            _resource("variable", "variables", str(300_000 + index), type="v")
            for index in range(count)
        ],
    )


def test_the_container_wide_union_view_is_paged_like_every_other_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FINDING 6 (SHOULD_FIX). `--union` is emitted whole, so it does not fit the envelope.

    `measurement gtm-graph` pages `NODES`, `EDGES`, `FINDINGS` and `COVERAGE`,
    but `data["union"]` is the complete `GtmUnionClassification` list -- up to
    `MAXIMUM_NODES` rows -- placed in the summary the pager may not shrink.  The
    union is the only container-wide classification the module offers, i.e. the
    only view finding 2 does not mis-state, and it becomes unreachable a few
    hundred resources into a container: the smallest page an agent can ask for
    is refused `OUTPUT_LIMIT_EXCEEDED`.
    """

    runner = _configure(tmp_path, monkeypatch, _Fixture(items=_many_resource_items(300)))
    record_digest = _retain(tmp_path, runner)
    plain = _graph(runner, tmp_path, record_digest, "--limit", "1")
    assert plain.exit_code == 0, plain.output

    result = _graph(runner, tmp_path, record_digest, "--union", "--limit", "1")
    assert result.exit_code == 0, result.output


# ------------------------------------------------------------------ finding 7


def test_the_audit_returns_a_bounded_result_for_a_container_the_graph_accepts() -> None:
    """FINDING 7 (SHOULD_FIX). The audit's finding bound refuses a graphable container.

    The reader admits 10,000 resources and `MAXIMUM_NODES` is 10,000, but
    `evaluate_gtm_web_audit` raises `GTM_AUDIT_BOUND_EXCEEDED` above 5,000
    findings, and `measurement gtm-graph` evaluates the audit unconditionally --
    even for `--section NODES`, which needs none of it.  A container of 3,300
    tags, triggers and variables therefore has no dependency graph output at
    all, rather than a bounded, paged result that states its own bound.  Every
    other bound in this ticket is reported rather than fatal
    (`inbound_source_ref_status`, `scope_ref_status`, `member_ref_status`).
    """

    inventory = _read(_Fixture(items=_many_resource_items(3_300)))
    graph = build_gtm_dependency_graph(inventory)
    assert graph.counts.nodes == 9_900
    sections = evaluate_gtm_web_audit(
        inventory=inventory,
        graph=graph,
        source_record_digest="a" * 64,
        snapshot_digest="b" * 64,
    )
    assert sections.findings


# ------------------------------------------------------------------ finding 8


class _LiveEqualsLatestFixture(_Fixture):
    """One version, holding one tag, serving as both the live and the latest read."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/tagmanager/v2/")
        if path.endswith("/versions:live") or path == f"{_PARENT}/versions/8":
            self.requests.append(request)
            body = _version("8")
            body["tag"] = [
                {
                    "path": f"{_PARENT}/versions/8/tags/11",
                    "accountId": "1",
                    "containerId": "2",
                    "containerVersionId": "8",
                    "tagId": "11",
                    "name": "Tag 11",
                    "type": "gaawe",
                    "fingerprint": "provider-value",
                    "parameter": [],
                }
            ]
            return httpx.Response(200, json=body)
        return super().handler(request)


def test_a_container_whose_live_version_is_its_latest_version_can_be_inventoried() -> None:
    """FINDING 8 (BLOCKING for the capability; the defect is M01.1's reader).

    `full_inventory` builds a `LIVE_VERSION` scope and a `LATEST_VERSION` scope.
    When live and latest are the same version -- the state AGENTS rule 9
    *requires* before staging, and the normal state of a real container -- both
    scopes take the same `scope_ref`, so every version resource is emitted twice
    and `GtmFullInventory` rejects the observation with an untyped
    `pydantic.ValidationError` ("GTM inventory resources must have unique
    references").  M01.2's fixtures only ever use empty version bodies, so the
    dependency graph, its audit and `measurement gtm-graph` are unreachable for
    any real container in that state.
    """

    inventory = _read(_LiveEqualsLatestFixture(items=_clean_items()))
    assert inventory.live_latest.status == "LIVE_EQUALS_LATEST"
    assert build_gtm_dependency_graph(inventory).counts.nodes > 0


# ------------------------------------------------------------------ finding 9


def test_a_full_inventory_the_reader_admits_can_be_retained_and_graphed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FINDING 9 (BLOCKING for the capability; the defect was M01.1's snapshot shape).

    `measurement gtm-graph` reads only from verified local history, so every
    graph this ticket can produce must first survive `ledger.append`.  A
    `GtmFullInventoryMeasurementSnapshot` canonicalizes to roughly 890 bytes per
    resource, so a container crossed `MAX_RECORD_BYTES` (1 MiB) at about 1,150
    resources -- one ninth of the reader's own 10,000-resource bound and of
    `MAXIMUM_NODES` -- and `measurement gtm --full-inventory` failed closed with
    `LEDGER_RECORD_TOO_LARGE` (exit 2), leaving no graph reachable at all.
    `M01.1.retention` repaired it by retaining such an observation as bounded
    chunk records and a commit record; this test is the original finding, no
    longer an expected failure.
    """

    runner = _configure(tmp_path, monkeypatch, _Fixture(items=_many_resource_items(500)))
    record_digest = _retain(tmp_path, runner)
    result = _graph(runner, tmp_path, record_digest, "--limit", "1")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["counts"]["nodes"] == 1_500


# ----------------------------------------------------------------- finding 10


class _MergeConflictFixture(_Fixture):
    """One accessible workspace whose `getStatus` reports a merge conflict."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/tagmanager/v2/")
        if path.endswith("/status"):
            self.requests.append(request)
            return httpx.Response(
                200,
                json={
                    "workspaceChange": [],
                    "mergeConflict": [{"entityInBaseVersion": {"tag": {"tagId": "11"}}}],
                },
            )
        return super().handler(request)


def test_the_stage_gate_projection_carries_a_reported_merge_conflict() -> None:
    """FINDING 10 (SHOULD_FIX). A reported merge conflict is dropped by the projection.

    `validate_gtm_stage_gate` documents itself as "failing closed on any gap",
    and `docs/measurement-control.md` requires that no plan "silently resolves a
    merge conflict".  The reader retains `counts.workspace_merge_conflicts` and
    the audit reports `GTM_WORKSPACE_MERGE_CONFLICTS` at HIGH severity, but
    `container_inventory_from_gtm_inventory` never reads that counter and
    `WorkspaceInventory` has no field able to carry it, so the structure the
    gate evaluates cannot express the condition at all.  The gate returns
    `PRECONDITIONS_MET` for a container whose workspace is in conflict.
    """

    inventory = _read(_MergeConflictFixture(items=_clean_items()))
    assert inventory.counts.workspace_merge_conflicts == 1
    with pytest.raises(Exception) as error:
        validate_gtm_stage_gate(inventory, workspace_limit=3)
    assert getattr(error.value, "code", None) in {"WORKSPACE_CONFLICT", "INVENTORY_INCOMPLETE"}


# ------------------------------------------------------------------- CLI glue


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
    return runner.invoke(app, ["--data-dir", str(tmp_path), "measurement", "gtm-graph", *arguments])
