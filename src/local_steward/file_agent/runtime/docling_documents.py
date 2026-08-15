"""Offline Docling projection into STEWARD's backend-neutral document graph."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import version
import os
from pathlib import Path
import sys
from typing import Any, cast


_ROLE_BY_LABEL = {
    "title": "HEADING",
    "section_header": "HEADING",
    "text": "PARAGRAPH",
    "paragraph": "PARAGRAPH",
    "list_item": "LIST_ITEM",
    "table": "TABLE",
    "picture": "FIGURE",
    "formula": "FORMULA",
    "code": "CODE",
    "page_header": "HEADER",
    "page_footer": "FOOTER",
    "document_index": "METADATA",
    "footnote": "NOTE",
    "caption": "CAPTION",
}


def _reference(value: object) -> str | None:
    raw = getattr(value, "cref", None)
    if isinstance(raw, str):
        return raw
    raw = getattr(value, "self_ref", None)
    return raw if isinstance(raw, str) else None


def _safe_visual_region(document: object, provenance: object) -> dict[str, object] | None:
    bbox = getattr(provenance, "bbox", None)
    if bbox is None:
        return None
    page_no = getattr(provenance, "page_no", None)
    pages = getattr(document, "pages", None)
    if not isinstance(page_no, int) or not isinstance(pages, dict):
        return None
    page = pages.get(page_no)
    size = getattr(page, "size", None)
    width = getattr(size, "width", None)
    height = getattr(size, "height", None)
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool) for value in (width, height)
    ):
        return None
    converter = getattr(bbox, "to_top_left_origin", None)
    if callable(converter):
        try:
            bbox = converter(float(cast(int | float, height)))
        except (AttributeError, TypeError, ValueError):
            return None
    values = [getattr(bbox, name, None) for name in ("l", "t", "r", "b")]
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
        return None
    left, top, right, bottom = [float(cast(int | float, value)) for value in values]
    if right <= left or bottom <= top:
        return None
    return {
        "page": page_no,
        "bbox": [left, top, right, bottom],
        "page_size": [float(cast(int | float, width)), float(cast(int | float, height))],
        "coordinate_space": "PAGE_POINTS_TOP_LEFT",
    }


def _safe_annotations(item: object) -> list[dict[str, object]]:
    """Project bounded enrichment facts without retaining model objects or images."""

    projected: list[dict[str, object]] = []
    annotations = getattr(item, "annotations", None)
    if not isinstance(annotations, list):
        return projected
    for annotation in annotations[:8]:
        kind = getattr(annotation, "kind", None)
        if kind == "classification":
            classes: list[dict[str, object]] = []
            predicted = getattr(annotation, "predicted_classes", None)
            if isinstance(predicted, list):
                for candidate in predicted[:8]:
                    name = getattr(candidate, "class_name", None)
                    confidence = getattr(candidate, "confidence", None)
                    if isinstance(name, str) and isinstance(confidence, (int, float)):
                        classes.append({"class_name": name[:128], "confidence": float(confidence)})
            if classes:
                projected.append({"kind": "classification", "predicted_classes": classes})
        elif kind == "description":
            text = getattr(annotation, "text", None)
            if isinstance(text, str):
                projected.append({"kind": "description", "text": text[:2_048]})
    return projected


def _item_text(item: object, document: object) -> str | None:
    text = getattr(item, "text", None)
    if isinstance(text, str):
        return text
    exporter = getattr(item, "export_to_markdown", None)
    if callable(exporter):
        try:
            projected = exporter(document)
        except (AttributeError, TypeError, ValueError):
            return None
        return projected if isinstance(projected, str) else None
    return None


def _project_document(
    document: object, *, text_source: str | None = None
) -> list[dict[str, object]]:
    """Project Docling nodes without retaining binary payloads or host paths."""
    items: list[dict[str, object]] = [
        {
            "kind": "document",
            "node_id": "document:root",
            "role": "DOCUMENT",
            "text_or_value": None,
            "parent": None,
            "location": {"ordinal": 0, "depth": 0},
            "extension": {"graph_schema": "DocumentGraphV2"},
        }
    ]
    iterator = getattr(document, "iterate_items", None)
    if not callable(iterator):
        raise RuntimeError("Docling returned no iterable document graph")
    for ordinal, entry in enumerate(iterator(with_groups=True), start=1):
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise RuntimeError("Docling returned an invalid graph item")
        item, depth = entry
        label_object = getattr(item, "label", None)
        label = getattr(label_object, "value", None)
        if not isinstance(label, str):
            label = type(item).__name__.removesuffix("Item").lower() or "item"
        node_id = _reference(item) or f"document:item:{ordinal}"
        parent = _reference(getattr(item, "parent", None)) or "document:root"
        location: dict[str, int | str] = {
            "ordinal": ordinal,
            "depth": int(depth) if isinstance(depth, int) and not isinstance(depth, bool) else 0,
        }
        extension: dict[str, object] = {"label": label}
        provenance_items = getattr(item, "prov", None)
        if isinstance(provenance_items, list) and provenance_items:
            provenance = provenance_items[0]
            page_no = getattr(provenance, "page_no", None)
            if isinstance(page_no, int) and not isinstance(page_no, bool):
                location["page"] = page_no
            visual_region = _safe_visual_region(document, provenance)
            if visual_region is not None:
                extension["visual_region"] = visual_region
        table_data = getattr(item, "data", None)
        table_rows = getattr(table_data, "num_rows", None)
        table_columns = getattr(table_data, "num_cols", None)
        if isinstance(table_rows, int) and isinstance(table_columns, int):
            extension["rows"] = table_rows
            extension["columns"] = table_columns
        annotations = _safe_annotations(item)
        if annotations:
            extension["annotations"] = annotations
        text = _item_text(item, document)
        if text_source is not None and text is not None:
            extension["text_source"] = text_source
        items.append(
            {
                "kind": f"docling_{label}",
                "node_id": node_id,
                "role": _ROLE_BY_LABEL.get(label, "OTHER"),
                "text_or_value": text,
                "parent": parent,
                "location": location,
                "extension": extension,
            }
        )
    return items


def _docling_document_worker(
    source_path: str,
    *,
    enrich: bool,
    macos_ocr: bool,
    page_range: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """Convert one staged document with local-only, explicitly selected services."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    accelerator_module = import_module("docling.datamodel.accelerator_options")
    backend_options_module = import_module("docling.datamodel.backend_options")
    base_models_module = import_module("docling.datamodel.base_models")
    pipeline_options_module = import_module("docling.datamodel.pipeline_options")
    converter_module = import_module("docling.document_converter")
    accelerator_device = getattr(accelerator_module, "AcceleratorDevice")
    accelerator_options = getattr(accelerator_module, "AcceleratorOptions")
    epub_backend_options = getattr(backend_options_module, "EpubBackendOptions")
    input_format_type = getattr(base_models_module, "InputFormat")
    pdf_pipeline_options = getattr(pipeline_options_module, "PdfPipelineOptions")
    image_format_option = getattr(converter_module, "ImageFormatOption")
    document_converter = getattr(converter_module, "DocumentConverter")
    epub_format_option = getattr(converter_module, "EpubFormatOption")
    pdf_format_option = getattr(converter_module, "PdfFormatOption")

    suffix = Path(source_path).suffix.lower()
    input_format_by_suffix = {
        ".pdf": input_format_type.PDF,
        ".epub": input_format_type.EPUB,
        ".docx": input_format_type.DOCX,
        ".xlsx": input_format_type.XLSX,
        ".pptx": input_format_type.PPTX,
        ".png": input_format_type.IMAGE,
        ".jpg": input_format_type.IMAGE,
        ".jpeg": input_format_type.IMAGE,
        ".tif": input_format_type.IMAGE,
        ".tiff": input_format_type.IMAGE,
    }
    input_format = input_format_by_suffix.get(suffix)
    if input_format is None:
        raise RuntimeError("Docling received an unsupported staged format")

    format_options: dict[object, object] = {}
    if input_format in {input_format_type.PDF, input_format_type.IMAGE}:
        pipeline = pdf_pipeline_options()
        pipeline.allow_external_plugins = False
        pipeline.enable_remote_services = False
        pipeline.do_ocr = True
        pipeline.do_table_structure = True
        pipeline.do_formula_enrichment = enrich
        pipeline.do_code_enrichment = enrich
        pipeline.do_picture_classification = enrich
        pipeline.do_picture_description = False
        pipeline.generate_picture_images = enrich
        pipeline.images_scale = 2.0 if enrich else 1.0
        if macos_ocr:
            ocr_mac_options = getattr(pipeline_options_module, "OcrMacOptions")
            pipeline.ocr_options = ocr_mac_options(
                lang=["zh-Hans", "zh-Hant", "en-US", "fr-FR", "de-DE", "es-ES"],
                recognition="accurate",
                force_full_page_ocr=True,
            )
        pipeline.document_timeout = 105.0
        pipeline.accelerator_options = accelerator_options(device=accelerator_device.AUTO)
        model_cache = os.environ.get("LOCAL_STEWARD_DOCUMENT_MODEL_CACHE")
        if model_cache:
            pipeline.artifacts_path = model_cache
        if input_format == input_format_type.PDF:
            format_options[input_format_type.PDF] = pdf_format_option(pipeline_options=pipeline)
        else:
            format_options[input_format_type.IMAGE] = image_format_option(pipeline_options=pipeline)
    elif input_format == input_format_type.EPUB:
        format_options[input_format_type.EPUB] = epub_format_option(
            backend_options=epub_backend_options(
                fetch_images=False,
                enable_local_fetch=False,
                enable_remote_fetch=False,
            )
        )

    converter = document_converter(
        allowed_formats=[input_format],
        format_options=format_options or None,
    )
    convert_arguments: dict[str, object] = {"raises_on_error": True}
    if page_range is not None:
        convert_arguments["page_range"] = page_range
    result = converter.convert(source_path, **convert_arguments)
    status = getattr(getattr(result, "status", None), "value", None)
    if status not in {"success", "partial_success"}:
        raise RuntimeError("Docling conversion did not complete")
    warnings = ["DOCLING_PARTIAL_SUCCESS"] if status == "partial_success" else []
    if page_range is not None:
        warnings.append(f"TARGETED_PAGE_RANGE:{page_range[0]}-{page_range[1]}")
    if macos_ocr:
        warnings.extend(("OCR_ENGINE:MACOS_VISION", "OCR_MODE:FULL_PAGE"))
    return {
        "backend_name": "Docling",
        "backend_version": version("docling"),
        "warnings": warnings,
        "items": _project_document(
            result.document,
            text_source="LOCAL_OCR" if macos_ocr else None,
        ),
    }


