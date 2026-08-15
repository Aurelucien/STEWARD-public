"""Optional local CTC forced alignment for operation-scoped audio graphs."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
from typing import Any, Sequence
import wave

from ...evidence import canonical_json


ALIGNMENT_BACKEND = "transformers-ctc-forced-alignment"
ALIGNMENT_MODEL_BY_LANGUAGE = {
    "en": (
        "facebook/wav2vec2-base-960h",
        "22aad52d435eb6dbaf354bdad9b0da84ce7d6156",
    ),
    "ja": (
        "jonatasgrosman/wav2vec2-large-xlsr-53-japanese",
        "cf031e020336460d15a417eba710bbc5bb43be9a",
    ),
    "zh": (
        "jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn",
        "99ccb2737be22b8bb50dcfcc39ad4d567fb90cfd",
    ),
}
ALIGNMENT_LANGUAGE_ALIASES = {
    "cmn": "zh",
    "en": "en",
    "eng": "en",
    "ja": "ja",
    "jpn": "ja",
    "zh": "zh",
    "zho": "zh",
}
ALIGNMENT_LICENSE_BY_LANGUAGE = {"en": "Apache-2.0", "ja": "Apache-2.0", "zh": "Apache-2.0"}
ALIGNMENT_TEXT_NORMALIZER_BY_LANGUAGE = {
    "en": "UNICODE_IDENTITY_PLUS_UPPERCASE_V1",
    "ja": "UNICODE_IDENTITY_V1",
    "zh": "OPENCC_T2S_0.1.7_V1",
}
ALIGNMENT_EVALUATION_BY_LANGUAGE = {
    "en": {
        "state": "SUPPORTED_AND_EVALUATED",
        "scope": "PUBLIC_REAL_AUDIO_ASSERTIONS",
        "evidence_id": "openai-whisper-jfk",
    },
    "ja": {
        "state": "SUPPORTED_AND_EVALUATED",
        "scope": "SYNTHETIC_TOKEN_SLOT_BOUNDARIES",
        "evidence_id": "generated-token-slots-ja-v1",
    },
    "zh": {
        "state": "SUPPORTED_AND_EVALUATED",
        "scope": "SYNTHETIC_TOKEN_SLOT_BOUNDARIES",
        "evidence_id": "generated-token-slots-zh-v1",
    },
}
MAX_ALIGNMENT_WORDS = 10_000


class AudioAlignmentUnavailable(RuntimeError):
    """The requested local aligner or language model is unavailable."""


@dataclass(frozen=True, slots=True)
class AlignmentModel:
    language: str
    model_id: str
    revision: str
    path: Path
    identity_sha256: str


@dataclass(frozen=True, slots=True)
class _TokenOwner:
    word_index: int
    token_id: int


def _language_root(language: str | None) -> str | None:
    if not isinstance(language, str) or not language:
        return None
    root = language.lower().split("-", 1)[0]
    normalized = ALIGNMENT_LANGUAGE_ALIASES.get(root)
    return normalized if normalized in ALIGNMENT_MODEL_BY_LANGUAGE else None


def _cache_root() -> Path:
    return Path(os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface")))


@lru_cache(maxsize=len(ALIGNMENT_MODEL_BY_LANGUAGE))
def resolve_local_alignment_model(language: str | None) -> AlignmentModel:
    """Resolve an exact already-local alignment model without network access."""
    normalized = _language_root(language)
    if normalized is None:
        raise AudioAlignmentUnavailable("no governed aligner exists for the detected language")
    model_id, revision = ALIGNMENT_MODEL_BY_LANGUAGE[normalized]
    if normalized == "zh":
        try:
            opencc_version = version("opencc-python-reimplemented")
        except PackageNotFoundError as error:
            raise AudioAlignmentUnavailable(
                "the governed Chinese alignment normalizer is unavailable"
            ) from error
        if opencc_version != "0.1.7":
            raise AudioAlignmentUnavailable(
                "the governed Chinese alignment normalizer version is unavailable"
            )
    path = _cache_root() / "hub" / f"models--{model_id.replace('/', '--')}" / "snapshots" / revision
    required = (path / "config.json", path / "preprocessor_config.json")
    model_files = (path / "model.safetensors", path / "pytorch_model.bin")
    if (
        not path.is_dir()
        or not all(item.is_file() for item in required)
        or not any(item.is_file() for item in model_files)
    ):
        raise AudioAlignmentUnavailable(
            f"the pinned local {normalized} alignment model is unavailable"
        )
    manifest: list[dict[str, object]] = []
    for item in sorted(path.iterdir(), key=lambda value: value.name):
        if not item.is_file():
            continue
        digest = sha256()
        with item.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        manifest.append(
            {"name": item.name, "bytes": item.stat().st_size, "sha256": digest.hexdigest()}
        )
    identity = sha256(
        canonical_json(
            {
                "backend": ALIGNMENT_BACKEND,
                "language": normalized,
                "model_id": model_id,
                "revision": revision,
                "text_normalizer": ALIGNMENT_TEXT_NORMALIZER_BY_LANGUAGE[normalized],
                "manifest": manifest,
            }
        )
    ).hexdigest()
    return AlignmentModel(normalized, model_id, revision, path, identity)


def alignment_runtime_capabilities() -> dict[str, object]:
    models: list[dict[str, object]] = []
    for language, (model_id, revision) in sorted(ALIGNMENT_MODEL_BY_LANGUAGE.items()):
        installed = False
        identity: str | None = None
        try:
            model = resolve_local_alignment_model(language)
            installed = True
            identity = model.identity_sha256
        except AudioAlignmentUnavailable:
            pass
        evaluation_state = (
            ALIGNMENT_EVALUATION_BY_LANGUAGE[language]["state"] if installed else "UNSUPPORTED"
        )
        models.append(
            {
                "language": language,
                "model_id": model_id,
                "revision": revision,
                "installed": installed,
                "model_identity_sha256": identity,
                "license": ALIGNMENT_LICENSE_BY_LANGUAGE[language],
                "evaluation_state": evaluation_state,
                "evaluation_scope": ALIGNMENT_EVALUATION_BY_LANGUAGE[language]["scope"],
                "evaluation_evidence_id": ALIGNMENT_EVALUATION_BY_LANGUAGE[language]["evidence_id"],
                "text_normalizer": ALIGNMENT_TEXT_NORMALIZER_BY_LANGUAGE[language],
            }
        )
    return {
        "backend": ALIGNMENT_BACKEND,
        "supported_languages": sorted(ALIGNMENT_MODEL_BY_LANGUAGE),
        "language_aliases": dict(sorted(ALIGNMENT_LANGUAGE_ALIASES.items())),
        "evaluation_states": [
            "SUPPORTED_AND_EVALUATED",
            "INSTALLED_NOT_EVALUATED",
            "UNSUPPORTED",
        ],
        "unlisted_language_state": "UNSUPPORTED",
        "models": models,
        "runtime_downloads_allowed": False,
        "persistence_effect": "NONE",
        "word_limit": MAX_ALIGNMENT_WORDS,
    }


def _read_pcm(wav_path: Path) -> tuple[Any, int]:
    try:
        np = import_module("numpy")
    except ImportError as error:
        raise AudioAlignmentUnavailable("numpy is unavailable") from error
    with wave.open(str(wav_path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise AudioAlignmentUnavailable("decoded alignment PCM is not mono signed-16")
        sample_rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    return samples.astype("float32") / 32768.0, sample_rate


def _token_owners(
    tokenizer: Any,
    words: Sequence[dict[str, object]],
    *,
    language: str,
) -> list[_TokenOwner]:
    owners: list[_TokenOwner] = []
    unk = getattr(tokenizer, "unk_token_id", None)
    pad = getattr(tokenizer, "pad_token_id", None)
    for word_index, word in enumerate(words):
        text = word.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        normalized = _normalize_alignment_text(text.strip(), language=language)
        encoded = tokenizer(normalized, add_special_tokens=False)
        token_ids = getattr(encoded, "input_ids", None)
        if token_ids is None and isinstance(encoded, dict):
            token_ids = encoded.get("input_ids")
        if not isinstance(token_ids, list) or not token_ids:
            continue
        cleaned = [
            token for token in token_ids if type(token) is int and token != unk and token != pad
        ]
        owners.extend(_TokenOwner(word_index, token) for token in cleaned)
    return owners


@lru_cache(maxsize=1)
def _opencc_t2s() -> Any:
    try:
        opencc = import_module("opencc")
    except ImportError as error:
        raise AudioAlignmentUnavailable(
            "the governed Chinese alignment normalizer is unavailable"
        ) from error
    return opencc.OpenCC("t2s")


def _normalize_alignment_text(text: str, *, language: str) -> str:
    if language == "en":
        return text.upper()
    if language == "zh":
        converted = _opencc_t2s().convert(text)
        if not isinstance(converted, str):
            raise AudioAlignmentUnavailable("Chinese alignment normalization failed")
        return converted
    return text


@lru_cache(maxsize=len(ALIGNMENT_MODEL_BY_LANGUAGE))
def _load_alignment_runtime(model_path: str) -> tuple[Any, Any]:
    try:
        transformers = import_module("transformers")
    except ImportError as error:
        raise AudioAlignmentUnavailable("the local CTC alignment runtime is unavailable") from error
    processor = transformers.Wav2Vec2Processor.from_pretrained(model_path, local_files_only=True)
    acoustic_model = transformers.Wav2Vec2ForCTC.from_pretrained(model_path, local_files_only=True)
    acoustic_model.eval()
    return processor, acoustic_model


def ctc_token_frames(
    emission: Any, token_ids: Sequence[int], blank_id: int
) -> list[tuple[int, float]]:
    """Return one emission frame and score per token using exact CTC backtracking."""
    torch = import_module("torch")

    if emission.ndim != 2 or not token_ids or emission.shape[0] < len(token_ids):
        raise AudioAlignmentUnavailable("CTC emissions cannot cover the requested tokens")
    tokens = torch.tensor(list(token_ids), dtype=torch.long, device=emission.device)
    time_steps = int(emission.shape[0])
    token_count = len(token_ids)
    trellis = torch.full(
        (time_steps + 1, token_count + 1),
        -float("inf"),
        dtype=emission.dtype,
        device=emission.device,
    )
    trellis[:, 0] = 0.0
    for time_index in range(time_steps):
        stay = trellis[time_index, 1:] + emission[time_index, blank_id]
        change = trellis[time_index, :-1] + emission[time_index, tokens]
        trellis[time_index + 1, 1:] = torch.maximum(stay, change)
    time_index = int(torch.argmax(trellis[:, token_count]).item())
    token_index = token_count
    frames: list[tuple[int, float]] = []
    while token_index > 0 and time_index > 0:
        stayed = trellis[time_index - 1, token_index] + emission[time_index - 1, blank_id]
        changed = (
            trellis[time_index - 1, token_index - 1]
            + emission[time_index - 1, token_ids[token_index - 1]]
        )
        if changed > stayed:
            score = float(emission[time_index - 1, token_ids[token_index - 1]].exp().item())
            frames.append((time_index - 1, score))
            token_index -= 1
        time_index -= 1
    if token_index != 0:
        raise AudioAlignmentUnavailable("CTC backtracking did not align every token")
    frames.reverse()
    return frames


def _align_segment(
    samples: Any,
    sample_rate: int,
    words: list[dict[str, object]],
    *,
    model: AlignmentModel,
    segment_start_ms: int,
    segment_end_ms: int,
    window_start_ms: int,
) -> list[dict[str, object]]:
    torch = import_module("torch")

    processor, acoustic_model = _load_alignment_runtime(str(model.path))
    relative_start = max(0, segment_start_ms - window_start_ms)
    relative_end = max(relative_start + 1, segment_end_ms - window_start_ms)
    start_sample = min(len(samples), round(relative_start * sample_rate / 1000))
    end_sample = min(len(samples), round(relative_end * sample_rate / 1000))
    segment_samples = samples[start_sample:end_sample]
    if not len(segment_samples):
        return words
    owners = _token_owners(
        processor.tokenizer,
        words,
        language=model.language,
    )
    if not owners:
        return words
    inputs = processor(segment_samples, sampling_rate=sample_rate, return_tensors="pt")
    with torch.inference_mode():
        logits = acoustic_model(**inputs).logits[0]
    emission = torch.log_softmax(logits, dim=-1)
    blank_id = acoustic_model.config.pad_token_id
    if not isinstance(blank_id, int):
        blank_id = processor.tokenizer.pad_token_id
    if not isinstance(blank_id, int):
        raise AudioAlignmentUnavailable("the CTC blank token is undefined")
    frames = ctc_token_frames(emission, [owner.token_id for owner in owners], blank_id)
    grouped: dict[int, list[tuple[int, float]]] = {}
    for owner, frame in zip(owners, frames, strict=True):
        grouped.setdefault(owner.word_index, []).append(frame)
    frame_ms = (segment_end_ms - segment_start_ms) / max(1, int(emission.shape[0]))
    aligned: list[dict[str, object]] = []
    for word_index, word in enumerate(words):
        value = dict(word)
        word_frames = grouped.get(word_index)
        if word_frames:
            start = segment_start_ms + round(word_frames[0][0] * frame_ms)
            end = segment_start_ms + round((word_frames[-1][0] + 1) * frame_ms)
            value.update(
                {
                    "start_ms": max(segment_start_ms, start),
                    "end_ms": min(segment_end_ms, max(start + 1, end)),
                    "probability": sum(score for _frame, score in word_frames) / len(word_frames),
                    "alignment_status": "CTC_FORCED_ALIGNED",
                    "timestamp_accuracy": "MODEL_ALIGNED",
                    "alignment_backend": ALIGNMENT_BACKEND,
                    "alignment_model_id": model.model_id,
                    "alignment_model_revision": model.revision,
                    "alignment_model_identity_sha256": model.identity_sha256,
                }
            )
        else:
            value.update(
                {
                    "start_ms": None,
                    "end_ms": None,
                    "alignment_status": "UNALIGNED",
                    "timestamp_accuracy": "UNALIGNED",
                    "alignment_backend": ALIGNMENT_BACKEND,
                    "alignment_model_id": model.model_id,
                    "alignment_model_revision": model.revision,
                    "alignment_model_identity_sha256": model.identity_sha256,
                }
            )
        aligned.append(value)
    return aligned


def align_transcript_words(
    wav_path: Path,
    segments: list[dict[str, object]],
    *,
    language: str | None,
    window_start_ms: int,
) -> tuple[list[dict[str, object]], AlignmentModel, int, int]:
    """Align ASR words while preserving every original word string unchanged."""
    model = resolve_local_alignment_model(language)
    samples, sample_rate = _read_pcm(wav_path)
    output: list[dict[str, object]] = []
    total_words = 0
    aligned_words = 0
    for segment in segments:
        copied = dict(segment)
        words_value = segment.get("words")
        location = segment.get("location")
        if not isinstance(words_value, list) or not isinstance(location, dict):
            output.append(copied)
            continue
        words = [dict(word) for word in words_value if isinstance(word, dict)]
        if total_words + len(words) > MAX_ALIGNMENT_WORDS:
            raise AudioAlignmentUnavailable("the bounded alignment word limit was exceeded")
        total_words += len(words)
        start_ms = location.get("start_ms")
        end_ms = location.get("end_ms")
        if type(start_ms) is not int or type(end_ms) is not int or end_ms <= start_ms:
            copied["words"] = words
            output.append(copied)
            continue
        try:
            aligned = _align_segment(
                samples,
                sample_rate,
                words,
                model=model,
                segment_start_ms=start_ms,
                segment_end_ms=end_ms,
                window_start_ms=window_start_ms,
            )
        except AudioAlignmentUnavailable as error:
            aligned = [
                {
                    **word,
                    "start_ms": None,
                    "end_ms": None,
                    "alignment_status": "UNALIGNED",
                    "timestamp_accuracy": "UNALIGNED",
                    "alignment_backend": ALIGNMENT_BACKEND,
                    "alignment_model_id": model.model_id,
                    "alignment_model_revision": model.revision,
                    "alignment_model_identity_sha256": model.identity_sha256,
                    "alignment_reason": str(error),
                }
                for word in words
            ]
        copied["words"] = aligned
        aligned_words += sum(
            word.get("alignment_status") == "CTC_FORCED_ALIGNED" for word in aligned
        )
        output.append(copied)
    return output, model, aligned_words, total_words - aligned_words


__all__ = [
    "ALIGNMENT_BACKEND",
    "ALIGNMENT_MODEL_BY_LANGUAGE",
    "AudioAlignmentUnavailable",
    "AlignmentModel",
    "align_transcript_words",
    "alignment_runtime_capabilities",
    "ctc_token_frames",
    "resolve_local_alignment_model",
]
