# Generated installed-bridge binding ultra review

## Scope

This independent M35 review covers the static Python binding rendered from the
closed 18-field native artifact contract and the paired wheel inspection that
carries it. It uses synthetic metadata, dynamically executed generated source,
and handcrafted ZIP files only. It does not import a configured wheel, start a
helper or host, access Keychain or OS authentication, connect XPC, read a
ledger, or contact a provider.

## Boundaries reviewed

- An ordinary checkout and ordinary wheel must remain unconfigured. The checked
  in resolver returns `None` and has no runtime configuration loader.
- The factory must reconstruct the exact ordered 18 metadata fields, reject
  scalar aliases, and require both the full metadata digest and the
  protocol-seven digest to match the reconstructed facts.
- A malformed or incoherent private binding mutation must fail at the fixed
  `NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` boundary before request construction.
- The configured wheel must contain exactly the archived `adhelm` and bundled
  skills payloads, plus the closed dist-info surface. Matching generated source
  alone cannot authorize a changed bridge module or an added `.pth` or `.data`
  member.

## Findings and repairs

The review first reproduced an opaque-binding gap: replacing its retained
configuration through `object.__setattr__` changed the protocol facts while the
previous validator retained only a metadata digest. The factory now retains the
protocol digest and rebuilt field tuple; every use rebuilds and compares the
metadata and protocol projection before exposing facts. The direct regression
uses a valid forged `ApprovalHostConfiguration` and receives the fixed
not-installed result.

The review also reproduced a wheel-boundary gap: a synthetic wheel with the
right generated binding and a changed
`adhelm/_installed_native_approval_bridge.py` was accepted. The builder now
snapshots every archived `src/adhelm` and bundled-skill payload before the offline
wheel build, checks the source snapshot again afterward, and requires the ZIP's
exact payload member set and SHA-256 values. The independent tests also reject
an added `.pth` member and a foreign `.data` member.

Python private slots are not a defense against arbitrary code already running
inside the trusted Python process. A coherent replacement of every private
field through `object.__setattr__` is outside this boundary; the generated
artifact's exact wheel payload check supplies the source-integrity binding. The
binding object is still nonserializable and does not accept externally supplied
runtime metadata, receipts, pins, or authority.

## Validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_generated_installed_approval_bridge_binding.py \
  tests/test_generated_installed_approval_bridge_binding_ultra_review.py \
  tests/test_native_approval_artifact_builder_ultra_review.py
```

The focused command passed 21 fake-only cases. New regressions cover one
binding mutation and three wheel-payload attacks; the artifact suite also
retains its earlier eight cases. No test launches configured code or executes
a native product.
