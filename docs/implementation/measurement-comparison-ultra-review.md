# Measurement comparison ultra review

This independent review covered the frozen retained-measurement comparator,
its closed CLI result path, and the installed comparison schemas. It used only
synthetic normalized GA4 and GTM records in a temporary local ledger. It made
no provider, network, credential-store, sharing-key, native-host, or mutation
call.

The review exercised the comparison boundary as historical evidence rather
than remote reconciliation. It confirms that the two selected records come
from one checkpoint, must have a matching profile and provider scope, and use
nonoverlapping read intervals. The result binds the outer interval, keeps the
actor `UNKNOWN`, leaves exact change time null, and treats an absence as
`OBSERVED_ABSENT_FROM_LIST` rather than deletion.

During review, a self-consistent rehashed GA4 source could replace the
canonical JSON string for a Decimal default value with an integer-looking JSON
number. Canonicalization could then normalize that source before Pydantic
validation. The frozen comparator now rejects a non-string GA4
`default_value.numeric_value` before normalization; the independent regression
covers both integer and float forms.

The independent tests in
`tests/test_measurement_comparison_ultra_review.py` cover:

- HMAC-rotation-shaped GA4 fingerprint differences, including the required
  `NOT_EVIDENCED` key-continuity caveat and
  `NORMALIZED_OBSERVATION_DIFFERENCE` fact basis;
- deterministic, read-only repeated comparison against an unchanged verified
  checkpoint;
- GTM opaque-marker absence as an observation, while retaining GTM coverage,
  dependency, and sequential-read caveats without source code fields;
- bounded agent-facing CLI redaction of GTM native references and HMAC-backed
  markers; and
- self-consistent rehashed GA4 Decimal numeric forms rejected before model
  normalization.

The focused independent suite passed 5 tests. The broader comparison,
measurement-history, CLI, GA4, GTM, and OpenAI focused suite passed 88 tests.
Ruff check, Ruff format check, and source-only mypy passed for the reviewed
comparison and CLI modules. This review does not establish live provider
conformance, stable sharing-key continuity, remote actor or timestamp,
deletion, browser/runtime firing, processed receipt, native optimization
association, provider execution, or v1 completion.
