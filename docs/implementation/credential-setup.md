# Local credential setup

Credential setup manages only entries in the approved operating-system
credential store for profiles that already exist in local configuration. It
does not perform OAuth, provider requests, account discovery, external CLI or
MCP invocation, or remote revocation.

Status, store, rotate, and local-remove bind an exact configured profile and
credential reference. Results include only profile, provider, slot, reference,
state, and operation. They never include a secret or backend exception detail.
Machine status is bounded and never prompts.

Interactive store and rotate obtain a value only through a hidden
local-terminal prompt. Both stdin and stderr must be TTYs before prompting, so
the prompt cannot fall back to stdin. A `GetPassWarning` is treated as a failed
prompt before fallback or echo. Empty, oversized, and control-character values
fail locally. Secrets are never accepted from JSON, configuration, arguments,
logs, or agent chat.

Store preserves an existing entry. Rotate is the explicit replacement
operation. Google Ads can use its separately configured secondary developer
token reference; other providers reject a secondary slot. Explicit-CI profiles
are CI-managed and cannot be changed through local OS-store setup.

OS-store references are unique across all configured OS-store primary and
secondary slots. Explicit-CI references name environment variables and have a
separate namespace, so they can be shared by CI profiles. The
export-pseudonymization and OpenAI pause credential-binding references are
reserved in every source. Local remove deletes only the local entry and reports
LOCAL_REMOVE_ONLY. It makes no claim of provider remote revocation.

Before an OS-store mutation, AdHelm obtains a nonblocking, case-insensitive,
per-OS-user lock for the exact credential reference. The private lock root is
independent of `--data-dir`; it stays held across backend access, the hidden
prompt, both reads, the write or removal, and final readback. A second
cooperating AdHelm setup returns a bounded busy result before opening the
credential store or prompting. Export-pseudonymization and OpenAI pause
credential-binding key initialization each use their own reserved slot lock and
require exact post-write readback before use.

The coordinator is implemented only on macOS and Linux with the required
POSIX no-follow filesystem behavior. On other platforms, local credential
mutation fails closed before a store write or prompt. Each mutation still
checks its pre-prompt state and final local readback. A same-UID process that
does not cooperate with AdHelm's lock can directly alter the OS store or its
private coordination path; no portable keychain API can prove atomic behavior
against that external intervention. A failed or mismatched readback therefore
reports an unverified local change rather than success.
