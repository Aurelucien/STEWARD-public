"""Stateless region-aware visual projection for bounded current documents."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from importlib import import_module
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import unicodedata
from typing import Any, cast

from ...document_observation import (
    selected_document_scope,
    validate_document_scoped_path,
)
from ...document_execution import BoundedDocumentParseCache
from ...evidence import canonical_json
from ...models import StewardConfig
from .scope_binding import ScopeBinding, ScopeBindings
from .structured_documents import (
    IMAGE_SOURCE_FORMATS,
    IsolatedParserWorker,
    NormalizedDocumentItem,
    ProjectOwnedBoundedDocumentIngress,
    StructuredDocumentParserAdapter,
    _WorkerExecution,
    _IngressFailure,
    identify_document_format,
)
from .video_documents import (
    VIDEO_SOURCE_FORMATS,
    VIDEO_SUFFIX_BY_FORMAT,
    ffmpeg_runtime_version,
    probe_video,
)


MAX_VISUAL_SCALE = 3.0
MAX_VISUAL_PIXELS = 12_000_000
MAX_VISUAL_BYTES = 6 * 1024 * 1024
MAX_VISUAL_DECODE_WORK_BYTES = 1024 * 1024 * 1024
MAX_VISUAL_QUERY_BYTES = 512
MAX_VISUAL_NODE_ID_BYTES = 512
MAX_OFFICE_RENDER_SECONDS = 60.0
VISUAL_SOURCE_FORMATS = frozenset(
    {"PDF", "DOCX", "XLSX", "PPTX", *IMAGE_SOURCE_FORMATS, *VIDEO_SOURCE_FORMATS}
)
OFFICE_SOURCE_FORMATS = frozenset({"DOCX", "XLSX", "PPTX"})


@dataclass(frozen=True, slots=True)
class DocumentVisualRequest:
    """One bounded visual page or graph-node request."""

    scope_id: str
    relative_path: str
    page: int | None = None
    node_id: str | None = None
    content_query: str | None = None
    expected_source_sha256: str | None = None
    scale: float = 2.0
    video_timestamp_ms: int | None = None


@dataclass(frozen=True, slots=True)
class DocumentVisualArtifact:
    """Safe metadata plus one ephemeral MCP image payload."""

    status: str
    source_format: str | None
    scope_id: str
    relative_path: str
    source_sha256: str | None
    renderer_name: str | None
    renderer_version: str | None
    rendered_page: int | None
    page_count: int | None
    rendered_bbox: tuple[float, float, float, float] | None
    selected_node_id: str | None
    selected_role: str | None
    selection_query: str | None
    matched_node_count: int
    mime_type: str | None
    pixel_width: int | None
    pixel_height: int | None
    image_sha256: str | None
    image_bytes: int
    parser_backend_name: str | None
    parser_backend_version: str | None
    parser_observation_digest: str | None
    warnings: tuple[str, ...]
    render_elapsed_ms: int
    render_peak_memory_bytes: int | None
    identification_reason: str | None = None
    _image_data: bytes = field(default=b"", repr=False)
    source_pixel_width: int | None = None
    source_pixel_height: int | None = None
    source_mode: str | None = None
    source_frame_count: int | None = None
    estimated_decode_work_bytes: int | None = None
    decoder_output_width: int | None = None
    decoder_output_height: int | None = None
    decoder_subsample: float | None = None
    rendered_timestamp_ms: int | None = None

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema_name": "local_steward.document_visual_artifact",
            "schema_version": 2,
            "status": self.status,
            "source_kind": (
                "CURRENT_FILESYSTEM_VIDEO"
                if self.source_format in VIDEO_SOURCE_FORMATS
                else "CURRENT_FILESYSTEM_DOCUMENT"
            ),
            "source_format": self.source_format,
            "scope_id": self.scope_id,
            "relative_path": self.relative_path,
            "source_sha256": self.source_sha256,
            "renderer_name": self.renderer_name,
            "renderer_version": self.renderer_version,
            "rendered_page": self.rendered_page,
            "rendered_timestamp_ms": self.rendered_timestamp_ms,
            "page_count": self.page_count,
            "rendered_bbox": list(self.rendered_bbox) if self.rendered_bbox else None,
            "selected_node_id": self.selected_node_id,
            "selected_role": self.selected_role,
            "selection_query": self.selection_query,
            "matched_node_count": self.matched_node_count,
            "mime_type": self.mime_type,
            "pixel_width": self.pixel_width,
            "pixel_height": self.pixel_height,
            "image_sha256": self.image_sha256,
            "image_bytes": self.image_bytes,
            "parser_backend_name": self.parser_backend_name,
            "parser_backend_version": self.parser_backend_version,
            "parser_observation_digest": self.parser_observation_digest,
            "warnings": list(self.warnings),
            "resource_usage": {
                "render_elapsed_ms": self.render_elapsed_ms,
                "render_peak_memory_bytes": self.render_peak_memory_bytes,
                "estimated_decode_work_bytes": self.estimated_decode_work_bytes,
                "decode_work_limit_bytes": MAX_VISUAL_DECODE_WORK_BYTES,
            },
            "decode_projection": {
                "source_pixel_width": self.source_pixel_width,
                "source_pixel_height": self.source_pixel_height,
                "source_mode": self.source_mode,
                "source_frame_count": self.source_frame_count,
                "decoder_output_width": self.decoder_output_width,
                "decoder_output_height": self.decoder_output_height,
                "decoder_subsample": self.decoder_subsample,
            },
        }
        if self.identification_reason is not None:
            value["identification_reason"] = self.identification_reason
        digest_payload = dict(value)
        value["artifact_digest"] = sha256(canonical_json(digest_payload)).hexdigest()
        return value

    @property
    def image_data(self) -> bytes:
        return self._image_data


def _validate_request(request: DocumentVisualRequest) -> None:
    if not request.scope_id or not request.relative_path:
        raise ValueError("scope and relative path are required")
    if request.page is not None and (
        isinstance(request.page, bool) or not isinstance(request.page, int) or request.page < 1
    ):
        raise ValueError("visual page must be a positive integer")
    for value, limit, name in (
        (request.node_id, MAX_VISUAL_NODE_ID_BYTES, "node ID"),
        (request.content_query, MAX_VISUAL_QUERY_BYTES, "content query"),
    ):
        if value is not None and (
            not value.strip() or len(value.encode("utf-8")) > limit or "\x00" in value
        ):
            raise ValueError(f"visual {name} is invalid")
    if request.node_id is not None and request.page is not None:
        raise ValueError("visual node and page selectors are mutually exclusive")
    if request.node_id is not None and request.content_query is not None:
        raise ValueError("visual node and content-query selectors are mutually exclusive")
    if request.video_timestamp_ms is not None and (
        type(request.video_timestamp_ms) is not int or request.video_timestamp_ms < 0
    ):
        raise ValueError("video timestamp must be a nonnegative integer")
    if request.video_timestamp_ms is not None and any(
        value is not None for value in (request.page, request.node_id, request.content_query)
    ):
        raise ValueError("video timestamp and document selectors are mutually exclusive")
    if (
        isinstance(request.scale, bool)
        or not isinstance(request.scale, (int, float))
        or not 0.5 <= float(request.scale) <= MAX_VISUAL_SCALE
    ):
        raise ValueError("visual scale is outside the bounded range")
    expected = request.expected_source_sha256
    if expected is not None and re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ValueError("expected source SHA-256 is invalid")


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _visual_region(item: NormalizedDocumentItem) -> tuple[int, tuple[float, ...]] | None:
    extension = item.extension
    if not isinstance(extension, dict):
        return None
    raw = extension.get("visual_region")
    if not isinstance(raw, dict):
        return None
    page = raw.get("page")
    bbox = raw.get("bbox")
    coordinate_space = raw.get("coordinate_space")
    if (
        not isinstance(page, int)
        or coordinate_space != "PAGE_POINTS_TOP_LEFT"
        or not isinstance(bbox, list)
        or len(bbox) != 4
        or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool) for value in bbox
        )
    ):
        return None
    return page, tuple(float(cast(int | float, value)) for value in bbox)


def _select_item(
    items: tuple[NormalizedDocumentItem, ...], request: DocumentVisualRequest
) -> tuple[NormalizedDocumentItem | None, int]:
    if request.node_id is not None:
        matches = [item for item in items if item.node_id == request.node_id]
        if len(matches) != 1:
            raise ValueError("visual node selector did not resolve exactly one node")
        return matches[0], 1
    if request.content_query is None:
        return None, 0
    query = _fold(request.content_query)
    matches = [
        item
        for item in items
        if item.text_or_value is not None and query in _fold(item.text_or_value)
    ]
    if not matches:
        raise ValueError("visual content query did not resolve a graph node")
    ranked = sorted(
        enumerate(matches),
        key=lambda pair: (
            0 if _visual_region(pair[1]) is not None else 1,
            0 if pair[1].role in {"FIGURE", "FORMULA", "TABLE"} else 1,
            pair[0],
        ),
    )
    selected = ranked[0][1]
    if selected.role == "CAPTION":
        caption_region = _visual_region(selected)
        if caption_region is not None:
            page = caption_region[0]
            nearby = [
                item
                for item in items
                if item.role in {"FIGURE", "FORMULA", "TABLE"}
                and (_visual_region(item) or (None, ()))[0] == page
            ]
            if nearby:
                selected = nearby[0]
    return selected, len(matches)


def _office_to_pdf(source_path: Path) -> Path:
    configured = os.environ.get("LOCAL_STEWARD_SOFFICE")
    executable = configured if configured else shutil.which("soffice")
    if not executable:
        raise RuntimeError("LibreOffice headless renderer is unavailable")
    executable_path = Path(executable).resolve(strict=True)
    if not executable_path.is_file() or not os.access(executable_path, os.X_OK):
        raise RuntimeError("LibreOffice headless renderer is unavailable")
    output_root = source_path.parent / "office-render"
    profile_root = source_path.parent / "office-profile"
    output_root.mkdir(mode=0o700)
    profile_root.mkdir(mode=0o700)
    environment = dict(os.environ)
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        environment.pop(name, None)
    environment["SAL_USE_VCLPLUGIN"] = "svp"
    command = [
        str(executable_path),
        "--headless",
        "--nologo",
        "--nodefault",
        "--norestore",
        "--nolockcheck",
        f"-env:UserInstallation={profile_root.as_uri()}",
        "--convert-to",
        "pdf",
        "--outdir",
        str(output_root),
        str(source_path),
    ]
    completed = subprocess.run(
        command,
        cwd=source_path.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=MAX_OFFICE_RENDER_SECONDS,
        check=False,
    )
    output = output_root / f"{source_path.stem}.pdf"
    if completed.returncode != 0 or not output.is_file() or output.is_symlink():
        raise RuntimeError("LibreOffice did not produce one bounded PDF projection")
    return output


def _render_pdf(
    source_path: Path,
    *,
    page_number: int,
    scale: float,
    bbox: tuple[float, float, float, float] | None,
) -> dict[str, Any]:
    pymupdf = import_module("pymupdf")

    document = pymupdf.open(source_path)
    try:
        if page_number > document.page_count:
            raise ValueError("visual page is outside the rendered document")
        page = document.load_page(page_number - 1)
        clip = page.rect
        if bbox is not None:
            padding = 8.0
            requested = pymupdf.Rect(
                bbox[0] - padding,
                bbox[1] - padding,
                bbox[2] + padding,
                bbox[3] + padding,
            )
            clip = requested & page.rect
            if clip.is_empty or clip.is_infinite:
                raise ValueError("visual node region is outside its page")
        bounded_scale = min(
            scale,
            (MAX_VISUAL_PIXELS / max(1.0, clip.width * clip.height)) ** 0.5,
        )
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(bounded_scale, bounded_scale),
            clip=clip,
            alpha=False,
        )
        image = pixmap.tobytes("png")
        if len(image) > MAX_VISUAL_BYTES:
            raise MemoryError
        return {
            "image_data": image,
            "mime_type": "image/png",
            "pixel_width": pixmap.width,
            "pixel_height": pixmap.height,
            "page_count": document.page_count,
            "rendered_page": page_number,
            "rendered_bbox": [float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1)],
            "renderer_name": "PyMuPDF",
            "renderer_version": pymupdf.VersionBind,
        }
    finally:
        document.close()


def _render_image(
    source_path: Path,
    *,
    page_number: int,
    bbox: tuple[float, float, float, float] | None,
) -> dict[str, Any]:
    Image = import_module("PIL.Image")

    setattr(Image, "MAX_IMAGE_PIXELS", MAX_VISUAL_PIXELS * 8)
    with Image.open(source_path) as source:
        frame_count = int(getattr(source, "n_frames", 1))
        if page_number > frame_count:
            raise ValueError("visual page is outside the image")
        source.seek(page_number - 1)
        source_width, source_height = source.size
        source_mode = str(source.mode)
        source_bands = max(1, len(source.getbands()))
        bytes_per_channel = 2 if source_mode.startswith(("I;16", "I", "F")) else 1
        target_edge = int(MAX_VISUAL_PIXELS**0.5)
        target_scale = min(
            1.0,
            target_edge / max(1, source_width),
            target_edge / max(1, source_height),
        )
        decoder_target = (
            max(1, int(source_width * target_scale)),
            max(1, int(source_height * target_scale)),
        )
        # Pillow's JPEG draft uses decoder-native 1/2, 1/4 or 1/8 sampling before
        # a full RGB frame is materialized. Other codecs safely retain their
        # native dimensions and remain governed by the decoded-work estimate.
        if (getattr(source, "format", "") or "").upper() == "JPEG":
            source.draft("RGB", decoder_target)
        decoder_width, decoder_height = source.size
        decoder_subsample = max(
            source_width / max(1, decoder_width),
            source_height / max(1, decoder_height),
        )
        output_pixels = min(decoder_width * decoder_height, MAX_VISUAL_PIXELS)
        estimated_decode_work_bytes = (
            decoder_width
            * decoder_height
            * (source_bands * bytes_per_channel + 3)
            + output_pixels * 3
        )
        if estimated_decode_work_bytes > MAX_VISUAL_DECODE_WORK_BYTES:
            raise MemoryError
        image = source.convert("RGB")
        rendered_bbox = (0.0, 0.0, float(source_width), float(source_height))
        if bbox is not None:
            left = max(0, int(bbox[0] - 8))
            top = max(0, int(bbox[1] - 8))
            right = min(source_width, int(bbox[2] + 8))
            bottom = min(source_height, int(bbox[3] + 8))
            if right <= left or bottom <= top:
                raise ValueError("visual node region is outside the image")
            scale_x = decoder_width / max(1, source_width)
            scale_y = decoder_height / max(1, source_height)
            image = image.crop(
                (
                    int(left * scale_x),
                    int(top * scale_y),
                    max(int(left * scale_x) + 1, int(right * scale_x)),
                    max(int(top * scale_y) + 1, int(bottom * scale_y)),
                )
            )
            rendered_bbox = (float(left), float(top), float(right), float(bottom))
        if image.width * image.height > MAX_VISUAL_PIXELS:
            image.thumbnail((int(MAX_VISUAL_PIXELS**0.5), int(MAX_VISUAL_PIXELS**0.5)))
        from io import BytesIO

        buffer = BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        payload = buffer.getvalue()
        mime_type = "image/png"
        if len(payload) > MAX_VISUAL_BYTES:
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=88, optimize=True)
            payload = buffer.getvalue()
            mime_type = "image/jpeg"
        if len(payload) > MAX_VISUAL_BYTES:
            raise MemoryError
        return {
            "image_data": payload,
            "mime_type": mime_type,
            "pixel_width": image.width,
            "pixel_height": image.height,
            "page_count": frame_count,
            "rendered_page": page_number,
            "rendered_bbox": list(rendered_bbox),
            "renderer_name": "Pillow",
            "renderer_version": getattr(Image, "__version__", "unknown"),
            "source_pixel_width": source_width,
            "source_pixel_height": source_height,
            "source_mode": source_mode,
            "source_frame_count": frame_count,
            "estimated_decode_work_bytes": estimated_decode_work_bytes,
            "decoder_output_width": decoder_width,
            "decoder_output_height": decoder_height,
            "decoder_subsample": decoder_subsample,
        }


def _render_video(
    source_path: Path,
    *,
    source_format: str,
    timestamp_ms: int,
) -> dict[str, Any]:
    probe = probe_video(str(source_path), source_format)
    duration_ms = probe.get("duration_ms")
    stream_index = probe.get("primary_video_stream_index")
    if (
        type(duration_ms) is not int
        or type(stream_index) is not int
        or not 0 <= timestamp_ms < duration_ms
    ):
        raise ValueError("video timestamp is outside the source")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("FFmpeg frame renderer is unavailable")
    target = source_path.with_name("video-frame.png")
    completed = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{timestamp_ms / 1000:.3f}",
            "-i",
            str(source_path),
            "-map",
            f"0:{stream_index}",
            "-frames:v",
            "1",
            "-vf",
            "scale='min(3464,iw)':-2",
            "-c:v",
            "png",
            "-y",
            str(target),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60.0,
        check=False,
    )
    if (
        completed.returncode != 0
        or len(completed.stdout) > 1024
        or len(completed.stderr) > 64 * 1024
        or not target.is_file()
        or target.stat().st_size > MAX_VISUAL_BYTES
    ):
        raise ValueError("video frame render failed")
    image_data = target.read_bytes()
    Image = import_module("PIL.Image")
    with Image.open(target) as image:
        width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_VISUAL_PIXELS:
        raise MemoryError
    return {
        "image_data": image_data,
        "mime_type": "image/png",
        "pixel_width": width,
        "pixel_height": height,
        "page_count": 1,
        "rendered_page": 1,
        "rendered_bbox": [0.0, 0.0, float(width), float(height)],
        "renderer_name": "FFmpeg",
        "renderer_version": ffmpeg_runtime_version(),
        "rendered_timestamp_ms": timestamp_ms,
    }


def _render_document_worker(source_path: str) -> dict[str, Any]:
    request_path = Path(source_path).with_name("render-request.json")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    if not isinstance(request, dict):
        raise ValueError("visual render request is invalid")
    source_format = request.get("source_format")
    page = request.get("page")
    scale = request.get("scale")
    raw_bbox = request.get("bbox")
    video_timestamp_ms = request.get("video_timestamp_ms")
    if (
        not isinstance(source_format, str)
        or not isinstance(page, int)
        or not isinstance(scale, (int, float))
    ):
        raise ValueError("visual render request is invalid")
    bbox: tuple[float, float, float, float] | None = None
    if raw_bbox is not None:
        if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
            raise ValueError("visual render region is invalid")
        bbox = tuple(float(value) for value in raw_bbox)  # type: ignore[assignment]
    path = Path(source_path)
    if source_format in OFFICE_SOURCE_FORMATS:
        projected = _office_to_pdf(path)
        result = _render_pdf(projected, page_number=page, scale=float(scale), bbox=bbox)
        result["renderer_name"] = "LibreOffice+PyMuPDF"
        return result
    if source_format == "PDF":
        return _render_pdf(path, page_number=page, scale=float(scale), bbox=bbox)
    if source_format in IMAGE_SOURCE_FORMATS:
        return _render_image(path, page_number=page, bbox=bbox)
    if source_format in VIDEO_SOURCE_FORMATS and (
        video_timestamp_ms is None or type(video_timestamp_ms) is int
    ):
        return _render_video(
            path,
            source_format=source_format,
            timestamp_ms=video_timestamp_ms if type(video_timestamp_ms) is int else 0,
        )
    raise ValueError("source format has no visual renderer")


@dataclass(slots=True)
class VisualDocumentAdapter:
    """Coordinate deep graph selection with one bounded ephemeral render."""

    ingress: ProjectOwnedBoundedDocumentIngress
    parser: StructuredDocumentParserAdapter
    worker: IsolatedParserWorker = field(
        default_factory=lambda: IsolatedParserWorker(
            _render_document_worker,
            timeout_seconds=90.0,
            memory_bytes=2 * 1024 * 1024 * 1024,
        )
    )

    def observe(self, request: DocumentVisualRequest) -> DocumentVisualArtifact:
        _validate_request(request)
        parser_backend_name: str | None = None
        parser_backend_version: str | None = None
        parser_digest: str | None = None
        selected: NormalizedDocumentItem | None = None
        matched_node_count = 0
        bbox: tuple[float, float, float, float] | None = None
        page = request.page
        warnings: list[str] = []
        source_sha256: str | None = None
        if request.node_id is not None or request.content_query is not None:
            # Page-local OCR nodes belong to the scan-aware AUTO graph, not to
            # Docling's DEEP graph. Reuse the producing route so READ node IDs
            # remain resolvable by VIEW and the bounded parse cache can be reused.
            parser_profile = (
                "AUTO"
                if request.node_id is not None
                and request.node_id.startswith("pdf:page:")
                and ":ocr-line:" in request.node_id
                else "DEEP"
            )
            observation = self.parser.observe(
                {
                    "scope_id": request.scope_id,
                    "relative_path": request.relative_path,
                    "parser_profile": parser_profile,
                }
            )
            if observation.status != "COMPLETE":
                return self._failure(
                    observation.status,
                    request,
                    observation.source_format,
                    observation.provenance.source_sha256,
                    observation.identification_reason,
                )
            source_sha256 = observation.provenance.source_sha256
            parser_backend_name = observation.backend_name
            parser_backend_version = observation.backend_version
            parser_digest = observation.result_digest
            warnings.extend(observation.warnings)
            selected, matched_node_count = _select_item(observation.items, request)
            assert selected is not None
            region = _visual_region(selected)
            if region is not None:
                page = region[0]
                bbox = (region[1][0], region[1][1], region[1][2], region[1][3])
            else:
                raw_page = selected.location.get("page") or selected.location.get("slide")
                if isinstance(raw_page, int):
                    page = raw_page
                warnings.append("NODE_REGION_UNAVAILABLE_FULL_PAGE_RENDERED")
        admitted = self.ingress.admit(
            {"scope_id": request.scope_id, "relative_path": request.relative_path}
        )
        if isinstance(admitted, _IngressFailure):
            return self._failure(admitted.status, request, None, None, None)

        def finish(artifact: DocumentVisualArtifact) -> DocumentVisualArtifact:
            admitted.close()
            return artifact

        if source_sha256 is not None and admitted.source_sha256 != source_sha256:
            return finish(
                self._failure(
                    "SOURCE_CHANGED",
                    request,
                    None,
                    admitted.source_sha256,
                    "SOURCE_DIGEST_CHANGED",
                )
            )
        if (
            request.expected_source_sha256 is not None
            and admitted.source_sha256 != request.expected_source_sha256
        ):
            return finish(
                self._failure(
                    "SOURCE_CHANGED",
                    request,
                    None,
                    admitted.source_sha256,
                    "SOURCE_DIGEST_CHANGED",
                )
            )
        source_sha256 = admitted.source_sha256
        identified = identify_document_format(admitted._staged_path, admitted.relative_path)
        if identified.status is not None:
            return finish(
                self._failure(
                    identified.status,
                    request,
                    identified.source_format,
                    source_sha256,
                    identified.reason,
                )
            )
        source_format = identified.source_format
        if source_format not in VISUAL_SOURCE_FORMATS:
            return finish(
                self._failure(
                    "UNSUPPORTED_VISUAL",
                    request,
                    source_format,
                    source_sha256,
                    "VISUAL_RENDERER_UNAVAILABLE",
                )
            )
        if source_format in OFFICE_SOURCE_FORMATS and identified.external_relationship_count:
            return finish(
                self._failure(
                    "UNSUPPORTED_VISUAL",
                    request,
                    source_format,
                    source_sha256,
                    "OFFICE_EXTERNAL_RELATIONSHIPS_PRESENT",
                )
            )
        page = page or 1
        suffix = {
            "PDF": ".pdf",
            "DOCX": ".docx",
            "XLSX": ".xlsx",
            "PPTX": ".pptx",
            "PNG": ".png",
            "JPEG": ".jpg",
            "TIFF": ".tiff",
            **VIDEO_SUFFIX_BY_FORMAT,
        }[source_format]
        with admitted.staged_copy(suffix) as staged_path:
            request_path = staged_path.with_name("render-request.json")
            request_path.write_bytes(
                canonical_json(
                    {
                        "source_format": source_format,
                        "page": page,
                        "scale": float(request.scale),
                        "bbox": list(bbox) if bbox else None,
                        "video_timestamp_ms": request.video_timestamp_ms,
                    }
                )
            )
            request_path.chmod(stat.S_IRUSR)
            execution = self.worker.run(staged_path)
        if execution.status != "COMPLETE" or execution.payload is None:
            return finish(
                self._failure(
                    execution.status,
                    request,
                    source_format,
                    source_sha256,
                    "VISUAL_RENDER_FAILED",
                    elapsed_ms=execution.elapsed_ms,
                    peak_memory=execution.peak_memory_bytes,
                )
            )
        payload = execution.payload
        image_data = payload.get("image_data")
        rendered_bbox = payload.get("rendered_bbox")
        renderer_name = payload.get("renderer_name")
        renderer_version = payload.get("renderer_version")
        rendered_page = payload.get("rendered_page")
        page_count = payload.get("page_count")
        mime_type = payload.get("mime_type")
        pixel_width = payload.get("pixel_width")
        pixel_height = payload.get("pixel_height")
        source_pixel_width = payload.get("source_pixel_width")
        source_pixel_height = payload.get("source_pixel_height")
        source_mode = payload.get("source_mode")
        source_frame_count = payload.get("source_frame_count")
        estimated_decode_work_bytes = payload.get("estimated_decode_work_bytes")
        decoder_output_width = payload.get("decoder_output_width")
        decoder_output_height = payload.get("decoder_output_height")
        decoder_subsample = payload.get("decoder_subsample")
        rendered_timestamp_ms = payload.get("rendered_timestamp_ms")
        if (
            not isinstance(image_data, bytes)
            or len(image_data) > MAX_VISUAL_BYTES
            or not isinstance(rendered_bbox, list)
            or len(rendered_bbox) != 4
            or not all(isinstance(value, (int, float)) for value in rendered_bbox)
            or not isinstance(renderer_name, str)
            or not isinstance(renderer_version, str)
            or not isinstance(rendered_page, int)
            or not isinstance(page_count, int)
            or not isinstance(mime_type, str)
            or not isinstance(pixel_width, int)
            or not isinstance(pixel_height, int)
            or (
                source_format in VIDEO_SOURCE_FORMATS
                and type(rendered_timestamp_ms) is not int
            )
            or (
                source_format in IMAGE_SOURCE_FORMATS
                and (
                    not isinstance(source_pixel_width, int)
                    or not isinstance(source_pixel_height, int)
                    or not isinstance(source_mode, str)
                    or not isinstance(source_frame_count, int)
                    or not isinstance(estimated_decode_work_bytes, int)
                    or not isinstance(decoder_output_width, int)
                    or not isinstance(decoder_output_height, int)
                    or not isinstance(decoder_subsample, (int, float))
                )
            )
        ):
            return finish(
                self._failure(
                    "PARSER_FAILED",
                    request,
                    source_format,
                    source_sha256,
                    "VISUAL_RENDER_INVALID",
                    elapsed_ms=execution.elapsed_ms,
                    peak_memory=execution.peak_memory_bytes,
                )
            )
        return finish(
            DocumentVisualArtifact(
                "COMPLETE",
                source_format,
                request.scope_id,
                request.relative_path,
                source_sha256,
                renderer_name,
                renderer_version,
                rendered_page,
                page_count,
                tuple(float(value) for value in rendered_bbox),  # type: ignore[arg-type]
                selected.node_id if selected else None,
                selected.role if selected else None,
                request.content_query,
                matched_node_count,
                mime_type,
                pixel_width,
                pixel_height,
                sha256(image_data).hexdigest(),
                len(image_data),
                parser_backend_name,
                parser_backend_version,
                parser_digest,
                tuple(warnings),
                execution.elapsed_ms,
                execution.peak_memory_bytes,
                None,
                image_data,
                source_pixel_width if isinstance(source_pixel_width, int) else None,
                source_pixel_height if isinstance(source_pixel_height, int) else None,
                source_mode if isinstance(source_mode, str) else None,
                source_frame_count if isinstance(source_frame_count, int) else None,
                (
                    estimated_decode_work_bytes
                    if isinstance(estimated_decode_work_bytes, int)
                    else None
                ),
                decoder_output_width if isinstance(decoder_output_width, int) else None,
                decoder_output_height if isinstance(decoder_output_height, int) else None,
                float(decoder_subsample) if isinstance(decoder_subsample, (int, float)) else None,
                rendered_timestamp_ms if type(rendered_timestamp_ms) is int else None,
            )
        )

    @staticmethod
    def _failure(
        status: str,
        request: DocumentVisualRequest,
        source_format: str | None,
        source_sha256: str | None,
        reason: str | None,
        *,
        elapsed_ms: int = 0,
        peak_memory: int | None = None,
    ) -> DocumentVisualArtifact:
        return DocumentVisualArtifact(
            status,
            source_format,
            request.scope_id,
            request.relative_path,
            source_sha256,
            None,
            None,
            None,
            None,
            None,
            request.node_id,
            None,
            request.content_query,
            0,
            None,
            None,
            None,
            None,
            0,
            None,
            None,
            None,
            (),
            elapsed_ms,
            peak_memory,
            reason,
        )


def inspect_document_visual(
    config: StewardConfig,
    request: DocumentVisualRequest,
    *,
    parse_cache: BoundedDocumentParseCache[_WorkerExecution] | None = None,
) -> DocumentVisualArtifact:
    """Resolve one configured scope and produce one ephemeral visual artifact."""

    _validate_request(request)
    scope = selected_document_scope(config, request.scope_id)
    validate_document_scoped_path(config, scope, request.relative_path)
    root = scope.normalized_path
    bindings = ScopeBindings(
        (ScopeBinding(scope.scope_id, root),),
        (str(root),),
        (scope.scope_id,),
    )
    ingress = ProjectOwnedBoundedDocumentIngress(bindings, require_same_device=True)
    return VisualDocumentAdapter(
        ingress,
        StructuredDocumentParserAdapter(ingress, parse_cache=parse_cache),
    ).observe(request)


__all__ = [
    "MAX_VISUAL_BYTES",
    "MAX_VISUAL_DECODE_WORK_BYTES",
    "MAX_VISUAL_PIXELS",
    "MAX_VISUAL_SCALE",
    "DocumentVisualArtifact",
    "DocumentVisualRequest",
    "VisualDocumentAdapter",
    "inspect_document_visual",
]