def docling_document_worker(source_path: str) -> dict[str, Any]:
    """Convert one staged document with the ordinary deep Docling profile."""

    return _docling_document_worker(source_path, enrich=False, macos_ocr=False)


@dataclass(frozen=True, slots=True)
class DoclingPageRangeWorker:
    """Pickle-safe callable for one bounded original PDF page interval."""

    page_start: int
    page_end: int

    def __call__(self, source_path: str) -> dict[str, Any]:
        return _docling_document_worker(
            source_path,
            enrich=False,
            macos_ocr=False,
            page_range=(self.page_start, self.page_end),
        )


@dataclass(frozen=True, slots=True)
class DoclingOcrPageRangeWorker:
    """Pickle-safe local OCR callable for one bounded original PDF interval."""

    page_start: int
    page_end: int

    def __call__(self, source_path: str) -> dict[str, Any]:
        return _docling_document_worker(
            source_path,
            enrich=False,
            macos_ocr=True,
            page_range=(self.page_start, self.page_end),
        )


def docling_enriched_document_worker(source_path: str) -> dict[str, Any]:
    """Run formula, code and picture classification only when explicitly routed."""

    return _docling_document_worker(source_path, enrich=True, macos_ocr=False)


def docling_macos_ocr_worker(source_path: str) -> dict[str, Any]:
    """Use the local macOS Vision OCR fallback without network services."""

    if sys.platform != "darwin":
        raise RuntimeError("macOS Vision OCR is unavailable on this platform")
    return _docling_document_worker(source_path, enrich=False, macos_ocr=True)


__all__ = [
    "DoclingOcrPageRangeWorker",
    "DoclingPageRangeWorker",
    "docling_document_worker",
    "docling_enriched_document_worker",
    "docling_macos_ocr_worker",
]
