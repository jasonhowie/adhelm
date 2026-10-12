# Meta campaign PAUSE pending exchange V2

M48 joins an already-prepared private Meta campaign PAUSE V2 pending request to
the sealed M47 pipe transport without exposing a request, candidate, receipt,
public key, helper path, or grantor reference to the caller. The only entry is
package-private:

```python
_exchange_and_accept_pending_installed_meta_campaign_pause_approval_bridge_v2(
    kernel, ledger, *, pending, adapter, cancellation
) -> AcceptedNativeApproval
```

`adapter` must be M47's module-issued
`_MetaCampaignPauseApprovalBridgePipeAdapterV2`; it remains bound to the fixed
Meta helper role. The entry has no caller clock, native candidate, receipt,
key, metadata digest, source record, executable, or provider input.

## One-use ordering

The entry first structurally revalidates and atomically consumes `pending`.
Only one concurrent caller can pass that transition. It then holds the
revalidated request, challenge, source prefixes, and reviewed metadata inside
the module while it performs these steps:

1. Recheck the kernel/ledger coordinator and exact retained source against all
   captured stream prefixes before the helper can launch.
2. Run exactly one M47 exchange using a private wrapper around the kernel's UTC
   clock. M47 keeps its own monotonic pipe deadline; the wrapper records its
   exchange-start, candidate-validation, and final UTC samples and rejects a
   rollback.
3. After M47 returns a validated candidate, reload the source again and call
   the exact Meta candidate acceptance continuation using the recorded first
   and final time bounds. The continuation never calls `_pending_data` again.

The prelaunch check can therefore fail safely with the pending already spent.
A caller cannot use a failed exchange as evidence that the same native nonce
or pending value is reusable.

## Closed failures

Malformed pending facts and an already-used pending keep their existing
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_INVALID` and
`..._PENDING_CONSUMED` outcomes. Once a valid pending is consumed, a local
coordinator, retained-source/prefix, or held-metadata failure before M47 begins
is the fixed precondition
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_PENDING_EXCHANGE_PRECONDITION`.
The pending remains consumed.

M47's own prelaunch cancellation, unavailable fixed transport, protocol, and
time preconditions retain their existing diagnostics. Any M47 result after a
helper may have launched remains its existing ambiguous
`..._EXCHANGE_UNVERIFIED` result. Once M47 has returned a candidate, source
reload, clock, candidate, receipt-claim, or grant-recording failure maps to
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_ACCEPTANCE_UNVERIFIED`; it is
ambiguous and instructs the caller not to retry.

## Boundaries

This is local composition only. It does not resolve an installed binding, add a
generated binding, configure a default runtime, launch a real helper in tests,
perform an operating-system or provider call, expose a CLI route, or execute a
provider operation. The returned acceptance record is local receipt-claim and
grant history; it does not claim installed-host provenance or provider egress.

The test suite uses synthetic signed candidate frames and fake POSIX pipes. It
covers successful single use, prelaunch source and transport failures with a
spent pending value, lost reply and cancellation ambiguity, before/after
source-prefix rewrite, benign append-only history extension, clock rollback and
expiry, and concurrent at-most-one launch:

```text
UV_CACHE_DIR=/private/tmp/adhelm-m48-uv-cache uv run pytest -q \
  tests/test_meta_campaign_pause_pending_exchange_v2.py \
  tests/test_installed_meta_campaign_pause_approval_bridge_v2.py \
  tests/test_meta_campaign_pause_approval_bridge_transport_v2.py
```
