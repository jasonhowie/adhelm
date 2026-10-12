# Meta token inventory and reporting integration

The existing `inventory meta_ads` and `report meta-ads` commands accept a
configured `META_ADS_ACCOUNT_TOKEN` profile with READ purpose. They use the
[private inspected read session](meta-ads-token-read-session.md) and never pass
the protected bundle into the generic credential resolver. The customer declares
the native account ID, currency and timezone at enrollment. Actual provider
identity must match before inventory or aggregate reporting.

The workflow captures validated configuration before its ledger checkpoint and
sharing-key preflight. The private factory checks that exact configuration digest
before inspection or account reads. Report input must match the native account,
timezone and any declared query currency before sharing-key access. Reporting
then binds the query hash to the discovered canonical account and currency.
The required attribution windows remain explicit in the query contract.

All fixed inventory collections must finish and form a valid hierarchy before
retention. The session holds configuration and credential locks before any ledger
append, and its callback rechecks bundle continuity and expiry immediately before
each durable snapshot, change or performance record. The callback does not obtain
ledger locks or read history. An interrupted local append sequence can retain its
earlier records; the workflow returns failure and never claims a transactional
inventory append. No provider writes occur. Ephemeral results also require the
final continuity check but do not append observations.

Report artifact projection runs after the read session and its retention locks
close. It follows saved artifact preferences and derives from canonical report
history. Shared results use existing opaque reference projections; subject and
inspector tokens, private binding keys, inspected principal references and raw
inspection payloads are absent from output and history.

The new protected-bundle path rejects `--ci` explicitly. CI environment
credentials, token refresh, EDIT/activation workflows and live Meta access are
not established by this integration. Existing separately scoped profile routes
retain their prior development behavior.

The 21-case integration group uses a synthetic OS store and `httpx.MockTransport`
through the actual inspector, provider adapter, history and existing CLI routes.
It covers hierarchy and external-change records, campaign/ad reports, canonical
query provenance, ephemeral results, configuration drift before HTTP, rotation
after the final response, expiry inside the ledger append window, query mismatch,
CI refusal, redaction and artifact projection after lock release. Other provider
inventory, reporting, saved-report and artifact regressions are run alongside it.
This remains development evidence; no customer credentials or live API were used.
