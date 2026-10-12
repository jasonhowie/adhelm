# Generated installed approval bridge binding

`scripts.native_approval_artifact_contract.render_generated_installed_approval_bridge_binding()`
is a pure renderer for a configured artifact source tree. It accepts only the
already rebuilt `NativeApprovalHostArtifactContractV1` and emits a replacement
for `adhelm._generated_installed_approval_bridge_binding`.

The checked-in module deliberately returns no binding. Ordinary source checkouts
and ordinary development wheels therefore continue to fail with
`NATIVE_APPROVAL_BRIDGE_NOT_INSTALLED` before a retained-plan read, pipe launch,
or receipt operation.

## Generated surface

The isolated replacement contains only:

- an ordered immutable tuple of the exact 18 metadata scalars;
- the SHA-256 digest of canonical full metadata; and
- the SHA-256 digest of the exact seven-field Python protocol configuration.

Its sole private resolver calls M34's private issuance factory. That factory
rebuilds the 18-key mapping with no coercion, rejects duplicate, missing,
unknown, bool, float, selector, access-group, storage, and limit variants,
reconstructs `ApprovalHostConfiguration`, and recomputes both digests before
issuing the opaque in-process binding. The generated module never receives the
issuer token.

The opaque binding retains the rebuilt immutable 18-field tuple and both
digests. Each later M34 use rebuilds that tuple again, requires its full digest,
and requires the retained configuration to equal the tuple's protocol-seven
projection. A malformed or incoherent private field/digest mutation therefore
fails closed before the bridge can build a request. A coherent replacement of
every private fact by arbitrary code in the same process is outside the stated
same-UID/in-process threat model; the generated wheel payload and its
build-time source hashes provide the applicable static source-binding boundary.

M34 imports this fixed module name only inside its binding resolver, after M34
has defined its factory. This avoids a partially initialized circular import.
The default module has no factory import or call at import time.

The renderer has no runtime behavior: it does not read a manifest, file,
environment variable, package resource, path, receipt, public-key pin, ledger,
or provider configuration. It does not start the helper or native host.

## Artifact boundary and limits

The paired artifact builder owns replacing this file only inside its isolated
archived source tree. It must hash the generated Python source before and after
the wheel build, compare the static metadata/protocol digests with the matching
Swift generated source, and stage the wheel only as build evidence. There is no
normal-wheel build hook and no runtime metadata loader.

The full metadata digest binds the generated static selectors, service, storage,
provenance, and limits beyond the protocol-seven projection. It does not prove
that the wheel, helper, or host is signed, installed, current, enrolled, or
trusted. The private issuer and static module boundary are in-process misuse
guards under the documented same-UID limitation; they do not establish approval
authority. Native current verification, nonce consumption, the private helper
exchange, JSONL claim/grant, and provider execution remain separate boundaries.

## Focused validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_generated_installed_approval_bridge_binding.py
```

The tests cover the unconfigured default, deterministic rendering, generated
module execution after M34 is initialized, complete metadata/protocol digest
pairing, strict scalar and field-order rejection, and absence of runtime loader
or trust/receipt imports. They use synthetic metadata only and launch no helper
or native process.
