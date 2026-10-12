# Google desktop OAuth onboarding

This development slice adds explicit customer-owned Google desktop-client
enrollment and reconnect for the installed Google Ads, GA4 Data, GA4 Admin, and
GTM **READ** profiles, plus separate GA4 Admin **EDIT** (`analytics.edit`)
and Google Ads **EDIT** (`adwords`) profiles. It does not perform advertising
or measurement mutations. Google Ads uses the same broad `adwords` scope for
reads and writes; local purpose and bundle binding provide application-level
separation. The [Google Ads EDIT declaration](google-ads-edit-profile.md)
requires explicit account currency and timezone.

The selected agent discovers `google-oauth-profile-draft` and prepares a
nonsecret input containing the declared account/property, client reference,
scope, currency/timezone, and distinct credential references. The draft has no
principal and is never added to active configuration. A local user explicitly
starts `auth google-oauth enroll --input <draft> --client-config <private-file>
--interactive`. Reconnection uses `auth google-oauth reconnect --profile <id>
--client-config <private-file> --interactive` for the exact existing subject,
client, scope, and purpose. A READ profile cannot be widened through reconnect;
EDIT requires explicit enrollment of a separate profile and credential reference.
The client JSON comes from the customer's own desktop app;
the CLI neither registers an app nor bypasses Google app review requirements.

Client files must be bounded regular files, owned by the current OS user with
no group/other permissions or extra hard links. Symlink files are refused. The
reader accepts only the documented installed-client shape, checks endpoint
metadata, and always uses fixed runtime endpoints. File bytes, client secrets,
codes, tokens, raw subjects, and browser authorization URLs are never copied to
configuration, canonical history, logs, artifacts, or structured command output.
Starting with 0.1.1, the explicitly interactive terminal handoff displays only
the validated authorization URL on stderr so the user can open it manually.
This includes state/nonce/PKCE challenge, never the verifier, token or callback
code. The URL is not displayed without a local TTY. `--no-browser` skips the OS
launcher while preserving the same bounded, single-use loopback flow.
The OS credential bundle is a separate private format, not a public CLI schema.
It uses a dedicated fixed OS-store service, so legacy raw-token profiles cannot
retrieve bundle bytes even if they select the same reference. Both services
share the per-reference mutation lock; source-aware status and removal use the
appropriate service.

The protocol follows Google's installed-app flow: a system browser and IPv4
loopback callback, authorization code with S256 PKCE, state, and OIDC nonce.
The request contains `openid` plus the selected API scope and explicit consent
for an offline grant. The maintained `google-auth` verifier checks the signed
ID token through a fixed certificate fetch; application checks bind its nonce
and subject to the attempt. See [Google's native-app flow](https://developers.google.com/identity/protocols/oauth2/native-app),
[Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect),
and [Google's Python verifier](https://googleapis.dev/python/google-auth/latest/reference/google.oauth2.id_token.html).

OAuth HTTP is isolated from the ordinary provider transport. It permits only
the fixed token form POST and certificate GET, with bounded responses/timeouts,
no redirects, no environment-selected credentials/proxies, and no token-request
retry. Browser launch uses a fixed OS launcher, never shell interpolation,
ambient `BROWSER` configuration, or a caller-selected executable. A random
callback path, exact Host, bounded HTTP parsing, private clock checks, and a
single-use listener precede token exchange. Callback responses are fixed text.

The verified issuer/subject pair becomes a versioned, domain-separated SHA-256
operational reference. It identifies the OAuth subject; it is not an account
permission or resource-access proof. Native account IDs remain customer-declared
until the appropriate provider read workflow verifies them. Those workflows
capture one access token for identity discovery and subsequent reads in that
operation.

Local enrollment takes a configuration snapshot and a private credential-slot
snapshot before the browser opens. After authorization, it reacquires the
configuration lock and rejects changed configuration. It saves the final
nonsecret profile, then installs the exact bound grant under the credential
slot lock with an unchanged-slot check and exact readback. A local failure after
the config write reports uncertainty and leaves a profile that requires status
inspection/reconnection; it does not automatically repeat the browser flow.
Reconnection cannot overwrite a credential removed or replaced during the
handoff. Cooperating CLI configuration writers use the same lock; manual edits
and arbitrary local programs do not participate in it.

Refresh persists and verifies a protected `REFRESH_STARTED` state before token
egress. An unresolved attempt blocks automatic replay. A usable response must
preserve the sealed subject/client/scope binding and be written/read back before
its access token is returned. An omitted refresh response ID token is not fresh
identity verification; omitted scope is not fresh scope evidence. Local removal
uses the same lock and never claims provider revocation.

All development tests use synthetic token responses, keys, stores, sockets,
and launcher seams. No actual enrollment, OS secret-store exercise, provider
resource access, or release credential-lifecycle evidence is claimed. The
current private file/credential coordination path fails closed on unsupported
platforms; Windows ACL/locking, other write-purpose profiles and provider OAuth
lifecycles, and live evidence remain required work. The GA4 Admin edit scope is
broad management authority at Google; an enrolled EDIT bundle is never a
compiled plan or a direct/delegated execution authorization. See the
[protected lifecycle](google-oauth-credential-lifecycle.md) and
[paired resource-access diagnostic](ga4-admin-access.md).
