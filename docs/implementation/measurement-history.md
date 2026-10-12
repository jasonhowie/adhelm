# Retained measurement configuration reads

The development CLI provides `measurement ga4-admin <profile> <stream-id>` and
`measurement openai-conversions <profile>`, plus
`measurement gtm <profile> <container-id>`. Each explicitly selected read loads
its typed profile, discovers the expected account/property identity, and reads
only its fixed configuration collections. GA4 uses a distinct `ga4_admin` read
profile with exactly the analytics readonly scope and declared currency/timezone.
OpenAI uses an `openai_ads` read profile with declared currency/timezone; its
provider-side scope vocabulary remains unverified, so a local scope declaration
does not prove a restricted bearer key. CI credentials require `--ci-enabled`.
`schema measurement-ga4-read` and `schema measurement-openai-read` expose the
versioned request contracts; `schema measurement-gtm-read` describes the GTM
web-container read; `schema measurement-observation` exposes retained
configuration evidence.

After local argument/profile validation, GA4 acquires the private OS-stored
sharing key before provider requests and uses a separate HMAC context to
fingerprint configuration and event names. OpenAI's digest-only read receipt
needs no sharing key; later redacted resource review and exports do. Tokens,
raw bodies, event-name text, rule conditions, source
names/IDs, campaign association contents, and raw custom code are not retained
or returned. Native operational resource IDs remain in private normalized
history; agent review and exports replace them with keyed opaque references.

GTM requires exactly the readonly scope and numeric expected account/container
IDs. It validates the local request/profile before acquiring the 32-byte private
sharing key, snapshots one credential for the session, then proves the remote
account and web container before reading inventory. An absent optional profile
returns `NOT_CONFIGURED_OPTIONAL`; unrelated provider workflows continue.
Account/container feature availability remains TRUE, FALSE or UNKNOWN. GTM
version and workspace references include their account/container parents.

Only a complete bounded read can append `MEASUREMENT_OBSERVATION` to the existing
`changes` JSONL stream. A closed discriminated snapshot retains GA4 Admin
key-event/rule metadata, OpenAI standard conversion-setting metadata, or GTM
identity/version-count/workspace metadata, with an
exact content digest, selected profile, UTC read interval, provider identity,
and capability version. Rows are ordered by canonical reference before retention.
The ledger revalidates nested schemas, digests, unique resource membership,
parent/account bindings and envelope provenance both on append and verified read.
Every ledger record stays within the one-MiB limit. A complete GTM inventory
larger than one record is retained as bounded chunk records and a commit record
(see [GTM inventory](gtm-inventory.md#retention-beyond-one-record)); any other
oversized observation, and a chunk run the ledger cannot hold, fails retention
explicitly. It is never truncated into a false complete result.

Repeated reads append observations. Identical normalized snapshots preserve
their snapshot digest while their record digests and observed intervals differ.
The observations are sequential and non-atomic; they do not assert a provider
change time or actor. `occurred_at` and `run_id` remain null, source is `UNKNOWN`,
and automatic comparison/change attribution remains `NOT_EVALUATED` in this
slice. Canonical shape and hashes are integrity checks, not protection from a
same-user replacement of the entire ledger or independently signed proof that
a remote call occurred.

The read response is a compact digest/count receipt that fits the minimum
configured output bound. `measurement catalog` lists verified observations;
`measurement show <record-digest>` returns redacted identity, read interval,
capability metadata, provenance and bounded resource pages. `--cursor` and
`--limit` select rows without new provider calls. Saved row/byte limits apply;
pages shrink to fit, and an indivisible required review fails with an actionable
request to increase the byte limit. No mandatory metadata is silently removed.
An uncertain local append/fsync fails with `MEASUREMENT_RETENTION_UNVERIFIED` and
instructs the agent to inspect existing history before repeating the read.
Deterministic schema, selection and path rejections retain their actionable
diagnostics. The ledger completes short writes before fsync and fails on zero
progress; a partial write cannot be returned as a successful retained record.

GTM review includes two version-summary rows, individual HMAC-backed custom-code
markers, and sorted workspace-metadata rows. It never places raw HTML/template
source in history or review. Splitting code markers into individual rows permits
bounded pagination. Review preserves missing latest-header counter names,
sequential reread status, and feature availability. Workspace contents remain
`NOT_INSPECTED`, dependencies `NOT_PROVEN`, staging `NOT_EVALUATED`, and compiler
and browser evidence `NOT_PROVEN_BY_INVENTORY`. The version/workspace read does
not satisfy the full inventory or staging contract. Required metadata plus one
row may need a larger output bound than the compact read receipt.

The normal verified ledger index and JSONL/CSV export paths include these
observations. No separate CSV write path, background polling, artifact creation,
remote mutation, approval, browser request or event submission is introduced.
`CONFIGURATION_READ` never proves runtime firing, processed receipt, native
optimization association, or permission to change anything. Those fields remain
explicitly unevaluated. OpenAI's list-response contract is still fixture-based
and live-unverified; source documentation did not include a response example.

Tests exercise both the individual boundaries and complete command-to-parser-to-
ledger-to-review flows with synthetic HTTP and blocked real sockets. All guarded
measurement writes, browser/processed
verification, full lifecycle conformance and live release evidence remain open.
