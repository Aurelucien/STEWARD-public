# Audio routing

Use one `steward_read_document` call for selected WAV, FLAC, MP3, M4A/AAC,
OGG/Vorbis or Opus.

- “What does it say?” or broad transcription → `READ`.
- Duration, codec, channels or sample rate without transcription → `STRUCTURE`.
- A known literal phrase with cited context → `EVIDENCE` plus `content_query`.
- Positions/counts for a known phrase → `LOCATE` plus `content_query`.

Omit `audio_analysis` for ordinary transcription. Select deeper work only when needed:

- exact word timing, word-level navigation or subtitle timing → `ALIGNED_WORDS`;
- “who speaks when”, conversation turns or speaker count → `SPEAKER_TURNS`;
- speaker turns together with word timing → `ALIGNED_WORDS_AND_SPEAKERS`.

Speaker labels are anonymous and file-local. Overlap is a model-derived intersection,
not source separation. Distinguish approximate, aligned and unaligned words.

English, Japanese and Chinese are the evaluated local alignment registry. Other
languages retain transcription; preserve typed unavailable/unaligned results and
Never silently substitute another language model or depth.

Pass `audio_language` only when known. Never use `VIEW`, OCR, cloud or a temporary
parser. Do not call `CAPABILITIES`; a typed unavailable result is sufficient.

Do not correct ASR from filenames or expectations. Preserve source SHA-256, model,
`base_transcript_digest`, `MODEL_DERIVED`, uncertainty, citations and
`AUDIO_TIME_RANGE`. Report aligned/unaligned counts; approximate timing is not exact.
`diarization_quality` covers observed coverage/overlap only; with
`REFERENCE_UNAVAILABLE`, do not claim diarization accuracy. Label translation or
summary as interpretation.

Pass `audio_continuation` unchanged only when remaining time matters. Never repeat a
success, retry a timeout or rediscover a unique source. Broad `READ` already contains
enough evidence; load `evidence-delivery.md` only for focused evidence.
