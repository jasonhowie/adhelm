"""Deliberately unconfigured installed-approval bridge build selection.

The configured artifact builder may replace this module only in an isolated
source tree with literals rendered from the closed native metadata contract.
This checked-in source never loads configuration from a file, environment,
manifest, package resource, or runtime path.
"""

from __future__ import annotations


def _resolve_generated_installed_approval_bridge_binding() -> object:
    """Keep ordinary source and development wheels explicitly unconfigured."""
    return None
