"""Bounded local audio probe, VAD and ASR worker for current-file inspection."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Any

from ...evidence import canonical_json
from .audio_alignment import (
    AudioAlignmentUnavailable,
    align_transcript_words,
    alignment_runtime_capabilities,
)
from .audio_graph import build_audio_document_graph
from .audio_diarization import (
    AudioDiarizationUnavailable,
    diarization_runtime_capabilities,
    diarize_audio,
    diarization_observation_quality,
)


AUDIO_SOURCE_FORMATS = frozenset({"WAV", "FLAC", "MP3", "M4A", "AAC", "OGG", "OPUS"})
AUDIO_SUFFIX_BY_FORMAT = {
    "WAV": ".wav",
    "FLAC": ".flac",
    "MP3": ".mp3",
    "M4A": ".m4a",
    "AAC": ".aac",
    "OGG": ".ogg",
    "OPUS": ".opus",
}
AUDIO_FORMAT_BY_SUFFIX = {value: key for key, value in AUDIO_SUFFIX_BY_FORMAT.items()}
_AUDIO_CONTAINER_RULES: dict[str, tuple[set[str], set[str]]] = {
    "WAV": ({"wav"}, {"pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le", "pcm_f64le"}),
    "FLAC": ({"flac"}, {"flac"}),
    "MP3": ({"mp3"}, {"mp3"}),
    "M4A": ({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}, {"aac", "alac"}),
    "AAC": ({"aac"}, {"aac"}),
    "OGG": ({"ogg"}, {"vorbis"}),
    "OPUS": ({"ogg"}, {"opus"}),
}
MAX_AUDIO_SOURCE_BYTES = 1024 * 1024 * 1024
MAX_AUDIO_WINDOW_MS = 15 * 60 * 1000
MAX_AUDIO_SPEECH_MS = 12 * 60 * 1000
MAX_AUDIO_DECODED_PCM_BYTES = 32 * 1024 * 1024
MAX_AUDIO_SEGMENTS = 4_000
MAX_AUDIO_WORDS = 20_000
MAX_AUDIO_PROBE_OUTPUT_BYTES = 1024 * 1024
MAX_AUDIO_PROCESS_STDERR_BYTES = 64 * 1024
AUDIO_SAMPLE_RATE = 16_000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_WIDTH_BYTES = 2
DEFAULT_AUDIO_MODEL_ID = "Systran/faster-whisper-base"
DEFAULT_AUDIO_MODEL_REVISION = "ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
AUDIO_DECODING_POLICY_ID = "STEWARD_FASTER_WHISPER_DETERMINISTIC_V1"
AUDIO_DECODER_CPU_THREADS = 1
AUDIO_DECODER_NUM_WORKERS = 1
AUDIO_DECODER_BEAM_SIZE = 5
AUDIO_DECODER_TEMPERATURE = 0.0
AUDIO_DECODING_POLICY = {
    "task": "transcribe",
    "device": "cpu",
    "compute_type": "int8",
    "cpu_threads": AUDIO_DECODER_CPU_THREADS,
    "num_workers": AUDIO_DECODER_NUM_WORKERS,
    "word_timestamps": True,
    "condition_on_previous_text": False,
    "beam_size": AUDIO_DECODER_BEAM_SIZE,
    "temperature": AUDIO_DECODER_TEMPERATURE,
    "vad_filter": False,
}
AUDIO_DECODING_POLICY_SHA256 = sha256(canonical_json(AUDIO_DECODING_POLICY)).hexdigest()
AUDIO_CONTINUATION_DOMAIN = "local_steward.audio_continuation.v1"
AUDIO_CONTINUATION_SCHEMA_NAME = "local_steward.audio_continuation"
AUDIO_TIME_CONTINUATION_SCHEMA_VERSION = 1
AUDIO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION = 2
AUDIO_ANALYSIS_MODES = frozenset(
    {"TRANSCRIPT", "ALIGNED_WORDS", "SPEAKER_TURNS", "ALIGNED_WORDS_AND_SPEAKERS"}
)


class AudioRuntimeUnavailable(RuntimeError):
    """The local audio runtime is not installed or its model is not local."""


class AudioSourceInvalid(OSError):
    """The admitted source is not one supported, usable audio stream."""


def _source_sha256(source_path: str) -> str:
    digest = sha256()
    with Path(source_path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _package_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _default_model_path() -> Path:
    cache_root = Path(
        os.environ.get(
            "HF_HOME",
            str(Path.home() / ".cache" / "huggingface"),
        )
    )
    return (
        cache_root
        / "hub"
        / "models--Systran--faster-whisper-base"
        / "snapshots"
        / DEFAULT_AUDIO_MODEL_REVISION
    )


def resolve_local_audio_model() -> tuple[Path, str, str]:
    """Resolve one already-local pinned model without permitting a download."""
    model_path = _default_model_path()
    required = (model_path / "config.json", model_path / "model.bin")
    if not model_path.is_dir() or not all(path.is_file() for path in required):
        raise AudioRuntimeUnavailable("the pinned local faster-whisper model is unavailable")
    revision = (
        model_path.name
        if len(model_path.name) == 40
        and all(char in "0123456789abcdef" for char in model_path.name)
        else "LOCAL_EXPLICIT"
    )
    manifest = []
    for path in sorted(model_path.iterdir(), key=lambda item: item.name):
        if not path.is_file():
            continue
        digest = sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        manifest.append(
            {"name": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}
        )
    identity = {
        "model_id": DEFAULT_AUDIO_MODEL_ID,
        "revision": revision,
        "manifest": manifest,
    }
    return model_path, revision, sha256(canonical_json(identity)).hexdigest()


def audio_runtime_capabilities() -> dict[str, object]:
    """Publish path-free audio runtime readiness."""
    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    faster_whisper_version = _package_version("faster-whisper")
    ctranslate2_version = _package_version("ctranslate2")
    onnxruntime_version = _package_version("onnxruntime")
    model_revision: str | None = None
    model_identity_sha256: str | None = None
    try:
        _path, model_revision, model_identity_sha256 = resolve_local_audio_model()
    except AudioRuntimeUnavailable:
        pass
    return {
        "schema_name": "local_steward.audio_runtime_capabilities",
        "schema_version": 1,
        "supported_formats": sorted(AUDIO_SOURCE_FORMATS),
        "probe_ready": ffprobe is not None and ffmpeg is not None,
        "vad_ready": faster_whisper_version is not None and onnxruntime_version is not None,
        "asr_ready": (
            faster_whisper_version is not None
            and ctranslate2_version is not None
            and model_revision is not None
        ),
        "ffmpeg_available": ffmpeg is not None,
        "ffprobe_available": ffprobe is not None,
        "faster_whisper_version": faster_whisper_version,
        "ctranslate2_version": ctranslate2_version,
        "onnxruntime_version": onnxruntime_version,
        "silero_vad_source": "faster-whisper-bundled-onnx-v6",
        "model_id": DEFAULT_AUDIO_MODEL_ID,
        "model_revision": model_revision,
        "model_identity_sha256": model_identity_sha256,
        "decoding_policy_id": AUDIO_DECODING_POLICY_ID,
        "decoding_policy_sha256": AUDIO_DECODING_POLICY_SHA256,
        "decoding_policy": dict(AUDIO_DECODING_POLICY),
        "alignment": alignment_runtime_capabilities(),
        "diarization": diarization_runtime_capabilities(),
        "runtime_downloads_allowed": False,
        "persistence_effect": "NONE",
        "window_limit_ms": MAX_AUDIO_WINDOW_MS,
        "speech_limit_ms": MAX_AUDIO_SPEECH_MS,
        "decoded_pcm_limit_bytes": MAX_AUDIO_DECODED_PCM_BYTES,
    }


def audio_request_digest(
    *,
    source_sha256: str,
    scope_id: str,
    relative_path: str,
    intent: str,
    content_query: str | None,
    language: str | None,
    model_identity_sha256: str | None,
    analysis: str = "TRANSCRIPT",
    alignment_model_identity_sha256: str | None = None,
    diarization_model_identity_sha256: str | None = None,
    decoding_policy_sha256: str = AUDIO_DECODING_POLICY_SHA256,
) -> str:
    payload = {
        "source_sha256": source_sha256,
        "scope_id": scope_id,
        "relative_path": relative_path,
        "intent": intent,
        "content_query": content_query,
        "language": language,
        "model_identity_sha256": model_identity_sha256,
        "analysis": analysis,
        "alignment_model_identity_sha256": alignment_model_identity_sha256,
        "diarization_model_identity_sha256": diarization_model_identity_sha256,
        "decoding_policy_sha256": decoding_policy_sha256,
    }
    return sha256(AUDIO_CONTINUATION_DOMAIN.encode() + b"\0" + canonical_json(payload)).hexdigest()


def _run_bounded(command: list[str], *, timeout: float, stdout_limit: int) -> bytes:
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise AudioRuntimeUnavailable("the bounded media subprocess is unavailable") from error
    if (
        len(completed.stdout) > stdout_limit
        or len(completed.stderr) > MAX_AUDIO_PROCESS_STDERR_BYTES
    ):
        raise AudioSourceInvalid("media subprocess output exceeded its bound")
    if completed.returncode != 0:
        raise AudioSourceInvalid("media subprocess rejected the admitted source")
    return completed.stdout


def probe_audio(source_path: str) -> dict[str, object]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise AudioRuntimeUnavailable("ffprobe is unavailable")
    raw = _run_bounded(
        [
            ffprobe,
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-probesize",
            str(32 * 1024 * 1024),
            "-analyzeduration",
            "30000000",
            "-select_streams",
            "a",
            "-show_entries",
            "format=format_name,duration,size,bit_rate:stream=index,codec_name,codec_type,sample_rate,channels,channel_layout,duration,bit_rate",
            "-of",
            "json",
            source_path,
        ],
        timeout=15.0,
        stdout_limit=MAX_AUDIO_PROBE_OUTPUT_BYTES,
    )
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AudioSourceInvalid("ffprobe returned invalid JSON") from error
    streams = payload.get("streams")
    media_format = payload.get("format")
    if (
        not isinstance(streams, list)
        or len(streams) != 1
        or not isinstance(streams[0], dict)
        or streams[0].get("codec_type") != "audio"
        or not isinstance(media_format, dict)
    ):
        raise AudioSourceInvalid("exactly one usable audio stream is required")
    stream = streams[0]
    duration_raw = stream.get("duration", media_format.get("duration"))
    if not isinstance(duration_raw, (str, int, float)) or isinstance(duration_raw, bool):
        raise AudioSourceInvalid("audio duration is unavailable")
    try:
        duration_ms = round(float(duration_raw) * 1000)
    except (TypeError, ValueError, OverflowError) as error:
        raise AudioSourceInvalid("audio duration is unavailable") from error
    if duration_ms <= 0:
        raise AudioSourceInvalid("audio duration must be positive")
    sample_rate = stream.get("sample_rate")
    channels = stream.get("channels")
    try:
        sample_rate_value = int(sample_rate) if sample_rate is not None else None
        channels_value = int(channels) if channels is not None else None
    except (TypeError, ValueError) as error:
        raise AudioSourceInvalid("audio stream metadata is invalid") from error
    if sample_rate_value is not None and sample_rate_value <= 0:
        raise AudioSourceInvalid("audio sample rate is invalid")
    if channels_value is not None and channels_value <= 0:
        raise AudioSourceInvalid("audio channel count is invalid")
    return {
        "duration_ms": duration_ms,
        "container": media_format.get("format_name"),
        "codec": stream.get("codec_name"),
        "sample_rate_hz": sample_rate_value,
        "channels": channels_value,
        "channel_layout": stream.get("channel_layout"),
        "bit_rate": stream.get("bit_rate", media_format.get("bit_rate")),
        "stream_index": stream.get("index"),
    }


def _validate_audio_identity(source_format: str, probe: dict[str, object]) -> None:
    allowed_containers, allowed_codecs = _AUDIO_CONTAINER_RULES[source_format]
    raw_container = probe.get("container")
    codec = probe.get("codec")
    containers = (
        {item.strip() for item in raw_container.split(",")}
        if isinstance(raw_container, str)
        else set()
    )
    if not containers.intersection(allowed_containers) or codec not in allowed_codecs:
        raise AudioSourceInvalid("audio container identity does not match the selected format")


def _decode_window(source_path: str, target_path: Path, *, start_ms: int, end_ms: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise AudioRuntimeUnavailable("ffmpeg is unavailable")
    duration_ms = end_ms - start_ms
    decoded_bytes = (
        duration_ms * AUDIO_SAMPLE_RATE * AUDIO_CHANNELS * AUDIO_SAMPLE_WIDTH_BYTES // 1000
    )
    if duration_ms <= 0 or decoded_bytes > MAX_AUDIO_DECODED_PCM_BYTES:
        raise AudioSourceInvalid("decoded audio window exceeds its bound")
    _run_bounded(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            f"{start_ms / 1000:.3f}",
            "-t",
            f"{duration_ms / 1000:.3f}",
            "-i",
            source_path,
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            str(AUDIO_CHANNELS),
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-c:a",
            "pcm_s16le",
            "-y",
            str(target_path),
        ],
        timeout=max(30.0, min(180.0, duration_ms / 1000 / 3)),
        stdout_limit=1024,
    )
    if not target_path.is_file() or target_path.stat().st_size > MAX_AUDIO_DECODED_PCM_BYTES:
        raise AudioSourceInvalid("decoded audio artifact is invalid")


def _speech_intervals(wav_path: Path) -> tuple[list[dict[str, int]], int]:
    try:
        from faster_whisper.audio import decode_audio  # type: ignore[import-untyped]
        from faster_whisper.vad import (  # type: ignore[import-untyped]
            VadOptions,
            get_speech_timestamps,
        )
    except ImportError as error:
        raise AudioRuntimeUnavailable("faster-whisper Silero VAD is unavailable") from error
    waveform = decode_audio(str(wav_path), sampling_rate=AUDIO_SAMPLE_RATE)
    raw = get_speech_timestamps(
        waveform,
        VadOptions(
            threshold=0.5,
            min_speech_duration_ms=100,
            min_silence_duration_ms=500,
            speech_pad_ms=200,
        ),
        sampling_rate=AUDIO_SAMPLE_RATE,
    )
    intervals: list[dict[str, int]] = []
    speech_ms = 0
    for item in raw:
        if not isinstance(item, dict):
            continue
        start = item.get("start")
        end = item.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or end <= start:
            continue
        start_ms = round(start * 1000 / AUDIO_SAMPLE_RATE)
        end_ms = round(end * 1000 / AUDIO_SAMPLE_RATE)
        available = MAX_AUDIO_SPEECH_MS - speech_ms
        if available <= 0:
            break
        if end_ms - start_ms > available:
            end_ms = start_ms + available
        intervals.append({"start_ms": start_ms, "end_ms": end_ms})
        speech_ms += end_ms - start_ms
        if speech_ms >= MAX_AUDIO_SPEECH_MS:
            break
    return intervals, speech_ms


def _clip_timestamps(intervals: list[dict[str, int]], window_duration_ms: int) -> list[float]:
    clips: list[float] = []
    for interval in intervals:
        start = interval["start_ms"] / 1000
        end_ms = min(interval["end_ms"], max(0, window_duration_ms - 20))
        if end_ms <= interval["start_ms"]:
            continue
        clips.extend((start, end_ms / 1000))
    return clips


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _transcribe(
    wav_path: Path,
    *,
    model_path: Path,
    language: str | None,
    intervals: list[dict[str, int]],
    window_start_ms: int,
    window_duration_ms: int,
) -> tuple[list[dict[str, object]], str | None, int]:
    try:
        from faster_whisper import WhisperModel  # type: ignore[import-untyped]
    except ImportError as error:
        raise AudioRuntimeUnavailable("faster-whisper is unavailable") from error
    clips = _clip_timestamps(intervals, window_duration_ms)
    if not clips:
        return [], language, 0
    model = WhisperModel(
        str(model_path),
        device="cpu",
        compute_type="int8",
        cpu_threads=AUDIO_DECODER_CPU_THREADS,
        num_workers=AUDIO_DECODER_NUM_WORKERS,
        local_files_only=True,
    )
    raw_segments, info = model.transcribe(
        str(wav_path),
        task="transcribe",
        word_timestamps=True,
        language=language,
        clip_timestamps=clips,
        condition_on_previous_text=False,
        beam_size=AUDIO_DECODER_BEAM_SIZE,
        temperature=AUDIO_DECODER_TEMPERATURE,
        vad_filter=False,
    )
    detected_language = getattr(info, "language", language)
    items: list[dict[str, object]] = []
    word_count = 0
    for ordinal, segment in enumerate(raw_segments, start=1):
        if ordinal > MAX_AUDIO_SEGMENTS:
            break
        text = getattr(segment, "text", None)
        start = _number(getattr(segment, "start", None))
        end = _number(getattr(segment, "end", None))
        if not isinstance(text, str) or start is None or end is None or end < start:
            continue
        words: list[dict[str, object]] = []
        raw_words = getattr(segment, "words", None)
        if raw_words is not None:
            for word in raw_words:
                if word_count >= MAX_AUDIO_WORDS:
                    break
                word_text = getattr(word, "word", None)
                word_start = _number(getattr(word, "start", None))
                word_end = _number(getattr(word, "end", None))
                if not isinstance(word_text, str):
                    continue
                words.append(
                    {
                        "text": word_text,
                        "start_ms": (
                            window_start_ms + round(word_start * 1000)
                            if word_start is not None
                            else None
                        ),
                        "end_ms": (
                            window_start_ms + round(word_end * 1000)
                            if word_end is not None
                            else None
                        ),
                        "probability": _number(getattr(word, "probability", None)),
                    }
                )
                word_count += 1
        items.append(
            {
                "kind": "audio_transcript_segment",
                "role": "PARAGRAPH",
                "text_or_value": text.strip(),
                "parent": "audio:timeline",
                "location": {
                    "start_ms": window_start_ms + round(start * 1000),
                    "end_ms": window_start_ms + round(end * 1000),
                    "ordinal": ordinal,
                },
                "extension": {
                    "model_derived": True,
                    "timestamp_accuracy": "MODEL_APPROXIMATE",
                    "language": detected_language
                    if isinstance(detected_language, str)
                    else language,
                    "avg_logprob": _number(getattr(segment, "avg_logprob", None)),
                    "no_speech_prob": _number(getattr(segment, "no_speech_prob", None)),
                    "word_count": len(words),
                    "word_timestamps_published": False,
                },
                "words": words,
            }
        )
    return items, detected_language if isinstance(detected_language, str) else language, word_count


def transcribe_media_window(
    source_path: str,
    *,
    start_ms: int,
    end_ms: int,
    language: str | None = None,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Reuse the governed base audio path for one audio stream in another container."""
    if start_ms < 0 or end_ms <= start_ms or end_ms - start_ms > MAX_AUDIO_WINDOW_MS:
        raise AudioSourceInvalid("media audio window is invalid")
    model_path, model_revision, model_identity_sha256 = resolve_local_audio_model()
    with TemporaryDirectory(prefix="steward-media-audio-") as temporary:
        wav_path = Path(temporary) / "window.wav"
        _decode_window(source_path, wav_path, start_ms=start_ms, end_ms=end_ms)
        decoded_pcm_bytes = wav_path.stat().st_size
        intervals, speech_ms = _speech_intervals(wav_path)
        transcript, detected_language, word_count = _transcribe(
            wav_path,
            model_path=model_path,
            language=language,
            intervals=intervals,
            window_start_ms=start_ms,
            window_duration_ms=end_ms - start_ms,
        )
    projected: list[dict[str, object]] = []
    for ordinal, item in enumerate(transcript, start=1):
        extension = item.get("extension")
        extension_value = dict(extension) if isinstance(extension, dict) else {}
        extension_value["source_kind"] = "AUDIO_ASR"
        projected.append(
            {
                **item,
                "node_id": f"video:audio-asr:{ordinal}",
                "parent": "video:timeline",
                "extension": extension_value,
            }
        )
    return projected, {
        "decoded_pcm_bytes": decoded_pcm_bytes,
        "vad_speech_ms": speech_ms,
        "asr_segment_count": len(projected),
        "asr_word_count": word_count,
        "detected_language": detected_language,
        "model_id": DEFAULT_AUDIO_MODEL_ID,
        "model_revision": model_revision,
        "model_identity_sha256": model_identity_sha256,
        "decoding_policy_id": AUDIO_DECODING_POLICY_ID,
        "decoding_policy_sha256": AUDIO_DECODING_POLICY_SHA256,
        "base_transcript_digest": base_transcript_digest(projected),
    }


