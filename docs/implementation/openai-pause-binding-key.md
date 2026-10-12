# OpenAI pause credential-binding key

Status: `IMPLEMENTING`. This is a local credential lifecycle for future
OpenAI pause planning; it is not a provider capability, authorization grant,
or live validation claim.

`openai_pause_binding_key(profile, *, ci_enabled=False)` returns the private
key used only by the already domain-separated OpenAI pause credential-binding
HMAC. It never reads a provider credential, constructs a provider client,
makes a request, writes a plan, or opens the export-pseudonymization key.

The key is stored under the fixed internal OS-store reference
`openai-pause-credential-binding-v1`. On the first OS-store use, AdHelm takes
the existing case-insensitive credential-mutation lock, generates exactly 32
random bytes with `secrets.token_hex(32)`, stores their 64-character hex
encoding, and requires an exact readback before returning decoded bytes. A
missing, short, non-hex, replaced, unreadable, or unverified value fails with
a bounded diagnostic. The key and any backend failure detail are never placed
in user-visible diagnostics, formatted tracebacks, plans, logs, evidence, or
CLI output.

This slot is distinct from `export-pseudonymization-v1`.

| Internal reference | Purpose | Rotation relationship |
| --- | --- | --- |
| `export-pseudonymization-v1` | HMAC pseudonymization of local projections and exports | Unchanged by pause-binding-key creation or replacement. |
| `openai-pause-credential-binding-v1` | Input key for the pause adapter's credential-binding HMAC | Unchanged by export-key creation or replacement. |

The two references are reserved case-insensitively in legacy primary and
secondary profile slots, OpenAI account-key slots, explicit-CI references,
configuration revalidation, credential access, and credential setup. They
cannot be selected as a provider secret or overwritten through provider
credential setup. A constructed Pydantic object is revalidated before either
provider or binding-key store access.

An `EXPLICIT_CI` OpenAI account-key profile may supply its provider credential
only through the existing explicitly enabled CI mechanism. It cannot supply
this separate binding key through an ambient environment variable, a file, or
another fallback. Until a separately reviewed CI binding-key lifecycle exists,
`openai_pause_binding_key` returns
`OPENAI_PAUSE_BINDING_KEY_CI_UNSUPPORTED` before environment or OS-store
access. Future pause planning must preserve that failure boundary.

There is no user-facing rotate, delete, export, or fallback operation for the
binding key. A future explicit rotation design must remain separate from export
pseudonymization rotation and re-plan any action whose credential-binding HMAC
would change. This lifecycle does not itself establish a current provider
identity, plan provenance, approval, execution result, or provider write.
