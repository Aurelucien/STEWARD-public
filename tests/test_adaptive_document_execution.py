"""Acceptance for adaptive routing and bounded process-memory parse reuse."""

from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock

from local_steward.document_execution import (
    BoundedDocumentParseCache,
    initial_document_profile,
)
from local_steward.file_agent.runtime import (
    ProjectOwnedBoundedDocumentIngress,
    ScopeBinding,
    ScopeBindings,
    StructuredDocumentParserAdapter,
)
from local_steward.file_agent.runtime.structured_documents import _WorkerExecution


def _payload(backend_name: str, text: str, *, role: str = "PARAGRAPH") -> dict[str, object]:
    return {
        "backend_name": backend_name,
        "backend_version": "test-version",
        "warnings": [],
        "items": [
            {
                "kind": "test_block",
                "text_or_value": text,
                "parent": None,
                "location": {"page": 1},
                "role": role,
            }
        ],
    }


@dataclass
class _Worker:
    backend_name: str
    text: str
    role: str = "PARAGRAPH"
    calls: int = 0
    paths: list[Path] = field(default_factory=list)

    def run(self, source_path: Path) -> _WorkerExecution:
        self.calls += 1
        self.paths.append(source_path)
        return _WorkerExecution(
            "COMPLETE",
            _payload(self.backend_name, self.text, role=self.role),
            25,
            4_096,
        )


@dataclass
class _NativeFidelityWorker:
    calls: int = 0

    def run(self, _source_path: Path) -> _WorkerExecution:
        self.calls += 1
        return _WorkerExecution(
            "COMPLETE",
            {
                "backend_name": "PyMuPDF4LLM",
                "backend_version": "test-version",
                "warnings": [],
                "items": [
                    {
                        "kind": "pdf_form_field",
                        "role": "FORM_FIELD",
                        "text_or_value": "approved evidence field",
                        "parent": "page:1",
                        "location": {"page": 1, "form_field": 1},
                        "extension": {"field_name": "decision"},
                    }
                ],
            },
            25,
            4_096,
        )


def _adapter(tmp_path: Path) -> tuple[Path, StructuredDocumentParserAdapter, _Worker, _Worker]:
    root = tmp_path / "documents"
    root.mkdir()
    bindings = ScopeBindings((ScopeBinding("managed", root),), (str(root),), ("managed",))
    cache: BoundedDocumentParseCache[_WorkerExecution] = BoundedDocumentParseCache()
    adapter = StructuredDocumentParserAdapter(
        ProjectOwnedBoundedDocumentIngress(bindings), parse_cache=cache
    )
    fast = _Worker("PyMuPDF4LLM", "sufficient digital document text")
    deep = _Worker("Docling", "deep document text", "DOCUMENT")
    adapter.worker = fast  # type: ignore[assignment]
    adapter.docling_worker = deep  # type: ignore[assignment]
    return root, adapter, fast, deep


def test_planner_routes_supported_intents_without_model_guessing() -> None:
    assert initial_document_profile("PDF", "AUTO", "READ") == "FAST"
    assert initial_document_profile("PDF", "AUTO", "STRUCTURE") == "STRUCTURE_NATIVE"
    assert initial_document_profile("XLSX", "AUTO", "TABLES") == "FAST"
    assert initial_document_profile("PDF", "AUTO", "FORMULAS") == "ENRICHED"
    assert initial_document_profile("EPUB", "FAST", "READ") == "DEEP"
    assert initial_document_profile("PNG", "AUTO", "READ") == "DEEP"


def test_adaptive_read_stops_after_sufficient_fast_parse_and_reuses_it(tmp_path: Path) -> None:
    root, adapter, fast, deep = _adapter(tmp_path)
    (root / "same-a.pdf").write_bytes(b"%PDF-1.7\nidentical")
    (root / "same-b.pdf").write_bytes(b"%PDF-1.7\nidentical")

    first = adapter.observe(
        {"scope_id": "managed", "relative_path": "same-a.pdf", "parser_profile": "AUTO"}
    )
    second = adapter.observe(
        {"scope_id": "managed", "relative_path": "same-b.pdf", "parser_profile": "AUTO"}
    )

    assert first.execution is not None and second.execution is not None
    assert first.execution.attempts[0].cache_status == "MISS"
    assert second.execution.attempts[0].cache_status == "HIT"
    assert second.provenance.relative_path == "same-b.pdf"
    assert second.resources.parser_elapsed_ms == 0
    assert fast.calls == 1
    assert deep.calls == 0


