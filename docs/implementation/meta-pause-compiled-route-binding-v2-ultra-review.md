# Meta campaign PAUSE compiled route binding V2 ultra review

This independent M49 review covered the separate private Python generated
binding and installed composition entry, the pure paired-artifact renderers,
and the native Core/MacOS route-binding construction. The added tests use only
static metadata, generated source executed in a test module, and fake
kernel/ledger seams. They do not launch a helper, invoke XPC, access an
operating-system store, authenticate a user, contact a provider, or run a
configured artifact.

## Compiled Python route

The checked-in generated module is deliberately absent: its resolver returns
`None` and imports no factory, file, environment, manifest, resource, or
runtime selector. The installed-only entry therefore returns
`NATIVE_META_CAMPAIGN_PAUSE_APPROVAL_BRIDGE_V2_NOT_INSTALLED` with the fixed
precondition exit before it can inspect the retained source digest, prepare an
M46 pending request, construct M47's fixed pipe adapter, or start native
activity.

A separately rendered module contains only an ordered static 18-field metadata
tuple, its complete canonical metadata digest, the seven-field protocol digest,
and a lazy call to the V2-only issuer. The issuer reconstructs every closed
scalar, checks the complete metadata digest, reconstructs the protocol
configuration, and checks its digest. The opaque V2 binding repeats those
checks each time facts are used and returns only detached configuration plus
the complete metadata digest.

The V1 generated binding and resolver cannot satisfy the V2 type and issuer
boundary even when their metadata and protocol facts happen to be identical.
An incoherent metadata-only mutation, including one that leaves the
seven-field protocol digest unchanged, also fails before source or helper work.
This protects malformed or incoherent private state. It does not claim to seal
against arbitrary same-process code that coherently replaces all private facts
and the resolver.

The sole installed composition entry accepts only `kernel`, `ledger`, and the
retained source-record digest. It derives reviewed metadata from the checked
V2 binding, calls M46 to prepare a sealed pending value, then calls M48 with
M47's fixed system V2 adapter and an internal non-caller cancellation object.
It exposes no caller binding, clock, key, adapter, path, candidate, receipt,
grantor, or cancellation input. M46/M48 retain the consume-before-launch and
post-candidate ambiguity handling; this review makes no claim that a configured
artifact, helper, host, or provider execution is available.

## Paired native route

`GeneratedMetaCampaignPauseApprovalRouteBinding` is absent in ordinary source.
A future paired static source may create a V2 route binding only when the
complete native metadata descriptor and its seven-field protocol configuration
match its rendered digests. A present but malformed or mismatched generated
route throws the fixed host-not-installed outcome; it does not fall back to the
V1 route. The installed listener resolves this binding against its bootstrap
metadata and configuration before constructing protected post-signing
factories.

The native paired factory always creates the existing V1 collaborator and
creates the V2 collaborator only for an exact route binding. Both protocol
views use the same redemption factory, coordinator, fixed-slot store, and
runtime operation gate. Runtime shutdown therefore invalidates the shared gate
for both routes. These are build-configuration consistency checks, not proof
of code identity, an installed artifact, a protected-key operation, receipt
authority, or a live approval.

## Default-source hash guards

The offline development validator independently pins both ordinary Python
generated-module bytes: the existing V1 module and the new Meta V2 module.
It requires each exact source member in a snapshot, then requires each wheel
member to occur exactly once with the same fixed SHA-256 before the validator
can use that wheel. A missing, duplicated, or changed Meta member fails closed
without importing it.

The native unconfigured launch-smoke validator separately pins both the
existing default metadata source and the default-absent Meta route source. It
checks the absent-route SHA-256 before it invokes any native target; a missing
or configured replacement fails before `subprocess.run`. The reviewed values
match the current checked-in files:

- V1 Python default module: `1377be2450de426cec82ce37fb9301e310bbbf201be32ea67c677cfe57722ef3`
- Meta V2 Python default module: `00b530fa0bc985a132695b75fa55b5d31e18f58fc86d7a2d1c129fa8383c2f2c`
- Meta native default-absent route: `eb068767a51e8b9dc3970eb43017d4273b0fa45e13d6525b12a3dfd08d06d625`

These guards establish only that the validators are operating on the reviewed
unconfigured defaults. They do not validate a configured artifact or turn a
development smoke result into installed-route evidence.

## Independent coverage and results

`tests/test_compiled_meta_campaign_pause_approval_bridge_v2_ultra_review.py`
adds five distinct fake-only checks:

- a renderer-produced static V2 module resolves lazily, deterministically, and
  without a V1 or runtime-loader import;
- an exact V1 binding returned through the V2 generated-module seam fails
  before retained-source or fixed-helper activity;
- a type-shaped V2 value with missing private state fails at the same early
  readiness boundary;
- changing a non-protocol metadata field while retaining the original
  digest fails on use before pending preparation or helper construction; and
- the checked-in default refuses before it can inspect even a malformed source
  argument.

The focused Python review group passed **44 tests**:

```text
UV_CACHE_DIR=/private/tmp/adhelm-m49-uv-cache uv run pytest -q \
  tests/test_compiled_meta_campaign_pause_approval_bridge_v2.py \
  tests/test_compiled_meta_campaign_pause_approval_bridge_v2_ultra_review.py \
  tests/test_native_approval_artifact_contract.py
```

Ruff, formatting, and strict mypy passed for the independent test, and
`git diff --check` was clean. The owner’s focused compiled-route group passed
**8/8**, including a temporary-ledger M46-to-M48 fake-pipe composition smoke.

The native owner’s focused fake-only Core/MacOS route-binding group passed
**8/0**. It covers default absence, exact metadata/protocol admission,
metadata-only and configuration mismatch refusal before protected collaborators,
V1 preservation, and shared-gate invalidation through both V1 and V2 protocol
views. Its final sandboxed native package run passed **292/0**. No native
executable was run by this review.

During integration, root corrected two test-only issues without changing the
renderer, generated defaults, or route behavior: the generated-renderer test
now imports its helper through the collection-safe module name rather than a
`tests.` package path, and it decodes the renderer's deliberate Swift Unicode
escapes before comparing the two digest literals. The final five-file fake-only
Python group, including the hash guards and independent review cases, passed
**72 tests**.

Root separately ran the actual working-source unconfigured native products
through both source hash guards. All **16 refusal cases** passed; the summary
is `/private/tmp/adhelm-m49-working-native-smokes/summary.json` and the refusal
transcript SHA-256 is
`94ea882f3d796268385cf76e066095a683ed49102d3f2ca97de0f45f6e658281`.
This is unconfigured entrypoint evidence, distinct from the fake behavioral
tests and from any later exact-commit or configured-artifact validation.

## Finding status

No unresolved M49 source finding remains in this bounded review. The route is
still deliberately unconfigured in ordinary source. A future artifact profile
must pair the generated Python source, generated native route source, fixed
Meta helper, and reviewed build evidence before this composition can indicate
installed-route readiness.

## Exact committed follow-up validation

The exact M49 source `b90d9171d2d7e6ef9d39659110a4445b34815c7b`
passed **3,415 Python tests**, **1,266 targeted ultra regressions**, and
**292 native tests**, with strict source typing and guarded unconfigured
smokes. The two unsigned V1-profile builds matched all eight staged files.
The configured host, helper and wheel were not executed. Full Python validation
completed at `2026-09-06T22:01:15.309878Z`; the raw report SHA-256 is
`13dfff7cba0bc63353fdaf9cb6919441a171e0b240ced95ce9639c77522825ce`. M50 artifact-profile work is excluded
from this archived source and evidence.
