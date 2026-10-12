# GA4 CREATE plan artifact ultra review

This independent fake-only review covers the retained GA4 key-event CREATE
plan artifact renderer, `artifacts ga4-key-event-plan`, and saved-rule artifact
generation after plan retention. It used temporary ledgers, fixture OAuth
records, in-memory sharing keys, and local temporary directories. It made no
provider, credential-store, browser, native-host, or remote calls.

## Findings repaired during review

The first renderer accepted `ONCE_PER_SESSION` in the static review even though
the retained GA4 CREATE candidate is bounded to `ONCE_PER_EVENT`. It now accepts
only `ONCE_PER_EVENT` and binds the artifact review's `reviewed_at` to the
historical `as_of` and retained `recorded_at`; an artifact therefore cannot
describe a retained plan using a different local review instant.

The original provenance conversion mixed a JSON timestamp from retained
history with a Python `datetime` derived from the action before canonical
serialization. It now derives the expiry only from the revalidated action,
serializes it as canonical UTC text, and reparses the complete closed
provenance shape. This keeps normal retained-source rendering deterministic
and rejects extra or mismatched source fields.

A valid `Ga4CreatePlanArtifactDocument` could be altered with nested
`model_copy()` calls and then serialize `execution_authority: true`. Raw JSON
also accepted numeric aliases such as `execution_authority: 0` and
`remote_mutations: 0.0`. The document now has a nonrecursive serializer
boundary that validates the complete emitted shape, and all authority,
recovery, and historical controls require exact booleans. The result is a
static historical file with no mutation command or authority claim.

## Independent coverage

`tests/test_ga4_create_plan_artifacts_ultra_review.py` adds 15 cases covering:

- nested constructed-document authority forgery and raw scalar aliases;
- a final source recheck at the shared writer handoff after zero-key preflight;
- partial second-format publication, completed-reference preservation, and
  private local-error redaction;
- rejection of a symlinked output destination before it can receive bytes;
- deterministic reuse, retained `recorded_at` historical timing, HMAC
  reference redaction, and JSON/CSV/HTML static-output constraints;
- malformed truthy replacement authorization before history or sharing-key
  access; and
- forged loader provenance before redaction/key access plus the explicit CLI
  partial-result envelope.

The existing focused suites cover the shared descriptor-relative writer,
version/reuse behavior, full document and output-envelope bounds before a
sharing key or file writer, saved-rule selection, root schema/discovery, and
default `ON_REQUEST` file-free behavior.

## Validation

The independent file passed 15 tests. The artifact library owner reports 19
focused library tests and the CLI owner reports 54 focused integration tests,
with scoped Ruff formatting/checks and source mypy passing. The independent
test file passed Ruff check and format verification before review freeze.

This evidence establishes only a locally rendered historical review of a
verified retained source supplied by its loader contract. It does not prove a
current remote GA4 state, provider create permission, authorization,
reservation, filesystem state after publication, or any execution capability.
