# Security and privacy

## Reporting a vulnerability

Use GitHub private vulnerability reporting when it is enabled for this repository.
Do not place credentials, private file contents, personal paths, or exploit details in
a public issue. If private reporting is unavailable, open a minimal issue requesting a
private contact channel without including sensitive details.

## Local data boundary

STEWARD is intended to run with normal user permissions. A configured scope grants
read access only to the paths selected by the operator. Snapshot acquisition records
metadata and optional bounded payload observations; it does not grant permission to
modify files.

Current-document and media parsing is read-only. Temporary staged files, decoded
frames, OCR results, ASR transcripts, embeddings, and parser caches are bounded and
non-persistent unless a caller explicitly creates a separate artifact outside
STEWARD.

## Credentials and providers

Core product paths do not require a network provider. Optional OpenAI-compatible
adapters read credentials from environment variables and must never serialize them
into Evidence, logs, exceptions, or test artifacts. Rotate a credential immediately
if it is ever committed, even if the commit is later removed.

## Repository hygiene

The public release exporter excludes:

- `config/steward.toml`
- live SQLite databases and sidecars
- Evidence, caches, and quarantine contents
- experimental run directories
- acceptance transcripts and tool ledgers
- superseded installed-plugin copies

Before publication, run a full-history secret scanner and inspect the generated
`PUBLIC_RELEASE_MANIFEST.json`. A successful scan reduces risk but is not proof that
all published information is harmless.

## Supported security boundary

STEWARD is an observation and evidence tool, not an antivirus product, sandbox, data
loss prevention system, or access-control substitute. Optional ClamAV and YARA checks
are diagnostics and do not make untrusted files safe to execute.
