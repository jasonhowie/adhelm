# Meta Ads compiler ultra review

Reviewed: 2026-09-04

## Scope and evidence

This review covered the offline Meta Ads single-image traffic compiler in
`src/adhelm/providers/meta_compiler.py`, its builder fixtures, the added
adversarial regressions, and
`docs/research/meta-compiler-contract.md`. It used the pinned official
[Business SDK v26.0.0 release](https://github.com/facebook/facebook-python-business-sdk/releases/tag/26.0.0)
and its generated request definitions as field evidence. No SDK was installed,
no provider HTTP was sent, and no credential or customer data was read.

## Findings and remediation

| Severity | Finding | Regression and disposition |
| --- | --- | --- |
| P1 | A creative association accepted a resized rendition or a new rendition ID if a caller changed the manifest reference too. It did not require the sole rendered image to be the `HERO` rendition. | `test_identity_rendition_cannot_be_resized_reidentified_or_repurposed` requires exact asset dimensions, identity transformation digest, canonical rendition ID, and one `HERO` manifest reference. |
| P1 | The compiler required matching primary text and headline but not a complete CTA/destination component. A manifest could omit the CTA or carry an unserved alternate text component. | `test_manifest_must_exactly_match_the_fixed_copy_and_cta_surface` requires exactly the three native serving components, the exact `message`, `name`, and `call_to_action.type` slots, `LEARN_MORE`, and the canonical destination reference. |
| P1 | A serialized compiled result could be revalidated after changing native creative content, a zero lifetime cap, blank names, a naïve schedule, or an invalid account timezone. Fixed targeting also accepted zero or duplicate country/platform/position values. | `test_compiled_spec_revalidates_local_relationships_budget_and_unverified_statuses` and `test_native_requests_are_closed_paused_and_keep_schedule_and_budget_on_campaign` reparse forged payloads. Native request models now retain their bounds, schedule checks, nonblank names, exact singleton targeting, and an output creative-content digest binding. |
| P2 | A campaign/ad set/ad handle could collide with the derived creative handle. The compiler accepted a time-zone string that was not a usable IANA zone. | `test_role_handles_must_be_globally_unambiguous_and_timezone_must_be_iana` rejects both before compilation. |
| P2 | Image references allowed non-ASCII and DEL characters, which are unsafe opaque provider values for a later request boundary. | `test_image_reference_rejects_non_printable_or_non_ascii_provider_values` requires printable ASCII 33 through 126. |
| P2 | The review result stated a feed placement without saying whether the account can use it or whether Meta preserves the candidate creative unchanged. | The planning boundary and redacted review now mark placement eligibility and creative-transformation readback as required. The research contract makes the same limit explicit. |

## Retained limits

`compile_meta_ads_campaign` only produces a local
`OFFLINE_CANDIDATE_NOT_EXECUTABLE`; it cannot create, upload, authorize, or
activate a campaign. The generated SDK demonstrates outer request fields and
enums, but it does not prove a customer's Page, image, account, currency,
timezone, placement, policy approval, conversion setup, or live provider
response. The Facebook Feed nesting remains a candidate that requires guarded
API readback before a future write plan.

This fixture evidence does not establish native-ID binding, drift handling,
idempotency, recovery, provider lifecycle behavior, live delivery, release
evidence, or v1 completion.

## Validation

```text
.venv/bin/pytest -q tests/test_meta_compiler.py tests/test_meta_compiler_ultra_review.py tests/test_creative.py
# 23 passed

.venv/bin/ruff check src/adhelm/providers/meta_compiler.py tests/test_meta_compiler.py tests/test_meta_compiler_ultra_review.py
.venv/bin/ruff format --check src/adhelm/providers/meta_compiler.py tests/test_meta_compiler.py tests/test_meta_compiler_ultra_review.py
.venv/bin/mypy src/adhelm/providers/meta_compiler.py tests/test_meta_compiler.py tests/test_meta_compiler_ultra_review.py
# clean
```
