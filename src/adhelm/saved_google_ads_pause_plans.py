"""Retain and select exact Google Ads PAUSE plans from verified local history."""

import re
from collections.abc import Callable
from typing import Any

from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_pause_plan_records import (
    google_ads_campaign_pause_plan_record,
    revalidate_google_ads_campaign_pause_plan_record,
)
from adhelm.ledger import Ledger
from adhelm.providers.google_ads_pause_contract import GoogleAdsCampaignPausePlannedAction

_KIND = "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN"


def retain_google_ads_campaign_pause_plan(
    ledger: Ledger,
    action: GoogleAdsCampaignPausePlannedAction,
    *,
    before_durable_append: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Retain one local plan without asserting a current remote condition.

    Configured callers acquire configuration then credential locks before this
    call.  Their final callback uses only already-held state, performs no
    ledger I/O, and runs immediately before the durable payload append under
    the ledger stream lock.  Historical retention grants no execution authority.
    """

    record = google_ads_campaign_pause_plan_record(action)
    checked = record.action
    if ledger.profile_ref != checked.plan.profile_ref:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_PROVENANCE",
            "The Google Ads PAUSE plan belongs to a different local workspace.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    return ledger.append(
        "changes",
        _KIND,
        record.model_dump(mode="json", warnings="error"),
        provider="google_ads",
        account_ref=checked.plan.subplans[0].account.account_ref,
        source="LOCAL_CLI",
        _before_durable_append=before_durable_append,
    )


def load_google_ads_campaign_pause_plan(
    ledger: Ledger, record_digest: str
) -> tuple[GoogleAdsCampaignPausePlannedAction, dict[str, Any]]:
    """Load one exact retained Google Ads PAUSE plan with its checkpoint facts."""

    if type(record_digest) is not str or re.fullmatch(r"[a-f0-9]{64}", record_digest) is None:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_SELECTION",
            "An exact retained Google Ads PAUSE record digest is required.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    checkpoint = ledger.checkpoint()
    records = [
        item
        for item in checkpoint["records_by_stream"]["changes"]
        if item["record_kind"] == _KIND and item["record_digest"] == record_digest
    ]
    if len(records) != 1:
        raise AdhelmError(
            "GOOGLE_ADS_PAUSE_PLAN_NOT_RETAINED",
            "The Google Ads PAUSE plan is not retained.",
        )
    stored = records[0]
    record = revalidate_google_ads_campaign_pause_plan_record(stored["payload"])
    return record.action, {
        "record_digest": stored["record_digest"],
        "plan_digest": record.plan_digest,
        "action_digest": record.action_digest,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "recorded_at": stored["recorded_at"],
        "historical_evidence_status": record.historical_evidence_status,
        "execution_authority": False,
        "requires_current_kernel_checks": True,
    }


def google_ads_campaign_pause_plan_catalog(
    ledger: Ledger, *, cursor: int = 0, limit: int = 50
) -> dict[str, Any]:
    """Return newest-first bounded historical plan summaries only."""

    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 500:
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_SELECTION",
            "Google Ads PAUSE plan pagination is invalid.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    checkpoint = ledger.checkpoint()
    records = [
        item
        for item in reversed(checkpoint["records_by_stream"]["changes"])
        if item["record_kind"] == _KIND
    ]
    if cursor > len(records):
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_SELECTION",
            "Cursor exceeds the retained Google Ads PAUSE plan count.",
            exit_code=ExitCode.INVALID_INPUT,
        )
    rows = []
    for stored in records[cursor : cursor + limit]:
        record = revalidate_google_ads_campaign_pause_plan_record(stored["payload"])
        rows.append(
            {
                "record_digest": stored["record_digest"],
                "plan_digest": record.plan_digest,
                "recorded_at": stored["recorded_at"],
                "expires_at": record.expires_at.isoformat(),
                "capability_status": record.action.status,
                "maximum_incremental_spend": record.maximum_incremental_spend.model_dump(
                    mode="json"
                ),
                "execution_authority": False,
            }
        )
    return {
        "plans": rows,
        "checkpoint_digest": checkpoint["checkpoint_digest"],
        "total_count": len(records),
        "next_cursor": str(cursor + limit) if cursor + limit < len(records) else None,
        "historical_evidence_status": "HISTORICAL_PLAN_NOT_CURRENT_REMOTE_EVIDENCE",
    }
