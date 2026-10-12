# Privacy-screen performance ultra review

Reviewed: 2026-09-04

## Scope

This hermetic review covers only the shared email-shaped sensitive-text screen
in `src/adhelm/privacy.py`. It used synthetic strings and no network, provider,
credential, customer-data, or live operation.

## Finding fixed

| Severity | Finding | Disposition |
| --- | --- | --- |
| P1 | The email expression retried a failed local-part match at every character in a long non-whitespace, non-`@` run. A synthetic 600,000-character no-`@` string and a late-`@` string without a valid domain dot therefore had quadratic scan behavior. | The expression now attempts a match only at the start of each whitespace/`@`-delimited run. A legacy successful match that begins within a run also succeeds from that run's start because the local-part already accepts the preceding characters. This preserves the existing broad email shape while bounding candidate attempts by runs rather than characters. |

## Preserved behavior

- NFKC normalization still happens before sensitive-text screening.
- The short equivalence corpus checks ordinary matches, malformed nonmatches,
  multiple-`@` input, a domain run whose usable dot is not its first character,
  Unicode whitespace, and fullwidth normalized email punctuation against the
  former expression.
- Existing synthetic email, bearer-token, and phone fixtures still fail with
  `SENSITIVE_DATA_RISK`.
- `_SECRET`, `_PHONE`, closed-key screening, and all collection policy remain
  unchanged. This is not a semantic privacy redesign or a claim that lexical
  screening proves arbitrary customer prose is nonsensitive.

## Validation

```text
.venv/bin/pytest -q tests/test_privacy_performance_ultra_review.py tests/test_core.py
# 30 passed
.venv/bin/ruff format --check src/adhelm/privacy.py tests/test_privacy_performance_ultra_review.py
.venv/bin/ruff check src/adhelm/privacy.py tests/test_privacy_performance_ultra_review.py
.venv/bin/mypy src/adhelm/privacy.py tests/test_privacy_performance_ultra_review.py
# clean
```

The independent regression runs each 600,000-character hostile no-match shape
in a subprocess with a 10-second deadline. It asserts completion and does not
use a fragile elapsed-time threshold.
