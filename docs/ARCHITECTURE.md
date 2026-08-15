# Architecture

STEWARD is organized around a deliberately small authority model: source files are
user-owned, immutable Evidence records historical observations, and SQLite provides a
rebuildable operational index.

## Core layers

### Configuration and scopes

A TOML configuration names the internal data directories and the filesystem roots
that may be observed. Scope resolution canonicalizes paths, rejects excluded or
protected paths, and does not treat a historical scope identifier as current
filesystem authorization.

### Runs and Evidence

Each persistent operation has a Run ledger. Evidence records are canonical JSON with
ordered sequence numbers, previous-record digests, and a record digest. Terminal Run
state and Snapshot state are verified independently.

Evidence is the historical fact source. Existing records are not rewritten to make a
later health check pass.

### Derived storage

SQLite stores query-oriented projections of Runs, Evidence records, Snapshots, and
Entries. The database can be rebuilt from compatible Evidence. Operational replay
classifies every item and excludes only explicitly governed lifecycle inconsistencies;
it does not silently perform best-effort recovery.

Read operations use guarded immutable SQLite sessions. Writer connections, replay,
migration, backup, replacement, and rollback stay on a separate boundary.

### Snapshot queries

Snapshot inventory, verification, Entries, diffs, relations, duplicate groups,
structure, and growth queries verify their Evidence and Run dependencies before
publishing business results. Pagination is deterministic and all filters are part of
the request identity.

### Current document and media observation

Current-file inspection is separate from Snapshot authority. A parser operation pins
the source identity, uses a format-specific resource profile, and rejects publication
if the source changes before release.

Parser outputs are normalized into document or media graph items with provenance and
logical locations. OCR, ASR, diarization, scene detection, and visual-semantic results
remain model-derived observations. Results are not appended to Snapshot Evidence.

### Agent integration

The CLI and Python APIs are the product authority. MCP and Codex-plugin surfaces are
adapters over the same configuration and services. Host tool approval remains owned by
the host; STEWARD does not invent a second authorization token or persist task memory.

## Data flow

```text
configured source ──read──> bounded observation
       │                        │
       │                        ├──> operation result (non-persistent)
       │                        │
       └──Snapshot──> immutable Evidence ──replay──> SQLite index
                              │                         │
                              └────────verify──────────┘
```

## Non-goals

- Automatic cleanup, organization, deletion, quarantine, or deployment
- Treating model output as verbatim source text
- Treating a historical Snapshot as the current filesystem
- Persistent indexing of document contents or decoded media by default
- Using SQLite as the sole source of historical truth
