# ADR 0005: Threat model and egress control

- Status: Accepted
- Date: 2026-09-04

## Decision

Treat local files, provider/model text, remote responses, URLs, browser pages,
artifacts, and external agents as untrusted inputs. Enforce schema allowlists,
sensitive-data rejection, output encoding, exact destination policy, and
operation-bound authorization before data crosses a trust boundary.

Default execution has no vendor proxy, telemetry, crash upload, hosted store,
automatic update, license heartbeat, or generic shell/MCP/SQL/SOQL passthrough.
Network access is denied unless the command's declared capability and current
plan allow the exact provider endpoint class. Browser tests separately bind
public host/path, consent states, payload field names, and destination domains.

Regulated mode rejects person-level identifiers, audience files, raw CRM rows,
entered values, DOM/page text, sensitive URL/data-layer values, raw click IDs,
person-level events, and enhanced identifiers before display, persistence,
evidence, or egress.

The actionable controls and boundary inventory are in
[`threat-model.md`](../threat-model.md) and [`egress-map.md`](../egress-map.md).

## Consequences

- Network-capture tests must prove declared destinations and denied-state
  behavior.
- Necessary account/resource IDs and approved request bodies may reach only the
  selected provider; exports pseudonymize identifiers.
- Local execution reduces vendor custody but is not an air gap or compliance
  certification.
