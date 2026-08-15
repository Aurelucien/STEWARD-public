# Evidence delivery

Read this reference only when answering from a STEWARD evidence packet or diagnosing an incomplete result.

For current-document `EVIDENCE`, treat `facts` as unique cited native nodes. Each slice's zero-based `fact_indexes` points into `facts`; `anchor_fact_index` and `anchor_citation_id` identify the query anchor. Cite the stable `citation_id` and its `native_location`. Preserve source SHA-256, verification, unknowns, omissions, bounds, and continuation when material.

Use compact diagnostics by default. `diagnostic_detail: "FULL"` adds the complete parser execution trace and resource accounting without changing the facts, citations, selection digest, or source identity. Do not repeat a successful call merely to obtain diagnostics or a cache hit.

For `EVIDENCE_SET`, keep every document packet distinct. Preserve each source, current SHA-256, historical relation when present, packet digest, citations, status, unknowns, omissions, and sibling failure. A successful sibling does not erase another item's failure, and a missing match in one file is not a corpus-wide negative claim.

Lexical matching performs only reported normalization such as NFKC/case folding, soft-hyphen removal, line-break dehyphenation, or whitespace normalization. It does not provide semantic similarity or implicit OCR correction. If a natural concept produces `NO_EVIDENCE`, use native `STRUCTURE` once to identify an exact heading before one focused retry. A `TIMEOUT` is different: preserve it as an incomplete attempt and do not automatically launch broad structure parsing; use one shortest distinctive literal term for a fresh focused request only when necessary.

For visual results, inspect the returned image before making visual-semantic claims. The image is ephemeral; structured provenance retains source SHA-256, page/node/crop, renderer, projection, and artifact digest.

Never fill an unknown with a guess or present packet interpretations as observed facts.
