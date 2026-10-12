# Python/native approval protocol ultra review

Root independently reviewed the Terra-built Python bridge and generated four
actual Swift CryptoKit signatures for the two timestamp variants of the pause
challenge, policy installation and policy revocation. The generator uses a
fresh software P-256 key in memory and exports only synthetic public material.
It neither imports nor invokes Keychain or OS authentication.

The first cross-language run reproduced a material mismatch: Python review
facts used each state reference's raw fingerprint, while the native review
hashed the complete canonical state reference, including its revision. The
Python calculation now matches that native contract. All four checked-in
Swift receipts verify in Python and their complete canonical review bytes and
digests agree. Wrong pins reject the same actual native signatures.

Reproduction sources are `scripts/GenerateApprovalReceiptVectors.swift`,
`tests/fixtures/swift-approval-receipts-v1.json`, and
`tests/test_approval_protocol_ultra_review.py`. Compile the generator against
the checked-in ApprovalHostCore module and pass its synthetic challenge fixture
directory. Generated signatures are intentionally fresh; the private key is
never saved. Tests consume the checked-in public vectors without regenerating
keys or invoking the native host.

The broader protocol suite covers closed scalar types, canonical JSON,
duplicate keys, nonce/signature padding, lexical timestamps, domain separation,
exact challenge/subject/configuration bindings and receipt expiry without
reapplying the initial request-age window after user review. None of this proves
that the software test key was enrolled or that a user authenticated. The
installed pin and revocation resolver remains a required integration boundary.
