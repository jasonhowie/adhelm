# Meta campaign PAUSE artifact profile V2

`scripts/build_native_approval_artifact.py` has two closed build layouts. The
implicit `V1` layout is the existing unsigned development artifact. The only
additional layout is the explicit build-only profile
`META_CAMPAIGN_PAUSE_V2`:

```text
--profile META_CAMPAIGN_PAUSE_V2
```

The selector is validated before the builder reads the contract, resolves a
revision, creates an output directory, or runs a tool. It is not stored as a
runtime option, environment value, bootstrap setting, or installation signal.
An unknown, constructed, or non-string profile fails with
`NATIVE_ARTIFACT_PROFILE_INVALID`.

## Preserved V1 layout

The default path keeps the V1 descriptor, full 18-field metadata, seven-field
protocol projection, generated V1 sources, fixed Swift command sequence,
staged files, deterministic archive, manifest, and `ArtifactBuildResult`
shape unchanged. Its staged executable set remains only:

```text
adhelm-macos-installed-approval-host
adhelm-macos-approval-client
```

The fixed Core-only inspector remains a separate build/inspection product and
is never staged or executed as an artifact component.

It now reads the two archived Meta generated paths as integrity facts. Both
must equal their reviewed unconfigured defaults before any Swift build and at
each later source-integrity checkpoint:

```text
src/adhelm/_generated_meta_campaign_pause_approval_bridge_binding_v2.py
native/macos-approval-host/Sources/ApprovalHostCore/GeneratedMetaCampaignPauseApprovalRouteBinding.swift
```

This prevents a source revision containing an already configured Meta route
from being built or staged through the ordinary V1 profile. The default Python
module remains an ordinary wheel member only as its reviewed absent
implementation. The Swift route remains compiled as its reviewed absent source.
Neither makes a V1 artifact Meta-ready.

## Explicit Meta layout

The Meta profile operates only in the builder's extracted private source tree.
It replaces exactly the two paths above with the existing deterministic
renderers in `scripts/native_approval_artifact_contract.py`:

- `render_generated_meta_campaign_pause_approval_bridge_binding_v2`, which
  emits the M49 Python resolver over the ordered full 18-field tuple; and
- `render_generated_meta_campaign_pause_approval_route_binding_v2`, which
  emits the Core route binding over the same full metadata digest and
  seven-field protocol-configuration digest.

The builder hashes both generated values before any build, rechecks them around
every fixed Swift build and descriptor inspection, around the offline wheel
build, and immediately before and after staging. It also retains the existing
V1 generated-source checks. The wheel inspector requires the V1 and Meta
generated Python members, their exact payload hashes and `RECORD` rows, and
the M49 compiled-binding module. It reads ZIP bytes only and never imports the
configured wheel.

The profile adds one exact Swift product and staged executable:

```text
adhelm-macos-meta-campaign-pause-approval-client
AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-meta-campaign-pause-approval-client
```

The builder checks that helper as the same fixed ARM64/macOS target as the
existing host/client, writes it mode `0755`, hashes it as a component, and
includes it once in the deterministic archive. It never invokes that helper,
the host, a provider, a credential store, or an installed wheel.

Only an explicit Meta-profile manifest adds
`meta_campaign_pause_approval_bridge_v2`. Its closed evidence records the
profile name, both generated-source hashes, the existing descriptor metadata
and protocol digests, the fixed helper relative path/hash, and its Mach-O build
target. Its scope is static build evidence and explicitly says it is neither
installed nor approval authority. The generic artifact result remains the same
unsigned, uninstalled status for both profiles.

## Fake-only checks

The builder suite uses a fake Git archive, Swift runner, Mach-O bytes, wheel,
and tar file. It verifies default V1 isolation, invalid profile preflight,
source mutation detection, the exact Meta wheel and helper closure, and no
executable invocation:

```text
UV_CACHE_DIR=/private/tmp/adhelm-m50-uv-cache uv run pytest -q \
  tests/test_build_native_approval_artifact.py \
  tests/test_native_approval_artifact_builder_ultra_review.py \
  tests/test_native_approval_paired_build.py
```