def test_source_digest_change_invalidates_reuse(tmp_path: Path) -> None:
    root, adapter, fast, _deep = _adapter(tmp_path)
    source = root / "changing.pdf"
    source.write_bytes(b"%PDF-1.7\nfirst")
    first = adapter.observe(
        {"scope_id": "managed", "relative_path": source.name, "parser_profile": "AUTO"}
    )
    source.write_bytes(b"%PDF-1.7\nsecond")
    second = adapter.observe(
        {"scope_id": "managed", "relative_path": source.name, "parser_profile": "AUTO"}
    )

    assert first.provenance.source_sha256 != second.provenance.source_sha256
    assert second.execution is not None
    assert second.execution.attempts[0].cache_status == "MISS"
    assert fast.calls == 2


def test_low_quality_fast_result_escalates_once_to_deep(tmp_path: Path) -> None:
    root, adapter, fast, deep = _adapter(tmp_path)
    (root / "scan.pdf").write_bytes(b"%PDF-1.7\nscan")
    fast.text = ""
    deep.text = "recovered deep document text"

    result = adapter.observe(
        {"scope_id": "managed", "relative_path": "scan.pdf", "parser_profile": "AUTO"}
    )

    assert result.status == "COMPLETE"
    assert result.backend_name == "Docling"
    assert result.execution is not None
    assert result.execution.selected_profile == "DEEP"
    assert result.execution.escalation_reason == "INSUFFICIENT_EXTRACTABLE_TEXT"
    assert [attempt.profile for attempt in result.execution.attempts] == ["FAST", "DEEP"]
    assert "AUTO_ESCALATED_TO_DEEP" in result.warnings
    assert fast.calls == deep.calls == 1


def test_focused_pdf_evidence_uses_quality_gated_local_ocr(tmp_path: Path) -> None:
    root, adapter, fast, _deep = _adapter(tmp_path)
    (root / "scan.pdf").write_bytes(b"%PDF-1.7\nscan")
    fast.text = ""
    ocr = _Worker("Docling", "OCR target evidence", "DOCUMENT")
    adapter.macos_ocr_worker = ocr  # type: ignore[assignment]

    result = adapter.observe(
        {
            "scope_id": "managed",
            "relative_path": "scan.pdf",
            "parser_profile": "AUTO",
            "intent": "EVIDENCE",
            "content_query": "target evidence",
        }
    )

    assert result.status == "COMPLETE"
    assert result.execution is not None and result.execution.selection is not None
    assert result.execution.selected_profile == "OCR"
    assert result.execution.escalation_reason == "EVIDENCE_NATIVE_TEXT_INSUFFICIENT"
    assert result.execution.selection.strategy == "QUALITY_GATED_LOCAL_OCR"
    assert [attempt.profile for attempt in result.execution.attempts] == ["FAST", "OCR"]
    assert "EVIDENCE_QUALITY_GATED_LOCAL_OCR" in result.warnings
    assert fast.calls == ocr.calls == 1


def test_native_fidelity_evidence_is_not_replaced_by_generic_deep_text(
    tmp_path: Path,
) -> None:
    root, adapter, _fast, deep = _adapter(tmp_path)
    (root / "form.pdf").write_bytes(b"%PDF-1.7\nform")
    native = _NativeFidelityWorker()
    adapter.worker = native  # type: ignore[assignment]

    result = adapter.observe(
        {
            "scope_id": "managed",
            "relative_path": "form.pdf",
            "parser_profile": "AUTO",
            "intent": "EVIDENCE",
            "content_query": "approved evidence field",
        }
    )

    assert result.status == "COMPLETE"
    assert result.items[0].kind == "pdf_form_field"
    assert result.execution is not None
    assert result.execution.selected_profile == "FAST"
    assert [attempt.profile for attempt in result.execution.attempts] == ["FAST"]
    assert native.calls == 1 and deep.calls == 0


