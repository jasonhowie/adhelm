"""Independent review of M01.1 (complete fixed GTM Web inventory).

These tests were written by a reviewer who did not implement M01.1.  Each one
encodes the behaviour the ticket, `AGENTS.md` rule 13, rule 20 and
`docs/definition-of-done.md` require, and each one currently fails against the
integrated implementation.  No product code is changed here.

They use `httpx.MockTransport` through the same fixture boundary as
`tests/test_gtm_inventory.py`: no credential, no live container, no provider
request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_gtm_inventory import (
    _default_workspace_items,
    _Fixture,
    _install_workflow_fixture,
    _read,
    _resource,
    _tag,
)
from typer.testing import CliRunner

from adhelm import measurement_cli
from adhelm.canonical import canonical_bytes
from adhelm.cli import app
from adhelm.ledger import Ledger
from adhelm.projections import export_verified
from adhelm.saved_measurements import load_measurement, measurement_rows

# A single sentinel that must never survive anywhere, in any surface.
_LEAK = "ZZINDEPENDENTREVIEWLEAKZZ"
_SCRIPT = f"<script>window.stolen='{_LEAK}'</script>"


def _tag_reference_items() -> dict[str, list[dict[str, Any]]]:
    """A documented `tagReference` parameter whose value carries script text.

    `Parameter.type == "tagReference"` documents a tag **name**.  A container
    whose value is not a tag name is exactly the adversarial case rule 13
    covers: the value is script, it matches no read resource, and it must
    therefore never be emitted as text.
    """

    items = _default_workspace_items()
    items["tags"] = [
        _tag(
            "700",
            parameter=[{"type": "tagReference", "key": "cleanup", "value": _SCRIPT}],
        ),
        _tag("701", setupTag=[{"tagName": f"javascript:void('{_LEAK}')"}]),
    ]
    return items


def test_unresolved_name_selectors_never_carry_raw_text() -> None:
    """Finding 1 (BLOCKING): only VARIABLE_REFERENCE selectors are withheld.

    `GtmInventoryClient._resolve` withholds an unresolved selector only when
    `edge_kind == "VARIABLE_REFERENCE"`.  Every other name-form selector
    (`PARAMETER_TAG_REFERENCE`, `SETUP_TAG`, `TEARDOWN_TAG`) keeps its raw
    provider text even when it matches no read resource, so arbitrary
    customer configuration text — including custom code — reaches the
    normalized inventory.  The rule is uniform: nothing textual survives
    except a name that matches a read resource.
    """

    inventory = _read(_Fixture(items=_tag_reference_items()))
    assert _LEAK not in canonical_bytes(inventory).decode()

    # `NATIVE_ID`, `PUBLIC_ID` and `TEMPLATE_TYPE` selectors are pattern-bound
    # and cannot carry free text; only the `NAME` form is a text channel.
    for edge in inventory.edges:
        if edge.selector_form != "NAME" or edge.resolution == "RESOLVED":
            continue
        assert edge.selector is None, (
            f"unresolved {edge.edge_kind} edge kept its selector text: {edge.selector!r}"
        )
        assert edge.selector_status == "WITHHELD_UNRESOLVED_REFERENCE"


def test_a_value_classified_as_custom_code_is_never_also_emitted_verbatim() -> None:
    """Finding 1 (BLOCKING), sharpened: the module contradicts itself.

    `_classify_parameter` classifies the very same parameter value as
    `CUSTOM_HTML` and records it as `DIGESTED_NEVER_PERSISTED`, while
    `_edges` emits that identical string verbatim as `edges[].selector`.  A
    value the implementation itself has ruled custom code cannot also be
    published as edge text.
    """

    inventory = _read(_Fixture(items=_tag_reference_items()))
    source = next(item for item in inventory.resources if item.resource_client_ref == "700")
    assert [marker.classification for marker in source.code_markers] == ["CUSTOM_HTML"]
    assert all(marker.source_status == "DIGESTED_NEVER_PERSISTED" for marker in source.code_markers)

    selectors = [edge.selector for edge in inventory.edges if edge.selector is not None]
    assert _SCRIPT not in selectors


def test_custom_code_never_reaches_the_retained_record_or_review_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1 (BLOCKING), end to end: the leak reaches canonical history.

    `docs/definition-of-done.md` requires that raw custom-code source never
    reaches logs, evidence, support bundles, telemetry, or remote analysis.
    The leaked selector is retained in the append-only ledger record and is
    replayed by `measurement show` and `measurement_rows`, which is the
    agent-visible sharing boundary.
    """

    fixture = _Fixture(items=_tag_reference_items())
    _install_workflow_fixture(monkeypatch, tmp_path, fixture)
    monkeypatch.setattr(measurement_cli, "export_key", lambda: b"k" * 32)
    monkeypatch.setattr("adhelm.credentials.export_key", lambda: b"k" * 32)
    runner = CliRunner()
    retained = runner.invoke(
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
    assert retained.exit_code == 0, retained.output
    receipt = dict(json.loads(retained.output)["data"])

    on_disk = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and _LEAK in path.read_bytes().decode("utf-8", "replace")
    ]
    assert on_disk == [], f"custom code reached retained files: {on_disk}"

    shown = runner.invoke(
        app,
        ["--data-dir", str(tmp_path), "measurement", "show", receipt["record_digest"]],
    )
    assert shown.exit_code == 0, shown.output
    assert _LEAK not in shown.output

    observation, _ = load_measurement(Ledger(tmp_path), receipt["record_digest"])
    assert _LEAK not in json.dumps(measurement_rows(observation))

    bundle = tmp_path.parent / "independent-review-export"
    export_verified(Ledger(tmp_path), bundle, opaque_key=b"k" * 32)
    exported = [
        path
        for path in bundle.rglob("*")
        if path.is_file() and _LEAK in path.read_bytes().decode("utf-8", "replace")
    ]
    assert exported == [], f"custom code reached the export bundle: {exported}"


