# GA4 key-event CREATE contract ultra review

This independent review covers the pure
`adhelm.providers.ga4_create_contract` boundary. It uses fake-only inventories
through `httpx.MockTransport`; it does not open a credential store, access a
provider, write history, or authorize a mutation.

The review added 23 adversarial cases in
`tests/test_ga4_create_contract_ultra_review.py`. They verify the following
properties.

- The action reconstructs one fixed zero-spend `ga4_admin` CREATE subplan with
  one fixed host and scope, no recovery action, no predicted resource ID, and
  explicit non-authority, non-permission, and required-readback facts.
- The authorizable `CompositePlan` changes when any tested source provenance
  changes: configuration or profile-configuration digest, final checkpoint,
  selection time, observation interval, or complete inventory. A plan built
  from one set of facts cannot pass full keyed binding against another.
- Fresh conversion input prose drift cannot reproduce the retained compiler
  candidate. Raw scalar aliases, unknown fields, alternate scopes, recovery
  actions, compiler-body changes, and constructed authority or permission
  promotions fail through a fixed public error without echoing the injected
  value.
- A structurally coherent but forged semantic transition can be retained only
  as an untrusted closed shape. Its plan digest changes, and
  `validate_ga4_key_event_create_plan_binding` rejects it when it reconstructs
  the state from the current fingerprint key, fresh input, and inventory.
- The raw-input preflight bounds UTF-8 bytes in both object keys and values
  before structural parsing. This closes an oversized-key path that previously
  bypassed the stated one-mebibyte input bound.

Two review findings were repaired by the source owner before this review
froze. The original action reference did not commit all outer observation and
execution provenance, so a coherent configuration or inventory rewrite could
retain the same plan digest. It now hashes the full closed candidate,
selection, observation, execution, native request, and semantic-state facts.
The JSON preflight now includes dictionary-key bytes in its per-string and
aggregate limits.

`revalidate_ga4_key_event_create_planned_action` is deliberately a structural
retention parser. It does not establish provider freshness, OAuth validity,
permission, reservation, execution authority, or keyed semantic authenticity.
Consumers that use a retained action at a trusted boundary must call
`validate_ga4_key_event_create_plan_binding` with fresh selected facts and the
current fingerprint key, then perform the separate configured reread,
authorization, guarded write, and readback gates.

Validation after the repairs:

- `pytest -q tests/test_ga4_create_contract.py tests/test_ga4_create_contract_ultra_review.py` — 38 passed.
- Adjacent M21–M23 fake-only group — 173 passed.
- Scoped Ruff check and formatting, source mypy, and `git diff --check` passed.
