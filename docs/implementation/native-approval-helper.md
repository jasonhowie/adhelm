# Private installed approval helper

The fixed `adhelm-macos-approval-client` target connects the private Python pipe
exchange to the dedicated native bridge route. It is prepared implementation;
the checked-in compiled metadata remains unconfigured. No public CLI command
invokes it, and no configured helper, host, XPC peer or OS approval was executed
for this increment.

The helper accepts no arguments and requires the exact three-variable transport
environment. Its standard descriptors must be pipes. It validates its own
compiled client signing requirement before creating the fixed authenticated
native client. Request and candidate payloads use four-byte big-endian lengths,
role-specific caps, a single frame and EOF. Input chunks are bounded; incomplete,
trailing or second frames permanently poison the decoder. Decoder output is
consumable once and detached from mutable caller storage.

A 300-second monotonic deadline covers the entire exchange. UTC observations
must not move backwards. Parent disconnects are checked during input, immediately
before client creation and request, while waiting, and before output writes.
Client invalidation runs on every outcome after creation. A lost parent can
still race any local check; disconnection cancels the native interaction, and
there is no receipt replay or rollback of a consumed nonce. A late candidate or
nonzero helper exit cannot become Python approval acceptance.

The client can return only a typed candidate or a fixed public failure. The
helper revalidates candidate bytes against the original request, compiled
metadata and its own clock, then writes at most 16 KiB per operation. Full output
is still rejected by the Python parent unless it sees clean EOF on stdout and
stderr plus successful process exit. There is no acknowledgement or retry.

The development artifact builder now compiles and stages the helper at
`AdHelmApprovalHost.app/Contents/MacOS/adhelm-macos-approval-client` alongside the host.
Both binaries use the same generated metadata source; the builder checks that
source between tool calls, validates each binary's architecture/deployment/SDK
commands, and hashes both into the manifest and deterministic archive. Only the
Core-only metadata inspector is executed. The helper needs its distinct compiled
client signing identity before installation; building bytes does not establish
that identity. The manifest explicitly records that neither host nor client was
started. No signing, registration, installation or Keychain setup is performed.

Validation uses in-memory pipe/client/time operations, synthetic signed native
candidates and fake artifact tools. The native validator may execute the default
helper's refusal path only after requiring the exact checked-in unconfigured
metadata hash. The configured artifact builder never calls that smoke path.
