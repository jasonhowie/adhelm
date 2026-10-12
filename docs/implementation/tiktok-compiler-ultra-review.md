# TikTok compiler ultra review

Reviewed: 2026-09-04
Status: **IMPLEMENTING / UNVERIFIED**

## Scope and evidence

This review covered the offline TikTok v1.3 traffic/video compiler in
`src/adhelm/providers/tiktok_compiler.py`, its fixture tests, the adversarial
regressions in `tests/test_tiktok_compiler_ultra_review.py`, and the declared
video boundary in `src/adhelm/video_creative.py`. It used the current official
v1.3 ad-group reference plus TikTok-maintained route and schema references for
regular ad create, regular creative fields, identities, and video assets.

No TikTok SDK was installed. No customer credentials, account data, provider
HTTP, upload, preview, mutation, activation, or live eligibility check was
performed.

## Findings and remediation

| Severity | Finding | Regression and disposition |
| --- | --- | --- |
| P1 | The candidate used a bare minor-unit budget and called the native total-budget field `budget_minor_units`, although the v1.3 ad-group contract defines a major-unit numeric `budget`; it also retained Python datetimes instead of documented UTC timestamp strings. | The brief now requires positive account-matching USD `Money`; the compiler derives a two-decimal `Decimal` major-unit total and emits `YYYY-MM-DD HH:MM:SS` UTC strings. The native fragment rejects wrong decimal scale, legacy fields, and malformed timestamps. `test_emits_v13_major_unit_total_budget_utc_times_and_video_field_fragment` and `test_rejects_microseconds_documented_date_ceiling_names_and_non_native_budget_forms` cover the correction. |
| P1 | An image-only `CreativeAssetVersion` and image rendition were presented as the serving video, so a PNG digest could be bound to a TikTok `video_id`. | The compiler now requires a direct declared VIDEO asset and derived VIDEO rendition in the manifest. `test_closed_association_cannot_accept_an_image_placeholder_or_extra_native_fields` and `tests/test_video_creative.py` reject image substitution and prove the content/dimension/duration identity chain. |
| P1 | The local creative fragment used `VIDEO` without an explicit identity type and did not distinguish a reviewed native field fragment from a complete request. | It now fixes `CUSTOMIZED_USER`, `SINGLE_VIDEO`, `LEARN_MORE`, and `DISABLE`, but documents that the fragment has no advertiser/ad-group provider IDs or `creatives` wrapper. It is not executable. |
| P1 | Serialized output did not retain enough source data to revalidate the controlled destination, video identity chain, account/evidence relationship, exact creative association, or input binding after `model_copy`. | The output now retains a closed source brief, destination policy/ref, declared video IDs, and reference/input digests. Revalidation checks each projection before accepting serialized output. `test_reparse_rejects_forged_native_destination_reference_and_video_graph` covers native destination, reference-digest, and declared-video forgery. |
| P2 | Native names accepted controls and emoji, local handles were not restated in the serialized graph, and a schedule with microseconds had no exact native representation. | Names and refs are printable/closed, names reject symbol/emoji characters, handles are pattern-bound in output, and schedule times must be whole UTC seconds. |

## Retained boundaries

The declared video objects are declarations, not decoded or registered assets.
They say that frame, palette, OCR, transcript, audio, and component-performance
facts are unavailable. An evidence-reference change does not mean media bytes
changed. No component-level performance claim is made.

The resulting graph remains a local, paused, non-executable candidate. Local
handles are not provider IDs, and the native models are field fragments rather
than request bodies. A future guarded executor must verify canonical video
history, account/currency/identity, video and identity readiness, current
schedule and budget eligibility, objective/targeting/placement, policy,
destination/tracking, conversion settings, transformations, drift, and
authorization before constructing a full provider request. Fixture validation
does not prove field acceptance, provider policy approval, conversion behavior,
delivery, reporting, recovery, or release completion.

## Sources

- [TikTok v1.3 ad-group create reference](https://business-api.tiktok.com/gateway/docs/index?doc_id=1739499616346114)
- [TikTok-maintained v1.3 Ad API route list](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/AdApi.md)
- [TikTok-maintained regular Ad Create body](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/AdCreateBody.md)
- [TikTok-maintained regular creative fields](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/AdcreateCreatives.md)
- [TikTok-maintained identity API reference](https://github.com/tiktok/tiktok-business-api-sdk/blob/main/python_sdk/docs/IdentityApi.md)

## Validation

```text
uv run pytest tests/test_tiktok_compiler.py tests/test_tiktok_compiler_ultra_review.py tests/test_video_creative.py -q
uv run ruff check src/adhelm/video_creative.py src/adhelm/providers/tiktok_compiler.py tests/test_video_creative.py tests/test_tiktok_compiler.py tests/test_tiktok_compiler_ultra_review.py
uv run ruff format --check src/adhelm/video_creative.py src/adhelm/providers/tiktok_compiler.py tests/test_video_creative.py tests/test_tiktok_compiler.py tests/test_tiktok_compiler_ultra_review.py
uv run mypy src/adhelm/video_creative.py src/adhelm/providers/tiktok_compiler.py
```
