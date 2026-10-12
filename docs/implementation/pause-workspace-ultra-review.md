# Workspace binding independent review

The independent regressions in `tests/test_pause_workspace_ultra_review.py`
check the new workspace binding against existing legacy pause and durable-plan
fixtures. They preserve the pinned legacy action digest, reject explicit null
or coerced workspace values, and prevent routing a new workspace plan into a
credential-role ledger. A retained new action also completes the existing
guarded kernel fixture with distinct workspace and credential identities.

Review reproduced an input-boundary defect in the new pure plan builder:
invalid expiry values escaped as a raw Pydantic error containing caller data.
The builder now rejects malformed or naive expiry inputs with a sanitized
application error before reconstruction. The regression includes a unique
untrusted marker and proves it is absent from the error.

The combined workspace, saved-plan and execution-plan review suite passed 93
tests in the working tree. The exact passing commit is recorded separately by
the development validator. Provider behavior used only the existing HTTP
fixture and synthetic approval host; this proves neither native approval nor
live provider conformance.
