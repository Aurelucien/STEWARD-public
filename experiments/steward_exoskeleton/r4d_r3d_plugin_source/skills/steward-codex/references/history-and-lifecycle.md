# History and Snapshot lifecycle

Read this reference only for `steward_history`, `steward_update_snapshot`, or `steward_recover_snapshot_run`.

Use deterministic selectors: `EXACT_ID` for a supplied or previously returned identity, `ONLY_COMPATIBLE` only when exactly one candidate works, `LATEST_VALID` for the newest historical baseline, and `PREVIOUS_VALID` for its immediate predecessor. Never treat a historical Snapshot as current filesystem truth.

For a natural historical question, use `ANALYZE_SNAPSHOT` with `analysis_profile: "AUTO"`, an exact or deterministic Snapshot selector, and a bounded question. `AUTO` chooses only an existing projection and publishes its profile/base decision. Preserve the grounded packet's facts, citations, Evidence anchors, verification, unknowns, omissions, continuation, and routing decision. Do not silently replace a typed profile or continuation failure with another selector.

Use `steward_update_snapshot` only when the user requests acquisition or refresh. Prefer the only compatible Scope, a named configured root, or an exact Scope identity; Codex presents the configured write approval. Snapshot operations append Evidence and update the derived index but do not alter user files.

Use `steward_recover_snapshot_run` only for an exact incomplete Run ID. Recovery uses the host destructive-approval surface and does not scan or change user files.
