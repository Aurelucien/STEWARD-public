# Contributing

STEWARD welcomes focused bug fixes, tests, documentation improvements, parser
adapters, and reproducible performance work.

## Development setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
```

Run the standard checks before submitting a change:

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/mypy src/local_steward
.venv/bin/pip check
git diff --check
```

## Change expectations

- Preserve the distinction between source data, immutable Evidence, derived indexes,
  and model-derived observations.
- Keep reads bounded, source-pinned, and fail-closed when a file changes.
- Do not add automatic file mutation, provider transmission, or credential discovery
  to an existing read path.
- Add regression tests for accepted behavior and typed failure behavior.
- Mark OCR, ASR, diarization, visual-semantic, and similar outputs as model-derived.
- Avoid committing real user files, private corpora, absolute host paths, transcripts,
  model caches, or runtime acceptance artifacts.

## Test fixtures

Prefer small synthetic fixtures generated during a test. Public third-party fixtures
must have a clear license, pinned source URL, declared SHA-256, and bounded download.
Tests must not download data unless they are explicitly marked as external-corpus
tests.

## Pull requests

Explain the user impact, the boundary being changed, and the checks performed. Keep
unrelated refactors separate from behavioral fixes. Security issues should follow
[SECURITY.md](SECURITY.md) instead of a public pull request.
