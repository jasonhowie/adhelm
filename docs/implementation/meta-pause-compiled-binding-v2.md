# Meta campaign PAUSE compiled binding V2

`adhelm._compiled_meta_campaign_pause_approval_bridge_v2` is the separate,
private compiled-binding boundary for the Meta campaign PAUSE V2 route.  It
leaves the V1 compiled binding, generated V1 source, protocol bytes, and
admission unchanged.

The checked-in
`_generated_meta_campaign_pause_approval_bridge_binding_v2.py` is deliberately
unconfigured. Its only resolver returns `None`; it has no loader, package
resource, manifest, environment, configuration, credential, helper, or runtime
import. Consequently the installed-only entry returns
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED` before it reads a
retained plan, prepares a pending request, constructs the fixed M47 pipe
adapter, or starts native activity.

## Generated artifact contract

A future reviewed artifact may replace only the generated Python source with a
static call to:

```python
_issue_compiled_meta_campaign_pause_approval_bridge_binding_v2_from_generated_metadata(
    *, metadata_fields, expected_metadata_digest, expected_protocol_configuration_digest
)
```

The input is exactly the existing ordered 18-field native metadata tuple. The
factory uses the established metadata-only validator to rebuild every scalar,
requires the complete canonical metadata SHA-256 digest, reconstructs the
closed seven-field `ApprovalHostConfiguration`, and requires its canonical
protocol digest. It issues only the opaque,
module-created `_CompiledMetaCampaignPauseApprovalBridgeBindingV2` value.

Every resolver and use redoes those checks. The private fact accessor,
`_compiled_meta_campaign_pause_approval_bridge_binding_facts_v2(binding)`,
returns freshly detached seven-field configuration and full metadata-digest
facts only after the full 18-field tuple and both digests still agree. A V1
compiled binding cannot satisfy this type or issuer check. This protects the
fixed artifact boundary, not arbitrary same-process code that can coherently
replace every private fact and resolver.

## Installed-only composition

The only installed composition entry is:

```python
_request_and_record_installed_meta_campaign_pause_approval_v2(
    kernel, ledger, *, source_plan_record_digest
) -> AcceptedNativeApproval
```

It accepts no caller metadata, key, clock, helper path, adapter, candidate,
receipt, grantor, or cancellation value. Once the separate Meta binding has
been checked, it derives M46 reviewed metadata, prepares one retained-source
pending request, and invokes M48 with M47's sealed fixed-system adapter and an
internal never-cancelled object. M48 retains the consume-before-launch,
source-prefix, clock, receipt, claim, and no-retry boundaries. This layer does
not add a resolver to ordinary runtime, expose a CLI route, contact a provider,
or claim a configured host is present.

The test suite uses static synthetic metadata, a temporary verified ledger,
and M47's sealed fake POSIX pipe adapter only; it never launches the system
helper:

```text
UV_CACHE_DIR=/private/tmp/adhelm-m49-uv-cache uv run pytest -q \
  tests/test_compiled_meta_campaign_pause_approval_bridge_v2.py
```
