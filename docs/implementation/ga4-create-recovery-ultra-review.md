# GA4 CREATE recovery-disposition independent review

This review examined the M22 `SubPlan` recovery exception in
`src/adhelm/safety.py`, its builder tests, and the public JSON schema. The change
adds one digest-bound disposition:
`RECONCILE_EXPORT_STOP_NO_REMOTE_COMPENSATION`. It is accepted only when a
`ga4_admin` `CREATE` subplan has an empty recovery-action list.

The independent regressions verify that the existing OpenAI pause action and
native challenge vectors remain byte-for-byte identical, including omission of
the new field. They exercise direct JSON, `model_copy`, and `model_construct`
attempts to use the disposition for another provider, another operation, or a
remote `DELETE` action. The kernel rejects each forged plan with
`INVALID_SAFETY_INPUT` before it can access a ledger, authorization, or adapter.

The review found that Pydantic `Literal` equality accepted `true` and `1.0` for
the existing `CompositePlan.schema_version`, and `1` and `1.0` for
`stop_on_non_verified`. Root added exact raw-type before validators for both
controls. The review tests cover those JSON and forged-model paths at the
kernel boundary. Valid historical plan bytes remain unchanged.

The installed schema exposes the same conditional shape: supplying the
disposition requires `ga4_admin`, `CREATE`, and zero recovery actions; omitting
it requires at least one recovery action. The result is not a recovery command,
does not add native approval support, and does not authorize a retry, pause,
delete, restore, or other remote compensation. A later mutation still needs a
new plan and authorization, and reconciliation preserves ambiguous outcomes.

Validation run offline:

```sh
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_recovery_disposition.py \
  tests/test_recovery_disposition_ultra_review.py \
  tests/test_safety_input_boundary.py \
  tests/test_safety_input_ultra_review.py \
  tests/test_approval_protocol.py \
  tests/test_native_approval_vectors.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check \
  src/adhelm/safety.py tests/test_recovery_disposition.py \
  tests/test_recovery_disposition_ultra_review.py
```

This is offline structural evidence only. It does not establish live GA4 key
event creation, provider-side recovery, or an executable GA4 approval path.
