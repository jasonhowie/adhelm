# Native approval bridge protocol ultra review

## Scope

This independent M32 review covers only the bounded canonical request and
candidate wire parsers in the Core and Python private protocol modules, plus
their shared synthetic fixture. It exercises fake ephemeral P-256 keys and
fixed retained-plan challenge fixtures.

It does not launch a helper, open a pipe or XPC connection, access Security,
Keychain, LocalAuthentication, an installed host, a ledger, a provider, or a
receipt-redemption store. The candidate remains non-authorizing evidence; this
review makes no claim that nonce redemption, current-key trust, execution, or
Python acceptance occurred.

## Required boundaries

- A request has exactly seven fields and a candidate exactly 19, with canonical
  JSON/base64url, strict scalar types, direct retained-plan kinds only, and
  the fixed 98,304/131,072-byte caps.
- A candidate must bind one request ID digest, request digest, source-context
  digest, full metadata digest, protocol-seven digest, canonical expected
  challenge, nonce digest, receipt facts, X9.63 key fingerprint, and receipt
  signature.
- The exchange-start clock must revalidate the exact challenge. Native
  observation must be six-fraction UTC and satisfy
  \`issued_at <= observed_at < expiry\` and
  \`exchange_started_at <= observed_at <= current_now\`; both parsers reject
  clock rollback and \`current_now >= expiry\`.
- Private request/candidate values must not become Codable/serializable
  transport, raw-payload output, a trust object, an execution permit, or a
  provider grant.

## Findings repaired before freeze

- Core request construction originally let a primitive or a policy challenge
  become a typed bridge request. It now requires a canonical direct-plan
  object before issuing that value; complete challenge validation still happens
  at the trusted exchange start.
- The Core product source exposed a package-level fixture candidate producer.
  It was moved into the test target, where it immediately feeds the production
  decoder. Product code now has no candidate constructor that can simulate
  nonce consumption.
- Python exposed raw candidate receipt/challenge extractors. They were removed;
  a future consumer must use context-bound revalidation rather than obtain raw
  private slots.
- Python now re-decodes the exact challenge at the exchange-start sample,
  matching Core's issuance-skew check. Core now also rejects a current-time
  sample at or after receipt expiry, matching Python.
- The independent native duplicate-key test initially appended escaped quote
  characters and therefore exercised malformed JSON. It now constructs an
  actual second unescaped \`"schema_version"\` key, asserts that construction,
  and verifies decoder rejection.

## Independent coverage and result

The independent Swift class adds seven fake-only cases for pre-typed primitive
and policy rejection; full request/source/challenge/static-digest correlation;
replacement-key/fingerprint and X9.63-prefix attacks; exchange/expiry bounds;
canonical duplicate/trailing/scalar/signature attacks; wire caps; and
non-Codable/redacted surfaces.

The independent Python module adds eight fake-only cases for both direct source
kinds, strict raw scalar/duplicate/noncanonical frames, source and metadata
correlation, signature/key checks, exchange/expiry boundaries, constructed
object revalidation, caps, and opaque non-authority behavior.

\`\`\`sh
env SWIFTPM_MODULECACHE_OVERRIDE=/private/tmp/adhelm-m32-ultra-module-cache \\
  CLANG_MODULE_CACHE_PATH=/private/tmp/adhelm-m32-ultra-module-cache \\
  swift test --package-path native/macos-approval-host \\
    --enable-xctest --disable-swift-testing \\
    --scratch-path /private/tmp/adhelm-m32-ultra-build \\
    --filter 'NativeApprovalBridgeProtocol(Tests|UltraReviewTests)'
# 16 passed: 9 base + 7 independent

UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \\
  tests/test_native_approval_bridge_protocol.py \\
  tests/test_native_approval_bridge_protocol_ultra_review.py
# 41 passed: 33 base + 8 independent
\`\`\`

\`ruff format --check\`, \`ruff check\`, \`swift-format lint --strict\`, and
\`git diff --check\` passed for the independent review files.

## Limits retained by the contract

This is a parser and evidence-shape contract. It has no subprocess or pipe
transport, no host identity/current-key resolution, no receipt redemption,
no XPC endpoint, no protected-store durability claim, no ledger reload, and no
provider or Python execution authority. Those boundaries require separate,
context-bound implementation and review.
