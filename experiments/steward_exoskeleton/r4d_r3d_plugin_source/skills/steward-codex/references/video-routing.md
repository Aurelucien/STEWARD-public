# Video routing

Use one `steward_read_document` route for MP4/M4V/MOV/MKV/WebM:

- Broad content: `READ`; omit `video_analysis` for bounded `MULTIMODAL`.
- Tracks/duration/codecs/dimensions: `STRUCTURE` (no media decode).
- Literal speech/subtitle phrase: `EVIDENCE` plus `content_query`; subtitle/ASR text anchors
  bound nearby decode.
- Visual event without words/time: `EVIDENCE` plus a short English visual `content_query`;
  omit `video_analysis` for the default `MULTIMODAL` route. Never use `SCENES` for a visual content query.
  Exact subtitle text wins and avoids visual search; otherwise the local model scans bounded
  temporary frames across the whole source.
- Phrase positions: `LOCATE` plus `content_query`. Known-time appearance: `VIEW` plus
  `video_timestamp_ms`; otherwise time zero.

Use `video_analysis: SCENES` only for unqueried frames; OCR requires `SCENES_AND_OCR` or
`MULTIMODAL_AND_OCR`. Do not call `CAPABILITIES`, split media or invoke audio separately.

Preserve SHA-256, exact times/tracks, stream, decode plan, policies, continuation and source
kinds. `EMBEDDED_SUBTITLE` is container text; `AUDIO_ASR`, `FRAME_OCR` and `VIDEO_TEXT_TRACK`
are derived; frames are ephemeral. Temporal overlap never proves semantic agreement.
`VIDEO_TEXT_TRACK` does not prove continuous visibility.

`VISUAL_SEMANTIC_RETRIEVAL` is a derived candidate, not truth. Preserve score, model identity,
time and `retrieval_candidate_not_truth`; use `VIEW` before a visual claim. Missing runtime is
an omission, never a match; do not request installation. `NO_TEXT_ANCHOR_FALLBACK` covers only
the leading window.

Preserve omissions; never invent content, identity or tracks. Pass `video_continuation`
unchanged only when needed; never persist frames/transcripts.