def test_edge_bound_is_reported_rather_than_silently_dropping_references() -> None:
    """Finding 2 (BLOCKING): the 20,000-edge bound truncates in silence.

    `GtmInventoryClient._resolve` stops emitting edges once `_edge_budget`
    reaches zero and records nothing: no `INCOMPLETE_*` coverage status, no
    dropped-edge counter, no completeness gap.  A container with more than
    20,000 references therefore reports
    `COMPLETE_WITHIN_DECLARED_BOUNDS` while references are missing.

    Every other bound in this module is reported (`INCOMPLETE_PAGE_BOUND`,
    `INCOMPLETE_ITEM_BOUND`, `INCOMPLETE_REQUEST_BOUND`,
    `INCOMPLETE_RESOURCE_BOUND`, `INCOMPLETE_WORKSPACE_BOUND`); this one is
    not, which is the exact fail-open direction the M01.2
    `PROVEN_UNREFERENCED` engine must never be handed.

    The fixture makes the consequence concrete: trigger `9999` is referenced
    by exactly one tag, that tag's edge falls past the bound, and the
    resulting "complete" observation contains no reference to it at all.
    """

    referenced = [str(1000 + index) for index in range(1000)]
    triggers = [_resource("trigger", "triggers", value, type="pageview") for value in referenced]
    triggers.append(_resource("trigger", "triggers", "9999", type="pageview"))
    tags = [_tag(str(500 + index), firingTriggerId=list(referenced)) for index in range(20)]
    # Read last, so its edges are the ones the budget discards.
    tags.append(_tag("999", firingTriggerId=["9999"]))
    items: dict[str, list[dict[str, Any]]] = {
        "tags": tags,
        "triggers": triggers,
        "variables": [],
        "folders": [],
        "built_in_variables": [],
        "templates": [],
        "zones": [],
        "clients": [],
        "transformations": [],
        "gtag_config": [],
    }

    inventory = _read(_Fixture(items=items))

    assert any(item.resource_client_ref == "9999" for item in inventory.resources)
    assert inventory.completeness.status == "INCOMPLETE"
    assert any(item.status.startswith("INCOMPLETE_") for item in inventory.coverage)
    assert any(
        edge.target_ref is not None and edge.target_ref.endswith("/triggers/9999")
        for edge in inventory.edges
    )


def test_a_withheld_name_still_resolves_to_its_single_read_resource() -> None:
    """Finding 3 (SHOULD_FIX): the digest resolution path can never match.

    `_Scope.by_name_digest` is keyed by `_text_digest(key, "name", value)` from
    `_bounded_name`, while an edge's `selector_digest` is
    `_text_digest(key, "selector", value)` from `_edges.add`.  The two HMAC
    domains differ, so the `by_name_digest` lookup in `_resolve` is
    unreachable: a reference to a resource whose name was withheld as
    oversize or sensitive is always reported
    `UNRESOLVED_NO_MATCHING_RESOURCE`, and the inventory is permanently
    `INCOMPLETE` for a reference it can in fact resolve.
    """

    name = "n" * 400
    items = _default_workspace_items()
    items["tags"] = [_tag("800", name=name), _tag("801", setupTag=[{"tagName": name}])]
    inventory = _read(_Fixture(items=items))

    target = next(item for item in inventory.resources if item.resource_client_ref == "800")
    assert target.name_status == "WITHHELD_OVERSIZE"

    edge = next(item for item in inventory.edges if item.edge_kind == "SETUP_TAG")
    assert edge.selector_status == "WITHHELD_OVERSIZE"
    assert edge.resolution == "RESOLVED"
    assert edge.target_ref == target.resource_ref


