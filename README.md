# STEWARD

STEWARD is a local-first macOS toolkit for verifiable filesystem snapshots and
bounded inspection of documents and media. It records historical observations as
immutable JSON Evidence and treats SQLite as a rebuildable query index.

The project is designed for tools and agents that need useful local context without
silently modifying user files or sending their contents to a provider.

> STEWARD is pre-1.0 software. Its read and evidence boundaries are deliberate, but
> command names and optional parser profiles may still evolve.

## What it does

- Creates metadata-only filesystem Snapshots for explicitly configured scopes.
- Verifies Snapshot Evidence, Run lifecycle, digests, and the derived index before
  publishing historical results.
- Lists and queries historical entries with deterministic pagination.
- Reviews changes, directory structure, growth, exact payload groups, and bounded
  cross-Snapshot relations.
- Reads PDF, EPUB, DOCX, XLSX, PPTX, PNG, JPEG, and TIFF through bounded local parser
  profiles.
- Transcribes supported audio locally and returns approximate source-time citations.
- Inspects supported video streams, subtitles, scenes, representative frames, OCR,
  and local ASR while keeping those modalities distinguishable.
- Exposes CLI, Python, MCP, and Codex-plugin integration surfaces over the same local
  configuration and evidence model.

STEWARD does **not** organize, move, delete, quarantine, or rewrite user files. It is
not a malware scanner and does not claim that historical Snapshots describe the
current filesystem.

## Requirements

- macOS
- Python 3.11 or newer
- SQLite
- Optional parser/media dependencies for the formats you want to inspect

## Installation

Clone the repository and create an isolated environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
```

Smaller dependency profiles are available:

```bash
.venv/bin/pip install -e .
.venv/bin/pip install -e ".[agent]"
.venv/bin/pip install -e ".[document-fast]"
.venv/bin/pip install -e ".[document-deep]"
.venv/bin/pip install -e ".[audio]"
```

The `full` and `dev` profiles include the complete local parsing stack and are much
larger than the core installation.

## Configuration

Copy the example and edit only the scopes you intend STEWARD to observe:

```bash
cp config/steward.example.toml config/steward.toml
```

`config/steward.toml`, the derived database, caches, Evidence, and quarantine data
are ignored by Git. Scope paths are resolved locally and are never part of the
public release export.

Check configuration and runtime readiness before acquiring a Snapshot:

```bash
.venv/bin/local-steward --config config/steward.toml config validate
.venv/bin/local-steward --config config/steward.toml doctor
.venv/bin/local-steward --config config/steward.toml runtime capabilities
```

## Snapshot workflow

Initialize the rebuildable index and acquire one configured scope:

```bash
.venv/bin/local-steward --config config/steward.toml storage init
.venv/bin/local-steward --config config/steward.toml snapshots acquire \
  --scope downloads --yes
```

Inspect only verified historical facts:

```bash
.venv/bin/local-steward --config config/steward.toml snapshots list
.venv/bin/local-steward --config config/steward.toml snapshots verify SNAPSHOT_ID
.venv/bin/local-steward --config config/steward.toml snapshots entries \
  SNAPSHOT_ID --limit 100 --offset 0
```

Use `--format json` for the stable automation envelope. Verification failure prevents
Snapshot or Entry business results from being published.

## Document and media inspection

The public CLI reads one confirmed file inside a configured scope:

```bash
.venv/bin/local-steward --config config/steward.toml documents inspect \
  --scope downloads \
  --path "report.pdf" \
  --yes \
  --evidence \
  --content-query "battery degradation"
```

Large and scan-heavy files use format-aware streaming or page-local projections
instead of loading the complete expanded document into memory. OCR, ASR, speaker,
scene, and visual-semantic outputs are marked as model-derived observations rather
than verbatim source facts.

See [Document and media inspection](docs/DOCUMENT-AND-MEDIA.md) for supported formats,
depths, pagination, and accuracy boundaries.

## Data model

STEWARD separates four concerns:

1. **Source files** remain user-owned and unchanged.
2. **Evidence** is immutable historical observation data.
3. **SQLite** is a replaceable index derived from Evidence.
4. **Parser results** are bounded operation results and are not persisted by default.

This distinction lets a historical inconsistency remain visible without treating a
healthy operational index as corrupted. See [Architecture](docs/ARCHITECTURE.md) and
[Evidence and storage](docs/EVIDENCE-AND-STORAGE.md).

## Privacy and security

- Core Snapshot and document workflows are provider-free.
- Optional provider adapters require explicit configuration and environment-based
  credentials.
- User file contents, decoded media, OCR, ASR, and embeddings are not committed to
  the repository.
- Current-file reads revalidate source identity and fail closed on replacement or
  mutation.
- The public export excludes local configuration, Evidence, databases, caches,
  transcripts, tool ledgers, and retained internal acceptance runs.

Read [Security and privacy](SECURITY.md) before testing STEWARD on sensitive data.

## Development

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src/local_steward
.venv/bin/pip check
git diff --check
```

The public release is generated from an explicit allowlist:

```bash
.venv/bin/python scripts/export_public_release.py /path/to/empty/output
```

The exporter writes a relative-path and SHA-256 manifest. It retains the current
Codex integration source while excluding internal experiment harnesses, retained
acceptance transcripts, and superseded plugin archives.

## Project documents

- [Capabilities](docs/CAPABILITIES.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Evidence and storage](docs/EVIDENCE-AND-STORAGE.md)
- [Document and media inspection](docs/DOCUMENT-AND-MEDIA.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## License

STEWARD is licensed under the [Apache License 2.0](LICENSE).
