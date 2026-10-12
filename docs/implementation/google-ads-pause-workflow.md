# Configured Google Ads campaign PAUSE planning

The private `plan_configured_google_ads_campaign_pause` workflow connects a
configured Google Ads OAuth EDIT profile to a fresh customer/campaign
observation and one retained M55 PAUSE proposal. It adds no command, native
approval, authorization, provider mutation, or activation path.

The request selects one profile, a canonical positive signed-int64 campaign
identifier, and an integer lifetime from 60 through 900 seconds (default 300).
Copied or constructed invalid requests, CI use, stale expected configuration,
an unsupported profile, and unresolved local mutation history fail before the
protected planning session opens.
This is an admission preflight, not a reservation against another process
starting work later. Retaining a proposal grants no execution authority; future
execution must independently recheck recovery state under its coordinator.

The session supplies a detached execution binding and one fresh sequential
customer/campaign pair. The workflow checks the exact configured profile,
client, principal, manager route, scope, account, currency and timezone, then
uses the existing closed M55 compiler to reconstruct the complete action.
Customer `CLOSED` remains an observed status rather than an eligibility gate.
The requested expiry is measured from completion of that pair. A request that
exceeds the credential window is rejected rather than shortened silently.

Retention enters the session's configuration and protected-credential recheck
context before the ledger append. Its final callback checks held continuity
and the local clock; it performs no ledger or provider I/O and does not acquire
configuration or credential locks again. Expiry or clock rollback prevents
the plan record from being appended. An interrupted local append reports
`GOOGLE_ADS_PAUSE_PLAN_RETENTION_UNVERIFIED` and never retries; a record that
was already appended remains available for historical inspection.
The final clock cannot precede the time sampled after plan construction. The
session and held protected-slot checks also maintain advancing timestamp floors,
so a forward sample followed by rollback above the original observation time
is rejected.

The returned object is private and nonserializable. Its action must pass
through the M55 historical review/projection boundary before sharing with an
agent. A later command integration must preflight complete bounded output and
artifact preferences before calling this workflow.

Tests use fake sessions and local temporary configuration/ledgers, including
source binding, expiry, preflight and retained-result uncertainty. Three full private-session integration cases also use real configuration,
protected lifecycle, reader, compiler, history and review components with fake
store/HTTP edges. They cover success, developer-token drift after the pair and
manual configuration drift at final append. The 647-case affected group and
whole-source static checks pass; archived validation remains separate. This
document makes no live provider or installed-authority claim.
