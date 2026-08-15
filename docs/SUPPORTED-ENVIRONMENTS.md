# Supported environments

This document separates what STEWARD requires, what the public release actually
tests, and what may work but is not yet a supported release target. A package being
installable is not, by itself, evidence that every parser or host integration is
available.

## Compatibility status

| Layer | Requirement or environment | Release status |
| --- | --- | --- |
| Operating system | macOS | Supported host family |
| CI host | GitHub-hosted macOS 14 on Apple silicon (`arm64`) | Validated on every public `main` change |
| Python | Python 3.11 or newer in package metadata | Python 3.11.x is CI-validated; newer versions are not yet release-gated |
| CPU | Apple silicon | CI-validated; Intel Mac is not yet release-gated |
| Database | Python's SQLite runtime and a local writable data directory | Required for Snapshot and storage workflows; no database server is required |
| Filesystem | Configured paths visible to the local macOS process | Local filesystem semantics are validated; mounted and network filesystems may differ |
| Linux and Windows | No formal release lane | Not yet supported or CI-validated; this is not a claim that they cannot work |

The supported core is therefore **macOS + Python 3.11 + local SQLite**. The
`requires-python = ">=3.11"` declaration means installation is permitted on newer
Python versions; it does not mean that every such version has passed the release
suite.

## Installation profiles

Install only the layer needed by the intended workflow:

| Profile | Enables | Additional runtime requirements |
| --- | --- | --- |
| Core (`pip install -e .`) | Configuration, Snapshot lifecycle, Evidence verification, SQLite storage, CLI and Python APIs | None beyond the supported core environment |
| `agent` | MCP and JSON-schema adapters | A compatible local MCP client; Codex-specific routing is not implied |
| `document-fast` | Lightweight PDF, Office and raster-image inspection | Format support depends on the bundled Python libraries |
| `document-deep` | Docling-backed document structure and deeper extraction | Requested model assets must already be available locally |
| `audio` | Local audio probing and ASR | FFmpeg/FFprobe and pre-provisioned ASR model assets |
| `audio-advanced` | Optional word alignment and anonymous speaker turns | FFmpeg/FFprobe plus the relevant pinned local model assets |
| `full` | All optional document, image, audio and video dependencies | FFmpeg/FFprobe and all model assets needed by the requested operations |
| `dev` | The `full` stack plus tests, linting and type checking | Same host requirements as `full`; used by contributors and CI |

`full` and `dev` are intentionally large because they include Docling, PyTorch,
Transformers and speech-processing dependencies. The core Snapshot product does not
require those packages.

STEWARD does not silently download a missing model while reading a file. Missing
binaries, parsers or models produce a typed degraded or unavailable result. Run
`runtime capabilities` on the target machine to inspect the effective stack instead
of inferring availability from the selected installation extra.

## External tools and host assets

- **FFmpeg and FFprobe** are required for supported audio and video inspection.
- **OCR, ASR, alignment, diarization and visual-semantic models** must be installed or
  cached before the operation that uses them. Their output remains model-derived.
- **ClamAV and YARA** are optional doctor integrations. Their absence does not make
  the supported Snapshot or inspection paths unavailable.
- Provider credentials are required only for separately configured provider
  adapters. The core Snapshot and local parsing paths are provider-free.

## Codex and MCP integration

The repository contains a local Codex plugin source and MCP adapters. The Codex
integration is validated in a local Codex host capable of loading the plugin skill,
starting its local MCP process and exposing host-hook metadata. It is an integration
layer, not a prerequisite for the CLI or Python API.

Other MCP clients may use the MCP adapter, but Codex-specific skill routing, host
hooks and execution receipts are not promised outside a compatible Codex host. All
surfaces must point at the same STEWARD configuration and data directory if they are
expected to observe the same Snapshots.

## Checking a target machine

After installation and configuration, inspect the effective environment:

```bash
.venv/bin/local-steward --config config/steward.toml config validate
.venv/bin/local-steward --config config/steward.toml doctor
.venv/bin/local-steward --config config/steward.toml runtime capabilities
```

For contributors, the portable public-CI lane is:

```bash
.venv/bin/pytest -q -m "not host_assets"
.venv/bin/ruff check .
.venv/bin/mypy src/local_steward
.venv/bin/pip check
```

The unfiltered local test suite additionally exercises tests marked `host_assets`.
Those tests require optional binaries or pinned model assets and are deliberately
kept separate from portable CI rather than being presented as universally available.
