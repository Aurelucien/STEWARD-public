# Capabilities

## Supported product paths

| Area | Supported behavior |
| --- | --- |
| Configuration | Validate local TOML configuration and configured scopes |
| Storage | Initialize, inspect, migrate, rebuild, back up, and validate the derived SQLite index |
| Snapshot lifecycle | Acquire, inspect acquisition state, recover an incomplete prefix explicitly, and refresh from a verified base |
| Historical inspection | List, verify, show, and page through Snapshot Entries |
| Historical analysis | Diff, change review, structure, growth, exact payload groups, and bounded cross-Snapshot relations |
| Documents | Bounded PDF, EPUB, DOCX, XLSX, PPTX, PNG, JPEG, and TIFF inspection |
| Audio | Local probe, VAD, transcription, optional word alignment, and anonymous speaker turns |
| Video | Stream metadata, subtitles, scenes, representative frames, frame OCR, local ASR, and bounded visual-semantic retrieval |
| Agent surfaces | JSON CLI, Python API, MCP adapters, and a local Codex plugin source |

## Evidence and accuracy classes

- **Authoritative historical evidence:** validated Run and Snapshot Evidence.
- **Derived deterministic fact:** reproducible query output from verified Evidence.
- **Current source observation:** pinned to one current-file identity for one operation.
- **Model-derived observation:** OCR, ASR, alignment, diarization, scene, or visual
  interpretation with explicit approximation and provenance.
- **Candidate:** retrieval or relationship hypothesis that requires verification and
  must not be presented as a confirmed fact.

## Dependency profiles

| Profile | Purpose |
| --- | --- |
| Core | Snapshot, storage, CLI, and local system observation |
| `agent` | MCP and JSON-schema integration |
| `document-fast` | Lightweight Office, image, and PDF paths |
| `document-deep` | Docling-backed document structure |
| `audio` | Local speech transcription |
| `audio-advanced` | Alignment and diarization dependencies |
| `full` | Complete optional local parsing stack |

An installed extra describes which Python dependencies were requested; it is not a
claim that external binaries or model assets are present. The release-gated platform
and the requirements for each profile are listed in
[Supported environments](SUPPORTED-ENVIRONMENTS.md). Use `runtime capabilities` to
inspect the effective capability set on the current host.

## Important limits

- Admission ceilings prevent unbounded source or expanded-container processing; they
  are not promises that every file below a ceiling will produce a complete graph.
- Broad results are paginated and bounded. A continuation must be followed to claim
  complete coverage.
- Scan-heavy PDF OCR is page-local and model-derived.
- Audio timestamps are approximate unless an explicit alignment result says otherwise.
- Video sampling does not imply every frame was observed.
- Missing optional parsers or models produce typed degraded or unavailable results.

## Not supported

- User-file mutation, cleanup, renaming, or deletion
- OCR-based claims of exact typography or verbatim text without source verification
- Speaker identity recognition
- Automatic network upload of file contents
- Persistent full-content search across all user files
- General malware containment or sandboxing
