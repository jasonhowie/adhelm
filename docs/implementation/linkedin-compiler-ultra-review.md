# LinkedIn compiler ultra review

Reviewed: 2026-09-04

## Scope

This independent review covered the fixture-only LinkedIn Marketing API 202608
existing-dark-single-image-post compiler and its owned tests and contract note.
It used public Microsoft Learn documentation only. No credentials, SDK,
provider call, provider write, customer data, media upload, post creation, or
live validation was used.

## Findings fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | The closed targeting models used aliases for documented `and`, `or`, and targeting-facet keys, but their default serialization emitted internal names such as `all_of` and `location_or`. A serialized candidate could not strictly reparse as the native graph. | Native wire models now serialize aliases by default. The regression checks exact nested keys, strict native reparse, and rejection of the internal alias. |
| P1 | `campaignGroupLocalHandle` and `campaignLocalHandle` were invented fields inside classes presented as native request bodies. LinkedIn's documented campaign and creative bodies require real `campaignGroup` and `campaign` URNs. | Complete native payload models now require the corresponding provider URN and reject local substitutes. The offline candidate instead carries explicit local-parent templates marked `REQUIRES_CREATE_READBACK_AND_REVALIDATION`; no synthetic native identifier is produced. |
| P1 | The dark-post declaration lacked the DSC account, `isDsc`, and single-image DSC type, and the compiled content digest did not bind the output post reference, Page author, or account to the creative graph. | The declared post now requires a matching DSC account, `isDsc: true`, `STANDARD` DSC type, active status, and no-feed distribution. The compiler binds account, Page author, and post reference into the creative-content digest; post, Page, or account substitution fails strict output revalidation. |
| P1 | The brief represented group total, daily budget, and unit cost as untyped integer minor units, so a nested budget had no ISO currency declaration. | All three inputs are now positive bounded `Money` values and must use the declared account currency. The compiler converts only their minor units to native decimal strings; model-copy and model-construct regressions reject currency or zero-value bypasses. |
| P2 | Fractional-millisecond input times were silently truncated by timestamp conversion, and a rehashed output could set a campaign daily budget above the group total cap. | Inputs require millisecond-aligned non-pre-epoch times, epochs use exact `timedelta` arithmetic, group and campaign schedules must match, and output revalidation enforces daily budget no greater than the group total. |

## Verified boundary

- The compiler emits only a DRAFT group native body and two non-egress local
  templates. A future executor must create and read back each parent resource,
  bind the returned URN to the complete native model, revalidate, reject drift,
  and obtain exact authorization before any write.
- The group `totalBudget` is the only compiled maximum exposure. Campaign daily
  budget and CPC unit cost are constrained fields, not an inferred lifetime
  cap.
- The campaign is fixed to US/en, `ENGAGEMENT`, `SPONSORED_UPDATES`, `CPC`,
  `NONE` optimization, disabled audience expansion/CTV/offsite delivery, and
  `NOT_POLITICAL`; its targeting serializes the documented `and` / `or` facet
  shape.
- The creative uses only `content.reference` to the existing `ugcPost` or
  `share` URN. It has no `inlineContent`, new-post, upload, CTA, or destination
  field. The local creative, asset, identity rendition, post declaration, and
  no-CTA/no-destination facts are canonically bound.
- `model_copy()`, `model_construct()`, ordinary serialized reparse, native
  payload reparse, nested targeting aliases, parent-handle substitutions,
  account/Page/post substitutions, overspend, fractional milliseconds, and
  sensitive fixture prose have regression coverage.

## Limits retained deliberately

- This is an offline candidate compiler, not proof that a group, campaign,
  creative, post, Page, image, target, locale, budget, or format is currently
  accepted or servable by LinkedIn.
- It does not prove account/Page roles or scopes, account currency, group
  campaign-type eligibility, DSC/post/image ownership, audience minimum,
  targeting eligibility, final format/rendering, creative ledger state, native
  conversion state, authorization, drift status, API response shape, or
  recovery behavior. Those must fail closed in a separately guarded workflow.
- NFKC/lexical privacy screening blocks known sensitive text and fields but
  cannot establish that arbitrary customer-provided creative meaning is
  nonsensitive. No raw provider post or image response is retained or fetched.

## Validation

```text
.venv/bin/pytest -q tests/test_linkedin_compiler.py tests/test_linkedin_compiler_ultra_review.py
# 9 passed
.venv/bin/ruff format --check src/adhelm/providers/linkedin_compiler.py \
  tests/test_linkedin_compiler.py tests/test_linkedin_compiler_ultra_review.py
.venv/bin/ruff check src/adhelm/providers/linkedin_compiler.py \
  tests/test_linkedin_compiler.py tests/test_linkedin_compiler_ultra_review.py
.venv/bin/mypy src/adhelm/providers/linkedin_compiler.py \
  tests/test_linkedin_compiler.py tests/test_linkedin_compiler_ultra_review.py
# clean
```

The public source basis is the versioning, campaign-group, campaign, creative,
Post Schema, and Image Ads documentation linked in the compiler contract.
