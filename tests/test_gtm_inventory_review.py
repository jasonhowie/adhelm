"""Coordinator review of the GTM full inventory: custom code never reaches evidence."""

from __future__ import annotations

from typing import Any

from test_gtm_inventory import _default_workspace_items, _Fixture, _read, _tag

from adhelm.canonical import canonical_bytes

_CODE = (
    "window.x=1;/*SENTINEL_SRC*/ var e='{{ document.cookie.split(\";\")[0] }}'; "
    '{{ alert("SENTINEL_CODE") }} {{Page URL}} {{ user_email_var }}'
)


def _inventory_with_brace_bearing_html() -> Any:
    items = _default_workspace_items()
    items["tags"].append(
        _tag(
            "901",
            type="html",
            parameter=[{"type": "template", "key": "html", "value": f"<script>{_CODE}</script>"}],
        )
    )
    return _read(_Fixture(items=items))


def test_unresolved_brace_tokens_from_custom_code_keep_only_their_digest() -> None:
    inventory = _inventory_with_brace_bearing_html()
    emitted = canonical_bytes(inventory).decode()
    for fragment in (
        "SENTINEL_SRC",
        "SENTINEL_CODE",
        "document.cookie",
        "alert(",
        "user_email_var",
    ):
        assert fragment not in emitted
    unresolved = [
        edge
        for edge in inventory.edges
        if edge.edge_kind == "VARIABLE_REFERENCE"
        and edge.source_ref.endswith("901")
        and edge.resolution == "UNRESOLVED_NO_MATCHING_RESOURCE"
    ]
    assert unresolved
    for edge in unresolved:
        assert edge.selector is None
        assert edge.selector_status == "WITHHELD_UNRESOLVED_REFERENCE"
        assert edge.selector_digest is not None


def test_resolved_variable_references_from_custom_code_keep_their_name() -> None:
    inventory = _inventory_with_brace_bearing_html()
    resolved = [
        edge
        for edge in inventory.edges
        if edge.edge_kind == "VARIABLE_REFERENCE"
        and edge.source_ref.endswith("901")
        and edge.resolution == "RESOLVED"
    ]
    assert resolved
    assert all(edge.selector is not None for edge in resolved)