def test_trigger_filter_parameter_values_are_classified_as_custom_code() -> None:
    """Finding 4 (SHOULD_FIX): only the top-level `parameter` tree is scanned.

    `_code_markers` walks `raw["parameter"]` only.  A `Trigger` carries its
    values under `filter`, `customEventFilter` and `autoEventFilter`, so a
    `javascript:` or `<script>` value there is neither digested nor counted,
    and `counts.custom_code_resources` under-reports the container.  The
    module docstring promises that "script-bearing parameter values are
    hashed and classified".
    """

    items = _default_workspace_items()
    items["triggers"] = [
        _resource("trigger", "triggers", "20", type="pageview"),
        _resource(
            "trigger",
            "triggers",
            "22",
            type="customEvent",
            filter=[
                {
                    "type": "equals",
                    "parameter": [
                        {"type": "template", "key": "arg1", "value": _SCRIPT},
                    ],
                }
            ],
        ),
    ]
    inventory = _read(_Fixture(items=items))

    assert _LEAK not in canonical_bytes(inventory).decode()
    trigger = next(item for item in inventory.resources if item.resource_client_ref == "22")
    assert [marker.classification for marker in trigger.code_markers] == ["CUSTOM_HTML"]
    assert trigger.code_markers[0].source_status == "DIGESTED_NEVER_PERSISTED"


def test_variable_references_inside_string_array_members_become_edges() -> None:
    """Finding 5 (NOTE): `_walk` skips scalar members of a JSON array.

    `_walk`'s list branch recurses into each child but yields nothing for a
    child that is not a dict or list, so a `{{ }}` token that sits directly
    inside a string array is never scanned.  Resource bodies are not
    schema-closed, so an undocumented or future GTM field of that shape
    silently produces no reference edge — a missed dependency, which is the
    fail-open direction for M01.2.
    """

    items = _default_workspace_items()
    items["tags"] = [_tag("650", undocumentedStringArray=["{{Page URL}}"])]
    inventory = _read(_Fixture(items=items))

    edges = [
        edge
        for edge in inventory.edges
        if edge.source_ref.endswith("/650") and edge.edge_kind == "VARIABLE_REFERENCE"
    ]
    assert [edge.selector for edge in edges] == ["Page URL"]


def test_container_environments_are_inventoried_or_explicitly_statused() -> None:
    """Finding 6 (SHOULD_FIX): a promised container collection is missing.

    `docs/measurement-control.md` "GTM Web contract / Inventory" requires the
    inventory to cover "destinations, live and latest container-version IDs,
    every accessible workspace and its pending changes, **environments**,
    tags, triggers, ...", and `docs/definition-of-done.md` binds the inventory
    row to everything promised there.

    `accounts.containers.environments.list` is a documented container-scoped
    v2 collection (`{container}/environments`, response array `environment`,
    with a documented `pageToken`), verified 2026-09-09.  `GtmCollection`
    has no `environments` member, no request is issued, and no coverage entry
    records the omission, so a container with environments is reported
    `COMPLETE_WITHIN_DECLARED_BOUNDS` for a surface that was never read.
    Reading it or recording an explicit status would both satisfy this; a
    silent omission does not (rule 20).
    """

    inventory = _read(_Fixture())
    container_collections = {
        item.collection for item in inventory.coverage if item.scope_kind == "CONTAINER"
    }
    assert "environments" in container_collections


def test_workspace_listing_order_does_not_change_the_retained_observation() -> None:
    """Finding 7 (SHOULD_FIX): the retained full inventory is order-dependent.

    `GtmFullInventory` sorts `coverage`, `resources` and `edges` by their
    exact local references, but `base` is the reader's result verbatim and
    `base.workspaces` keeps whatever order the provider's workspace pages
    returned.  The default `measurement gtm` path re-validates the fixed
    inventory with `workspaces` sorted by `workspace_ref` before retaining it
    (`src/adhelm/measurement_cli.py`); the `--full-inventory` path does not.

    So one unchanged container read twice can produce two different canonical
    payloads and two different retained `snapshot_digest` values, which
    defeats "identical input yields an identical digest" and any future
    reconciliation over these observations.
    """

    ordered = _read(_Fixture(workspaces=["3", "5"]))
    reordered = _read(_Fixture(workspaces=["5", "3"]))
    assert canonical_bytes(ordered) == canonical_bytes(reordered)
