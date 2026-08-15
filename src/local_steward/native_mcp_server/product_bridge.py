"""Compatibility bridge from host-approved MCP tools to the current product API."""

from __future__ import annotations

from ..agent_session import StewardSession
from ..document_execution import BoundedDocumentParseCache
from ..document_evidence import (
    DEFAULT_DOCUMENT_EVIDENCE_CONTEXT_ITEMS,
    DEFAULT_DOCUMENT_EVIDENCE_MAX_CHARS,
)
from ..document_observation import (
    DocumentInspectionPage,
    DocumentInspectionRequest,
    inspect_document,
)
from ..file_agent.runtime.visual_documents import (
    DocumentVisualArtifact,
    DocumentVisualRequest,
    inspect_document_visual,
)
from ..file_agent.runtime.structured_documents import _WorkerExecution
from ..models import ScanBudget
from ..models import StewardConfig
from ..snapshot_acquisition import (
    SnapshotAcquisitionRequest,
    SnapshotAcquisitionReport,
    acquire_snapshot,
    recover_snapshot_acquisition,
)
from ..snapshot_refresh import SnapshotRefreshReport, SnapshotRefreshRequest, refresh_snapshot


class HostApprovedProductBridge:
    """Own legacy confirmation flags after the Codex host tool boundary.

    These flags satisfy existing product API compatibility. They are not user
    approval Evidence and are never accepted from MCP arguments.
    """

    def __init__(self, session: StewardSession) -> None:
        self._config = session.config
        self._document_parse_cache = BoundedDocumentParseCache[_WorkerExecution]()

    def inspect_document(
        self,
        scope_id: str,
        relative_path: str,
        *,
        limit: int,
        offset: int,
        expected_source_sha256: str | None,
        content_query: str | None,
        content_limit: int,
        content_offset: int,
        parser_profile: str = "DEEP",
        view: str = "READ",
        intent: str = "READ",
        evidence_mode: str = "AUTO",
        evidence_context_items: int = DEFAULT_DOCUMENT_EVIDENCE_CONTEXT_ITEMS,
        evidence_max_characters: int = DEFAULT_DOCUMENT_EVIDENCE_MAX_CHARS,
        evidence_page: int | None = None,
        parser_timeout_seconds: float | None = None,
        audio_analysis: str = "TRANSCRIPT",
        audio_language: str | None = None,
        audio_continuation: dict[str, object] | None = None,
        video_analysis: str = "MULTIMODAL",
        video_continuation: dict[str, object] | None = None,
        operation_config: StewardConfig | None = None,
    ) -> DocumentInspectionPage:
        return inspect_document(
            operation_config or self._config,
            DocumentInspectionRequest(
                scope_id,
                relative_path,
                True,
                limit=limit,
                offset=offset,
                expected_source_sha256=expected_source_sha256,
                content_query=content_query,
                content_limit=content_limit,
                content_offset=content_offset,
                parser_profile=parser_profile,
                view=view,
                intent=intent,
                evidence_mode=evidence_mode,
                evidence_context_items=evidence_context_items,
                evidence_max_characters=evidence_max_characters,
                evidence_page=evidence_page,
                parser_timeout_seconds=parser_timeout_seconds,
                audio_analysis=audio_analysis,
                audio_language=audio_language,
                audio_continuation=audio_continuation,
                video_analysis=video_analysis,
                video_continuation=video_continuation,
            ),
            parse_cache=self._document_parse_cache,
        )

    def acquire(self, scope_id: str, budget: ScanBudget) -> SnapshotAcquisitionReport:
        return acquire_snapshot(
            self._config,
            SnapshotAcquisitionRequest(scope_id, budget, confirmed=True),
        )

    def inspect_document_visual(
        self,
        scope_id: str,
        relative_path: str,
        *,
        page: int | None,
        node_id: str | None,
        content_query: str | None,
        expected_source_sha256: str | None,
        scale: float,
        video_timestamp_ms: int | None = None,
        operation_config: StewardConfig | None = None,
    ) -> DocumentVisualArtifact:
        return inspect_document_visual(
            operation_config or self._config,
            DocumentVisualRequest(
                scope_id,
                relative_path,
                page,
                node_id,
                content_query,
                expected_source_sha256,
                scale,
                video_timestamp_ms,
            ),
            parse_cache=self._document_parse_cache,
        )

    def refresh(
        self,
        scope_id: str,
        base_snapshot_id: str,
        budget: ScanBudget,
        *,
        change_limit: int,
        change_offset: int,
    ) -> SnapshotRefreshReport:
        return refresh_snapshot(
            self._config,
            SnapshotRefreshRequest(
                scope_id,
                base_snapshot_id,
                budget,
                True,
                change_limit,
                change_offset,
            ),
        )

    def recover(self, run_id: str) -> SnapshotAcquisitionReport:
        return recover_snapshot_acquisition(self._config, run_id, confirmed=True)