def test_focused_raster_evidence_uses_quality_gated_local_ocr(tmp_path: Path) -> None:
    root, adapter, _fast, deep = _adapter(tmp_path)
    (root / "scan.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
            "1W7h2AAAAABJRU5ErkJggg=="
        )
    )
    deep.text = ""
    ocr = _Worker("Docling", "image OCR evidence", "DOCUMENT")
    adapter.macos_ocr_worker = ocr  # type: ignore[assignment]

    result = adapter.observe(
        {
            "scope_id": "managed",
            "relative_path": "scan.png",
            "parser_profile": "AUTO",
            "intent": "EVIDENCE",
            "content_query": "OCR evidence",
        }
    )

    assert result.status == "COMPLETE"
    assert result.execution is not None and result.execution.selection is not None
    assert result.execution.selected_profile == "OCR"
    assert result.execution.selection.strategy == "QUALITY_GATED_LOCAL_OCR"
    assert [attempt.profile for attempt in result.execution.attempts] == ["DEEP", "OCR"]
    assert deep.calls == ocr.calls == 1


def test_cache_is_lru_bounded_and_single_flights_one_key() -> None:
    cache: BoundedDocumentParseCache[str] = BoundedDocumentParseCache(
        max_entries=1, max_bytes=128, ttl_seconds=60
    )
    calls = 0
    lock = Lock()
    started = Event()
    release = Event()

    def compute() -> str:
        nonlocal calls
        with lock:
            calls += 1
        started.set()
        release.wait(timeout=2)
        return "shared"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            cache.get_or_compute,
            ("digest", "PDF", "FAST"),
            compute,
            size_of=len,
            cacheable=lambda _value: True,
        )
        assert started.wait(timeout=2)
        second = pool.submit(
            cache.get_or_compute,
            ("digest", "PDF", "FAST"),
            compute,
            size_of=len,
            cacheable=lambda _value: True,
        )
        release.set()
        values = {first.result(timeout=2)[0], second.result(timeout=2)[0]}
    assert values == {"shared"}
    assert calls == 1

    cache.get_or_compute(
        ("other", "PDF", "FAST"), lambda: "other", size_of=len, cacheable=lambda _value: True
    )
    _value, status = cache.get_or_compute(
        ("digest", "PDF", "FAST"),
        lambda: "recomputed",
        size_of=len,
        cacheable=lambda _value: True,
    )
    assert status == "MISS"


def test_cache_ttl_and_failure_policy_are_deterministic() -> None:
    now = [100.0]
    calls = 0
    cache: BoundedDocumentParseCache[str] = BoundedDocumentParseCache(
        max_entries=2, max_bytes=128, ttl_seconds=5, clock=lambda: now[0]
    )

    def compute() -> str:
        nonlocal calls
        calls += 1
        return f"value-{calls}"

    key = ("digest", "PDF", "FAST")
    assert cache.get_or_compute(key, compute, size_of=len, cacheable=lambda _value: True)[1] == (
        "MISS"
    )
    assert cache.get_or_compute(key, compute, size_of=len, cacheable=lambda _value: True)[1] == (
        "HIT"
    )
    now[0] += 6
    assert cache.get_or_compute(key, compute, size_of=len, cacheable=lambda _value: True)[1] == (
        "MISS"
    )
    assert calls == 2

    failure_key = ("failure", "PDF", "FAST")
    assert (
        cache.get_or_compute(failure_key, compute, size_of=len, cacheable=lambda _value: False)[1]
        == "MISS"
    )
    assert (
        cache.get_or_compute(failure_key, compute, size_of=len, cacheable=lambda _value: False)[1]
        == "MISS"
    )
    assert calls == 4
