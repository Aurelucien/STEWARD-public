# Document and media inspection

STEWARD provides local, bounded parsing for non-code files while keeping deterministic
source facts distinct from model-derived observations.

## Formats

| Kind | Formats | Typical observations |
| --- | --- | --- |
| Documents | PDF, EPUB, DOCX, XLSX, PPTX | text, hierarchy, tables, formulas, comments, notes, annotations, forms, charts |
| Images | PNG, JPEG, TIFF | bounded render, dimensions, visual region, optional OCR |
| Audio | WAV, FLAC, MP3, M4A, AAC, OGG, Opus | streams, speech regions, ASR, optional aligned words and anonymous turns |
| Video | MP4/M4V, MOV, MKV, WebM | streams, chapters, subtitles, scenes, frames, OCR, ASR, visual candidates |

## Routing

The parser chooses the lowest-cost route that can answer the requested question:

- `STRUCTURE` inspects containers and native hierarchy without requesting broad text.
- `LOCATE` finds bounded matching nodes or time ranges.
- `EVIDENCE` returns focused, citation-bearing observations.
- `READ` returns a paginated broad projection.
- `VIEW` renders one page, region, image, or video timestamp.

Large packages are scanned incrementally. Large spreadsheets use constant-state XML
mapping for focused evidence. EPUB has a tolerant native HTML fallback. Scan-heavy
PDFs use one RapidOCR engine with bounded page rendering and page disposal rather than
retaining a whole-document OCR image set.

## Pagination and reuse

Broad audio, video, and document reads can contain more items than one response. The
result reports `returned_count`, `full_item_count`, `has_more`, and a continuation or
accepted next offset. Continuations are bound to the source digest and analysis
configuration. Process-memory caching may reuse a completed parse, but it is bounded,
non-persistent, and never substitutes for source identity checks.

## Provenance

Each published item keeps its source kind and native location where available:

- document page, section, paragraph, sheet, cell, slide, table, or formula
- audio source-time range and model identity
- video stream, presentation timestamp, scene, frame, subtitle, OCR, or ASR origin
- stable citation and source digest in evidence results

OCR, ASR, alignment, diarization, scene detection, and visual-semantic retrieval are
model-derived. They may be incomplete or wrong. STEWARD records engine/model versions,
confidence or quality diagnostics when available, and coverage omissions.

## Resource behavior

Sources are streamed into bounded staging rather than loaded wholesale. OOXML and EPUB
containers are checked for member count, expanded size, duplicate or encrypted members,
compression ratio, unsafe relationships, and oversized control parts. Raster decoding
uses bounded projection and subsampling. Audio and video decode only bounded windows
required by the selected depth.

Resource failure is fail-closed: a result is not published as complete after timeout,
memory exhaustion, source mutation, unsupported structure, or missing authority.
