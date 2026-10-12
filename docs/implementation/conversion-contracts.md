# Customer-declared conversion contracts

Status: **IMPLEMENTING / UNVERIFIED**

`src/adhelm/conversion_contracts.py` defines a local, closed representation of a
customer's conversion meaning. It is a definition boundary only: it does not
contact a provider, discover a profile, inspect a website or CRM, retrieve a
record, emit an event, decide whether the declared meaning is true, or grant
execution authority.

The contract implements the customer-declared meaning requirement in
[`conversion-integrations.md`](../conversion-integrations.md). It does not turn
that declaration into native destination configuration, CRM attribution
evidence, browser proof, receipt proof, or a legal conclusion.

## Public API

```python
from adhelm.conversion_contracts import (
    ConversionContractInput,
    ConversionContract,
    compile_conversion_contract,
)

definition: ConversionContractInput
contract: ConversionContract = compile_conversion_contract(definition)
```

`ConversionContractInput` has these required definition fields (except its
fixed `schema_version=1`):

```text
schema_version = 1
contract_id: SafeRef
version: bounded positive integer
supersedes_ref: prior 64-hex content digest | null
business_outcome: BusinessOutcome
authoritative_source: AuthoritativeSource
qualifying_occurrence: QualifyingOccurrence
exclusions: tuple[Exclusion, ...]
counting_and_deduplication: CountingAndDeduplication
value_and_currency: ValueAndCurrency
allowed_parameter_schema: tuple[ConversionParameter, ...]
prohibited_parameters: tuple[closed prohibited categories, ...]
consent_contract_ref: SafeRef
destination_profiles: nonempty tuple[SafeRef, ...]
effective_from: aware datetime
```

The compiled wrapper has exactly this serialized shape:

```text
ConversionContract
  schema_version = 1
  definition: ConversionContractInput
  content_digest: 64-hex digest(definition)
  provenance = CUSTOMER_DECLARED
  execution_authority = false
```

`compile_conversion_contract()` rebuilds the complete nested input with
Pydantic before calculating the digest. This prevents `model_copy()` or
`model_construct()` from bypassing a child validator. The wrapper validator
recomputes the digest from `definition`, so a persisted wrapper with a changed
definition or digest is rejected during normal model validation.

The digest is RFC 8785 canonical JSON of the normalized `definition` only. An
aware timestamp is normalized to UTC. Printable statement whitespace is
preserved exactly rather than trimmed or otherwise normalized; blank statements
are rejected. This makes the content-addressing rule explicit without silently
altering customer wording.

## Closed definition fields

`BusinessOutcome`, `QualifyingOccurrence`, and each `Exclusion` carry an
opaque `SafeRef` plus a bounded `CustomerDeclaredStatement`. `AuthoritativeSource`
also records one closed source kind (`CRM_AGGREGATE`, `ANALYTICS_AGGREGATE`,
`NATIVE_DESTINATION`, or `CUSTOMER_DECLARED_BUSINESS_SYSTEM`) and a `SafeRef`.
These statements are customer declarations, not semantic evidence.

`CountingAndDeduplication` requires one internally consistent combination:

| Counting semantics | Required deduplication semantics | Required duplicate handling | Required window |
| --- | --- | --- | --- |
| `ONCE_PER_QUALIFYING_OCCURRENCE` | `SOURCE_MANAGED` | `EXCLUDE_DUPLICATES` | `NOT_APPLICABLE` |
| `ONCE_PER_DECLARED_WINDOW` | `SOURCE_MANAGED` | `EXCLUDE_DUPLICATES` | `CALENDAR_DAY` or `CONVERSION_WINDOW` |
| `EACH_QUALIFYING_OCCURRENCE` | `NOT_APPLICABLE` | `COUNT_EACH` | `NOT_APPLICABLE` |

No person-level deduplication key is accepted.

`ValueAndCurrency` preserves declared value and null behavior with a closed
combination of:

- `FIXED_VALUE`: a matching non-null `Money` and currency, with
  `NOT_ALLOWED` null semantics;
- `SOURCE_REPORTED_VALUE`: no local `Money`, one declared currency, and either
  `NOT_ALLOWED` or `PRESERVE_AS_UNAVAILABLE` for source nulls; or
- `NO_MONETARY_VALUE`: no `Money` or currency and `NO_MONETARY_VALUE` null
  semantics.

Source nulls are never converted to zero.

Each `ConversionParameter` declares `required` explicitly and can only be a
`BOOLEAN`, a bounded `INTEGER` (both endpoints required; span at most 100,000),
or a `LOW_CARDINALITY_ENUM` (one to 20 fixed values). It cannot use arbitrary
JSON Schema, dynamic code, an evaluator, a URL/DOM field, or a person,
identifier, click-ID, secret, form, or CRM-record name. Parameter names are
lowercase ASCII snake case and must be unique.

`prohibited_parameters` is a complete, exact declaration of the fixed boundary:

```text
PERSONAL_IDENTIFIER
AUTHENTICATION_SECRET
CLICK_IDENTIFIER
URL_OR_QUERY
DOM_OR_PAGE_TEXT
FORM_VALUE
CRM_RECORD_IDENTIFIER
```

Unknown fields are rejected by `StrictModel`. The shared data screen also
rejects sensitive value shapes before they can become a definition or digest.

## Version boundary

The input validator requires `version == 1` to use `supersedes_ref = null` and
every later version to name a syntactically valid prior content digest. It does
not inspect history, infer a prior version, or prove that the referenced digest
belongs to the same contract. Retention code must validate version succession,
prior digest binding, and effective-time ordering while holding its ledger lock.

## Focused checks

`tests/test_conversion_contracts.py` covers canonical digest binding, nested
revalidation after `model_copy()`, the non-executable wrapper, version form,
UTC normalization, blank declarations, closed fields, requiredness, finite
parameter domains, sensitive/identifier/URL/DOM name rejection, explicit
value/null/counting semantics, and person-level value screening.
