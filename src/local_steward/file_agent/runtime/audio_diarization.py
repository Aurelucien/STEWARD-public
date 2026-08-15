"""Optional local anonymous speaker diarization for operation-scoped audio graphs."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
import os
from pathlib import Path
from statistics import median
from typing import Any, Iterable
import wave

from ...evidence import canonical_json


DIARIZATION_BACKEND = "sherpa-onnx-offline-speaker-diarization"
DIARIZATION_RUNTIME_VERSION = "1.13.5"
SEGMENTATION_MODEL_ID = "pyannote/segmentation-3.0-int8-sherpa-onnx"
SEGMENTATION_MODEL_SHA256 = "d582f4b4c6b48205de7e0643c57df0df5615a3c176189be3fc461e9d18827b5d"
EMBEDDING_MODEL_ID = "nvidia/nemo-titanet-small-sherpa-onnx"
EMBEDDING_MODEL_SHA256 = "ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"
CLUSTER_THRESHOLD = 0.9
MIN_DURATION_ON = 0.3
MIN_DURATION_OFF = 0.5
MAX_SPEAKERS = 32
MAX_SPEAKER_TURNS = 4_000
DIARIZATION_POLICY_ID = "STEWARD_SHERPA_ANONYMOUS_DIARIZATION_V1"
DIARIZATION_POLICY_SHA256 = sha256(
    canonical_json(
        {
            "cluster_threshold": CLUSTER_THRESHOLD,
            "max_speaker_turns": MAX_SPEAKER_TURNS,
            "max_speakers": MAX_SPEAKERS,
            "min_duration_off": MIN_DURATION_OFF,
            "min_duration_on": MIN_DURATION_ON,
            "overlap_method": "INTERSECTING_DIARIZATION_TURNS",
            "speaker_labels": "ANONYMOUS_FILE_LOCAL_FIRST_APPEARANCE",
        }
    )
).hexdigest()


class AudioDiarizationUnavailable(RuntimeError):
    """The governed local diarization runtime or model is unavailable."""


@dataclass(frozen=True, slots=True)
class DiarizationModel:
    segmentation_path: Path
    embedding_path: Path
    identity_sha256: str


def _model_root() -> Path:
    configured = os.environ.get("STEWARD_AUDIO_MODEL_HOME")
    if configured:
        return Path(configured) / "diarization-v1"
    return Path.home() / ".cache" / "steward" / "audio" / "diarization-v1"


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def resolve_local_diarization_model() -> DiarizationModel:
    root = _model_root()
    segmentation = root / "pyannote-segmentation-3.0.int8.onnx"
    embedding = root / "nemo-titanet-small.onnx"
    try:
        runtime_version = version("sherpa-onnx")
    except PackageNotFoundError as error:
        raise AudioDiarizationUnavailable("sherpa-onnx is unavailable") from error
    if runtime_version != DIARIZATION_RUNTIME_VERSION:
        raise AudioDiarizationUnavailable("the governed sherpa-onnx version is unavailable")
    if not segmentation.is_file() or not embedding.is_file():
        raise AudioDiarizationUnavailable("the pinned local diarization models are unavailable")
    segmentation_digest = _file_sha256(segmentation)
    embedding_digest = _file_sha256(embedding)
    if (
        segmentation_digest != SEGMENTATION_MODEL_SHA256
        or embedding_digest != EMBEDDING_MODEL_SHA256
    ):
        raise AudioDiarizationUnavailable("the local diarization model identity is invalid")
    identity = sha256(
        canonical_json(
            {
                "backend": DIARIZATION_BACKEND,
                "runtime_version": runtime_version,
                "segmentation_model_id": SEGMENTATION_MODEL_ID,
                "segmentation_sha256": segmentation_digest,
                "embedding_model_id": EMBEDDING_MODEL_ID,
                "embedding_sha256": embedding_digest,
                "cluster_threshold": CLUSTER_THRESHOLD,
                "min_duration_on": MIN_DURATION_ON,
                "min_duration_off": MIN_DURATION_OFF,
            }
        )
    ).hexdigest()
    return DiarizationModel(segmentation, embedding, identity)


def diarization_runtime_capabilities() -> dict[str, object]:
    installed = False
    identity: str | None = None
    try:
        model = resolve_local_diarization_model()
        installed = True
        identity = model.identity_sha256
    except AudioDiarizationUnavailable:
        pass
    return {
        "backend": DIARIZATION_BACKEND,
        "runtime_version": DIARIZATION_RUNTIME_VERSION,
        "installed": installed,
        "model_identity_sha256": identity,
        "segmentation_model_id": SEGMENTATION_MODEL_ID,
        "segmentation_model_sha256": SEGMENTATION_MODEL_SHA256,
        "embedding_model_id": EMBEDDING_MODEL_ID,
        "embedding_model_sha256": EMBEDDING_MODEL_SHA256,
        "speaker_labels": "ANONYMOUS_FILE_LOCAL",
        "speaker_limit": MAX_SPEAKERS,
        "turn_limit": MAX_SPEAKER_TURNS,
        "overlap_method": "INTERSECTING_DIARIZATION_TURNS",
        "policy_id": DIARIZATION_POLICY_ID,
        "policy_sha256": DIARIZATION_POLICY_SHA256,
        "evaluation_state": "SUPPORTED_AND_SYNTHETICALLY_EVALUATED",
        "evaluation_evidence": "next-023d-two-source-mixtures-v1",
        "evaluation_scope": (
            "VAD_REFERENCED_TWO_SOURCE_MIXTURES_AND_OFFICIAL_SPEAKER_COUNT"
        ),
        "runtime_downloads_allowed": False,
        "persistence_effect": "NONE",
    }


def _read_pcm(wav_path: Path) -> tuple[Any, int]:
    np = import_module("numpy")
    with wave.open(str(wav_path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise AudioDiarizationUnavailable("decoded diarization PCM is not mono signed-16")
        sample_rate = source.getframerate()
        samples = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2")
    return samples.astype("float32") / 32768.0, sample_rate


def derive_overlap_regions(turns: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    values = list(turns)
    boundaries = sorted(
        {
            point
            for turn in values
            for key in ("start_ms", "end_ms")
            if type(point := turn.get(key)) is int
        }
    )
    regions: list[dict[str, object]] = []
    for start, end in zip(boundaries, boundaries[1:], strict=False):
        if end <= start:
            continue
        active_values: set[str] = set()
        for turn in values:
            turn_start = turn.get("start_ms")
            turn_end = turn.get("end_ms")
            label = turn.get("speaker_label")
            if (
                type(turn_start) is int
                and type(turn_end) is int
                and turn_start < end
                and turn_end > start
                and isinstance(label, str)
            ):
                active_values.add(label)
        active = sorted(active_values)
        if len(active) < 2:
            continue
        if regions and regions[-1]["end_ms"] == start and regions[-1]["speakers"] == active:
            regions[-1]["end_ms"] = end
        else:
            regions.append({"start_ms": start, "end_ms": end, "speakers": active})
    return regions


def _union_duration(ranges: Iterable[tuple[int, int]]) -> int:
    total = 0
    current_start: int | None = None
    current_end: int | None = None
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if current_start is None:
            current_start, current_end = start, end
        elif current_end is not None and start <= current_end:
            current_end = max(current_end, end)
        else:
            assert current_end is not None
            total += current_end - current_start
            current_start, current_end = start, end
    if current_start is not None and current_end is not None:
        total += current_end - current_start
    return total


def diarization_observation_quality(
    turns: Iterable[dict[str, object]],
    overlap_regions: Iterable[dict[str, object]],
    *,
    window_start_ms: int,
    window_end_ms: int,
) -> dict[str, object]:
    """Describe bounded output coverage without claiming reference accuracy."""
    turn_values = list(turns)
    overlap_values = list(overlap_regions)
    window_ms = max(0, window_end_ms - window_start_ms)
    turn_ranges = [
        (max(window_start_ms, start), min(window_end_ms, end))
        for turn in turn_values
        if type(start := turn.get("start_ms")) is int
        and type(end := turn.get("end_ms")) is int
    ]
    overlap_ranges = [
        (max(window_start_ms, start), min(window_end_ms, end))
        for region in overlap_values
        if type(start := region.get("start_ms")) is int
        and type(end := region.get("end_ms")) is int
    ]
    durations = [end - start for start, end in turn_ranges if end > start]
    covered_ms = _union_duration(turn_ranges)
    observation: dict[str, object] = {
        "observation_status": "COMPLETE" if turn_values else "EMPTY",
        "reference_accuracy_status": "REFERENCE_UNAVAILABLE",
        "turn_coverage_ms": covered_ms,
        "turn_coverage_ratio": covered_ms / window_ms if window_ms else 0.0,
        "overlap_ms": _union_duration(overlap_ranges),
        "median_turn_duration_ms": median(durations) if durations else None,
        "short_turn_count": sum(duration < 300 for duration in durations),
        "policy_sha256": DIARIZATION_POLICY_SHA256,
    }
    observation["observation_digest"] = sha256(
        canonical_json(
            {
                "overlap_regions": overlap_values,
                "policy_sha256": DIARIZATION_POLICY_SHA256,
                "turns": turn_values,
            }
        )
    ).hexdigest()
    return observation


def diarize_audio(
    wav_path: Path,
    *,
    window_start_ms: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]], DiarizationModel]:
    """Return bounded anonymous turns and overlap diagnostics in source-global time."""
    model = resolve_local_diarization_model()
    try:
        sherpa = import_module("sherpa_onnx")
    except ImportError as error:
        raise AudioDiarizationUnavailable("sherpa-onnx is unavailable") from error
    samples, sample_rate = _read_pcm(wav_path)
    pyannote = sherpa.OfflineSpeakerSegmentationPyannoteModelConfig()
    pyannote.model = str(model.segmentation_path)
    segmentation = sherpa.OfflineSpeakerSegmentationModelConfig()
    segmentation.pyannote = pyannote
    segmentation.num_threads = 2
    segmentation.provider = "cpu"
    embedding = sherpa.SpeakerEmbeddingExtractorConfig()
    embedding.model = str(model.embedding_path)
    embedding.num_threads = 2
    embedding.provider = "cpu"
    clustering = sherpa.FastClusteringConfig()
    clustering.num_clusters = -1
    clustering.threshold = CLUSTER_THRESHOLD
    config = sherpa.OfflineSpeakerDiarizationConfig(
        segmentation,
        embedding,
        clustering,
        MIN_DURATION_ON,
        MIN_DURATION_OFF,
    )
    if not config.validate():
        raise AudioDiarizationUnavailable("the local diarization configuration is invalid")
    diarizer = sherpa.OfflineSpeakerDiarization(config)
    if diarizer.sample_rate != sample_rate:
        raise AudioDiarizationUnavailable("the diarization sample rate is incompatible")
    result = diarizer.process(samples)
    if result.num_speakers > MAX_SPEAKERS or result.num_segments > MAX_SPEAKER_TURNS:
        raise AudioDiarizationUnavailable("the bounded diarization result limit was exceeded")
    raw_segments = list(result.sort_by_start_time())
    labels: dict[int, str] = {}
    turns: list[dict[str, object]] = []
    for segment in raw_segments:
        raw_speaker = int(segment.speaker)
        label = labels.setdefault(raw_speaker, f"SPEAKER_{len(labels):02d}")
        start_ms = window_start_ms + round(float(segment.start) * 1000)
        end_ms = window_start_ms + round(float(segment.end) * 1000)
        if end_ms <= start_ms:
            continue
        turns.append(
            {
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker_label": label,
            }
        )
    return turns, derive_overlap_regions(turns), model


__all__ = [
    "DIARIZATION_BACKEND",
    "DIARIZATION_POLICY_ID",
    "DIARIZATION_POLICY_SHA256",
    "AudioDiarizationUnavailable",
    "DiarizationModel",
    "derive_overlap_regions",
    "diarization_observation_quality",
    "diarization_runtime_capabilities",
    "diarize_audio",
    "resolve_local_diarization_model",
]
