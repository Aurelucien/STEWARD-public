"""Path-free runtime dependency and document-operation capability reporting."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
import platform
import shutil
from typing import Any

from .evidence import canonical_json
from .document_execution import (
    DEFAULT_DOCUMENT_CACHE_MAX_BYTES,
    DEFAULT_DOCUMENT_CACHE_MAX_ENTRIES,
    DEFAULT_DOCUMENT_CACHE_TTL_SECONDS,
    DOCUMENT_EXECUTION_SCHEMA_NAME,
    DOCUMENT_EXECUTION_SCHEMA_VERSION,
)
from .file_agent.runtime.structured_documents import (
    DOCUMENT_INGRESS_CHUNK_BYTES,
    MAX_ADAPTIVE_PARSER_ELAPSED_SECONDS,
    MAX_DOCUMENT_OPERATION_ELAPSED_SECONDS,
    MAX_EPUB_NATIVE_ITEMS,
    MAX_EPUB_NATIVE_READ_TEXT_BYTES,
    MAX_EXPANDED_BYTES,
    MAX_IMAGE_SOURCE_BYTES,
    MAX_PACKAGE_EXPANDED_BYTES,
    MAX_PACKAGE_MEMBERS,
    MAX_PACKAGE_SOURCE_BYTES,
    MAX_PDF_PAGE_OCR_PIXELS,
    MAX_PDF_SOURCE_BYTES,
    MAX_STREAMING_QUERY_MATCH_ITEMS,
    MAX_XLSX_EXPANDED_BYTES,
    STREAMING_QUERY_MAP_PDF_PAGE_THRESHOLD,
    STREAMING_QUERY_MAP_THRESHOLD_BYTES,
    PDF_PAGE_OCR_RENDER_SCALE,
)
from .file_agent.runtime.native_fidelity import (
    MAX_NATIVE_FIDELITY_ITEMS,
    MAX_PDF_NATIVE_AUXILIARY_PAGES,
)
from .file_agent.runtime.audio_documents import (
    AUDIO_SOURCE_FORMATS,
    MAX_AUDIO_SOURCE_BYTES,
    audio_runtime_capabilities,
)
from .file_agent.runtime.video_documents import (
    MAX_VIDEO_SOURCE_BYTES,
    VIDEO_SOURCE_FORMATS,
    video_runtime_capabilities,
)


CapabilityReport = dict[str, Any]

_DEPENDENCIES: dict[str, tuple[str, str, str]] = {
    "psutil": ("psutil", "psutil", "core"),
    "typer": ("typer", "typer", "core"),
    "jsonschema": ("jsonschema", "jsonschema", "agent"),
    "mcp": ("mcp", "mcp", "agent"),
    "markitdown": ("markitdown", "markitdown", "document-fast"),
    "openpyxl": ("openpyxl", "openpyxl", "document-fast"),
    "pillow": ("Pillow", "PIL", "document-fast"),
    "pymupdf": ("PyMuPDF", "pymupdf", "document-fast"),
    "pymupdf4llm": ("pymupdf4llm", "pymupdf4llm", "document-fast"),
    "python-pptx": ("python-pptx", "pptx", "document-fast"),
    "docling": ("docling", "docling", "document-deep"),
    "onnxruntime": ("onnxruntime", "onnxruntime", "document-deep"),
    "rapidocr": ("rapidocr", "rapidocr", "document-deep"),
    "torch": ("torch", "torch", "document-deep"),
    "transformers": ("transformers", "transformers", "document-deep"),
    "faster-whisper": ("faster-whisper", "faster_whisper", "audio"),
    "ctranslate2": ("ctranslate2", "ctranslate2", "audio"),
    "mypy": ("mypy", "mypy", "dev"),
    "pytest": ("pytest", "pytest", "dev"),
    "ruff": ("ruff", "ruff", "dev"),
}

_PROFILE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "core": ("psutil", "typer"),
    "agent": ("jsonschema", "mcp"),
    "document-fast": (
        "markitdown",
        "openpyxl",
        "pillow",
        "pymupdf",
        "pymupdf4llm",
        "python-pptx",
    ),
    "document-deep": ("docling", "onnxruntime", "rapidocr", "torch", "transformers"),
    "audio": ("faster-whisper", "ctranslate2", "onnxruntime"),
    "dev": ("mypy", "pytest", "ruff"),
}

_INSTALL_TARGETS = {
    "core": ".",
    "agent": ".[agent]",
    "document-fast": ".[document-fast]",
    "document-deep": ".[document-deep]",
    "audio": ".[audio]",
    "full": ".[full]",
    "dev": ".[dev]",
}


def _default_import_probe(import_name: str) -> bool:
    try:
        return find_spec(import_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _default_version_probe(distribution: str) -> str | None:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return None


def _status(required: tuple[str, ...], available: Mapping[str, bool]) -> str:
    return "AVAILABLE" if all(available[name] for name in required) else "UNAVAILABLE"


def _operation(
    dependencies: tuple[str, ...],
    available: Mapping[str, bool],
    *,
    system_requirements: tuple[str, ...] = (),
    system_available: Mapping[str, bool],
) -> dict[str, object]:
    missing_dependencies = [name for name in dependencies if not available[name]]
    missing_system = [name for name in system_requirements if not system_available[name]]
    return {
        "status": "AVAILABLE" if not missing_dependencies and not missing_system else "UNAVAILABLE",
        "missing_dependencies": missing_dependencies,
        "missing_system_requirements": missing_system,
    }


def inspect_runtime_capabilities(
    *,
    import_probe: Callable[[str], bool] = _default_import_probe,
    version_probe: Callable[[str], str | None] = _default_version_probe,
    executable_probe: Callable[[str], str | None] = shutil.which,
    platform_name: str | None = None,
) -> CapabilityReport:
    """Return one deterministic, host-path-free runtime capability report."""

    dependency_available: dict[str, bool] = {}
    dependency_records: list[dict[str, object]] = []
    for name, (distribution, import_name, tier) in _DEPENDENCIES.items():
        available = import_probe(import_name)
        dependency_available[name] = available
        dependency_records.append(
            {
                "name": name,
                "distribution": distribution,
                "import_name": import_name,
                "tier": tier,
                "available": available,
                "version": version_probe(distribution) if available else None,
            }
        )

    system_name = platform_name if platform_name is not None else platform.system()
    system_available = {
        "libreoffice": executable_probe("soffice") is not None,
        "macos-vision": system_name == "Darwin",
        "ffmpeg": executable_probe("ffmpeg") is not None,
        "ffprobe": executable_probe("ffprobe") is not None,
    }
    system_records = [
        {
            "name": name,
            "available": available,
            "path_disclosed": False,
        }
        for name, available in sorted(system_available.items())
    ]

    profile_dependencies = dict(_PROFILE_DEPENDENCIES)
    profile_dependencies["full"] = tuple(
        dict.fromkeys(
            (
                *profile_dependencies["core"],
                *profile_dependencies["agent"],
                *profile_dependencies["document-fast"],
                *profile_dependencies["document-deep"],
                *profile_dependencies["audio"],
            )
        )
    )
    profile_dependencies["dev"] = tuple(
        dict.fromkeys((*profile_dependencies["full"], *_PROFILE_DEPENDENCIES["dev"]))
    )
    profiles: dict[str, dict[str, object]] = {}
    for name in ("core", "agent", "document-fast", "document-deep", "audio", "full", "dev"):
        required = profile_dependencies[name]
        missing = [dependency for dependency in required if not dependency_available[dependency]]
        profiles[name] = {
            "status": _status(required, dependency_available),
            "install_target": _INSTALL_TARGETS[name],
            "dependencies": list(required),
            "missing_dependencies": missing,
        }

    deep = profile_dependencies["document-deep"]
    formats: dict[str, dict[str, dict[str, object]]] = {
        "PDF": {
            "STRUCTURE_NATIVE": _operation(
                ("pymupdf",), dependency_available, system_available=system_available
            ),
            "READ_FAST": _operation(
                ("pymupdf4llm",), dependency_available, system_available=system_available
            ),
            "READ_DEEP": _operation(deep, dependency_available, system_available=system_available),
            "OCR": _operation(deep, dependency_available, system_available=system_available),
            "VIEW": _operation(
                ("pymupdf",), dependency_available, system_available=system_available
            ),
        },
        "EPUB": {
            "READ_DEEP": _operation(deep, dependency_available, system_available=system_available),
        },
        "DOCX": {
            "READ_FAST": _operation(
                ("markitdown",), dependency_available, system_available=system_available
            ),
            "READ_DEEP": _operation(deep, dependency_available, system_available=system_available),
            "VIEW": _operation(
                ("pymupdf",),
                dependency_available,
                system_requirements=("libreoffice",),
                system_available=system_available,
            ),
        },
        "XLSX": {
            "READ_FAST": _operation(
                ("openpyxl",), dependency_available, system_available=system_available
            ),
            "READ_DEEP": _operation(deep, dependency_available, system_available=system_available),
            "VIEW": _operation(
                ("pymupdf",),
                dependency_available,
                system_requirements=("libreoffice",),
                system_available=system_available,
            ),
        },
        "PPTX": {
            "READ_FAST": _operation(
                ("python-pptx",), dependency_available, system_available=system_available
            ),
            "READ_DEEP": _operation(deep, dependency_available, system_available=system_available),
            "VIEW": _operation(
                ("pymupdf",),
                dependency_available,
                system_requirements=("libreoffice",),
                system_available=system_available,
            ),
        },
    }
    for source_format in ("PNG", "JPEG", "TIFF"):
        formats[source_format] = {
            "READ_DEEP": _operation(deep, dependency_available, system_available=system_available),
            "OCR": _operation(
                deep,
                dependency_available,
                system_requirements=("macos-vision",),
                system_available=system_available,
            ),
            "VIEW": _operation(
                ("pillow",), dependency_available, system_available=system_available
            ),
        }
    audio_operation = _operation(
        profile_dependencies["audio"],
        dependency_available,
        system_requirements=("ffmpeg", "ffprobe"),
        system_available=system_available,
    )
    for source_format in AUDIO_SOURCE_FORMATS:
        formats[source_format] = {
            "STRUCTURE": _operation(
                (),
                dependency_available,
                system_requirements=("ffprobe",),
                system_available=system_available,
            ),
            "READ": audio_operation,
            "LOCATE": audio_operation,
            "EVIDENCE": audio_operation,
        }
    video_probe_operation = _operation(
        (),
        dependency_available,
        system_requirements=("ffprobe",),
        system_available=system_available,
    )
    video_decode_operation = _operation(
        profile_dependencies["audio"],
        dependency_available,
        system_requirements=("ffmpeg", "ffprobe"),
        system_available=system_available,
    )
    for source_format in VIDEO_SOURCE_FORMATS:
        formats[source_format] = {
            "STRUCTURE": video_probe_operation,
            "READ": video_decode_operation,
            "LOCATE": video_decode_operation,
            "EVIDENCE": video_decode_operation,
            "VIEW": _operation(
                (),
                dependency_available,
                system_requirements=("ffmpeg", "ffprobe"),
                system_available=system_available,
            ),
        }

    visual_states = [
        operation["status"]
        for format_operations in formats.values()
        for action, operation in format_operations.items()
        if action == "VIEW"
    ]
    visual_status = (
        "AVAILABLE"
        if visual_states and all(state == "AVAILABLE" for state in visual_states)
        else "PARTIAL"
        if any(state == "AVAILABLE" for state in visual_states)
        else "UNAVAILABLE"
    )
    operations = {
        "CORE_PRODUCT": {"status": profiles["core"]["status"]},
        "NATIVE_AGENT": {
            "status": "AVAILABLE"
            if profiles["core"]["status"] == profiles["agent"]["status"] == "AVAILABLE"
            else "UNAVAILABLE"
        },
        "DOCUMENT_FAST": {"status": profiles["document-fast"]["status"]},
        "DOCUMENT_DEEP": {"status": profiles["document-deep"]["status"]},
        "DOCUMENT_ADAPTIVE": {
            "status": "AVAILABLE"
            if profiles["document-fast"]["status"]
            == profiles["document-deep"]["status"]
            == "AVAILABLE"
            else "PARTIAL"
            if "AVAILABLE"
            in {
                profiles["document-fast"]["status"],
                profiles["document-deep"]["status"],
            }
            else "UNAVAILABLE"
        },
        "DOCUMENT_EVIDENCE": {
            "status": "AVAILABLE"
            if profiles["document-fast"]["status"]
            == profiles["document-deep"]["status"]
            == "AVAILABLE"
            else "PARTIAL"
            if "AVAILABLE"
            in {
                profiles["document-fast"]["status"],
                profiles["document-deep"]["status"],
            }
            else "UNAVAILABLE",
            "selection": "DETERMINISTIC_NATIVE_CONTAINER_HIERARCHY_AWARE",
            "match_repairs": [
                "NFKC_CASEFOLD",
                "SOFT_HYPHEN_REMOVAL",
                "LINEBREAK_DEHYPHENATION",
                "WHITESPACE_NORMALIZATION",
            ],
            "persistence": "NONE",
            "max_targeted_pdf_pages": 8,
            "max_packet_characters": 32768,
        },
        "DOCUMENT_FORMULA": {
            "status": _operation(deep, dependency_available, system_available=system_available)[
                "status"
            ]
        },
        "DOCUMENT_NATIVE_FIDELITY": {
            "status": "AVAILABLE"
            if profiles["document-fast"]["status"] == "AVAILABLE"
            else "UNAVAILABLE",
            "projection": "BOUNDED_NATIVE_AUXILIARY_NODES",
            "features": {
                "PDF": ["METADATA", "OUTLINE", "ANNOTATIONS", "FORM_FIELDS", "REPAIR_FACTS"],
                "DOCX": ["COMMENTS", "FOOTNOTES", "ENDNOTES", "REVISIONS"],
                "XLSX": ["COMMENTS", "CHART_SOURCE_REFERENCES", "MERGED_CELL_FACTS"],
                "PPTX": [
                    "SPEAKER_NOTES",
                    "CHART_CACHED_DATA",
                    "ACCESSIBILITY_TEXT",
                    "MERGED_CELL_FACTS",
                ],
            },
            "formula_evaluation": False,
            "optional_component_recovery": "FAIL_COMPONENT_PRESERVE_DOCUMENT",
            "persistence": "NONE",
        },
        "DOCUMENT_VISUAL": {"status": visual_status},
        "AUDIO_LOCAL": {
            "status": audio_operation["status"],
            "model_output_authority": "MODEL_DERIVED_NOT_VERBATIM_EVIDENCE",
            "runtime_downloads_allowed": False,
            "persistence": "NONE",
        },
        "VIDEO_LOCAL": {
            "status": video_decode_operation["status"],
            "timeline_authority": "SOURCE_PRESENTATION_TIMESTAMPS",
            "source_distinction": [
                "SCENE",
                "REPRESENTATIVE_FRAME",
                "FRAME_OCR",
                "VIDEO_TEXT_TRACK",
                "EMBEDDED_SUBTITLE",
                "AUDIO_ASR",
                "VISUAL_SEMANTIC_RETRIEVAL",
            ],
            "focused_decode_planning": "WHOLE_SOURCE_TEXT_VISUAL_ANCHOR_OR_EXPLICIT_FALLBACK",
            "visual_semantic_candidate_authority": "MODEL_DERIVED_RETRIEVAL_NOT_TRUTH",
            "semantic_agreement_inferred": False,
            "runtime_downloads_allowed": False,
            "persistence": "NONE",
        },
    }

    report: CapabilityReport = {
        "schema_name": "local_steward.runtime_capabilities",
        "schema_version": 12,
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
            "executable_path_disclosed": False,
        },
        "dependencies": dependency_records,
        "system_requirements": system_records,
        "profiles": profiles,
        "operations": operations,
        "formats": formats,
        "document_execution": {
            "schema_name": DOCUMENT_EXECUTION_SCHEMA_NAME,
            "schema_version": DOCUMENT_EXECUTION_SCHEMA_VERSION,
            "routing": "DETERMINISTIC_QUALITY_DRIVEN",
            "cache": {
                "scope": "PROCESS_MEMORY",
                "max_entries": DEFAULT_DOCUMENT_CACHE_MAX_ENTRIES,
                "max_bytes": DEFAULT_DOCUMENT_CACHE_MAX_BYTES,
                "ttl_seconds": DEFAULT_DOCUMENT_CACHE_TTL_SECONDS,
                "single_flight": True,
                "stores_source_bytes": False,
                "persistence_effect": "NONE",
            },
        },
        "document_resources": {
            "schema_name": "local_steward.document_resources",
            "schema_version": 1,
            "source_admission": "STREAM_HASH_STAGE",
            "ingress_chunk_bytes": DOCUMENT_INGRESS_CHUNK_BYTES,
            "source_limits": {
                "PDF": MAX_PDF_SOURCE_BYTES,
                "EPUB": MAX_PACKAGE_SOURCE_BYTES,
                "DOCX": MAX_PACKAGE_SOURCE_BYTES,
                "XLSX": MAX_PACKAGE_SOURCE_BYTES,
                "PPTX": MAX_PACKAGE_SOURCE_BYTES,
                "PNG": MAX_IMAGE_SOURCE_BYTES,
                "JPEG": MAX_IMAGE_SOURCE_BYTES,
                "TIFF": MAX_IMAGE_SOURCE_BYTES,
                **{source_format: MAX_AUDIO_SOURCE_BYTES for source_format in AUDIO_SOURCE_FORMATS},
                **{source_format: MAX_VIDEO_SOURCE_BYTES for source_format in VIDEO_SOURCE_FORMATS},
            },
            "expanded_limits": {
                "EPUB": MAX_PACKAGE_EXPANDED_BYTES,
                "DOCX": MAX_PACKAGE_EXPANDED_BYTES,
                "XLSX": MAX_XLSX_EXPANDED_BYTES,
                "PPTX": MAX_PACKAGE_EXPANDED_BYTES,
                "absolute": MAX_EXPANDED_BYTES,
            },
            "archive_member_limit": MAX_PACKAGE_MEMBERS,
            "streaming_query_map": {
                "threshold_bytes": STREAMING_QUERY_MAP_THRESHOLD_BYTES,
                "formats": ["PDF", "EPUB", "DOCX", "XLSX", "PPTX"],
                "always_map_formats": ["EPUB"],
                "query_intents": ["LOCATE", "EVIDENCE"],
                "pdf_page_threshold": STREAMING_QUERY_MAP_PDF_PAGE_THRESHOLD,
                "pdf_query_intents": ["LOCATE", "EVIDENCE"],
                "max_matching_items": MAX_STREAMING_QUERY_MATCH_ITEMS,
            },
            "epub_native_fallback": {
                "backend": "STEWARDNativeEpub",
                "views": ["READ", "STRUCTURE"],
                "tolerant_html": True,
                "spine_order": True,
                "read_text_limit_bytes": MAX_EPUB_NATIVE_READ_TEXT_BYTES,
                "item_limit": MAX_EPUB_NATIVE_ITEMS,
            },
            "pdf_native_structure": {
                "backend": "PyMuPDFNativeStructure",
                "projection": "OUTLINE_AND_METADATA",
                "page_body_parsed": False,
                "page_auxiliary_scanned": False,
                "item_limit": MAX_NATIVE_FIDELITY_ITEMS,
            },
            "pdf_native_auxiliary": {
                "page_limit": MAX_PDF_NATIVE_AUXILIARY_PAGES,
                "item_limit": MAX_NATIVE_FIDELITY_ITEMS,
            },
            "pdf_page_ocr": {
                "backend": "STEWARDPageOCR",
                "engine": "RapidOCR",
                "execution": "PAGE_LOCAL_DISCARD_AFTER_PROJECTION",
                "render_scale": PDF_PAGE_OCR_RENDER_SCALE,
                "render_pixel_limit": MAX_PDF_PAGE_OCR_PIXELS,
                "read_trigger": "MAJORITY_LOW_NATIVE_TEXT",
                "query_trigger": "ANY_LOW_NATIVE_TEXT_PAGE",
                "text_authority": "MODEL_DERIVED",
                "persistence": "NONE",
            },
            "adaptive_parser_timeout_ceiling_seconds": (MAX_ADAPTIVE_PARSER_ELAPSED_SECONDS),
            "native_document_operation_timeout_seconds": (MAX_DOCUMENT_OPERATION_ELAPSED_SECONDS),
            "timeout_stages": ["INGRESS", "IDENTIFICATION", "PARSER", "RELEASE"],
            "stores_source_bytes_in_memory": False,
            "persistence_effect": "NONE",
        },
        "audio_runtime": audio_runtime_capabilities(),
        "video_runtime": video_runtime_capabilities(),
        "persistence_effect": "NONE",
    }
    report["report_digest"] = sha256(canonical_json(report)).hexdigest()
    return report


__all__ = ["CapabilityReport", "inspect_runtime_capabilities"]
