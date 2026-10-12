# Meta campaign PAUSE plan CLI

The installed command group is intentionally limited to local planning and
historical review:

```text
plan meta-campaign-pause create --input REQUEST.json
plan meta-campaign-pause catalog
plan meta-campaign-pause show RECORD_DIGEST
```

`create` accepts only the closed
`meta-campaign-pause-plan-request` schema: schema version 1, an existing Meta
`EDIT` profile ID, one native campaign ID, and a bounded expiration interval.
It reads the JSON through the regular-file input boundary before reading local
configuration, a credential bundle, or Meta. The configured planner proves a
fresh fixed account/campaign baseline, retains one exact local plan, and emits
a compact receipt containing only record, action, plan, and subplan digests;
expiry; capability status; native currency; and zero incremental spend. Native
account/campaign identifiers, profiles, principal, credentials, configuration
digests, action data, transport data, and scopes never enter the receipt.

The CLI resolves saved output policy before the planner. `--no-artifacts`
suppresses a matching saved rule for this task only. `--artifact-format` and
`--artifact-output` form an explicit task override using the installed common
artifact policy. A matching or explicit request can create only a static,
redacted local-history artifact after the plan has been durably retained and
reloaded; it cannot refresh the campaign, authorize an action, or send a
provider request. The command reserves the complete receipt and worst-case
partial artifact result before token access, account/campaign GETs, or plan
retention. See `meta-pause-plan-artifact-cli.md` for the closed artifact
boundary and failure handling.

The receipt fits the saved structured-output bound before the configured
planner runs. Its plan digest is an output reference only; it is not an input,
approval, provider conditional-write token, or execution grant. Planning is
development-fixture-only and has no native approval, provider POST, or remote
mutation path.

`catalog` reads only verified local history and returns bounded metadata pages.
It has no credential, sharing-key, or provider dependency. `show` reloads one
verified retained record and renders the M40 historical view. It first sizes a
synthetic HMAC-redacted result under the saved output bound, then obtains the
existing local sharing key for the final redaction. Expired plans remain
inspectable as history. Neither catalog nor show refreshes a profile, campaign,
or effective-delivery state.

The discovery entry explicitly says that Meta PAUSE currently supports retained
planning and redacted review only. Configuration-only `PAUSED` is an intended
postcondition, not proof that delivery stopped or residual spend is zero.
