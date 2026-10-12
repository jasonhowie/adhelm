# GA4 Admin EDIT inventory independent review

This review covers the configured, read-only GA4 Admin EDIT inventory path:
`ga4_admin_edit_workflow.py`, its dedicated observation/history boundary, and
the installed `measurement ga4-admin-edit` read, catalog, and show commands.
It uses synthetic `httpx.MockTransport` responses and local temporary ledgers
only. It does not use a customer credential, browser, provider endpoint, or
remote write.

`tests/test_ga4_admin_edit_workflow_ultra_review.py` adds 19 adversarial
cases. They verify that malformed public values, a non-EDIT profile, wrong
scope, nonnumeric native IDs, and omitted explicit-CI enablement fail before
credential or HTTP access. A successful configured read captures one EDIT
token and uses it for exactly the property, selected-Web-stream, and key-event
GETs. Configuration changes after token capture or after the last GET reject
the result. The CLI also rechecks configuration after its verified ledger
checkpoint and before acquiring the local sharing key, and rechecks again
before durable retention.

The cases inject a copied inventory with a changed content marker and an
otherwise authentic inventory for a different target. Both fail at the
workflow boundary. They also reject raw float/bool aliases in observation and
inventory controls, require the current fingerprint key before append, and
show that a physically rehashed ledger record with mismatched GA4 provider or
account provenance is rejected during checkpoint selection.

Historical catalog/show calls do not contact a provider. Pagination controls
require exact integers. At the saved 1 KiB minimum, the compact receipt and
catalog fit; a review that cannot fit fails before export-key acquisition
instead of omitting a row. The retention failure case returns the bounded
`MEASUREMENT_RETENTION_UNVERIFIED` receipt and does not claim a record was
written. Output checks keep native references, event names, token text, and
fingerprint material out of the returned data.

Focused validation used:

```text
./.venv/bin/python -m pytest -q \
  tests/test_ga4_admin_edit_inventory.py \
  tests/test_ga4_admin_edit_inventory_ultra_review.py \
  tests/test_ga4_admin_edit_workflow.py \
  tests/test_ga4_admin_edit_workflow_ultra_review.py
# 69 passed

./.venv/bin/ruff check tests/test_ga4_admin_edit_workflow_ultra_review.py
./.venv/bin/ruff format --check tests/test_ga4_admin_edit_workflow_ultra_review.py
```

This is a fixture-only, sequential configuration observation. A successful
GET does not prove granted OAuth scope metadata, create permission, capacity
reservation, runtime firing, processed receipt, fresh provider state after the
read interval, authorization, or execution authority. Matching markers prove
only local keyed-content integrity while the transient fingerprint key is
available; historical `show` intentionally reports content authentication as
not checked.
