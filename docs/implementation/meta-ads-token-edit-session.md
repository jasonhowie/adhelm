# Private Meta Ads EDIT observation session

`configured_meta_ads_token_edit_session(directory, profile_id,
expected_configuration_digest=...)` is a private in-process preparation
boundary for one fixed Meta campaign-state read. It is not a CLI command,
credential resolver, plan, ledger operation, approval, grant, provider write,
or a claim that an EDIT profile may mutate an account.

The factory hard-codes purpose `EDIT` and required scope `ads_management`.
It rejects a stale or malformed composition configuration digest before token
inspection, and rejects a READ profile before the fixed inspector request. The
single Graph `debug_token` inspection requires the configured app, an expiring
`USER` token, and `ads_management`. It is followed by one fixed account
identity GET. The normalized native account identifier, currency, and IANA
timezone must exactly equal the selected customer declaration before a session
is yielded.

The common private module `_meta_ads_token_session` was extracted from the
existing READ session for this slice. It has no public factory or generic
client/credential surface. Its two internal fixed entry points hard-code READ
or EDIT policy, including separate HMAC continuity domains. For both facades it
captures the exact configuration and profile digests, profile ID, subject and
inspector tokens, and binding key in a keyed continuity marker. Before every
fixed provider GET and every retention callback, it rereads the complete local
configuration and protected bundle under configuration then credential locks,
checks that marker, and requires both finite inspector expiries to remain
valid. Raw credentials are transient local values and are not retained by the
yielded context-manager frame.

The EDIT session exposes exactly:

- `identity`, a detached validated `MetaAdsIdentity`;
- `binding`, a detached nonserializable `MetaAdsTokenEditBinding` containing
  only profile/app references, derived principal reference, fixed required
  scope, credential-binding reference, configuration/profile digests, and the
  three validated inspection times;
- `read_campaign_state(campaign_id)`, one fixed `GET /v26.0/<campaign-id>`
  with `id,account_id,configured_status,effective_status`; and
- `retention_recheck()`, a local owner-thread-only context manager whose
  callback may be called repeatedly immediately before a future durable append.

The campaign reader accepts only native decimal campaign/account references and
the reviewed configured/effective status literals. It uses the same guarded
subject-token callback immediately before its sole GET. It returns only the
closed normalized state and does not retain a payload, token, generic HTTP
client, unknown scopes, inspector token, raw user identifier, or provider error
text.

During `retention_recheck`, session provider reads are rejected. The callback
does no network or ledger I/O, is bound to the owning thread, becomes invalid
on context exit, and requires at least one successful invocation. This permits
a future local history writer to recheck continuity immediately before its own
append without holding configuration or credential locks across a provider
request.

All coverage uses in-memory credential stores and `httpx.MockTransport`. The
focused M39 group contains 101 cases across the shared READ regression, EDIT
session, fixed campaign reader, and integration tests. No test opened an
operating-system store or contacted Meta.

The final root validation passed 190 affected cases, including this group,
Meta inventory/report workflows, generic read/report/artifact regressions and
the development validator. Strict mypy passed across 142 source files. These
are working-source checks; exact archived validation is recorded separately.

This observation establishes neither campaign revision/currentness nor pause
authority. A later explicit plan and guarded mutation slice must separately
re-read state, bind a closed pause action, obtain its own authorization, append
durable intent before egress, and reconcile ambiguity.
