# ADR 0001: deterministic Python core and explicit adapter boundaries

> Names superseded by [ADR 0013](0013-open-source-release.md): the product, package and command were renamed to AdHelm. The text below is the historical record.

Accepted 2026-09-04 for implementation; release remains IMPLEMENTING.

Use the build handoff's Python 3.12, uv, Typer, Pydantic v2, httpx, keyring,
SQLite, pytest, Ruff and mypy stack. `uv.lock` pins the resolved dependency
graph. The console executable is `adcp`; its version begins at `0.1.0.dev0`.
No installed command implies a release or live provider support claim.

The `src/adcp` package owns common schema, errors, canonicalization, privacy,
configuration, transport, storage, and execution. Provider adapters receive
explicit injectable transports. They expose typed operation contracts; there
is no user-facing HTTP/query/shell passthrough. Default tests block sockets and
use explicitly synthetic transport fixtures. The executable has no model,
customer-managed CLI/MCP, telemetry, or required vendor-service dependency.

RFC 8785 canonicalization uses the pinned `rfc8785` implementation. Reject
duplicate input JSON keys, non-finite numbers, and integer values outside the
interoperable range. Money is an integer plus an explicit currency. Provider
responses are parsed in memory, screened, and normalized before persistence.

JSONL is authoritative. SQLite is rebuildable. File locks protect stream
appends and separate coordinator locks protect authorization consumption.
Provider conditional writes, identity checks, and drift checks remain required;
local locks do not exclude provider UI edits. No connection is established by
module import, discovery, config inspection, or local doctor.

Standalone signed binaries, OCI distribution, platform signing, full schema
migrations, optional DuckDB, and local retention execution remain required work.
The Python wheel is a development artifact and cannot satisfy those release gates.

Consequences: one testable implementation keeps schemas and safety consistent;
freezing Python and native extractor dependencies will require separate
cross-platform packaging evidence. Reconsider the stack only if that release
work establishes a concrete blocker and migration cost.
