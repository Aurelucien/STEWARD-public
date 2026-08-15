# Evidence and storage

STEWARD records historical filesystem observations as immutable JSON Evidence and
uses SQLite schema version 3 as a rebuildable query index. The database is not a
replacement for Evidence and can be discarded and reconstructed when necessary.

## Runs and Evidence records

Every persistent Snapshot operation has a Run identifier and an append-only Run
ledger. Evidence envelopes contain an Evidence identifier, sequence, previous-record
digest, payload, and record digest. Canonical JSON and SHA-256 make byte-level
changes detectable.

A structurally valid ledger may still describe an incomplete historical attempt. A
nonterminal Run is not rewritten into a success merely to improve current health.
STEWARD reports that lifecycle fact and keeps it separate from byte corruption.

## Snapshot versions

Snapshot Evidence version 1 records metadata observations. Version 2 adds the
Snapshot Evidence version, hash-policy and payload-observation facts, allocation
fields, and aggregate observation summaries. Fields that were not observed remain
unknown rather than being inferred as zero or false.

The current SQLite projection stores both versions under schema version 3. Historical
Evidence bytes and digests do not change when the derived schema changes.

## Verification and replay

Before publishing a historical Snapshot result, STEWARD verifies:

- the Evidence envelope and chain digests;
- the intrinsic Snapshot and ordered Entry digests;
- the associated Run identity and lifecycle facts;
- the SQLite projection against authoritative Evidence; and
- any declared reuse dependency on an earlier verified Snapshot.

Strict replay remains available as an all-corpus consistency audit. Operational
replay also classifies each Snapshot as eligible or ineligible. Only the governed
Run/Snapshot lifecycle inconsistency may be excluded; corruption, unsupported or
unknown Evidence, missing authority, and unclassified states stop the rebuild.
Every exclusion remains visible in diagnostic accounting.

## Read and write boundaries

Writers own initialization, Snapshot persistence, migration, replay, backup, atomic
replacement, and rollback. Readers use operation-scoped immutable SQLite sessions,
reject WAL/SHM/journal sidecars, validate schema and source identity, and discard a
result if the database path, fingerprint, or directory state changes before
publication.

Current document and media reads do not append to Snapshot Evidence. Their output is
a source-pinned operation result and is non-persistent by default.

## Identity boundaries

An Entry is identified within one Snapshot by Snapshot, Scope, and relative path.
Device and inode values are observation hints, not durable cross-Snapshot identity.
Payload equality requires a complete verified hash observation; equal metadata is
not enough. Relations, duplicate groups, structure, and growth views are derived
query results, never new Evidence.
