# Live read pilot records

This directory holds the redacted records of live read pilots. A provider is
labelled `LIVE_PILOTED` in `adhelm discover`, `adhelm doctor` and every output
that names it only when a record for it is committed here. Every other provider is
`EXPERIMENTAL`. The release definition
([`release-0.1.0.md`](../release-0.1.0.md#live-read-pilot)) says what a pilot must
show; [`provider-release-labels.md`](../implementation/provider-release-labels.md)
says how the command and the label loader work.

No record is committed yet, so every provider is `EXPERIMENTAL`. The build session
never runs a pilot: a pilot is the owner's action on an account the owner
administers (build plan owner action 4).

## Running a pilot

Prerequisites, all owner-side:

- A build at the exact version and commit to be released. A record carries its
  `cli_version`, and a record for any other version does not label anything.
- A private data directory outside the repository, with a `READ` profile enrolled
  for the provider through its setup guide. GA4 needs a second `READ` profile for
  the GA4 Admin read. `doctor` requires a configured GA4 profile, so a pilot of any
  other provider needs one too.
- Credentials in the operating-system credential store. The pilot never passes the
  CI flag, so an `EXPLICIT_CI` profile cannot run one.

Prepare two closed inputs with the agent from `adhelm schema`: the provider's report
input (its `window_start` and `window_end` must be exactly seven inclusive days) and
an audit policy. GTM has neither: it reads a container and audits the retained
inventory.

```sh
adhelm pilot run --provider <provider> --profile <read-profile> \
  --report-input report.json --audit-policy policy.json \
  --ui-comparison <MATCH|MISMATCH> --source-commit "$(git rev-parse HEAD)" \
  --output docs/pilots/<provider>-<yyyy-mm-dd>.json
```

`adhelm discover` lists each provider's steps and the options its pilot takes. GA4
adds `--admin-profile` and `--stream-id`; GTM takes `--container-id` instead of the
report and policy inputs.

The command checks its whole selection, the named profiles (`--profile`, and
`--admin-profile` for GA4, must be matching `READ` profiles), the audit policy, and the
report input before it touches a credential or a provider. The report input gets
exactly the local checks `report <provider>` makes (one shared function): the
provider's own closed report schema; the profile's account; for LinkedIn an explicit
UTC profile; for GA4 the web-stream identity and the profile's property; for Google
Ads a scoped profile; for a Meta token
profile the account's declared time zone and currency. It must also declare the
seven-day window, which is checked first, before those preconditions. A report input
that would be refused is refused here, with no credential read, provider request or
ledger entry. It then runs the provider's fixed read commands in order: profile check
(`config show`), `doctor`, inventory, the seven-day report, the audit,
`history verify`. It stops at the first step that fails and writes no record. It makes
no change to any account and runs no write command.

### The provider UI comparison

Before the run, compare the same dimensions, dates, timezone and currency in the
provider's own UI against the report totals, within the provider's documented
precision, and pass the result: `MATCH` or `MISMATCH`. A recorded mismatch is
accepted evidence ([release definition](../release-0.1.0.md#live-read-pilot), item 6)
and the record keeps it, so a reader can see it. For GTM, which has no report, compare
the container's version and resource counts. The command cannot check this result; it
is the owner's statement.

## What a record contains

Pseudonyms, closed-vocabulary tokens, dates and digests only. The schema is
`adhelm schema pilot-record`:

- the provider, the CLI version, the source commit the owner passed, the platform and
  Python version, and the pilot date;
- an account pseudonym, a keyed hash of the profile's account made with the owner's
  local export key, so it matches the pseudonyms in that owner's exports and means
  nothing to anyone else;
- the report window (start and end dates, seven inclusive days), for providers that
  report;
- each step's command, outcome token and a digest of that step's result;
- the audit's coverage table: the closed dimension names and their statuses;
- the owner's `ui_comparison` and a digest that seals every other field.

It has no field that can hold an account or property identifier, a name, a token, a
URL, a path or a provider payload, and a test pins that every string in the schema is
a pattern, an enumeration or a date.

## Committing a record

Review the file, then commit it here, one file per pilot. A later record for the same
provider and version supersedes an earlier one; both stay as history.

A record labels its provider `LIVE_PILOTED` when it is for a released provider, parses
under the closed schema, has a digest that matches its contents, and carries a
`cli_version` equal to the build. `adhelm doctor` lists every file here with its state
(`ACCEPTED`, `PILOT_RECORD_VERSION_MISMATCH`, `PILOT_RECORD_DIGEST_MISMATCH`,
`PILOT_RECORD_INVALID`, `PILOT_RECORD_TOO_LARGE` or `PILOT_RECORD_UNREADABLE`), so a
stale or damaged record is reported rather than dropped. When the version changes,
re-run the pilots for the providers that should keep the label.

## What a record proves

The digest makes an edited record detectable. It does not prove that the pilot ran
live or on a real account: the command runs the same commands against whatever the
profile points at, and a record produced against a fake provider is indistinguishable
from a live one. The owner's commit is the attestation, and a record the owner did not
produce from their own run should not be accepted into this directory. The label
states that evidence exists; it moves no
[capability matrix](../capability-matrix.md) status and enables nothing.
