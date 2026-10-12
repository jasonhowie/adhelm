# Local creative registration

Reviewed: 2026-09-05
Status: **implemented local history workflow; no provider execution**

`creative analyze --input` reads one bounded regular local raster image. It
decodes the bytes locally, derives a content-addressed asset and deterministic
analysis, stores the image in the local vault, and retains the matching asset
and analysis records. The command preflights its complete compact result before
the vault or ledger write. `--ephemeral` returns an analysis result without a
retained source and cannot be used for registration.

`creative register --input` accepts the closed
`creative-registration-request` schema. `IDENTITY_IMAGE` names exact retained
asset and analysis record digests. The workflow reloads the verified local
history, reads the selected vault object, rehashes it through the vault
boundary, decodes it again, and requires exact equality with both retained
source records before deriving the only supported identity rendition, creative
version, and final-rendition fingerprint. `TEXT_ONLY` derives only the local
family and version. The caller supplies no rendition, version, fingerprint,
provider evidence, extraction evidence, or authority field.

For both forms, `created_at` is an explicit timezone-aware declared local
version time. It does not claim an authoring, provider, upload, serving, or
deployment time. An existing family retains its original `created_at`; a new
material version uses its own declared time. Exact repeated inputs reuse the
same content-derived graph. A changed image, copy, destination, component, or
serving manifest produces a distinct version or fails a retained-family
conflict check.

The workflow invokes only `Ledger.append_creative_registration`, which holds
the creative stream lock and appends the fixed dependency graph after its
complete graph/source validation. It does not expose a generic batch append.
After that call, the workflow reloads a verified checkpoint and binds every
closed graph record, the returned version record digest, and image source
records before returning a receipt. Any local readback uncertainty after a
durable append returns `CREATIVE_REGISTRATION_UNVERIFIED` with an
inspect-before-retry action. It does not invite a blind retry. A crash after
the version but before the final image fingerprint is shown as staged/missing
final-rendition evidence; an exact retry can finish the same closure.

`creative catalog` and `creative show RECORD_DIGEST` build their redacted
surfaces from a freshly verified local checkpoint. They expose only
content-addressed IDs/digests, normalized counts, historical local statuses,
and fixed no-authority controls. They omit the family identifier and label,
exact copy, message, destination, component text, raw image bytes, and vault
path. A final-rendition evidence status requires a retained final fingerprint
for every rendition in the version; a historical view does not evaluate
deployment, provider currentness, permission, or current vault availability.

The compact registration receipt is bounded to fit the configured 1,024-byte
minimum, including a UTC microsecond timestamp and the longest idempotent
status. Analysis and registration reject an output bound before their local
write effect. Catalog and show fail rather than silently truncate a complete
historical review that cannot fit their configured result bound.

All work is local. No provider credential, browser, provider API, native
approval host, deployment, remote mutation, current provider read, or provider
permission check occurs. Retained local evidence is not execution authority.

## Validation

```text
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run pytest -q \
  tests/test_creative_registration.py tests/test_creative_registration_cli.py \
  tests/test_cli_workflows.py tests/test_guidance.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff check \
  src/adhelm/creative_registration.py src/adhelm/cli.py \
  tests/test_creative_registration.py tests/test_creative_registration_cli.py \
  tests/test_guidance.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run ruff format --check \
  src/adhelm/creative_registration.py src/adhelm/cli.py \
  tests/test_creative_registration.py tests/test_creative_registration_cli.py \
  tests/test_guidance.py
UV_CACHE_DIR=/private/tmp/adhelm-uv-cache uv run mypy \
  src/adhelm/creative_registration.py src/adhelm/cli.py
```
