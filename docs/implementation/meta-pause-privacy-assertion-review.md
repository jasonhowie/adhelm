# Meta PAUSE fixture privacy assertion review

The M51 exact validation recorded one failure in the synthetic Meta plan-artifact
partial-output test.  It occurred in a combined test-only assertion that
rejected either synthetic token or the five-character fixture user ID `99999`
anywhere in serialized JSON.

That check was not a reliable privacy boundary.  The result contains allowlisted
SHA-256 plan, record, artifact, and opaque-reference digests.  A fixed-width
digest can contain the same five-character sequence without disclosing the
fixture user ID.  The archived temporary directory named by the failed pytest
transcript had already been cleaned, and the transcript does not identify which
member of the combined assertion matched.  The deterministic regression proves
that an incidental derived-digest collision is possible; it does not attribute
that mechanism conclusively to the historical failure.

`tests/test_meta_pause_planning_integration.py` now parses the closed JSON
result and checks its structure.  It still rejects the synthetic subject and
inspector token strings anywhere.  It rejects raw account, campaign, profile,
application, and user markers in any non-derived string, including numeric
scalars; rejects sensitive field names such as `principal_ref`,
`credential_binding_ref`, and token or receipt fields; and requires the
published account/profile/campaign reference fields to use `opaque:<sha256>`
values.  Bare SHA-256 digests, `opaque:`/`evidence:sha256:` derived references,
and the safe writer's terminal content-addressed artifact filename may contain
an incidental fixture-ID substring.  Parent path components remain checked, so
a meaningful `/99999/` directory is rejected.  The closed timestamp fields
accept only canonical timezone-aware ISO values, so a fractional second may
contain incidental digits; timestamp-looking text under any other field is
still rejected.

This fixture assertion does not prove redaction for arbitrary future output
schemas or provider payloads.  Typed result schemas, the Meta plan review and
artifact tests, and the production redaction boundary remain the relevant
controls for those surfaces.  No provider response, credential, or customer
data was inspected during this review.
