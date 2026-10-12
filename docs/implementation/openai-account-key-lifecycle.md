# OpenAI Ads account-key lifecycle

Implemented: 2026-09-09. `src/adhelm/openai_account_key_lifecycle.py` and
`src/adhelm/openai_account_key_cli.py`. This extends
[`openai-account-key-profile.md`](openai-account-key-profile.md), which
describes the closed configuration shape; this note describes what the product
does with the key that profile points at.

Nothing here promotes the OpenAI Ads `AUTH` operation group in
[`capability-matrix.md`](../capability-matrix.md). Every test is fixture-only
and no live OpenAI request has been run by this product.

## What OpenAI documents, and what it does not

Fetched 2026-09-09 from the
[Advertiser API overview](https://developers.openai.com/ads/api-overview.md)
and the
[Authentication reference](https://developers.openai.com/ads/api-reference/authentication):

| Fact | Documented? |
| --- | --- |
| The key is account scoped and sent as `Authorization: Bearer …` | Yes |
| A key is **issued in the Settings tab of Ads Manager** | Yes |
| Rate limits: 600 requests per minute per endpoint, 1,200 per minute overall, enforced by both ad account and IP address | Yes |
| Bulk job creation: 10 requests per 10 seconds per ad account | Yes (this build sends no bulk request) |
| An OAuth flow, an authorization code, or a refresh token | No — there is none |
| A key expiry, a rotation endpoint, or a programmatic revocation route | **ABSENT** from every reviewed page |
| A documented revocation screen, or what happens to in-flight requests after revocation | **ABSENT** — recorded as `UNVERIFIED` |
| `429` semantics, `Retry-After`, or the meaning of `401`/`403`/`404` | **ABSENT** — the classification below is ordinary HTTP behaviour, not documented OpenAI behaviour, and is recorded as `UNVERIFIED` |

Because there is no OAuth grant, this lifecycle is deliberately smaller than
the LinkedIn one in [`linkedin-oauth-contract.md`](../research/linkedin-oauth-contract.md):
there is no enrollment browser handoff, no refresh, and no scope selection. The
profile records `provider_scope_status=NOT_DOCUMENTED` and makes no
least-privilege claim.

## Commands

Both are registered under the existing `auth` group by
`openai_account_key_cli.register_commands`.

### `adhelm auth openai-key status --profile <id>`

Bounded local state only. It performs **no** provider request and reveals no
key material. It returns:

| Field | Meaning |
| --- | --- |
| `local_state` | `MISSING`, `UNVERIFIED` (a key is stored, but no retained record binds to *that* key), `READY`, or `CI_MANAGED` |
| `key_slot_present` | Whether the profile's OS-store slot holds a value |
| `account_ref` | The canonical `openai_ads.account.<id>` reference the profile is bound to |
| `identity_pseudonym` | The **masked identity**: a per-profile salted HMAC label for the ad account the **currently stored** key was last shown to resolve to, or `null` when the retained record does not bind to that key. It is a display alias, not a confidentiality measure — see below |
| `last_verified_at`, `last_verified_api_version` | When that verification happened and against which pinned API version, or `null` when the retained record does not bind to the stored key |
| `revocation_guidance` | The fixed sentence below |
| `provider_revocation_verified` | Always `false` |

An `EXPLICIT_CI` profile is reported as `CI_MANAGED` with no presence or
verification claim: that value is not in the OS store and has no local
lifecycle.

### `adhelm auth openai-key rotate --profile <id> --interactive`

The ordering is fixed and is the point of the command:

1. Snapshot the stored value under the credential mutation lock, then release
   the lock.
2. Prompt for the replacement without echo. No lock is held. A local
   interactive terminal is required; a non-interactive run is refused with
   `EXPLICIT_LOCAL_ROTATION_REQUIRED` (exit code 4).
3. Read `GET /v1/ad_account` with the **new** key and require it to resolve the
   configured account. **No credential mutation lock is held across this HTTP
   call**, and the previous key is still in place.
4. Re-acquire the lock, refuse with `CREDENTIAL_CONCURRENT_CHANGE` if the
   stored value changed meanwhile, write the replacement, and prove the write
   by exact readback.

Refusals and what they leave behind:

| Code | Exit | Effect |
| --- | --- | --- |
| `OPENAI_ACCOUNT_KEY_MISSING` | 4 | Nothing stored to rotate |
| `OPENAI_ACCOUNT_KEY_UNCHANGED` | 2 | The entered value equals the stored one; refused before any request |
| `OPENAI_ADS_KEY_INVALID_OR_EXPIRED` | 4 | The replacement was rejected; the previous key stays in place |
| `IDENTITY_MISMATCH` | 3 | The replacement resolved a different ad account; nothing was stored |
| `OPENAI_ACCOUNT_KEY_IDENTITY_MISMATCH` | 4 | The replacement's account disagrees with the retained masked identity; the previous key stays in place |
| `CREDENTIAL_CONCURRENT_CHANGE` | 3 | Another local process changed the slot during verification |
| `CREDENTIAL_CHANGE_UNVERIFIED` | 3 | The approved store did not reproduce the written value |
| `OPENAI_ACCOUNT_KEY_CI_ROTATION_UNSUPPORTED` | 4 | An explicit-CI value has no local rotation lifecycle |

No key, and no part of any provider response body, reaches a result, a
diagnostic, an exception, or the retained verification record.

## Local state

The non-secret verification record lives in its own credential-store service,
`adhelm.openai-account-key-v1`, under a reference derived by
hashing a fixed domain with the profile identifier — the profile identifier is
never echoed into the slot name. The record holds schema version 2, the profile
identifier, the per-profile identity salt, the masked identity, the **key
binding**, the verification instant, and the pinned API version. It holds no
key material, and an unreadable record — including a record written under any
other schema version — fails closed with `OPENAI_ACCOUNT_KEY_STATE_INVALID`
rather than being ignored.

The salt is retained across rotations, so the same ad account keeps the same
masked label and a replacement key for a different account is detectable.

### The key binding

The record is bound to the exact stored key it verified. `key_binding` is
`HMAC-SHA256(identity_salt, domain ‖ key)` — a keyed digest under the record's
own local salt, never the key and never an unkeyed hash of it, so a state slot
read on its own confirms no candidate key.

`status` recomputes that binding against the value the key slot currently holds
and reports `UNVERIFIED` with **no** pseudonym and **no** `last_verified_at`
when the two disagree. This is what makes the verification claim honest: the
generic `adhelm auth store` and `adhelm auth rotate` commands write the key slot
without consulting this record, so without the binding a key issued for a
different ad account would inherit the previous key's masked identity and
verification instant and be reported `READY`. `rotate` applies the same test:
the retained pseudonym guards a rotation only when it still binds to the value
being replaced.

### The masked identity is a display alias, not a confidentiality measure

`identity_pseudonym` is a stable local label so that two results can be
compared, and so that an account identifier does not have to be printed to say
"the same account as last time". It is **not** a confidentiality control. The
salt sits beside it in the same local state slot, and an OpenAI ad-account
identifier is a short opaque string, so anyone who can read the state slot can
recompute the label for any candidate account identifier. Treat it as an alias
for display, never as protection for the account identity.

### Rotation is not atomic across the two slots

`rotate` writes the key slot and then the state slot inside one credential
mutation lock, and proves each write by readback. Those are two operations on
an operating-system credential store, not one transaction. If the process is
killed, or the store fails, between them, the machine can hold the **new** key
with the **previous** verification record. That state is not silently
misreported: the key binding no longer matches, so `status` reports
`UNVERIFIED` with no pseudonym and no verification instant, which is exactly
what the machine can honestly say. Running `rotate` again — or storing the key
again and re-running `rotate` — restores a bound record. No local record can
tell you whether the previous key still works at OpenAI; see the revocation
guidance above.

## Revocation

Local removal is never described as revocation. Every status and rotation
result carries this exact sentence:

> OpenAI documents that an Ads API key is issued in the Settings tab of OpenAI
> Ads Manager; retire a key there. UNVERIFIED: no reviewed OpenAI Ads page
> documents a key expiry, a rotation endpoint, a revocation screen, or a
> programmatic revocation route, so no expiry, rotation, or revocation
> behaviour is known here. Removing the local copy with `adhelm auth
> remove-local` deletes this machine's stored value only; this product cannot
> observe the provider-side state of a key, cannot verify a provider-side
> revocation, and does not claim the key is either still valid or revoked at
> OpenAI.

The sentence deliberately makes **no** claim in either direction about the
key's provider-side lifetime. An earlier wording said the key "stays valid at
OpenAI until the provider retires it"; that was a positive assertion about a
credential lifetime this product has never observed and that the table above
records as `ABSENT` from every reviewed page, and it erred toward reassurance,
which is the wrong direction for a credential.

`provider_revocation_verified` and `previous_key_revoked_at_provider` are fixed
`false` in every result. `adhelm auth remove-local --profile <id>` remains the
generic local-removal command; for an `OPENAI_ADS_ACCOUNT_KEY` profile it now
deletes **both** the OS-store key slot and the derived verification record
below, proving each removal by readback, and it states
`remote_revocation_verified=false`. Clearing both is the safer of the two
available behaviours: refusing the generic command would leave the guidance
above pointing at a command that fails, while removing only the key slot would
leave a masked identity and a verification instant behind for a key this
machine no longer holds.

## Refusal classification for the fixed reads

`classify_openai_ads_transport_error` in
`src/adhelm/providers/openai_ads.py` converts one shared-transport refusal into
an exact OpenAI diagnostic. The shared transport records the observed HTTP
status and, when the provider sent a whole-second `Retry-After`, its bounded
value (clamped to 3,600 seconds); **neither comes from the response body**, so
no classification can leak one.

| Status | Code | Exit | Disclosure |
| --- | --- | --- | --- |
| 401 | `OPENAI_ADS_KEY_INVALID_OR_EXPIRED` | 4 | Names `auth openai-key status`, the Settings tab of Ads Manager, and `auth openai-key rotate` |
| 403 | `OPENAI_ADS_ACCESS_NOT_PERMITTED` | 4 | Names the Settings tab of Ads Manager for this ad account's access |
| 429 | `OPENAI_ADS_RATE_LIMITED` | 3 | The bounded `Retry-After` seconds when present, or an explicit statement that no whole-second hint was sent, plus the documented 600/1,200 requests-per-minute model |
| anything else | unchanged | unchanged | The shared transport's sanitized error is re-raised as-is rather than relabelled |

A `403` or `404` on the one fixed conversion-setting route is instead reported
as the typed `NOT_ENABLED_FOR_ACCOUNT` availability outcome by
`OpenAIAdsConversionSettingsClient.read_event_settings`, and
`openai_measurement_workflow` reads that route through it. UNVERIFIED: no
reviewed page documents the meaning of a `403` or a `404` on this exact fixed
route, and no reviewed page shows that OpenAI
documents conversion setup and pixel behaviour as account-enabled features.
That reading is this build's own bounded choice about one route, not documented
OpenAI behaviour. A `401` on the same route stays an authorization failure:
classifying a key the provider refused as a missing feature would hide a
credential problem. Whether an OpenAI Ads key can expire at all is itself
**ABSENT** from every reviewed page, so nothing here reasons about an expired
key.

## Evidence limits

- **Fixture-only.** `tests/test_openai_account_key_lifecycle.py` uses an
  in-memory credential store, a recording lock, and an `httpx.MockTransport`
  identity read.
- **No live OpenAI request** has been run by this product for enrollment,
  rotation, verification, or revocation.
- **No provider-UI reconciliation.** Nothing here has been checked against the
  Settings tab of OpenAI Ads Manager.
- **No sandbox exists.** OpenAI Ads publishes no test-account environment.
- **`UNVERIFIED` items** are marked as such above: revocation behaviour, key
  expiry, and the exact meaning of a `401`, `403`, `404`, or `429` from this
  API are not documented on any reviewed page.
