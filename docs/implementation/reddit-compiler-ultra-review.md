# Reddit compiler ultra review

Reviewed: 2026-09-04
Status: **IMPLEMENTING / FIXTURE-ONLY**

## Scope and evidence

This review covered the offline Reddit Ads v3 standard-traffic compiler in
`src/adhelm/providers/reddit_compiler.py`, its builder fixtures, the added
adversarial regressions in `tests/test_reddit_compiler_ultra_review.py`, and
the narrow contract in `docs/research/reddit-compiler-contract.md`.

It checked the public [Standard campaign guide](https://ads-api.reddit.com/docs/v3/guides/programs/campaign/campaign-setup),
[Create Campaign](https://ads-api.reddit.com/docs/v3/api/create-campaign),
[Create Ad Group](https://ads-api.reddit.com/docs/v3/api/create-ad-group),
[Create Ad](https://ads-api.reddit.com/docs/v3/api/create-ad), and current
[API history](https://ads-api.reddit.com/docs/v3/history). The raw OpenAPI
document was not independently parsed in this review, so the contract does not
claim live-wire schema verification beyond those official v3 references.

No SDK was installed. No credential, customer account, provider HTTP, upload,
mutation, preview, or activation was used.

## Findings and remediation

| Severity | Finding | Regression and disposition |
| --- | --- | --- |
| P1 | A serialized candidate accepted an ad-group schedule that differed from its CBO campaign while each individual range remained valid. | Native campaign and ad-group timestamps now require UTC offsets, and compiled-graph validation requires identical start and end values. `test_serialized_candidate_rejects_divergent_schedule_and_recomputed_destination` reproduces the former acceptance. |
| P1 | A serialized candidate could replace `click_url` and recompute its local content digest because it did not retain the canonical creative or destination policy needed to prove the relationship. | The compiled graph now retains the local creative and destination policy, repeats allowlist validation, and checks the native post and click URL against the canonical creative. The regression changes an otherwise permitted URL and recomputes the digest; revalidation fails. |
| P1 | The current v3 API history identifies `funding_instrument_id` as required for all campaigns, but the offline native campaign model omitted it. | The brief now requires account-bound local funding evidence, and the campaign candidate requires the same opaque issued ID. The planning boundary requires funding-instrument readback. The current Standard CBO example omits that field, so this remains a documented contract discrepancy requiring future live readback rather than a live compatibility claim. |
| P1 | The native and brief name fields allowed 512 characters, while the current v3 API history documents a 500-character maximum shared by campaign, ad-group, and ad names. | Every name field now has the documented 500-character ceiling. The regression uses `model_copy` to ensure compiler revalidation rejects 501-character names. |
| P1 | Pixel IDs were constrained to guessed provider prefixes and neither pixel nor funding evidence was bound to the declared account through the serialized graph. | Pixel and funding references use bounded printable opaque IDs without inventing a provider grammar, carry local evidence, and must match the account plus both native fields. The regressions cover `model_copy`, `model_construct`, serialized account substitution, unsupported identifier characters, and missing funding. |
| P2 | The closed targeting and review boundary needed explicit regression evidence after adding the retained graph. | A forged audience field fails through the strict native targeting model. The review projection is checked to exclude post, pixel, funding, evidence, destination, asset digest, and headline text. |

## Retained limits

The result remains a local `OFFLINE_CANDIDATE_NOT_EXECUTABLE` with
`FIXTURE_ONLY_NOT_LIVE_VALIDATED`. Local evidence is not proof that the account
owns or can use the pixel, funding instrument, post, media, placement,
destination, or creative. The compiler does not establish allocation,
eligibility, policy approval, post readiness, provider transformations,
provider-native ID binding, remote response behavior, delivery, spend
enforcement, pixel receipt, authorization, drift handling, recovery, or
release readiness.

The official v3 docs also announce upcoming objective-enum changes. Any future
executor must re-read the installed contract and live provider schema before
constructing a request. This fixture review does not establish v1 or release
completion.

## Validation

```text
.venv/bin/pytest -q tests/test_reddit_compiler.py tests/test_reddit_compiler_ultra_review.py
# 13 passed in 0.13s

.venv/bin/ruff check src/adhelm/providers/reddit_compiler.py tests/test_reddit_compiler.py tests/test_reddit_compiler_ultra_review.py
.venv/bin/ruff format --check src/adhelm/providers/reddit_compiler.py tests/test_reddit_compiler.py tests/test_reddit_compiler_ultra_review.py
.venv/bin/mypy src/adhelm/providers/reddit_compiler.py tests/test_reddit_compiler.py tests/test_reddit_compiler_ultra_review.py
# Success: no issues found in 3 source files
```
