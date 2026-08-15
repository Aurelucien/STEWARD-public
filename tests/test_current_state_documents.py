from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_public_entry_documents_describe_the_current_product_boundary() -> None:
    readme = _read("README.md")
    architecture = _read("docs/ARCHITECTURE.md")
    capabilities = _read("docs/CAPABILITIES.md")
    evidence = _read("docs/EVIDENCE-AND-STORAGE.md")
    media = _read("docs/DOCUMENT-AND-MEDIA.md")

    assert "immutable JSON Evidence" in readme
    assert "rebuildable query index" in readme
    assert "does **not** organize, move, delete" in readme
    assert "Apache License 2.0" in readme
    assert "acceptance transcript" in readme
    normalized_architecture = " ".join(architecture.split())
    assert "source files are user-owned" in normalized_architecture
    assert "SQLite" in architecture and "rebuildable" in architecture
    assert "Model-derived observation" in capabilities
    assert "schema version 3" in evidence
    assert "rebuildable" in evidence
    assert "RapidOCR" in media
    assert "not persisted" in media or "non-persistent" in media


def test_public_entry_documents_do_not_publish_internal_release_timeline() -> None:
    documents = (
        _read("README.md"),
        _read("docs/ARCHITECTURE.md"),
        _read("docs/CAPABILITIES.md"),
        _read("docs/EVIDENCE-AND-STORAGE.md"),
        _read("docs/DOCUMENT-AND-MEDIA.md"),
    )
    combined = "\n".join(documents)
    assert "/Users/" not in combined
    assert "/private/tmp/" not in combined
    assert "+codex.20" not in combined
    assert "NEXT-027" not in combined
    assert "LOCAL-V2A" not in combined
    assert "FORMAL ACCEPTANCE" not in combined
