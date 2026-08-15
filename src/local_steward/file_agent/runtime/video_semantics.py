"""Optional pinned local text-image retrieval for operation-scoped video frames."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
from typing import Any, Iterable, cast

from ...evidence import canonical_json


VIDEO_SEMANTIC_BACKEND = "transformers-clip-openclip-checkpoint"
VIDEO_SEMANTIC_MODEL_ID = "laion/CLIP-ViT-B-32-laion400M-e31"
VIDEO_SEMANTIC_MODEL_REVISION = "open_clip_v0.2_weights"
VIDEO_SEMANTIC_MODEL_SHA256 = "d867053b2301634007ed9af230bfb1a217ec634f6c0329f04092133ae5c4b89e"
VIDEO_SEMANTIC_POLICY_ID = "PINNED_LOCAL_CLIP_COSINE_TEMPORAL_NMS_V1"
VIDEO_SEMANTIC_RELATIVE_SCORE_MARGIN = 0.08
VIDEO_SEMANTIC_POLICY_SHA256 = sha256(
    canonical_json(
        {
            "backend": VIDEO_SEMANTIC_BACKEND,
            "candidate_authority": "MODEL_DERIVED_RETRIEVAL_NOT_TRUTH",
            "model_id": VIDEO_SEMANTIC_MODEL_ID,
            "model_revision": VIDEO_SEMANTIC_MODEL_REVISION,
            "normalization": "L2_COSINE",
            "runtime_downloads_allowed": False,
            "score_admission": {
                "kind": "WITHIN_MARGIN_OF_BEST",
                "margin": VIDEO_SEMANTIC_RELATIVE_SCORE_MARGIN,
                "minimum_candidates": 1,
            },
            "temporal_selection": "SCORE_THEN_NON_OVERLAPPING_WINDOW",
        }
    )
).hexdigest()


class VideoSemanticUnavailable(RuntimeError):
    """The governed local video semantic model or runtime is unavailable."""


@dataclass(frozen=True, slots=True)
class VideoSemanticModel:
    path: Path
    revision: str
    identity_sha256: str


def _model_path() -> Path:
    configured = os.environ.get("STEWARD_VIDEO_MODEL_HOME")
    if configured:
        return Path(configured) / "semantic-v1"
    return Path.home() / ".cache" / "steward" / "video" / "semantic-v1"


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def resolve_local_video_semantic_model() -> VideoSemanticModel:
    """Resolve one exact local checkpoint without network or mutable aliases."""
    try:
        transformers_version = version("transformers")
        version("torch")
    except PackageNotFoundError as error:
        raise VideoSemanticUnavailable("the video semantic runtime is unavailable") from error
    if not transformers_version.startswith("5."):
        raise VideoSemanticUnavailable("the governed transformers runtime is unavailable")
    path = _model_path()
    required = (
        "config.json",
        "preprocessor_config.json",
        "tokenizer_config.json",
        "special_tokens_map.json",
        "merges.txt",
        "vocab.json",
    )
    weights = path / "openclip-vit-b-32-laion400m-e31.pt"
    if not path.is_dir() or not weights.is_file() or any(
        not (path / name).is_file() for name in required
    ):
        raise VideoSemanticUnavailable("the pinned local CLIP model is unavailable")
    if _file_sha256(weights) != VIDEO_SEMANTIC_MODEL_SHA256:
        raise VideoSemanticUnavailable("the pinned local CLIP model identity is invalid")
    manifest = []
    for member in sorted(
        [*(path / name for name in required), weights], key=lambda item: item.name
    ):
        manifest.append(
            {
                "name": member.name,
                "bytes": member.stat().st_size,
                "sha256": _file_sha256(member),
            }
        )
    identity = sha256(
        canonical_json(
            {
                "backend": VIDEO_SEMANTIC_BACKEND,
                "model_id": VIDEO_SEMANTIC_MODEL_ID,
                "revision": VIDEO_SEMANTIC_MODEL_REVISION,
                "runtime_version": transformers_version,
                "manifest": manifest,
            }
        )
    ).hexdigest()
    return VideoSemanticModel(path, VIDEO_SEMANTIC_MODEL_REVISION, identity)


def video_semantic_runtime_capabilities() -> dict[str, object]:
    """Publish path-free readiness for the optional local retrieval layer."""
    model: VideoSemanticModel | None = None
    try:
        model = resolve_local_video_semantic_model()
    except VideoSemanticUnavailable:
        pass
    try:
        transformers_version: str | None = version("transformers")
        torch_version: str | None = version("torch")
    except PackageNotFoundError:
        transformers_version = None
        torch_version = None
    return {
        "backend": VIDEO_SEMANTIC_BACKEND,
        "ready": model is not None,
        "transformers_version": transformers_version,
        "torch_version": torch_version,
        "model_id": VIDEO_SEMANTIC_MODEL_ID,
        "model_revision": model.revision if model else None,
        "model_identity_sha256": model.identity_sha256 if model else None,
        "policy_id": VIDEO_SEMANTIC_POLICY_ID,
        "policy_sha256": VIDEO_SEMANTIC_POLICY_SHA256,
        "query_language_guidance": "ENGLISH_VISUAL_DESCRIPTION_PREFERRED",
        "candidate_authority": "MODEL_DERIVED_RETRIEVAL_NOT_TRUTH",
        "relative_score_margin": VIDEO_SEMANTIC_RELATIVE_SCORE_MARGIN,
        "runtime_downloads_allowed": False,
        "persistence_effect": "NONE",
    }


@lru_cache(maxsize=1)
def _load_runtime(model_path: str) -> tuple[Any, Any, Any]:
    transformers = import_module("transformers")
    torch = import_module("torch")
    config = transformers.CLIPConfig.from_pretrained(model_path, local_files_only=True)
    model = transformers.CLIPModel(config)
    source = torch.load(
        str(Path(model_path) / "openclip-vit-b-32-laion400m-e31.pt"),
        map_location="cpu",
        weights_only=True,
    )
    if not isinstance(source, dict):
        raise VideoSemanticUnavailable("the local OpenCLIP checkpoint is invalid")
    converted = _convert_openclip_state_dict(source, torch=torch)
    missing, unexpected = model.load_state_dict(converted, strict=False)
    if missing or unexpected:
        raise VideoSemanticUnavailable("the local OpenCLIP checkpoint is incompatible")
    tokenizer = transformers.CLIPTokenizer.from_pretrained(
        model_path, local_files_only=True
    )
    image_processor = transformers.CLIPImageProcessor.from_pretrained(
        model_path, local_files_only=True
    )
    model.eval()
    return model, tokenizer, image_processor


def _convert_openclip_state_dict(source: dict[str, Any], *, torch: Any) -> dict[str, Any]:
    """Map the official OpenCLIP ViT-B/32 checkpoint into Transformers CLIP names."""
    result: dict[str, Any] = {
        "logit_scale": source["logit_scale"],
        "text_model.embeddings.token_embedding.weight": source["token_embedding.weight"],
        "text_model.embeddings.position_embedding.weight": source["positional_embedding"],
        "text_model.final_layer_norm.weight": source["ln_final.weight"],
        "text_model.final_layer_norm.bias": source["ln_final.bias"],
        "text_projection.weight": source["text_projection"].T,
        "vision_model.embeddings.class_embedding": source["visual.class_embedding"],
        "vision_model.embeddings.patch_embedding.weight": source["visual.conv1.weight"],
        "vision_model.embeddings.position_embedding.weight": source[
            "visual.positional_embedding"
        ],
        "vision_model.pre_layrnorm.weight": source["visual.ln_pre.weight"],
        "vision_model.pre_layrnorm.bias": source["visual.ln_pre.bias"],
        "vision_model.post_layernorm.weight": source["visual.ln_post.weight"],
        "vision_model.post_layernorm.bias": source["visual.ln_post.bias"],
        "visual_projection.weight": source["visual.proj"].T,
    }

    def layers(*, source_prefix: str, target_prefix: str) -> None:
        for layer in range(12):
            source_layer = f"{source_prefix}.resblocks.{layer}"
            target_layer = f"{target_prefix}.layers.{layer}"
            weights = source[f"{source_layer}.attn.in_proj_weight"]
            biases = source[f"{source_layer}.attn.in_proj_bias"]
            q_weight, k_weight, v_weight = torch.chunk(weights, 3, dim=0)
            q_bias, k_bias, v_bias = torch.chunk(biases, 3, dim=0)
            for name, weight, bias in (
                ("q_proj", q_weight, q_bias),
                ("k_proj", k_weight, k_bias),
                ("v_proj", v_weight, v_bias),
            ):
                result[f"{target_layer}.self_attn.{name}.weight"] = weight
                result[f"{target_layer}.self_attn.{name}.bias"] = bias
            for suffix in ("weight", "bias"):
                result[f"{target_layer}.self_attn.out_proj.{suffix}"] = source[
                    f"{source_layer}.attn.out_proj.{suffix}"
                ]
                result[f"{target_layer}.layer_norm1.{suffix}"] = source[
                    f"{source_layer}.ln_1.{suffix}"
                ]
                result[f"{target_layer}.layer_norm2.{suffix}"] = source[
                    f"{source_layer}.ln_2.{suffix}"
                ]
                result[f"{target_layer}.mlp.fc1.{suffix}"] = source[
                    f"{source_layer}.mlp.c_fc.{suffix}"
                ]
                result[f"{target_layer}.mlp.fc2.{suffix}"] = source[
                    f"{source_layer}.mlp.c_proj.{suffix}"
                ]

    layers(source_prefix="transformer", target_prefix="text_model.encoder")
    layers(source_prefix="visual.transformer", target_prefix="vision_model.encoder")
    return result


def rank_video_frames(
    *, query: str, frames: Iterable[tuple[int, str]]
) -> tuple[list[dict[str, object]], VideoSemanticModel]:
    """Return cosine-ranked temporary frames; scores are retrieval hints, not truth."""
    normalized_query = " ".join(query.split())
    if not normalized_query or len(normalized_query) > 512:
        raise ValueError("video semantic query is empty or exceeds its bound")
    candidates = list(frames)
    if not candidates:
        return [], resolve_local_video_semantic_model()
    model_record = resolve_local_video_semantic_model()
    try:
        model, tokenizer, image_processor = _load_runtime(str(model_record.path))
        torch = import_module("torch")
        Image = import_module("PIL.Image")
        text_inputs = tokenizer([normalized_query], return_tensors="pt", padding=True)
        with torch.inference_mode():
            text_output = model.get_text_features(**text_inputs)
            text_features = getattr(text_output, "pooler_output", text_output)
            text_features = text_features / text_features.norm(dim=-1, keepdim=True)
        ranked: list[dict[str, object]] = []
        for offset in range(0, len(candidates), 8):
            batch = candidates[offset : offset + 8]
            images = []
            try:
                for _timestamp_ms, frame_path in batch:
                    image = Image.open(frame_path)
                    images.append(image.convert("RGB"))
                image_inputs = image_processor(images=images, return_tensors="pt")
                with torch.inference_mode():
                    image_output = model.get_image_features(**image_inputs)
                    image_features = getattr(image_output, "pooler_output", image_output)
                    image_features = image_features / image_features.norm(dim=-1, keepdim=True)
                    scores = (image_features @ text_features.T).squeeze(-1).tolist()
                if not isinstance(scores, list):
                    scores = [scores]
                for (timestamp_ms, _frame_path), score in zip(batch, scores, strict=True):
                    ranked.append(
                        {"timestamp_ms": timestamp_ms, "similarity": round(float(score), 6)}
                    )
            finally:
                for image in images:
                    image.close()
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
        raise VideoSemanticUnavailable("the local CLIP inference failed") from error
    ranked.sort(
        key=lambda item: (
            -cast(float, item["similarity"]),
            cast(int, item["timestamp_ms"]),
        )
    )
    return ranked, model_record


__all__ = [
    "VIDEO_SEMANTIC_MODEL_ID",
    "VIDEO_SEMANTIC_MODEL_REVISION",
    "VIDEO_SEMANTIC_POLICY_ID",
    "VIDEO_SEMANTIC_POLICY_SHA256",
    "VideoSemanticUnavailable",
    "rank_video_frames",
    "resolve_local_video_semantic_model",
    "video_semantic_runtime_capabilities",
]
