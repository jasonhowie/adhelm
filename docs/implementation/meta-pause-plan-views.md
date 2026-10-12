# Meta campaign PAUSE historical plan views

`meta_pause_plan_views.py` exposes one local, non-authorizing review surface:

```python
review_meta_campaign_pause_plan(
    value,
    opaque_key=private_key,
    reviewed_at=aware_utc_time,
    max_output_bytes=65536,
)
```

The function first reconstructs the complete
`MetaCampaignPausePlanRecord`. It therefore rejects malformed, copied, or
rehash-only action data before any sharing projection runs. The caller supplies
an opaque local key of at least 32 bytes; the function returns only fixed-width
HMAC pseudonyms for local workspace, account, campaign, profile, and client
references. It never reads configuration, credentials, a provider, the ledger,
or a clock.

The review retains the exact action, plan, and subplan digests; native account
currency; zero incremental spend; the historical `ACTIVE` configured and
observed effective baseline; the bounded observation interval; and the
configuration-only `PAUSED` postcondition. `NOT_PREDICTED` explicitly means a
configured pause does not prove effective delivery stopped or that residual
spend is zero. It also retains the absence of documented provider
compare-and-swap and idempotency support, the sequential non-atomic baseline,
the required post-pause readback, and reconcile-then-new-authorization recovery
semantics.

`reviewed_at` cannot predate the completed retained observation. This prevents
a review from labeling a plan against a historical state that had not yet been
observed. A later expiry does not invalidate the review; it changes only the
explicit historical expiry status.

Expiry is reported as a historical review fact. An expired plan remains
inspectable, but no review authorizes execution, grants provider access, or
proves current remote state. Before producing a caller-keyed view, the function
uses a synthetic fixed-width key to prove the complete canonical output fits
the requested bound; an undersized bound returns `OUTPUT_LIMIT_EXCEEDED` with
no partial result.

Exports recognize `META_CAMPAIGN_PAUSE_PLAN` in the common projection path.
The private `_meta_campaign_pause_plan_export_hmac_input()` is a narrowly named
intermediate used only immediately before that existing recursive HMAC pass. It
preserves raw reference values only long enough to pseudonymize them. Its
allowlist omits the retained action and action ID, credential binding,
configuration/profile digests, principal, required scope, inspection metadata,
and every other local execution provenance field. This dispatch runs regardless
of `include_marketing_text`, so JSONL and CSV exports cannot expose the raw
closed action.

Focused coverage is in `tests/test_meta_pause_plan_views.py` and exercises the
direct surface, expiry, malformed/rehashed rejection before redaction, output
preflight, and JSONL/CSV export absence of native identifiers and credential
markers.
