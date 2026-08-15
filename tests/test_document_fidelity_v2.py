"""NEXT-013 native location, lexical repair, and container-selection acceptance."""

from __future__ import annotations

from local_steward.document_evidence import build_document_evidence_selection
from local_steward.document_query import (
    DEHYPHENATED_MATCH_MODE,
    SOFT_HYPHEN_MATCH_MODE,
    WHITESPACE_MATCH_MODE,
    match_document_text,
)
from local_steward.file_agent.runtime.structured_documents import (
    NormalizedDocumentItem,
    StructuredDocumentParserAdapter,
)


def _item(
    text: str | None,
    *,
    kind: str = "paragraph",
    role: str = "PARAGRAPH",
    location: dict[str, int | str] | None = None,
) -> NormalizedDocumentItem:
    return NormalizedDocumentItem(
        kind,
        text,
        None,
        location or {"ordinal": 1},
        None,
        "node:1",
        role,
    )


def test_lexical_representation_repairs_preserve_explicit_match_mode() -> None:
    soft = match_document_text("inter\u00adnational", "international")
    wrapped = match_document_text("inter-\nnational", "international")
    spacing = match_document_text("alpha\n   beta", "alpha beta")

    assert soft is not None and soft.mode == SOFT_HYPHEN_MATCH_MODE
    assert wrapped is not None and wrapped.mode == DEHYPHENATED_MATCH_MODE
    assert spacing is not None and spacing.mode == WHITESPACE_MATCH_MODE
    assert match_document_text("alpha", "semantic synonym") is None


def test_workbook_evidence_publishes_native_sheet_and_cell_locator() -> None:
    items = (
        _item(
            "revenue formula =B2*2",
            kind="xlsx_cell",
            role="FORMULA",
            location={"sheet": "Summary", "sheet_index": 1, "cell": "C2"},
        ),
    )

    selection = build_document_evidence_selection(
        items,
        source_sha256="a" * 64,
        query="revenue formula",
        mode="MATCH",
        context_items=0,
        max_characters=1024,
        limit=10,
        offset=0,
        searchable=True,
        source_format="XLSX",
    )

    native = selection.slices[0].items[0].native_location
    assert native["kind"] == "WORKBOOK_CELL"
    assert native["label"] == 'sheet "Summary", cell C2'
    assert native["locator"] == "sheet:Summary/cell:C2"


def test_query_map_reports_native_slide_container_quality() -> None:
    items = (
        _item(
            "Quarterly result",
            kind="pptx_text",
            location={"slide": 3, "shape": 2},
        ),
        _item(
            "Unrelated",
            kind="pptx_text",
            location={"slide": 4, "shape": 1},
        ),
    )

    selection = StructuredDocumentParserAdapter._targeted_evidence_selection(
        items, "quarterly", source_format="PPTX"
    )

    assert selection.strategy == "FAST_QUERY_MAP_THEN_NATIVE_CONTAINER_QUALITY"
    assert selection.matched_container_ids == ("slide:3",)
    assert selection.selected_container_ids == ("slide:3",)
    assert selection.container_qualities[0].native_label == "slide 3"
    assert selection.container_qualities[0].quality.status == "SUFFICIENT"


def test_auxiliary_fidelity_items_publish_format_native_citation_kinds() -> None:
    cases = (
        (
            "DOCX",
            _item(
                "comment evidence",
                kind="docx_comment",
                role="NOTE",
                location={"comment": 7},
            ),
            "WORD_COMMENT",
            "comment:7",
        ),
        (
            "XLSX",
            _item(
                "reviewed cell",
                kind="xlsx_comment",
                role="NOTE",
                location={"sheet": "Summary", "cell": "A1", "comment": 1},
            ),
            "WORKBOOK_COMMENT",
            "sheet:Summary/cell:A1/comment:1",
        ),
        (
            "PPTX",
            _item(
                "speaker evidence",
                kind="pptx_speaker_notes",
                role="NOTE",
                location={"slide": 2, "notes": 1},
            ),
            "PRESENTATION_SPEAKER_NOTES",
            "slide:2/notes:1",
        ),
        (
            "PPTX",
            _item(
                "cached chart evidence",
                kind="pptx_chart",
                role="FIGURE",
                location={"slide": 2, "shape": 3, "chart": 1},
            ),
            "PRESENTATION_CHART",
            "slide:2/shape:3/chart:1",
        ),
        (
            "PPTX",
            _item(
                "accessibility evidence",
                kind="pptx_accessibility",
                role="CAPTION",
                location={"slide": 2, "shape": 1, "accessibility": 1},
            ),
            "PRESENTATION_ACCESSIBILITY",
            "slide:2/shape:1/accessibility:1",
        ),
        (
            "PDF",
            _item(
                "annotation evidence",
                kind="pdf_annotation",
                role="NOTE",
                location={"page": 3, "annotation": 2},
            ),
            "PDF_ANNOTATION",
            "page:3/annotation:2",
        ),
    )

    for source_format, item, expected_kind, expected_locator in cases:
        selection = build_document_evidence_selection(
            (item,),
            source_sha256="b" * 64,
            query="evidence" if source_format != "XLSX" else "reviewed",
            mode="MATCH",
            context_items=0,
            max_characters=1024,
            limit=10,
            offset=0,
            searchable=True,
            source_format=source_format,
        )
        native = selection.slices[0].items[0].native_location
        assert native["kind"] == expected_kind
        assert native["locator"] == expected_locator
