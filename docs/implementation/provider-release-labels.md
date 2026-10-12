# Provider release labels and the pilot command (ticket O3.3)

Decision: [ADR 0013](../adr/0013-open-source-release.md) decision 10.
Gate: [`release-0.1.0.md`](../release-0.1.0.md#provider-status-labels).
Procedure for the owner: [`docs/pilots/README.md`](../pilots/README.md).

Each of the eight 0.1.0 providers (Google Ads, Meta Ads, LinkedIn Ads, TikTok Ads,
Reddit Ads, OpenAI Ads, GA4, GTM) carries exactly one label. Salesforce and HubSpot
are outside the release and carry none.

| Label | Meaning (as every output states it) |
| --- | --- |
| `LIVE_PILOTED` | Fixture-verified and read-piloted on a live account; a redacted pilot record is committed. |
| `EXPERIMENTAL` | Fixture-verified only; no live pilot evidence. |

The label is a statement about evidence. It is not a capability, moves no status in
[`capability-matrix.md`](../capability-matrix.md), and enables nothing. It never
touches the write refusal.

## Where a label comes from

Only from committed pilot records, loaded by `src/adhelm/release_labels.py`:

- Source checkout: `docs/pilots/*.json`. Wheel: Hatch force-includes `docs/pilots`
  as `adhelm/pilot_record_files` (`pyproject.toml`), the same way `skills` becomes
  `adhelm/bundled_skills`, and the loader prefers that directory. Without either, there
  are no records and every provider is `EXPERIMENTAL`.
- A record labels its provider `LIVE_PILOTED` only if it passes, in order: a regular
  file (not a link) within 64 KiB; strict JSON that validates against
  `PilotRecord` (`adhelm schema pilot-record`); a record digest equal to the digest of
  its other fields; and a `cli_version` equal to this build's version.
- Failing records leave the provider `EXPERIMENTAL` and are reported, not dropped:
  `PILOT_RECORD_UNREADABLE`, `PILOT_RECORD_TOO_LARGE`, `PILOT_RECORD_INVALID`,
  `PILOT_RECORD_DIGEST_MISMATCH`, `PILOT_RECORD_VERSION_MISMATCH`.
  `doctor` lists every file with its state under `pilot_records`.
- Of several accepted records for one provider, the latest `pilot_date` wins. An
  unusable newer record does not hide an older valid one.

Nothing else sets a label. No configuration field, environment variable, flag or
caller-supplied output field does: a configuration that carries a label is rejected by
the closed `Configuration` schema, and `ResultEnvelope.provider_labels` is recomputed
from the command and data on every construction, so a value passed in is overwritten.
The records directory is read once per process and cached.

## Where a label appears

- `discover`: `providers[].release_label` for every provider row (`null` for
  Salesforce and HubSpot), `release_labels` (meanings, source, the rule above,
  `grants_capability: false`) and the `pilot` section: the command, the record
  schema, each provider's steps and options, derived from the same plan the command
  runs. `providers[].release_required` is true for exactly the eight released
  providers and GA4 Admin (part of the GA4 release), so every required row has a
  label and an implementation other than `NOT_IMPLEMENTED`. Salesforce and HubSpot
  stay listed as `release_required: false`, `implementation: NOT_IMPLEMENTED`.
- `doctor`: `providers[].release_label` and `pilot_records`.
- Every output that names a released provider: `ResultEnvelope.provider_labels`, a
  list of `{provider, label, meaning}` (plus `pilot_record_digest` for a piloted
  provider) for each provider named. An output names a provider when its command path
  starts with a provider slug in any token (`report google-ads`, `measurement
  gtm-graph`, `measurement ga4-admin`, `compile openai-ads`), starts with a short
  provider name (`meta`, `openai`, `linkedin`, `reddit`, `tiktok`: `plan
  meta-campaign-pause`, `execute openai-pause`, `auth tiktok-token status`), or when
  any `provider` field in its data does (`ga4_admin` and `gtm_web_inventory` name GA4
  and GTM). `google` is not a short name: `auth google-oauth` is shared by three
  providers and names none. Because it is a field of the envelope, refusals
  (`FAILED_CLOSED`) carry it too, and so does every output built outside `emit`. An
  output that names no released provider omits the field, so it is byte-identical to
  the envelope before labels.
- The rule has no command-group exemption: `plan`, `execute` and `recover` outputs are
  labelled like `compile` and `artifacts` outputs, and their refusals (all an
  `execute` leaf returns in this build) carry the label. The one exception is by
  outcome, not by command, and is written into the release definition: the closed
  success receipts that are sized byte for byte to the 1 KiB minimum output bound
  (`EXECUTION_RESULT` and the GA4 key-event create plan receipt,
  `BOUND_SIZED_RECEIPT_OUTCOMES`). A label adds about 130 bytes per provider, which
  neither receipt has; shrinking them would change what unreleased write contracts
  retain, so they stay unlabelled and a test pins the list. The other plan receipts
  fit with the label, and every other output of those commands is labelled.
- The README provider table is public-docs work (ticket O4.2); it should be generated
  from `adhelm discover` rather than maintained by hand.

## The pilot command

`pilot run --provider <p> --profile <read profile> --ui-comparison MATCH|MISMATCH
--source-commit <40 hex> --output <new file>` plus the provider's options
(`discover` → `pilot.providers`). `src/adhelm/pilot_workflow.py` runs the release
definition's sequence by invoking the installed commands in process, in the order the
plan in `src/adhelm/pilot_records.py` fixes, so a pilot cannot exercise a path a user's
own run does not:

| Provider | Steps (command) |
| --- | --- |
| Google, Meta, LinkedIn, TikTok, Reddit, OpenAI Ads | `config show`, `doctor`, `inventory`, `report <slug>`, `audit --provider-rules`, `history verify` |
| GA4 | `config show`, `doctor`, `measurement ga4-admin`, `report ga4`, `audit --provider-rules`, `history verify` |
| GTM | `config show`, `doctor`, `measurement gtm --full-inventory`, `measurement gtm-graph --section COVERAGE`, `history verify` |

The release definition's "doctor and account identity" is `doctor` plus the identity
each inventory and report read discovers from the provider and binds to the profile
(a mismatch fails the step). "Enrollment" is observable only as a configured `READ`
profile (GA4 needs both `ga4` and `ga4_admin`); working credentials are shown by the
reads that follow.

Order of events:

1. **Local validation, before any credential, ledger or provider access**: provider is
   released; `--ui-comparison` is `MATCH` or `MISMATCH`; `--source-commit` is a full
   lowercase hex commit; the provider's options are all present and none that do not
   apply; the output is a new file in an existing directory; the platform is supported;
   `--profile` (and GA4's `--admin-profile`, which must be a `ga4_admin` profile) is a
   `READ` profile for the provider; the report input gets exactly the checks
   `report <provider>` makes before any credential, ledger or provider access, because
   both call the one function `workflows.require_report_preconditions` (so they cannot
   drift): the input parses against the provider's closed query schema (with GA4's
   aggregate discriminator); its account is the profile's; for LinkedIn the profile is
   an explicit UTC profile (`TIMEZONE_MISMATCH`); for GA4 the web-stream identity is
   present and the property is the profile's; for Google Ads the profile is a scoped
   OAuth profile
   (`SCOPED_PROFILE_REQUIRED`); for a Meta token
   profile the query's source time zone and currency are the account declaration's
   (`IDENTITY_MISMATCH`); the pilot never passes `--ci`, so the Meta interactive-store
   refusal under `--ci` does not apply. The pilot adds its own rule that the window is
   exactly seven inclusive days (`INVALID_PILOT_WINDOW`), and it checks that window
   first, before the report preconditions: a bad window is reported as such whatever
   else is wrong with the input. A report input that is not a JSON object declares no
   window and is refused as `INVALID_PILOT_WINDOW`, where `report <provider>` refuses
   the same input as `SCHEMA_VALIDATION_FAILED`. Input that is not JSON is refused with
   its own diagnostic (`INVALID_JSON`) before that. The audit policy validates after the
   report input. Refusals are `INVALID_PILOT_SELECTION` (exit 2), `INVALID_PILOT_WINDOW`
   (2), the shared input-file, schema, identity and precondition codes
   (`SCHEMA_VALIDATION_FAILED`, `IDENTITY_MISMATCH`, `TIMEZONE_MISMATCH`,
   `SCOPED_PROFILE_REQUIRED`), and
   `READ_PROFILE_REQUIRED` (3). In each case there is no ledger, no output, no
   credential read, no provider request and no export-key read;
   `tests/test_pilot_run_providers.py` runs these with a guard that fails the test on
   any of them, and a test pins that the report command and the pilot both refuse
   through `require_report_preconditions`.
2. **The steps**, each a clean exit plus the pilot's own acceptance rule: the profiles
   exist; the inventory was retained complete (GA4 and GTM: with a retained record
   digest; GTM as a full inventory); the report
   was retained for the declared window (the retained report is loaded from verified local
   history by the digest the step names, and its provider and window are checked, so the
   result does not depend on how the step's output was displayed or bounded); the
   audit returned its whole coverage table; history verified. The first failure stops
   the run: `pilot run` exits with that step's exit code and `PILOT_INCOMPLETE`, lists
   each step as `PASSED`, `FAILED` (with its diagnostic code and next action) or
   `NOT_RUN`, and writes nothing. `doctor` must exit clean, so a missing GA4 profile
   fails the `DOCTOR` step.
3. **The record**, only for a complete run, created exclusively and never replacing a
   file (`PILOT_OUTPUT_EXISTS` if one appeared meanwhile).

The record (`PilotRecord`) holds the provider, `cli_version`, the owner-supplied
`source_commit`, platform, Python version, pilot date, the account pseudonym
(`opaque_ref` of the profile's account with the local export key), the report window,
per step the command, outcome token and a digest of the step's result, the audit
coverage table, `ui_comparison`, and its digest. Every string field is a pattern,
enumeration or date; `tests/test_pilot_records.py` fails if a free-form string is
added. The command never keeps a step's result, and the command's own output repeats
only record fields and diagnostic codes.

## Findings the end-to-end tests surfaced

Running all eight providers through the command found one defect outside the command:
`audit --provider-rules` could not join a LinkedIn or OpenAI report to the inventory
`run_inventory` had retained, because the report envelope carries the canonical
`<provider>.account.<id>` reference and the inventory the bare profile account. The
pilot could never have completed for either provider. `retained_account_key` now
reduces both spellings with one fixed prefix per provider (one table covers Meta,
TikTok, Reddit, LinkedIn and OpenAI); a different account still refuses.

## Limits

- The command reads provider state, so it uses the credential store and the network;
  it is run only by the owner. Its tests use `httpx.MockTransport` and fixture
  credentials. The build session has not run it, and no provider is `LIVE_PILOTED`.
- A record cannot prove it came from a live run (see the pilot README). The label is
  as honest as the owner's commit.
- `ui_comparison` and `source_commit` are inputs the command cannot verify.
- Pilots use the operating-system credential store only; they never pass the CI flag.
- Labels are per provider, not per operation, and a record covers the read operations
  the plan runs. It says nothing about refresh, revocation or other accounts.

## Tests

`tests/test_pilot_records.py` (schema, plan, window, digest, closed strings),
`tests/test_release_labels.py` (no records means all `EXPERIMENTAL`; every rejection
state; independent providers; latest wins; no forged or configured label; discover,
doctor and envelope surfaces; `release_required` only for labelled, implemented
providers, with and without a GA4 record; refusals from the plan, execute and recover
groups; the exempt receipt list; wheel packaging) and `tests/test_pilot_run.py` and
`tests/test_pilot_run_providers.py` (all eight providers end to end against fake
providers; no identifier, name, token or payload in the record or output; only fixed
reads sent; every local refusal before any provider or credential; each failure stops
without a record; a record labels its provider only for this build).
`tests/conftest.py` withholds the shipped records from every test so the
suite does not depend on what the owner commits.
