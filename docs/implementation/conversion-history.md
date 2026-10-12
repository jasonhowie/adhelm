# Retained customer conversion definitions

The development CLI exposes `conversion register --input <definition.json>`,
`conversion catalog`, and `conversion show <record-digest>`. The installed
`conversion-definition` and `conversion-contract` schemas describe the input
and compiled wrapper. They implement the local declaration boundary described
in [conversion contracts](conversion-contracts.md).

Registration reads bounded JSON, validates the complete closed definition, and
compiles its canonical content digest. Missing required outcome/source/
qualification/counting meaning returns `USER_DEFINITION_REQUIRED`. Registration
does not acquire a provider credential or sharing key, inspect configured
sources, call a provider, emit events, or create an executable plan.

A `CONVERSION_CONTRACT` in the canonical `changes` stream retains the exact
definition, digest, fixed `CUSTOMER_DECLARED` provenance, and false execution
authority. Its envelope records the local registration action without a
provider, account, or run. Statements describe the customer's intended meaning;
they are not proof that a source implements it, that classification is accurate,
or that a consent policy is valid.

For each contract ID, the first retained version must be one with no predecessor.
Every later registration must increment the latest version by one, name that
version's exact content digest, and use a strictly later UTC effective time.
The ledger validates this chain under its append lock and on verified scans.
Concurrent attempts to register the same next version cannot fork the chain.
Older records remain unchanged. Future effective times are permitted and do
not activate anything; no effective-time scheduling is introduced. A changed
definition must be explicitly registered as a new version before future mappings
can use it. Mapping/report linkage is separate implementation work.

The minimum-size registration receipt contains digests, version, and local
retention/no-authority facts. Catalog and show inspect verified local history;
identifiers and destination profile references become keyed opaque references
using the OS-stored sharing key. Empty catalogs need no key. Show preserves the
canonical content and predecessor digests but labels the definition as a
redacted projection; its displayed fields cannot be rehashed as the original.
Bounded declaration statements remain visible after schema and sensitive-data
screening because they explain the customer's intended conversion meaning.

Review uses deterministic field order and paginated whole fields, preserving
provenance, declaration origin, and no-authority facts on every page. Saved row
and byte bounds apply. If one whole field plus mandatory review metadata does
not fit, the command asks for a larger byte bound rather than dropping part of
the definition. Catalog pagination likewise never silently omits versions.

Uncertain append/fsync returns `CONVERSION_RETENTION_UNVERIFIED` and directs the
agent to history verification and the catalog before retrying. Deterministic
schema and version conflicts retain their specific diagnostics. Normal verified
JSONL/CSV projection includes the definition without a second write path.

Fixtures cover version succession, concurrent registration, rehashed parent
tampering, provenance rejection, bounded local review, reference redaction,
missing meaning, and uncertain local retention. They do not establish real
credential-store behavior, source semantics, native conversion configuration,
CRM attribution, browser/receipt verification, legal approval, or execution
authority.
