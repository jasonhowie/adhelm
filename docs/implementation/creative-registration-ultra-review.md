# Creative registration ultra review

## Scope

This independent review covers the installed local registration workflow,
its sealed receipts, and its catalog/show projection boundary. It uses only
temporary local ledgers and fake failure seams. It does not call a provider,
credential store, browser, native approval host, or image vault outside the
workflow's ordinary text-only no-read assertion.

## Findings and verified behavior

The workflow must not trust a valid-shaped result returned by the durable
append helper. A forged result can name a real record digest from a different
creative version. The receipt path now reloads a verified checkpoint and
requires each exact closure payload plus the returned version record digest to
match before it emits a receipt. The regression verifies that a substituted
real digest is reported as `CREATIVE_REGISTRATION_UNVERIFIED` with exit code
6, rather than as a successful registration.

The multi-record append has no rollback boundary after its first child record.
An injected final-checkpoint failure after a durable child append is therefore
normalized to `CREATIVE_REGISTRATION_UNVERIFIED`, with the fixed
inspect-before-retry action. The private injected diagnostic is not exposed.

The public receipt models reject raw numeric aliases for false controls and
reject constructed receipts with an inconsistent source kind or a missing
required field at serialization. A text-only registration performs no image
vault read. Raw schema aliases fail before any checkpoint or vault operation.

Catalog and show are historical local views. Their output contains fixed
no-authority and no-currentness controls and does not disclose the family
identifier or label, destination, or exact creative text.

## Independent coverage

`tests/test_creative_registration_ultra_review.py` has six adversarial cases:

- a forged result cannot substitute another real retained version;
- a final ledger checkpoint failure after a durable child is ambiguous;
- malformed raw input reaches neither history nor the image vault;
- text-only registration never reads the image vault;
- raw and constructed receipt forgeries cannot serialize; and
- catalog/show stay redacted and explicitly historical.

The independent file passes all six cases. The combined registration, closure,
history, projection, CLI-workflow, and guidance group passes 54 cases. Ruff
check and format validation pass for the owned review tests and projection
source.

## Limits

The receipt proves only that the specified closed local graph was retained and
reloaded from a verified local checkpoint. It does not prove a provider
creative, deployment, serving state, permission, current vault availability,
or remote mutation authority.
