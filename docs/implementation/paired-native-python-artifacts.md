# Paired native and Python development artifacts

The existing exact-source development builder now produces one Python wheel
alongside the native host and fixed helper. One closed build contract generates
both Swift metadata and Python binding source inside the same archived Git
snapshot. Neither normal source builds nor ordinary development wheels acquire
configuration from a runtime file, environment, manifest or build hook.

The manifest binds the full metadata digest, protocol digest, both generated
source hashes, native descriptor, both executable hashes and wheel hash. The
wheel contains the generated binding module; it has no receipt or public-key
pin and establishes no installed identity. The default checked-in binding
continues to return the fixed unconfigured result.

## Bounded offline build

This remains the fixed local arm64 development profile: Swift 6.2.3 with the
reviewed SDK, CPython 3.12.13, and the existing local uv 0.11.32 executable.
The Python build has one fixed argv shape. It runs offline, disables Python
downloads, source overrides and keyring subprocesses, and uses the existing
local build cache. It cannot install or run the resulting wheel. A toolchain
mismatch fails before staging, and unavailable cached build dependencies fail
explicitly. Build-cache dependency resolution is not release dependency
attestation or a license/provenance decision.

Child build tools receive only a closed environment of locale, time, builder
workspace paths and fixed Git configuration controls. Provider credentials,
Python import overrides and uv/pip configuration are not inherited.

Before wheel construction the builder hashes every archived `src/adhelm` file and
every bundled skill. After construction it requires that source inventory to
remain identical. It inspects the wheel as bounded ZIP data without importing
or executing it. The wheel must contain exactly those application/skill files
with matching bytes, plus the four expected distribution metadata files. The
console entrypoint is fixed. Closed identity/tag checks and the generated
module's RECORD hash/size reject conflicting package or binding facts. Foreign
payloads, duplicate members, traversal paths, symlink entries, oversized data,
missing files and source changes fail before the artifact output is created.

The manifest records a canonical source-payload digest and count and the bridge
source hash. Together these checks prevent a correct generated binding from
masking altered packaged bridge code. They do not attest the operating system,
installed host or Python runtime, protect against hostile code already executing
inside the process, or replace release signing.

The final deterministic tar includes the Python wheel, manifest, app, both
executable native components and optional entitlement template. Only the
Core-only native metadata inspector executes during the configured build. No
configured host, helper or wheel is launched, installed, signed, registered or
notarized. Local nonce-state setup, explicit runtime integration and real
OS/provider evidence remain separate requirements.