def base_transcript_digest(segments: list[dict[str, object]]) -> str:
    """Digest base ASR text/ranges before optional alignment or diarization."""
    values: list[dict[str, object]] = []
    for segment in segments:
        location = segment.get("location")
        values.append(
            {
                "text": segment.get("text_or_value"),
                "start_ms": location.get("start_ms") if isinstance(location, dict) else None,
                "end_ms": location.get("end_ms") if isinstance(location, dict) else None,
                "ordinal": location.get("ordinal") if isinstance(location, dict) else None,
            }
        )
    return sha256(canonical_json(values)).hexdigest()


@dataclass(frozen=True, slots=True)
class AudioDocumentWorker:
    """Pickle-safe task-owned audio worker used by the existing isolated executor."""

    source_format: str
    mode: str
    start_ms: int = 0
    language: str | None = None
    request_digest: str | None = None
    source_sha256: str | None = None
    analysis: str = "TRANSCRIPT"

    def __call__(self, source_path: str) -> dict[str, Any]:
        probe = probe_audio(source_path)
        _validate_audio_identity(self.source_format, probe)
        source_sha256 = self.source_sha256 or _source_sha256(source_path)
        duration_value = probe["duration_ms"]
        if not isinstance(duration_value, int) or isinstance(duration_value, bool):
            raise AudioSourceInvalid("audio duration is invalid")
        duration_ms = duration_value
        backend_version = _package_version("faster-whisper") or "UNAVAILABLE"
        if self.mode == "PROBE":
            graph = build_audio_document_graph(
                source_sha256=source_sha256,
                duration_ms=duration_ms,
                window_start_ms=0,
                window_end_ms=duration_ms,
                probe=probe,
                intervals=[],
                segments=[],
                model_id=None,
                model_identity_sha256=None,
            )
            return {
                "backend_name": "FFprobe",
                "backend_version": _run_bounded(
                    [shutil.which("ffprobe") or "ffprobe", "-version"],
                    timeout=5,
                    stdout_limit=16_384,
                )
                .decode("utf-8", "replace")
                .splitlines()[0],
                "warnings": [],
                "items": graph.public_items(),
                "resource_extension": {
                    "media_kind": "AUDIO",
                    "duration_ms": duration_ms,
                    "window_start_ms": None,
                    "window_end_ms": None,
                    "vad_speech_ms": None,
                    "asr_word_count": None,
                    "audio_graph": graph.summary(),
                },
                "continuation": None,
            }
        model_path, model_revision, model_identity_sha256 = resolve_local_audio_model()
        if self.analysis not in AUDIO_ANALYSIS_MODES:
            raise AudioSourceInvalid("audio analysis mode is invalid")
        start_ms = self.start_ms
        if start_ms < 0 or start_ms >= duration_ms:
            raise AudioSourceInvalid("audio continuation start is outside the source")
        end_ms = min(duration_ms, start_ms + MAX_AUDIO_WINDOW_MS)
        with TemporaryDirectory(prefix="steward-audio-") as temporary:
            wav_path = Path(temporary) / "window.wav"
            _decode_window(source_path, wav_path, start_ms=start_ms, end_ms=end_ms)
            decoded_pcm_bytes = wav_path.stat().st_size
            intervals, speech_ms = _speech_intervals(wav_path)
            transcript, detected_language, word_count = _transcribe(
                wav_path,
                model_path=model_path,
                language=self.language,
                intervals=intervals,
                window_start_ms=start_ms,
                window_duration_ms=end_ms - start_ms,
            )
            transcript_digest = base_transcript_digest(transcript)
            transcript_segment_count = len(transcript)
            alignment_model = None
            aligned_word_count = 0
            unaligned_word_count = 0
            if self.analysis in {"ALIGNED_WORDS", "ALIGNED_WORDS_AND_SPEAKERS"}:
                try:
                    (
                        transcript,
                        alignment_model,
                        aligned_word_count,
                        unaligned_word_count,
                    ) = align_transcript_words(
                        wav_path,
                        transcript,
                        language=(
                            detected_language
                            if isinstance(detected_language, str)
                            else self.language
                        ),
                        window_start_ms=start_ms,
                    )
                except AudioAlignmentUnavailable as error:
                    raise AudioRuntimeUnavailable(str(error)) from error
            speaker_turns: list[dict[str, object]] = []
            overlap_regions: list[dict[str, object]] = []
            diarization_model = None
            if self.analysis in {"SPEAKER_TURNS", "ALIGNED_WORDS_AND_SPEAKERS"}:
                try:
                    (
                        speaker_turns,
                        overlap_regions,
                        diarization_model,
                    ) = diarize_audio(wav_path, window_start_ms=start_ms)
                except AudioDiarizationUnavailable as error:
                    raise AudioRuntimeUnavailable(str(error)) from error
        graph = build_audio_document_graph(
            source_sha256=source_sha256,
            duration_ms=duration_ms,
            window_start_ms=start_ms,
            window_end_ms=end_ms,
            probe=probe,
            intervals=intervals,
            segments=transcript,
            model_id=DEFAULT_AUDIO_MODEL_ID,
            model_identity_sha256=model_identity_sha256,
            speaker_turns=speaker_turns,
            overlap_regions=overlap_regions,
            diarization_model_identity_sha256=(
                diarization_model.identity_sha256 if diarization_model else None
            ),
        )
        speech_bound_reached = speech_ms >= MAX_AUDIO_SPEECH_MS
        next_start_ms = end_ms if end_ms < duration_ms else None
        if speech_bound_reached and intervals:
            next_start_ms = min(end_ms, start_ms + intervals[-1]["end_ms"])
        continuation = (
            {
                "schema_name": AUDIO_CONTINUATION_SCHEMA_NAME,
                "schema_version": AUDIO_TIME_CONTINUATION_SCHEMA_VERSION,
                "request_digest": self.request_digest,
                "source_sha256": self.source_sha256,
                "next_start_ms": next_start_ms,
            }
            if next_start_ms is not None
            else None
        )
        return {
            "backend_name": "FasterWhisper",
            "backend_version": backend_version,
            "warnings": (
                (["AUDIO_SPEECH_LIMIT_REACHED"] if speech_bound_reached else [])
                + (["AUDIO_WORDS_PARTIALLY_UNALIGNED"] if unaligned_word_count else [])
            ),
            "items": graph.public_items(
                include_words=self.analysis in {"ALIGNED_WORDS", "ALIGNED_WORDS_AND_SPEAKERS"},
                include_speakers=self.analysis in {"SPEAKER_TURNS", "ALIGNED_WORDS_AND_SPEAKERS"},
            ),
            "resource_extension": {
                "media_kind": "AUDIO",
                "duration_ms": duration_ms,
                "window_start_ms": start_ms,
                "window_end_ms": end_ms,
                "decoded_pcm_bytes": decoded_pcm_bytes,
                "decoded_pcm_bytes_limit": MAX_AUDIO_DECODED_PCM_BYTES,
                "vad_backend": "silero-vad",
                "vad_version": "6",
                "vad_speech_ms": speech_ms,
                "asr_backend": "faster-whisper",
                "asr_version": backend_version,
                "asr_model_id": DEFAULT_AUDIO_MODEL_ID,
                "asr_model_revision": model_revision,
                "asr_model_identity_sha256": model_identity_sha256,
                "asr_decoding_policy_id": AUDIO_DECODING_POLICY_ID,
                "asr_decoding_policy_sha256": AUDIO_DECODING_POLICY_SHA256,
                "base_transcript_digest": transcript_digest,
                "base_transcript_segment_count": transcript_segment_count,
                "base_transcript_digest_scope": "TEXT_RANGE_ORDINAL",
                "audio_request_digest": self.request_digest,
                "asr_word_count": word_count,
                "audio_analysis": self.analysis,
                "alignment_backend": (
                    "transformers-ctc-forced-alignment" if alignment_model else None
                ),
                "alignment_model_id": alignment_model.model_id if alignment_model else None,
                "alignment_model_revision": (alignment_model.revision if alignment_model else None),
                "alignment_model_identity_sha256": (
                    alignment_model.identity_sha256 if alignment_model else None
                ),
                "aligned_word_count": aligned_word_count,
                "unaligned_word_count": unaligned_word_count,
                "diarization_backend": (
                    "sherpa-onnx-offline-speaker-diarization" if diarization_model else None
                ),
                "diarization_model_identity_sha256": (
                    diarization_model.identity_sha256 if diarization_model else None
                ),
                "speaker_count": len(
                    {
                        turn.get("speaker_label")
                        for turn in speaker_turns
                        if isinstance(turn.get("speaker_label"), str)
                    }
                ),
                "speaker_turn_count": len(speaker_turns),
                "overlap_region_count": len(overlap_regions),
                "speaker_identity_scope": ("FILE_LOCAL_ANONYMOUS" if diarization_model else None),
                "diarization_quality": (
                    diarization_observation_quality(
                        speaker_turns,
                        overlap_regions,
                        window_start_ms=start_ms,
                        window_end_ms=end_ms,
                    )
                    if diarization_model
                    else None
                ),
                "audio_graph": graph.summary(),
                "detected_language": detected_language,
                "model_derived": True,
                "persistence_effect": "NONE",
            },
            "continuation": continuation,
        }


__all__ = [
    "AUDIO_DECODING_POLICY",
    "AUDIO_DECODING_POLICY_ID",
    "AUDIO_DECODING_POLICY_SHA256",
    "AUDIO_CONTINUATION_SCHEMA_NAME",
    "AUDIO_RESULT_PAGE_CONTINUATION_SCHEMA_VERSION",
    "AUDIO_TIME_CONTINUATION_SCHEMA_VERSION",
    "AUDIO_FORMAT_BY_SUFFIX",
    "AUDIO_ANALYSIS_MODES",
    "AUDIO_SOURCE_FORMATS",
    "AUDIO_SUFFIX_BY_FORMAT",
    "MAX_AUDIO_SOURCE_BYTES",
    "AudioDocumentWorker",
    "AudioRuntimeUnavailable",
    "audio_request_digest",
    "audio_runtime_capabilities",
    "base_transcript_digest",
    "resolve_local_audio_model",
    "transcribe_media_window",
]
