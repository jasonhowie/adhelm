# Google Ads campaign PAUSE plan views

M55 exposes a bounded historical review of one retained Google Ads campaign
PAUSE plan.  The public entry is:

```python
review_google_ads_campaign_pause_plan(
    value,
    opaque_key=key,
    reviewed_at=instant,
    max_output_bytes=65536,
)
```

The review first fully revalidates the closed record, then renders a complete
probe using a fixed local key before using the caller's sharing key.  An output
limit failure therefore returns before caller-key use or a partial review.
The final result is a typed `GoogleAdsCampaignPausePlanReview` with HMAC-redacted
references, a deterministic review digest, and no execution authority.

The only raw-reference helper,
`_google_ads_campaign_pause_plan_export_hmac_input`, is private to the ledger
projection path.  It emits a fixed allowlist immediately before the existing
recursive HMAC pass.  Public reviews and JSONL/CSV exports contain opaque
workspace, customer, campaign, profile, client-application, and optional
manager-login references.  They retain the historical customer status,
currency, timezone, v25 label, ENABLED baseline, PAUSED configuration-only
postcondition, `NOT_PREDICTED` delivery status, bounded read interval, zero
incremental spend, expiry, and fixed recovery limits.

The view excludes the action ID, principal reference, OAuth
binding, configuration and profile digests, requested scope, tokens, raw
credential slots, provider responses, and reusable approval material.  A
review at or after expiry remains historical evidence only and continues to
say that a current recheck is required before egress.

The projection does not prove archive provenance, a live provider state, a
provider write permission, acknowledgement semantics, delivery, spend, or a
current authorization.  Those claims require fresh guarded work in later
separate slices.
