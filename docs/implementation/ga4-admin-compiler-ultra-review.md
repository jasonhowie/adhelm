# GA4 Admin key-event compiler ultra review

Reviewed: 2026-09-04

## Scope and source check

This review covered the offline-only GA4 Admin key-event compiler, its baseline
fixtures, its public-input boundary, and the local write-contract note. It used
only local models and fixture tests; it made no provider request and accessed no
credential.

The official [KeyEvent resource](https://developers.google.com/analytics/devguides/config/admin/v1/rest/v1alpha/properties.keyEvents)
documents the fixed property-scoped create surface and its writable
`eventName`, required `countingMethod`, and optional `defaultValue` fields. The
official [event collection limits](https://support.google.com/analytics/answer/9267744?hl=en)
set a 40-character event-name limit and state that an overlong marked key event
will not be reported as a key event.

## Findings fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | The generic `EventContract` permits 80-character event names, 64-character field names, and an unbounded field tuple, while GA4 caps event/parameter names at 40 characters and events at 25 parameters. | The compiler now enforces the documented 40-character lower-ASCII subset and 25-field ceiling when building input and the request/candidate. Revalidation catches a forged `model_copy()` value too. |
| P1 | `model_copy()` or `model_construct()` could bypass the compiler input validators, allowing malformed native IDs, unsafe event names, or a nonexact numeric value into a candidate. | The public compiler reconstructs the full strict input before invoking the reference mapper, making a digest, or naming the fixed future target. |
| P1 | A candidate could target property `123` while carrying unrelated local property or Web-stream labels. | The compiler binds both references to their native IDs through a configured reference function. Its default matches the GA4 Admin reader, while a caller can supply the same configured local mapping used by discovery. |
| P2 | `Decimal('-0')` produced a wire `-0.0` and a different input/candidate digest even though canonical JSON encodes negative and positive zero identically. Cosmetic exact Decimal scales such as `1` and `1.0` also produced distinct candidates for the same API number. | Signed zero is rejected. Accepted exact binary64 values are normalized to their one binary64 Decimal representation before digesting, so equivalent exact forms have identical request, input digest, and candidate digest. |
| P1 | Concatenated sensitive labels such as `emailreceived` and `emailaddress` bypassed the prior underscore-token screen. Literal person-level text in free-form event semantics could be accepted before hashing. | The compiler screens each event and declared-field name for its bounded sensitive vocabulary and uses the central literal-data screen on semantic text before candidate creation. This does not claim to classify all business semantics. |

## Verified boundary

- The output is still a fixed, property-scoped `POST` candidate with only the
  documented writable body fields. It has no transport, credentials, approval,
  ledger append, mutation, browser action, or provider readback capability.
- Every compiler-produced candidate remains explicitly
  `OFFLINE_CANDIDATE_NOT_EXECUTABLE`, marks the live configuration effect as
  future-only, requires a later source-event collection/reserved-name check,
  and denies native conversion mapping and receipt/browser proof. The KeyEvent
  resource itself gives `click` as an example and can represent default GA
  events, so an offline compiler must not misclassify all collection-reserved
  names as invalid custom events.
- The Web-stream declaration is retained only for later verified identity and
  verification work; it is not invented into the property-scoped Admin path.
- Candidate output contains references, field names, and digests rather than
  free-form semantic definitions or raw person-level data.

## Remaining limits

This is an offline candidate compiler, not a GA4 Admin edit workflow. A later
write requires a verified current property/Web-stream session, complete
key-event inventory and duplicate/drift checks, a compiled executable plan,
direct or delegated exact authorization, a durable pre-egress journal,
configuration readback, and separate browser, Realtime, and processed-report
evidence. It cannot establish a Google Ads conversion or another provider's
native optimization association.

## Validation

```text
.venv/bin/pytest -q tests/test_ga4_admin_compiler.py tests/test_ga4_admin_compiler_ultra_review.py tests/test_ga4_admin.py tests/test_ga4_admin_ultra_review.py
# 27 passed
.venv/bin/ruff format --check src/adhelm/providers/ga4_admin_compiler.py tests/test_ga4_admin_compiler.py tests/test_ga4_admin_compiler_ultra_review.py
.venv/bin/ruff check src/adhelm/providers/ga4_admin_compiler.py tests/test_ga4_admin_compiler.py tests/test_ga4_admin_compiler_ultra_review.py
.venv/bin/mypy src/adhelm/providers/ga4_admin_compiler.py
# clean
```
