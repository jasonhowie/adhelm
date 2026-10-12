# OpenAI Ads compiler ultra review

Reviewed: 2026-09-04

## Scope and evidence

This review covered the offline compiler in
`src/adhelm/providers/openai_compiler.py`, its fixtures, and the write contract.
It used the current public [Campaigns](https://developers.openai.com/ads/api-reference/campaigns),
[Ad Groups](https://developers.openai.com/ads/api-reference/ad-groups),
[Ads](https://developers.openai.com/ads/api-reference/ads),
[Files](https://developers.openai.com/ads/api-reference/files), and
[Campaign Targeting](https://developers.openai.com/ads/campaign-targeting)
documentation. Context7 did not contain a matching Ads corpus. No provider HTTP,
credentials, customer data, or live account were used.

## Findings and remediation

| Severity | Finding | Regression and disposition |
| --- | --- | --- |
| P1 | Campaign targeting emitted `targeting.locations` as an array. The documented create body requires `targeting.locations.include`. | `test_campaign_targeting_emits_the_documented_locations_include_object` asserts the complete nested object. The compiler now emits the documented shape. |
| P1 | A caller could pair any currency exponent with `USD`, causing the native micros cap and review/authorization minor units to disagree by 100×. | `test_revalidation_rejects_usd_minor_unit_under_or_overstatement` covers exponents 0, 1, 3, and 6. The compiler now permits only the reviewed USD exponent of 2 and rejects other currencies. |
| P1 | `verification_state="VERIFIED"` plus an arbitrary digest was a self-attestation, not account-bound file evidence. The guessed `file_` grammar also rejected opaque provider-issued IDs. | `test_file_reference_needs_evidence_and_cannot_self_attest` and `test_file_reference_must_bind_the_same_account_and_an_opaque_id_is_preserved` require a bounded local evidence reference, account binding, and matching local asset digest while preserving opaque IDs. The result explicitly remains an offline candidate. |
| P1 | A chat-card had only a compiler-local content digest, not a canonical creative family/version/rendition/asset association. An allowed destination could change while retaining the old canonical creative version. | `test_creative_version_asset_and_rendition_are_bound_to_the_candidate`, `test_creative_content_must_match_the_declared_canonical_version`, and `test_destination_cannot_change_without_a_new_canonical_creative_version` require a matching `CreativeVersion`, asset, rendition, source digest, title/body components, and deterministic destination reference. |
| P2 | Pydantic `model_copy` and `model_construct` can bypass model validators before a public compiler call. A forged reversed schedule or invalid hierarchy could therefore be compiled. | `test_revalidation_rejects_a_schedule_forged_with_model_copy` verifies compiler-boundary revalidation. |

## Retained limits

The compiler is **not a write-capable plan**. Every result is
`OFFLINE_CANDIDATE_NOT_EXECUTABLE`; it requires verified account identity,
creative-ledger lookup, account-bound file readback, parent-ID binding, drift
checks, durable authorization, apply/verification, and recovery before egress.
The public sources establish request fields and hierarchy, not customer account
eligibility, file readback semantics, idempotency behavior, mutation responses,
or live lifecycle success. The source status therefore remains fixture-only and
cannot support a release or provider-write claim.

## Validation

```text
uv run pytest -q tests/test_openai_compiler.py tests/test_openai_compiler_ultra_review.py
# 19 passed

uv run ruff check src/adhelm/providers/openai_compiler.py tests/test_openai_compiler.py tests/test_openai_compiler_ultra_review.py
uv run ruff format --check src/adhelm/providers/openai_compiler.py tests/test_openai_compiler.py tests/test_openai_compiler_ultra_review.py
uv run mypy src/adhelm/providers/openai_compiler.py tests/test_openai_compiler.py tests/test_openai_compiler_ultra_review.py
# clean
```
