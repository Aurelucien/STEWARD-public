# Changelog

This project follows a pre-1.0 development model. User-visible changes are grouped
here instead of being embedded as an internal execution timeline in the README.

## Unreleased

- Adopt the Apache License 2.0 for the public source release.
- Prepare a deterministic, privacy-reviewed public source export.
- Replace the internal status narrative with public installation, architecture,
  capability, security, and contribution documentation.
- Add memory-bounded page-local OCR for scan-heavy PDFs.
- Add continuation-based pagination for long audio and video result sets.
- Add resilient EPUB fallback and safe parser failure diagnostics.
- Preserve local-only Snapshot, document, image, audio, and video evidence boundaries.

## 0.1.0

- Establish immutable Run and Evidence ledgers.
- Add rebuildable SQLite indexing and verified Snapshot inspection.
- Add bounded Snapshot acquisition, refresh, change review, structure, growth,
  duplicate, and relation queries.
- Add local structured-document and media inspection foundations.
