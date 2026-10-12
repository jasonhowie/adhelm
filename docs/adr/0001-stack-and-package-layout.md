# ADR 0001: Stack and package layout

> Names superseded by [ADR 0013](0013-open-source-release.md): the product, package and command were renamed to AdHelm. The text below is the historical record.

- Status: Superseded in part by 0001-local-runtime.md; the shipped package is `adcp` with flat modules under src/adcp.
- Date: 2026-09-04

## Decision

Use Python 3.12 with `uv`, Typer, Pydantic v2, `httpx`, `keyring`, SQLite,
pytest, Ruff, and mypy. Package one `lacp` distribution with these top-level
modules:

```text
lacp.cli             agent-first command and rendering adapters
lacp.schemas         versioned provider-neutral records
lacp.kernel          authorization, identity, drift, egress, and recovery
lacp.ledger          canonical JSONL and deterministic projections
lacp.providers       one adapter package per provider plus conformance
lacp.measurement     GA4, GTM, CRM aggregate, and native conversion adapters
lacp.creative        local creative identity and analysis
lacp.evidence        redacted evidence and release manifests
```

Provider packages depend on schemas and narrow kernel ports, not on the CLI.
SQLite is a rebuildable index; optional DuckDB reads verified projections only.
No model, browser, external CLI, MCP server, hosted database, or vendor service
is a core dependency.

## Consequences

- One shared kernel and conformance suite prevents six unrelated products.
- Injectable transports, clocks, credential stores, filesystems, and ID sources
  keep default tests credential-free and network-free.
- Platform packaging and local analyzers may require isolated optional extras,
  but unsupported extras must fail explicitly.
- A stack change requires a superseding ADR with release, migration, and
  reproducibility evidence.
