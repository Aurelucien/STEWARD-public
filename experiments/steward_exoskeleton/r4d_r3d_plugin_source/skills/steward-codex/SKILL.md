---
name: steward-codex
description: "Use STEWARD for cited local PDF, EPUB, Office, image, audio/video reads; Snapshot history/lifecycle; and Git continuity when STEWARD_HOST_OBSERVER_V1_ACTIVE is present. Trigger for discovery, facts, summaries, transcription/timing/speakers, scenes/OCR, native data, visuals, and cross-file evidence. Avoid ordinary questions, code/plain text, web, sharing, and mutation."
---

# STEWARD Codex

Use only MCP `local-steward-native`. Codex owns approval/edits; Snapshot writes need explicit approval. Never invent paths or confirmation. Identity disagreement is `STEWARD_PLUGIN_IDENTITY_MISMATCH`; missing receipts do not block coding. CLI/Python remain product surfaces; this is their Codex adapter.

## Route once

| Need | Tool |
|---|---|
| Current supported file content | `steward_read_document` |
| Verified history or change analysis | `steward_history` |
| Git observation without an active host observer | `steward_code_execution` |
| Explicit Snapshot acquire/refresh | `steward_update_snapshot` |
| Exact incomplete Run recovery | `steward_recover_snapshot_run` |

## Read documents

Select `absolute_path`, filename/title `query`, or returned `scope_id` plus `relative_path`. Read absolute files directly; STEWARD binds one call. Do not copy, add a Scope, or ask for an ID. Request host permission only on access failure. `query` stays in configured Scopes; clarify ambiguity.

Use `EVIDENCE` plus literal `content_query` for facts; `STRUCTURE` for hierarchy/tracks; `LOCATE` for positions; `READ` for broad content; `EXTRACT_TABLE`/`EXTRACT_FORMULA` for native data; `VIEW` for appearance; `EVIDENCE_SET` across files; `DISCOVER` for candidates. Do not preflight with `CAPABILITIES`. After conceptual `NO_EVIDENCE`, use native `STRUCTURE` once for an exact heading. On `TIMEOUT`, preserve failure; do not launch `STRUCTURE`; retry `EVIDENCE` only with one short literal.

When `has_more`, continue with returned `next_offset`, the same `limit`, and exact `source_sha256` as `expected_source_sha256`.

Call simple `STRUCTURE` and known-time `VIEW` directly. For audio, read [audio-routing.md](references/audio-routing.md) only for transcription, alignment or speakers. Read [video-routing.md](references/video-routing.md) for scenes/OCR/continuation; [document-routing.md](references/document-routing.md) for format fidelity, tables/formulas, OCR or diagnostics; [evidence-delivery.md](references/evidence-delivery.md) for `EVIDENCE`, `EVIDENCE_SET` or `NO_EVIDENCE`. Use `diagnostic_detail: "FULL"` only to diagnose.

## Other workflows

Read [history-and-lifecycle.md](references/history-and-lifecycle.md) for Snapshot work. Read [execution-continuity.md](references/execution-continuity.md) for receipts, Hook pickup or identity mismatch.

## Publish grounded answers

Answer from returned facts. Preserve source identity, verification, citations, locations, unknowns and material omissions. `NO_EVIDENCE` means not found/searchable, not false. Never use STEWARD to share or mutate files.
