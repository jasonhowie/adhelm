"""Explicit campaign-label disclosure over the normal redacted report view."""

from typing import Any, cast

from adhelm.projections import _redact_payload
from adhelm.reporting import NormalizedReport


def report_for_output(
    report: NormalizedReport, key: bytes, *, include_campaign_names: bool = False
) -> dict[str, Any]:
    shared = cast(dict[str, Any], _redact_payload(report.model_dump(mode="json"), key, False))
    if include_campaign_names:
        for source, target in zip(report.performance_rows, shared["performance_rows"], strict=True):
            name = source.provider_extensions.get("google_ads_campaign_name")
            if name is not None:
                target["provider_extensions"]["google_ads_campaign_name"] = name
    return shared
