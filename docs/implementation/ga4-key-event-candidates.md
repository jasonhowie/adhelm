# GA4 key-event candidate history

`measurement key-event-candidate` compiles one fixed GA4 Admin key-event
create candidate locally and can retain that historical review in the canonical
`changes` JSONL ledger. It is a separate measurement-candidate record rather
than an advertising `CANDIDATE_COMPILED` variant: a GA4 property key event has
no campaign, creative, or spend surface.

The input explicitly selects a retained conversion-definition record and one
declared destination profile. The library revalidates both the original
key-event input and the selected conversion contract before canonicalization.
It accepts only this structural subset:

- the conversion definition declares `EACH_QUALIFYING_OCCURRENCE`;
- it declares `NO_MONETARY_VALUE`;
- the GA4 request uses `ONCE_PER_EVENT` and no default value; and
- each declared event field is an exact Boolean conversion parameter with the
  same name and requiredness. Every required Boolean conversion parameter must
  be represented. Required integer or enum parameters, or an event field that
  would bind to either one, fail closed because the installed event contract
  cannot prove their bounded domain.

The record pins the exact retained definition record digest, definition content
digest, contract ID/version, declared effective time, selected destination
profile, consent reference, and the non-sensitive field structure. It retains
the fixed compiler candidate needed to revalidate the historical review, but it
does not retain the source `EventContract`, customer semantics prose, customer
statements, or an arbitrary input brief. The public catalog exposes only
digests, version/effective-time declarations, Boolean-field count, compact
uncertainty facts, and the number of unresolved planning gates. `show` carries
the complete redacted prospective-effects and planning-boundary maps. Neither
surface emits an event name, field name, native property or stream ID, native
request, destination/profile reference, or customer prose.

The retained candidate has one installed identity representation: its property
and Web-stream references must exactly equal
`ga4_admin.property.<property_client_ref>` and
`ga4_admin.web_stream.<web_stream_client_ref>`. The lower-level compiler keeps
an injected resolver seam for its own isolated tests, but that seam is not
accepted by this durable candidate surface. A rehashed local label therefore
cannot become a different retained property or stream claim.

The ledger accepts only `GA4_KEY_EVENT_CANDIDATE_COMPILED` records with
`provider: ga4_admin`, an account reference equal to the candidate's declared
property reference, `LOCAL_CLI` source, and null run/occurrence/observation
timestamps. Under the same changes-stream append lock, it requires that the
bound conversion-definition record is the latest preceding version for that
contract ID. A later definition blocks a new candidate made from an older
version. Historical candidates remain readable: scans compare each candidate
to the latest definition that preceded it at its original position, not a
later definition.

The candidate is always `OFFLINE_CANDIDATE_NOT_EXECUTABLE`, with
`FIXTURE_VERIFIED_LIVE_UNVERIFIED` compiler evidence, no remote side effects,
and no execution authority. It preserves the compiler's unresolved requirements
for current property and stream identity, event collection/reserved-name and
duplicate inventory, pre-apply drift recheck, semantics/privacy/consent,
exact authorization, configuration readback, browser/network, realtime,
processed reporting, and a separate native conversion mapping. The conversion
definition's effective timestamp is retained only as
`DECLARED_NOT_EVALUATED`; it does not schedule, activate, or establish a
current effect.

This structural binding does not prove the customer statement, source-event
mapping, collection, consent enforcement, property identity, duplicate status,
native mapping, browser behavior, GA4 receipt, processed reporting, or any
provider authorization. It performs no provider request, credential access,
browser operation, or mutation. Future planning must compile a new current
plan, obtain current provider evidence, and use the normal exact authorization
and drift controls before any write.

The library rejects raw Boolean/float aliases for fixed integer or Boolean
controls, nested constructed/copy-bypassed models, Python-only mapping values
such as `Decimal`, mismatch between the selected source record and the retained
definition, changed consent/destination/version/content binding, and altered
compiler field structure. It validates scalar types before RFC 8785
canonicalization so `1.0` and `true` cannot become integer `1` at a durable
boundary.
