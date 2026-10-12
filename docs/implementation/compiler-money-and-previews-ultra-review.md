# Compiler money and offline-preview ultra review

Reviewed 2026-09-04 against the offline OpenAI, Meta, Google Ads, LinkedIn,
Reddit, and TikTok compiler contracts, plus the fixed LinkedIn/Reddit/TikTok
preview commands. This review used local fixtures only; it made no provider
requests, read no credentials, and retained no candidate records.

The review found and fixed an OpenAI compiled-spec reparse gap. A rehashed
serialized spec could previously disconnect its typed `maximum_spend` from the
native lifetime-micros cap, and could replace the declared account currency.
The compiled model now fixes USD and exponent 2, and verifies the typed minor
units, activation boundary, and native micros are exactly equal within the
canonical integer range.

It also found and fixed a LinkedIn native-money reparse gap. `unitCost` accepted
any positive two-decimal string after a forged compiled graph, including values
above the typed `Money` ceiling. Native decimal amounts now have a bounded
canonical-cent representation, and compiled validation applies that check even
when nested Pydantic models were constructed or copied without their validators.

All six compiler public boundaries reparse their brief with serializer warnings
disabled only for that revalidation dump. The strict parse still rejects forged
float and boolean minor units, foreign currencies, underflow, and overflow. The
compiled validators bind the typed maximum spend to each provider's native
representation. Google Ads and the Google candidate history use the same
RFC-8785-safe maximum native micros value of `9007199254740991`; the candidate
history alignment and its regression are owned by the shared-history slice.

The additional preview commands accept only their three closed brief schemas.
They compile locally, emit the redacted review plus every planning gate and
typed maximum spend, check the exact final newline-delimited envelope size
before accessing the sharing key, and have no retention option or provider
transport path. Local handles and provider references are converted to opaque
references before output; native requests, creative copy, and destinations are
not emitted.

Validation included the focused compiler and preview suites, adversarial
cross-provider boundary and serialized-graph regressions in
`tests/test_compiler_money_and_previews_ultra_review.py`, and scoped Ruff and
mypy checks. The complete scoped compiler set passed 125 tests; the
shared-history canonical-native-cap regression also passed. The resulting
candidates remain offline and non-executable. A future executor must still
perform the documented account, eligibility, readback, drift, authorization,
and activation checks before any provider egress.
