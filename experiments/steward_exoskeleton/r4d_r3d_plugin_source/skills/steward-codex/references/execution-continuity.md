# Execution continuity

Read this reference only for Hook pickup, Git-workspace receipts, manual fixed-project observation, or plugin identity diagnosis.

When `STEWARD_HOST_OBSERVER_V1_ACTIVE` is present, trusted hooks already observe routine Codex command/edit activity in the actual Git workspace. Do not add manual STEWARD preflight/postflight calls or ask for a workspace path. If the observer provides one compact continuation at delivery, include its receipt identifier and material facts without running extra work solely for the receipt. Hook receipts are neither authorization nor Snapshot Evidence.

When the marker is absent, `steward_code_execution` is a manual fallback only for the configured STEWARD repository. Call `PREFLIGHT` before edits/commands and retain its baseline; call `POSTFLIGHT` afterward with that baseline and bounded `validation_claims`. The tool runs no command. Caller-reported checks remain `NOT_VERIFIED` by STEWARD. Preserve the receipt, change review, changes, unknowns, omissions, rollback boundary, and thread attribution concisely.

The expected identity is one tuple: plugin `steward-exoskeleton`, Skill `steward-codex`, MCP `local-steward-native`, current native/server versions, and Hook `STEWARD_HOST_OBSERVER_V1`. Use the loaded identity values rather than remembered version numbers. A service exposing only history/document/Snapshot tools is `STEWARD_PLUGIN_IDENTITY_MISMATCH`, commonly a stale process, old cache, broken binding, different Codex home, or repository product Skill. Do not claim the product lacks code execution or block authorized work; report `STEWARD_RECEIPT_UNAVAILABLE` only when a receipt matters.
