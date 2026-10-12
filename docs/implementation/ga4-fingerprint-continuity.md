# GA4 Admin fingerprint-key continuity

`Ga4AdminReadClient.inventory()` includes two optional markers in each newly
collected `Ga4AdminInventory`. The reader derives both locally with HMAC-SHA-256
under the same private fingerprint key already required for the fixed GA4 Admin
inventory. Neither accepts caller input.

`fingerprint_key_marker` has a fixed, versioned, domain-separated input:

```text
adhelm-ga4-admin-fingerprint-v1\0
inventory-key-marker\0
{"marker_purpose":"GA4_ADMIN_INVENTORY_KEY_CONTINUITY","marker_version":1}
```

It contains no GA4 property ID, web-stream ID, event name, rule content, URL,
customer statement, or provider response. It proves only key continuity.

`inventory_content_marker` uses the separate `inventory-content-marker` HMAC
purpose over the complete canonical normalized inventory surface: its identity,
every normalized key event and rule, `pagination_verified`, and
`fingerprint_key_marker`. It excludes only itself, which avoids self-reference.
The reader sorts all three member collections by local resource reference before
calculating this marker, matching the measurement workflow's deterministic
representation. Its domain starts with:

```text
adhelm-ga4-admin-fingerprint-v1\0
inventory-content-marker\0
```

The key remains transient in the reader and is never a model field, serialized
value, output value, or ledger field. Each marker is a 64-character lowercase
HMAC digest, not an unkeyed hash of low-entropy customer data.

The reader has no marker argument. It produces both values from its already
validated local fingerprint key only after its existing fixed, read-only
inventory succeeds. Reparsed history may contain syntactically valid markers so
it can be structurally checked, but parsing does not establish that a caller
held the claimed key or that either marker is trusted.

## Historical compatibility

Each field is optional only for inventories written before that field existed.
An omitted marker remains omitted when the inventory is dumped in JSON mode; it
does not become `null`. This preserves the exact canonical bytes and digests of
both markerless historical inventory and M18 key-marker-only inventory,
including their measurement snapshots and observations. Explicit JSON `null`
is rejected. The model schema exposes optional string-only properties, never
nullable/default-null inputs. The model also rejects Boolean and floating
aliases for its fixed schema version and pagination controls, and rejects
non-string, non-lowercase, or wrong-length markers before normalizing them.

The new content marker changes the digest of a newly read inventory and any
snapshot that contains it. That is intentional. It does not recanonicalize old
history.

## Meaning and limits

Matching `fingerprint_key_marker` values are only evidence that two new GA4
inventories used the same local fingerprint-key material and this exact marker
domain. Matching `inventory_content_marker` values additionally prove that the
supplied normalized content was HMACed under that key. They remain local
keyed-content evidence: any holder of the key can make a marker. They do not
prove that either inventory is current, authentic, collected under a particular
credential, complete at GA4, authorized, or unchanged remotely. They do not
prove capacity, consent, event semantics, browser firing, processed reporting,
native conversion mapping, or permission to mutate a provider.

A missing marker leaves the corresponding fact unknown. A comparison layer must
not query a key store to fill a historical gap. A duplicate-precondition layer
must require both fresh markers and verify the content marker before using an
opaque event-name fingerprint to claim an event is absent. This module does not
implement comparison policy, ledger ingestion, redaction, schema projection,
CLI behavior, credential access, or provider mutation.

`tests/test_ga4_admin_fingerprint_continuity.py` covers deterministic same-key
markers, different-key separation, both exact HMAC domains, deterministic
reader ordering before the content marker, no serialized key, strict malformed
scalar handling, constructed-model serialization guards, and pinned M16
markerless inventory/snapshot/observation digests.
