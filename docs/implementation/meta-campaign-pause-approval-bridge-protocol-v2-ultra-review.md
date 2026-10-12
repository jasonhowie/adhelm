# Meta campaign PAUSE approval bridge protocol V2: independent review

**Status:** reviewed for the M45 pure-codec slice.

This review covers the private Python and Core Swift V2 codecs plus their
synthetic interoperability fixture. It does not cover a helper, XPC route,
installed binding, source reload, ledger claim, nonce-store operation, trust
resolution, provider operation, or execution.

## Verified boundary

The V2 request accepts only the existing M42
`META_CAMPAIGN_PAUSE_PLAN` subject, with exactly seven canonical fields and a
98,304-byte cap. The V2 candidate has exactly nineteen canonical fields and a
131,072-byte cap. Both use the fixed V2 message strings and schema version 2.
The candidate binds the request ID and request digest, source-context digest,
metadata and protocol-configuration digests, complete canonical challenge,
nonce and challenge digests, receipt kind/ID/digest/expiry, returned P-256
X9.63 key and fingerprint, and a six-fraction native observation timestamp.

The independent cases cover whole-second and nonzero-six-fraction M42 vectors,
V2 source-context domain separation from V1, strict scalar and canonical JSON
rejection, canonical Base64URL/key/receipt limits, exchange and expiry
boundaries, constructed-object reparsing, and redacted representations. They
also show that V1 refuses a Meta V2 request, V2 refuses V1 request bytes, and
the V2 module imports no ledger, safety kernel, installed binding, transport,
subprocess, OS, or filesystem authority surface.

Request decoding reconstructs the complete M42 action rather than trusting a
kind tag. The adversarial cases reject incomplete action data, changed account
facts, changed spend, and an expiry beyond the retained plan before a typed
request can be returned.

## Findings repaired during review

1. The initial Swift request constructor and decoder checked a canonical
   top-level Meta kind but did not reconstruct the closed M42 action. A
   canonical request containing an incomplete subject could therefore become a
   typed V2 request until `boundChallenge`. The final
   `requireStructuralCanonicalMetaCampaignPauseChallenge` rebuilds the exact
   subject, action, plan, and expiry relation at construction and decode; only
   trusted exchange-clock eligibility remains deferred to `boundChallenge`.

2. The initial Swift fixture consumer used the whole-second M42 source path for
   both source cases. The final test maps the whole- and fractional-second case
   IDs to their corresponding M42 fixture paths, so the fractional vector is
   actually consumed rather than accidentally compared to the wrong source.

## Limits retained deliberately

The metadata, protocol-configuration, and source-context digests are
caller-supplied expected facts in this pure codec. Their syntax and correlation
are checked, but the codec does not prove a generated installed binding, host
identity, current active key, protected nonce consumption, retained ledger
source, or provider currentness. Receipt verification only proves
self-consistency with the candidate-returned key; it is not an independently
trusted-key assertion. No returned value is a grant, permit, trust pin, or
execution authority.

V1 bytes and its direct-kind routing remain unchanged. M45 adds no bridge
transport, XPC selector, helper, host, Keychain, LocalAuthentication, ledger,
or provider work.

## Validation

The Python codec plus independent review group passed 42 tests:

```text
UV_CACHE_DIR=/private/tmp/adhelm-m45-uv-cache uv run pytest -q \
  tests/test_meta_campaign_pause_approval_bridge_protocol_v2.py \
  tests/test_meta_campaign_pause_approval_bridge_protocol_v2_ultra_review.py
```

The owner-reported sandboxed Swift V2 Core group passed 5 tests, and the full
Swift package passed 255 tests. The native run kept SwiftPM sandboxing enabled;
its transcripts are `/private/tmp/adhelm-m45-v2-focused-tests.txt` and
`/private/tmp/adhelm-m45-full-swift-tests.txt`.
