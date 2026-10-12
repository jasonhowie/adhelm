"""Pure envelope binding for locally retained Google Ads campaign PAUSE plans."""

from typing import Any

from adhelm.errors import AdhelmError, ExitCode
from adhelm.google_ads_pause_plan_records import revalidate_google_ads_campaign_pause_plan_record


def validate_google_ads_pause_plan_envelope(
    kind: str,
    payload: dict[str, Any],
    workspace_profile_ref: str,
    provider: str | None,
    account_ref: str | None,
    source: str,
    run_id: str | None,
    observed_at: str | None,
    occurred_at: str | None,
) -> None:
    """Bind exactly one closed Google Ads plan to non-occurrence local history."""

    if kind != "GOOGLE_ADS_CAMPAIGN_PAUSE_PLAN":
        return
    record = revalidate_google_ads_campaign_pause_plan_record(payload)
    action = record.action
    if (
        provider != "google_ads"
        or account_ref != action.plan.subplans[0].account.account_ref
        or workspace_profile_ref != action.plan.profile_ref
        or source != "LOCAL_CLI"
        or run_id is not None
        or observed_at is not None
        or occurred_at is not None
    ):
        raise AdhelmError(
            "INVALID_GOOGLE_ADS_PAUSE_PLAN_PROVENANCE",
            "A Google Ads PAUSE plan must bind its local workspace and account without "
            "claiming a provider occurrence, observation or execution run.",
            exit_code=ExitCode.INVALID_INPUT,
        )
