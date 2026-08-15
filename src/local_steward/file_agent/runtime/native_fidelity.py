"""Bounded native-format fidelity projections for already admitted documents."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from importlib.metadata import version
from typing import Any
from xml.etree import ElementTree
import zipfile


MAX_NATIVE_FIDELITY_ITEMS = 512
MAX_NATIVE_TEXT_CHARS = 4_096
MAX_OPTIONAL_XML_BYTES = 16 * 1024 * 1024
MAX_CHART_POINTS = 512
MAX_PDF_NATIVE_AUXILIARY_PAGES = 512
MAX_PDF_NATIVE_WARNING_PAGE_SAMPLES = 8

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD = f"{{{_WORD_NS}}}"


def _local_name(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _bounded_text(value: object) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    text = str(value)
    return text[:MAX_NATIVE_TEXT_CHARS], len(text) > MAX_NATIVE_TEXT_CHARS


def _xml_text(element: ElementTree.Element, *, include_deleted: bool = True) -> str:
    accepted = {"t", "tab", "br", "cr"}
    if include_deleted:
        accepted.add("delText")
    values: list[str] = []
    for child in element.iter():
        name = _local_name(child.tag)
        if name in {"tab"}:
            values.append("\t")
        elif name in {"br", "cr"}:
            values.append("\n")
        elif name in accepted and child.text:
            values.append(child.text)
    return "".join(values).strip()


def _identifier(value: str | None) -> int | str | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value[:128]


def _optional_xml(
    archive: zipfile.ZipFile,
    member: str,
    component: str,
    warnings: list[str],
) -> ElementTree.Element | None:
    try:
        info = archive.getinfo(member)
    except KeyError:
        return None
    if info.file_size > MAX_OPTIONAL_XML_BYTES:
        warnings.append(f"DOCX_COMPONENT_RESOURCE_LIMIT:{component}")
        return None
    try:
        with archive.open(info) as handle:
            payload = handle.read(MAX_OPTIONAL_XML_BYTES + 1)
        if len(payload) > MAX_OPTIONAL_XML_BYTES:
            warnings.append(f"DOCX_COMPONENT_RESOURCE_LIMIT:{component}")
            return None
        return ElementTree.fromstring(payload)
    except (ElementTree.ParseError, OSError, RuntimeError, zipfile.BadZipFile):
        warnings.append(f"DOCX_COMPONENT_MALFORMED:{component}")
        return None


def project_docx_auxiliary(
    source_path: str,
) -> tuple[list[dict[str, object]], list[str]]:
    """Project comments, notes and revisions without weakening core DOCX admission."""

    items: list[dict[str, object]] = []
    warnings: list[str] = []
    omitted = 0

    def append(item: dict[str, object]) -> None:
        nonlocal omitted
        if len(items) >= MAX_NATIVE_FIDELITY_ITEMS:
            omitted += 1
            return
        items.append(item)

    try:
        with zipfile.ZipFile(source_path) as archive:
            document = _optional_xml(archive, "word/document.xml", "document", warnings)
            comments = _optional_xml(archive, "word/comments.xml", "comments", warnings)
            anchor_text: dict[int | str, str] = {}
            if document is not None:
                for paragraph in document.iter(f"{_WORD}p"):
                    identifiers = {
                        identifier
                        for node in paragraph.iter()
                        if _local_name(node.tag) in {"commentRangeStart", "commentReference"}
                        for identifier in [_identifier(node.get(f"{_WORD}id"))]
                        if identifier is not None
                    }
                    text = _xml_text(paragraph, include_deleted=False)
                    for identifier in identifiers:
                        anchor_text.setdefault(identifier, text[:MAX_NATIVE_TEXT_CHARS])
            if comments is not None:
                for comment in comments.iter(f"{_WORD}comment"):
                    comment_identifier = _identifier(comment.get(f"{_WORD}id"))
                    if comment_identifier is None:
                        continue
                    comment_text, truncated = _bounded_text(_xml_text(comment))
                    extension: dict[str, object] = {
                        "text_truncated": truncated,
                    }
                    for source_name, target_name in (
                        ("author", "author"),
                        ("initials", "initials"),
                        ("date", "date"),
                    ):
                        value = comment.get(f"{_WORD}{source_name}")
                        if value:
                            extension[target_name] = value[:256]
                    if comment_identifier in anchor_text:
                        extension["anchor_text"] = anchor_text[comment_identifier]
                    append(
                        {
                            "kind": "docx_comment",
                            "role": "NOTE",
                            "text_or_value": comment_text,
                            "parent": "document:current",
                            "location": {"comment": comment_identifier},
                            "extension": extension,
                        }
                    )

            for note_kind, member, element_name in (
                ("footnote", "word/footnotes.xml", "footnote"),
                ("endnote", "word/endnotes.xml", "endnote"),
            ):
                root = _optional_xml(archive, member, note_kind, warnings)
                if root is None:
                    continue
                for note in root.iter(f"{_WORD}{element_name}"):
                    note_identifier = _identifier(note.get(f"{_WORD}id"))
                    if isinstance(note_identifier, int) and note_identifier < 0:
                        continue
                    if note_identifier is None:
                        continue
                    note_text, truncated = _bounded_text(_xml_text(note))
                    if not note_text:
                        continue
                    append(
                        {
                            "kind": f"docx_{note_kind}",
                            "role": "NOTE",
                            "text_or_value": note_text,
                            "parent": "document:current",
                            "location": {note_kind: note_identifier},
                            "extension": {"text_truncated": truncated},
                        }
                    )

            if document is not None:
                revision_index = 0
                for revision in document.iter():
                    revision_kind = _local_name(revision.tag)
                    if revision_kind not in {"ins", "del"}:
                        continue
                    revision_text, truncated = _bounded_text(_xml_text(revision))
                    if not revision_text:
                        continue
                    revision_index += 1
                    extension = {
                        "revision_type": "INSERTION" if revision_kind == "ins" else "DELETION",
                        "text_truncated": truncated,
                    }
                    for source_name, target_name in (
                        ("author", "author"),
                        ("date", "date"),
                        ("id", "revision_id"),
                    ):
                        value = revision.get(f"{_WORD}{source_name}")
                        if value:
                            extension[target_name] = value[:256]
                    append(
                        {
                            "kind": "docx_revision",
                            "role": "REVISION",
                            "text_or_value": revision_text,
                            "parent": "document:current",
                            "location": {"revision": revision_index},
                            "extension": extension,
                        }
                    )
    except (OSError, RuntimeError, zipfile.BadZipFile):
        warnings.append("DOCX_AUXILIARY_PROJECTION_UNAVAILABLE")
    if omitted:
        warnings.append(f"DOCX_AUXILIARY_ITEMS_OMITTED:{omitted}")
    return items, warnings


def _safe_mapping(value: object, keys: Iterable[str]) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    projected: dict[str, str] = {}
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item:
            projected[key] = item[:MAX_NATIVE_TEXT_CHARS]
    return projected


def project_pdf_native(
    source_path: str,
    *,
    include_page_auxiliary: bool = True,
) -> tuple[list[dict[str, object]], list[str]]:
    """Project PDF outline, metadata, annotations and form fields without payloads."""

    pymupdf = import_module("pymupdf")
    items: list[dict[str, object]] = []
    warnings: list[str] = []
    item_limit_reached = False

    def append_item(item: dict[str, object]) -> bool:
        nonlocal item_limit_reached
        if len(items) >= MAX_NATIVE_FIDELITY_ITEMS:
            item_limit_reached = True
            return False
        items.append(item)
        return True

    document = pymupdf.open(source_path)
    try:
        if document.is_repaired or document.is_encrypted:
            append_item(
                {
                    "kind": "pdf_document",
                    "role": "DOCUMENT",
                    "text_or_value": None,
                    "node_id": "document:current",
                    "parent": None,
                    "location": {"document": "current"},
                    "extension": {
                        "page_count": document.page_count,
                        "repaired": bool(document.is_repaired),
                        "encrypted": bool(document.is_encrypted),
                    },
                }
            )
        if document.is_repaired:
            warnings.append("PDF_NATIVE_REPAIR_APPLIED")
        metadata = _safe_mapping(
            document.metadata,
            ("title", "author", "subject", "keywords", "creator", "producer"),
        )
        if metadata:
            append_item(
                {
                    "kind": "pdf_metadata",
                    "role": "METADATA",
                    "text_or_value": "\n".join(
                        f"{key}: {value}" for key, value in metadata.items()
                    ),
                    "node_id": "document:metadata",
                    "parent": "document:current",
                    "location": {"metadata": 1},
                    "extension": metadata,
                }
            )
        try:
            toc = document.get_toc(simple=True)
        except (RuntimeError, ValueError):
            toc = []
            warnings.append("PDF_OUTLINE_UNAVAILABLE")
        outline_stack: dict[int, str] = {}
        for outline_index, entry in enumerate(toc, start=1):
            if not isinstance(entry, list) or len(entry) < 3:
                continue
            level, title, page_number = entry[:3]
            if not isinstance(title, str) or not title:
                continue
            normalized_level = max(1, level) if isinstance(level, int) else 1
            parent_levels = [
                candidate for candidate in outline_stack if candidate < normalized_level
            ]
            parent = outline_stack[max(parent_levels)] if parent_levels else "document:current"
            node_id = f"pdf:outline:{outline_index}"
            for stale_level in [
                candidate for candidate in outline_stack if candidate >= normalized_level
            ]:
                del outline_stack[stale_level]
            outline_stack[normalized_level] = node_id
            if not append_item(
                {
                    "kind": "pdf_outline",
                    "role": "HEADING",
                    "text_or_value": title[:MAX_NATIVE_TEXT_CHARS],
                    "node_id": node_id,
                    "parent": parent,
                    "location": {
                        "page": page_number if isinstance(page_number, int) else 0,
                        "outline": outline_index,
                    },
                    "extension": {
                        "level": normalized_level,
                    },
                }
            ):
                break
        if not include_page_auxiliary:
            warnings.append("PDF_NATIVE_PAGE_AUXILIARY_NOT_SCANNED")
        else:
            annotation_failure_count = 0
            annotation_failure_samples: list[int] = []
            form_failure_count = 0
            form_failure_samples: list[int] = []
            auxiliary_page_limit = min(document.page_count, MAX_PDF_NATIVE_AUXILIARY_PAGES)
            scanned_pages = 0
            for page_number in range(1, auxiliary_page_limit + 1):
                if item_limit_reached:
                    break
                page = document.load_page(page_number - 1)
                scanned_pages += 1
                try:
                    annotations = page.annots() or ()
                    for annotation_index, annotation in enumerate(annotations, start=1):
                        info = _safe_mapping(
                            annotation.info,
                            (
                                "content",
                                "title",
                                "subject",
                                "name",
                                "creationDate",
                                "modDate",
                            ),
                        )
                        annotation_type = annotation.type
                        type_name = (
                            annotation_type[1]
                            if isinstance(annotation_type, tuple)
                            and len(annotation_type) > 1
                            and isinstance(annotation_type[1], str)
                            else str(annotation_type)
                        )
                        text = info.get("content") or info.get("subject") or info.get("title")
                        if not append_item(
                            {
                                "kind": "pdf_annotation",
                                "role": "NOTE",
                                "text_or_value": text,
                                "node_id": (
                                    f"pdf:page:{page_number}:annotation:{annotation_index}"
                                ),
                                "parent": f"page:{page_number}",
                                "location": {
                                    "page": page_number,
                                    "annotation": annotation_index,
                                },
                                "extension": {
                                    "annotation_type": type_name[:128],
                                    "info": info,
                                    "rect": [float(value) for value in annotation.rect],
                                },
                            }
                        ):
                            break
                except (RuntimeError, ValueError):
                    annotation_failure_count += 1
                    if len(annotation_failure_samples) < MAX_PDF_NATIVE_WARNING_PAGE_SAMPLES:
                        annotation_failure_samples.append(page_number)
                if item_limit_reached:
                    break
                try:
                    widgets = page.widgets() or ()
                    for widget_index, widget in enumerate(widgets, start=1):
                        field_name, _ = _bounded_text(widget.field_name)
                        field_value, value_truncated = _bounded_text(widget.field_value)
                        if not append_item(
                            {
                                "kind": "pdf_form_field",
                                "role": "FORM_FIELD",
                                "text_or_value": field_value,
                                "node_id": f"pdf:page:{page_number}:form:{widget_index}",
                                "parent": f"page:{page_number}",
                                "location": {
                                    "page": page_number,
                                    "form_field": widget_index,
                                },
                                "extension": {
                                    "field_name": field_name,
                                    "field_type": str(widget.field_type_string)[:128],
                                    "value_truncated": value_truncated,
                                    "rect": [float(value) for value in widget.rect],
                                },
                            }
                        ):
                            break
                except (RuntimeError, ValueError):
                    form_failure_count += 1
                    if len(form_failure_samples) < MAX_PDF_NATIVE_WARNING_PAGE_SAMPLES:
                        form_failure_samples.append(page_number)
            if annotation_failure_count:
                warnings.append(
                    "PDF_ANNOTATIONS_UNAVAILABLE:"
                    f"count:{annotation_failure_count}:"
                    f"sample_pages:{','.join(str(value) for value in annotation_failure_samples)}"
                )
            if form_failure_count:
                warnings.append(
                    "PDF_FORM_FIELDS_UNAVAILABLE:"
                    f"count:{form_failure_count}:"
                    f"sample_pages:{','.join(str(value) for value in form_failure_samples)}"
                )
            if scanned_pages < document.page_count:
                warnings.append(
                    f"PDF_NATIVE_AUXILIARY_PAGES_OMITTED:{document.page_count - scanned_pages}"
                )
    finally:
        document.close()
    if item_limit_reached:
        warnings.append(f"PDF_NATIVE_ITEM_LIMIT_REACHED:{MAX_NATIVE_FIDELITY_ITEMS}")
    return items, warnings


def _safe_chart_scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)[:512]


def pptx_chart_extension(chart: Any) -> dict[str, object]:
    """Return bounded cached chart data exposed by python-pptx."""

    extension: dict[str, object] = {
        "chart_type": str(chart.chart_type),
        "series_count": len(chart.series),
    }
    if bool(getattr(chart, "has_title", False)):
        title = getattr(getattr(chart, "chart_title", None), "text_frame", None)
        text = getattr(title, "text", None)
        if isinstance(text, str) and text:
            extension["title"] = text[:MAX_NATIVE_TEXT_CHARS]
    categories: list[str | int | float | bool | None] = []
    plots = getattr(chart, "plots", ())
    if plots:
        try:
            categories = [
                _safe_chart_scalar(getattr(category, "label", category))
                for category in plots[0].categories
            ][:MAX_CHART_POINTS]
        except (AttributeError, TypeError, ValueError):
            categories = []
    series_items: list[dict[str, object]] = []
    for series in list(chart.series)[:64]:
        values = [
            _safe_chart_scalar(value)
            for value in list(getattr(series, "values", ()))[:MAX_CHART_POINTS]
        ]
        series_items.append(
            {
                "name": _safe_chart_scalar(getattr(series, "name", None)),
                "values": values,
            }
        )
    if categories:
        extension["categories"] = categories
    if series_items:
        extension["series"] = series_items
    return extension


def pptx_shape_accessibility(shape: Any) -> dict[str, str]:
    """Project shape name and OOXML alternative-text fields without media reads."""

    projected: dict[str, str] = {}
    name = getattr(shape, "name", None)
    if isinstance(name, str) and name:
        projected["name"] = name[:512]
    element = getattr(shape, "element", None)
    iterator = getattr(element, "iter", None)
    if not callable(iterator):
        return projected
    for child in iterator():
        if _local_name(getattr(child, "tag", None)) != "cNvPr":
            continue
        for source_name, target_name in (
            ("descr", "description"),
            ("title", "title"),
        ):
            value = child.get(source_name)
            if isinstance(value, str) and value:
                projected[target_name] = value[:MAX_NATIVE_TEXT_CHARS]
        break
    return projected


def _openpyxl_reference(value: object) -> str | None:
    if value is None:
        return None
    for name in ("numRef", "strRef"):
        reference = getattr(value, name, None)
        formula = getattr(reference, "f", None)
        if isinstance(formula, str) and formula:
            return formula[:MAX_NATIVE_TEXT_CHARS]
    formula = getattr(value, "f", None)
    return formula[:MAX_NATIVE_TEXT_CHARS] if isinstance(formula, str) and formula else None


def openpyxl_chart_extension(chart: Any) -> dict[str, object]:
    """Return chart type and bounded source formulas without evaluating workbook data."""

    series_items: list[dict[str, object]] = []
    for series in list(getattr(chart, "ser", ()))[:64]:
        label = getattr(series, "tx", None)
        name = getattr(label, "v", None)
        name_reference = _openpyxl_reference(label)
        item: dict[str, object] = {}
        if isinstance(name, str) and name:
            item["name"] = name[:MAX_NATIVE_TEXT_CHARS]
        if name_reference:
            item["name_reference"] = name_reference
        for attribute, target in (
            ("val", "value_reference"),
            ("yVal", "value_reference"),
            ("cat", "category_reference"),
            ("xVal", "category_reference"),
        ):
            reference = _openpyxl_reference(getattr(series, attribute, None))
            if reference and target not in item:
                item[target] = reference
        series_items.append(item)
    return {
        "chart_type": type(chart).__name__,
        "series_count": len(getattr(chart, "ser", ())),
        "series_references": series_items,
        "evaluated": False,
    }


def chart_search_text(extension: dict[str, object]) -> str | None:
    """Build a bounded searchable projection from already extracted chart facts."""

    values: list[str] = []

    def collect(value: object) -> None:
        if len(values) >= MAX_CHART_POINTS:
            return
        if isinstance(value, dict):
            for nested in value.values():
                collect(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                collect(nested)
        elif isinstance(value, (str, int, float, bool)):
            text = str(value).strip()
            if text:
                values.append(text[:512])

    for key in ("title", "categories", "series", "series_references"):
        collect(extension.get(key))
    if not values:
        return None
    return "\n".join(values)[:MAX_NATIVE_TEXT_CHARS]


def fidelity_backend_versions() -> dict[str, str]:
    """Expose only installed parser versions used by native fidelity helpers."""

    return {
        "PyMuPDF": version("PyMuPDF"),
        "openpyxl": version("openpyxl"),
        "python-pptx": version("python-pptx"),
    }


__all__ = [
    "MAX_NATIVE_FIDELITY_ITEMS",
    "MAX_PDF_NATIVE_AUXILIARY_PAGES",
    "MAX_PDF_NATIVE_WARNING_PAGE_SAMPLES",
    "MAX_OPTIONAL_XML_BYTES",
    "chart_search_text",
    "fidelity_backend_versions",
    "openpyxl_chart_extension",
    "pptx_chart_extension",
    "pptx_shape_accessibility",
    "project_docx_auxiliary",
    "project_pdf_native",
]
