"""Pure envelope binding for locally retained Meta campaign PAUSE plans."""

from typing import Any

from adhelm.errors import AdhelmError, ExitCode
from adhelm.meta_pause_plan_records import revalidate_meta_campaign_pause_plan_record


def validate_meta_pause_plan_envelope(
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
    if kind != "META_CAMPAIGN_PAUSE_PLAN":
        return
    record = revalidate_meta_campaign_pause_plan_record(payload)
    action = record.action
    if (
        provider != "meta_ads"
        or account_ref != action.plan.subplans[0].account.account_ref
        or workspace_profile_ref != action.plan.profile_ref
        or source != "LOCAL_CLI"
        or run_id is not None
        or observed_at is not None
        or occurred_at is not None
    ):
        raise AdhelmError(
            "INVALID_META_PAUSE_PLAN_PROVENANCE",
            "A Meta PAUSE plan must bind its local workspace and account without "
            "claiming a provider occurrence, observation or execution run.",
            exit_code=ExitCode.INVALID_INPUT,
        )
