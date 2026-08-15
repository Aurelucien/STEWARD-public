"""Lazy public exports for the layered read-only File Agent runtime.

Importing this package must not import MCP, provider, Office, OCR, or deep-model
dependencies.  Each public symbol loads only its owning module on first use.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


_EXPORT_GROUPS: dict[str, tuple[str, ...]] = {
    ".mcp_client": (
        "FILESYSTEM_READ_ONLY_ALLOWLIST",
        "FilesystemMcpClient",
        "McpClientError",
        "McpToolDescriptor",
        "McpToolResult",
    ),
    ".bounded_content": (
        "MAX_CONTENT_BYTES_PER_AGENT_TURN",
        "MAX_CONTENT_BYTES_PER_READ",
        "MAX_CONTENT_READS_PER_ASSISTANT_BATCH",
        "MAX_SERIALIZED_BYTES_PER_AGENT_TURN",
        "BoundedUtf8ContentResult",
        "ProjectOwnedBoundedTextMcp",
    ),
    ".filesystem_tools": (
        "FilesystemToolPort",
        "register_bounded_utf8_file_tool",
        "register_filesystem_tools",
        "register_structured_document_tool",
    ),
    ".runtime": (
        "AgentRuntime",
        "AgentTurnRequest",
        "AgentTurnResult",
        "CombinedBudget",
        "CombinedBudgetLimits",
        "CombinedBudgetReport",
        "CombinedBudgetUsage",
        "RuntimeTool",
        "RuntimeToolResult",
        "SourceFamily",
        "ToolRegistry",
        "ToolTrace",
    ),
    ".failures": ("RuntimeFailure",),
    ".scope_binding": ("ScopeBinding", "ScopeBindings"),
    ".structured_documents": (
        "CURRENT_FILESYSTEM_DOCUMENT",
        "IMAGE_SOURCE_FORMATS",
        "DOCUMENT_INGRESS_CHUNK_BYTES",
        "MAX_SOURCE_BYTES",
        "MAX_EXPANDED_BYTES",
        "MAX_PDF_SOURCE_BYTES",
        "MAX_PACKAGE_SOURCE_BYTES",
        "MAX_IMAGE_SOURCE_BYTES",
        "MAX_PACKAGE_EXPANDED_BYTES",
        "MAX_XLSX_EXPANDED_BYTES",
        "MAX_PARSER_ELAPSED_SECONDS",
        "MAX_PARSER_MEMORY_BYTES",
        "MAX_PDF_PARSER_ELAPSED_SECONDS",
        "MAX_PDF_PARSER_MEMORY_BYTES",
        "MAX_DEEP_PARSER_ELAPSED_SECONDS",
        "MAX_DEEP_PARSER_MEMORY_BYTES",
        "MAX_DOCUMENT_OPERATION_ELAPSED_SECONDS",
        "MAX_PARSED_ITEMS_OR_BLOCKS",
        "MAX_NORMALIZED_OUTPUT_BYTES",
        "DocumentResourceUsage",
        "DocumentSourceProvenance",
        "NormalizedDocumentItem",
        "NormalizedDocumentObservation",
        "ProjectOwnedBoundedDocumentIngress",
        "IsolatedDocxWorker",
        "IsolatedDoclingWorker",
        "IsolatedEnrichedDoclingWorker",
        "IsolatedMacOcrWorker",
        "IsolatedParserWorker",
        "IsolatedPdfWorker",
        "IsolatedPptxWorker",
        "IsolatedXlsxWorker",
        "StructuredDocumentParserAdapter",
        "identify_document_format",
    ),
    ".steward_tools": ("StewardRuntimeDependencies", "register_steward_tools"),
    ".temporal_evidence": (
        "ComparisonOutcome",
        "CurrentMetadataObservation",
        "CurrentState",
        "EvidenceValue",
        "EvidenceValueState",
        "FieldComparison",
        "PayloadComparison",
        "ProjectOwnedCurrentMetadataObserver",
        "TemporalEvidenceRelation",
        "TemporalEvidenceRelationService",
        "VerifiedSnapshotEntryResolver",
        "register_temporal_evidence_tool",
    ),
    ".models": (
        "ModelFinalAnswer",
        "ModelMessage",
        "ModelMessageRole",
        "ModelToolCall",
        "ModelToolBatchResultMessage",
        "ModelToolCallingError",
        "ModelToolDescriptor",
        "ModelToolResultMessage",
        "ModelToolResultDisposition",
        "ModelTurnResult",
        "strict_json_object",
    ),
    ".openai_compatible": (
        "OpenAICompatibleToolCallingModel",
        "ToolCallingProviderSettings",
        "ToolCallingProviderTransport",
    ),
}

_EXPORT_MODULE = {
    export_name: module_name
    for module_name, export_names in _EXPORT_GROUPS.items()
    for export_name in export_names
}


def __getattr__(name: str) -> Any:
    """Load one requested runtime port without importing unrelated profiles."""

    module_name = _EXPORT_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *_EXPORT_MODULE})


__all__ = [
    "FILESYSTEM_READ_ONLY_ALLOWLIST",
    "FilesystemMcpClient",
    "McpClientError",
    "McpToolDescriptor",
    "McpToolResult",
    "FilesystemToolPort",
    "register_filesystem_tools",
    "register_bounded_utf8_file_tool",
    "register_structured_document_tool",
    "register_temporal_evidence_tool",
    "MAX_CONTENT_BYTES_PER_AGENT_TURN",
    "MAX_CONTENT_BYTES_PER_READ",
    "MAX_CONTENT_READS_PER_ASSISTANT_BATCH",
    "MAX_SERIALIZED_BYTES_PER_AGENT_TURN",
    "BoundedUtf8ContentResult",
    "ProjectOwnedBoundedTextMcp",
    "AgentRuntime",
    "AgentTurnRequest",
    "AgentTurnResult",
    "CombinedBudget",
    "CombinedBudgetLimits",
    "CombinedBudgetReport",
    "CombinedBudgetUsage",
    "RuntimeFailure",
    "RuntimeTool",
    "RuntimeToolResult",
    "SourceFamily",
    "ToolRegistry",
    "ToolTrace",
    "ScopeBinding",
    "ScopeBindings",
    "CURRENT_FILESYSTEM_DOCUMENT",
    "IMAGE_SOURCE_FORMATS",
    "DOCUMENT_INGRESS_CHUNK_BYTES",
    "MAX_SOURCE_BYTES",
    "MAX_EXPANDED_BYTES",
    "MAX_PDF_SOURCE_BYTES",
    "MAX_PACKAGE_SOURCE_BYTES",
    "MAX_IMAGE_SOURCE_BYTES",
    "MAX_PACKAGE_EXPANDED_BYTES",
    "MAX_XLSX_EXPANDED_BYTES",
    "MAX_PARSER_ELAPSED_SECONDS",
    "MAX_PARSER_MEMORY_BYTES",
    "MAX_PDF_PARSER_ELAPSED_SECONDS",
    "MAX_PDF_PARSER_MEMORY_BYTES",
    "MAX_DEEP_PARSER_ELAPSED_SECONDS",
    "MAX_DEEP_PARSER_MEMORY_BYTES",
    "MAX_PARSED_ITEMS_OR_BLOCKS",
    "MAX_NORMALIZED_OUTPUT_BYTES",
    "DocumentResourceUsage",
    "DocumentSourceProvenance",
    "NormalizedDocumentItem",
    "NormalizedDocumentObservation",
    "ProjectOwnedBoundedDocumentIngress",
    "IsolatedDocxWorker",
    "IsolatedDoclingWorker",
    "IsolatedEnrichedDoclingWorker",
    "IsolatedMacOcrWorker",
    "IsolatedParserWorker",
    "IsolatedPdfWorker",
    "IsolatedPptxWorker",
    "IsolatedXlsxWorker",
    "StructuredDocumentParserAdapter",
    "identify_document_format",
    "StewardRuntimeDependencies",
    "register_steward_tools",
    "ComparisonOutcome",
    "CurrentMetadataObservation",
    "CurrentState",
    "EvidenceValue",
    "EvidenceValueState",
    "FieldComparison",
    "PayloadComparison",
    "ProjectOwnedCurrentMetadataObserver",
    "TemporalEvidenceRelation",
    "TemporalEvidenceRelationService",
    "VerifiedSnapshotEntryResolver",
    "ModelFinalAnswer",
    "ModelMessage",
    "ModelMessageRole",
    "ModelToolCall",
    "ModelToolBatchResultMessage",
    "ModelToolCallingError",
    "ModelToolDescriptor",
    "ModelToolResultMessage",
    "ModelToolResultDisposition",
    "ModelTurnResult",
    "OpenAICompatibleToolCallingModel",
    "ToolCallingProviderSettings",
    "ToolCallingProviderTransport",
    "strict_json_object",
]
