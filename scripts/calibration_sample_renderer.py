from __future__ import annotations

"""Render Calibration v2 event streams with a real multisample kit.

The renderer consumes the JSON payload produced by ``CalibrationRenderWorker``
and writes a result JSON containing one durable audio artifact.  It deliberately
has no oscillator, noise-generator, or procedural-audio fallback: every audible
hit must come from a sample referenced by a KitManifestV1-compatible manifest.
"""

import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse
import uuid
import wave

import boto3
import numpy as np
import requests


class SampleRendererError(RuntimeError):
    pass


STRICT_ENVS = {"staging", "production", "prod", "live"}


def _app_env() -> str:
    return str(os.getenv("APP_ENV", "development")).strip().lower()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_key_part(value: Any, default: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text).strip("-._")
    return text or default


def _parse_s3_uri(uri: str) -> Tuple[str, str]:
    parsed = urlparse(str(uri or "").strip())
    if parsed.scheme.lower() != "s3":
        raise SampleRendererError(f"Expected s3:// URI, got {uri!r}")
    bucket = parsed.netloc.strip()
    key = parsed.path.lstrip("/").strip()
    if not bucket or not key:
        raise SampleRendererError("S3 URI must include bucket and key")
    return bucket, key


def _s3_client():
    kwargs: Dict[str, Any] = {
        "region_name": str(
            os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
            or ""
        ).strip()
        or None
    }
    endpoint = str(os.getenv("AWS_S3_ENDPOINT_URL") or "").strip()
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def _read_uri_bytes(uri: str, *, timeout_seconds: float = 45.0) -> bytes:
    value = str(uri or "").strip()
    if not value:
        raise SampleRendererError("Empty sample or manifest URI")
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()

    if scheme == "s3":
        bucket, key = _parse_s3_uri(value)
        response = _s3_client().get_object(Bucket=bucket, Key=key)
        body = response.get("Body")
        if body is None:
            raise SampleRendererError(f"S3 object has no body: {value}")
        return body.read()

    if scheme in {"http", "https"}:
        response = requests.get(value, timeout=timeout_seconds)
        response.raise_for_status()
        return response.content

    if scheme == "file":
        if _app_env() in STRICT_ENVS:
            raise SampleRendererError("Local file:// samples are not allowed in strict runtimes")
        path = Path(parsed.path)
    elif not scheme:
        if _app_env() in STRICT_ENVS:
            raise SampleRendererError("Local sample paths are not allowed in strict runtimes")
        path = Path(value)
    else:
        raise SampleRendererError(f"Unsupported URI scheme: {scheme}")

    if not path.is_file():
        raise SampleRendererError(f"Local file not found: {path}")
    return path.read_bytes()


def _manifest_base_uri(manifest_uri: str) -> str:
    parsed = urlparse(manifest_uri)
    scheme = parsed.scheme.lower()
    if scheme == "s3":
        bucket, key = _parse_s3_uri(manifest_uri)
        parent = key.rsplit("/", 1)[0] if "/" in key else ""
        return f"s3://{bucket}/{parent}/" if parent else f"s3://{bucket}/"
    if scheme in {"http", "https"}:
        return manifest_uri.rsplit("/", 1)[0] + "/"
    if scheme == "file":
        return str(Path(parsed.path).parent.resolve()) + os.sep
    return str(Path(manifest_uri).expanduser().resolve().parent) + os.sep


def _resolve_sample_uri(raw_uri: str, *, manifest_uri: str) -> str:
    value = str(raw_uri or "").strip()
    if not value:
        raise SampleRendererError("Manifest contains an empty sample URI")
    if urlparse(value).scheme:
        return value

    base = _manifest_base_uri(manifest_uri)
    parsed_base = urlparse(base)
    if parsed_base.scheme == "s3":
        bucket, base_key = _parse_s3_uri(base.rstrip("/"))
        key = "/".join(part for part in (base_key.rstrip("/"), value.lstrip("/")) if part)
        return f"s3://{bucket}/{key}"
    if parsed_base.scheme in {"http", "https"}:
        return urljoin(base, value)
    return str((Path(base) / value).resolve())


def _load_manifest(manifest_uri: str) -> Tuple[Dict[str, Any], str]:
    raw = _read_uri_bytes(manifest_uri)
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:
        raise SampleRendererError(f"Kit manifest JSON is invalid: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SampleRendererError("Kit manifest root must be an object")
    if not isinstance(manifest.get("articulations"), dict) or not manifest["articulations"]:
        raise SampleRendererError("Kit manifest has no articulations")
    return manifest, _sha256_bytes(raw)


def _pcm24_to_int32(raw: bytes) -> np.ndarray:
    values = np.frombuffer(raw, dtype=np.uint8)
    if len(values) % 3:
        raise SampleRendererError("Invalid 24-bit PCM byte length")
    triples = values.reshape(-1, 3).astype(np.int32)
    signed = triples[:, 0] | (triples[:, 1] << 8) | (triples[:, 2] << 16)
    signed = np.where((signed & 0x800000) != 0, signed - 0x1000000, signed)
    return signed.astype(np.int32)


def _decode_wav(raw: bytes) -> Tuple[np.ndarray, int]:
    try:
        with wave.open(io.BytesIO(raw), "rb") as handle:
            channels = int(handle.getnchannels())
            sample_width = int(handle.getsampwidth())
            sample_rate = int(handle.getframerate())
            frame_count = int(handle.getnframes())
            compression = handle.getcomptype()
            pcm = handle.readframes(frame_count)
    except Exception as exc:
        raise SampleRendererError(f"Could not read WAV sample: {exc}") from exc

    if compression != "NONE":
        raise SampleRendererError(f"Compressed WAV samples are not supported ({compression})")
    if channels not in {1, 2}:
        raise SampleRendererError(f"Only mono/stereo samples are supported, got {channels} channels")
    if sample_rate <= 0:
        raise SampleRendererError("Sample rate must be positive")

    if sample_width == 1:
        data = (np.frombuffer(pcm, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sample_width == 2:
        data = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    elif sample_width == 3:
        data = _pcm24_to_int32(pcm).astype(np.float32) / 8388608.0
    elif sample_width == 4:
        data = np.frombuffer(pcm, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise SampleRendererError(f"Unsupported PCM sample width: {sample_width} bytes")

    if data.size % channels:
        raise SampleRendererError("PCM data is not aligned to its channel count")
    data = data.reshape(-1, channels)
    if channels == 1:
        data = np.repeat(data, 2, axis=1)
    return np.asarray(data, dtype=np.float32), sample_rate


def _resample_linear(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio
    if audio.shape[0] < 2:
        return audio
    target_frames = max(1, int(round(audio.shape[0] * target_rate / float(source_rate))))
    source_x = np.arange(audio.shape[0], dtype=np.float64)
    target_x = np.linspace(0.0, float(audio.shape[0] - 1), target_frames, dtype=np.float64)
    channels = [np.interp(target_x, source_x, audio[:, index]) for index in range(audio.shape[1])]
    return np.stack(channels, axis=1).astype(np.float32)


def _db_to_gain(db_value: Any) -> float:
    db = _safe_float(db_value, 0.0)
    return float(10.0 ** (db / 20.0))


def _apply_pan(audio: np.ndarray, pan_value: Any) -> np.ndarray:
    pan = max(-1.0, min(1.0, _safe_float(pan_value, 0.0)))
    if abs(pan) < 1e-9:
        return audio
    angle = (pan + 1.0) * math.pi / 4.0
    gains = np.asarray([math.cos(angle), math.sin(angle)], dtype=np.float32)
    mono = audio.mean(axis=1, keepdims=True)
    return mono * gains.reshape(1, 2)


def _event_time_seconds(item: Mapping[str, Any], *, tempo_bpm: float) -> Optional[float]:
    for key in ("time_sec", "start_sec", "time", "t"):
        if item.get(key) is not None:
            return max(0.0, _safe_float(item.get(key), 0.0))
    if item.get("bar_pos_frac") is not None:
        bar_index = _safe_int(item.get("barIndex", item.get("bar_index", 0)), 0)
        fraction = _safe_float(item.get("bar_pos_frac"), 0.0)
        beat_seconds = 60.0 / max(1e-6, tempo_bpm)
        return max(0.0, (bar_index * 4.0 + fraction * 4.0) * beat_seconds)
    return None


def _normalize_events(payload: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
    run_events = payload.get("run_events") if isinstance(payload.get("run_events"), dict) else {}
    event_stream = run_events.get("event_stream") if isinstance(run_events.get("event_stream"), list) else []
    tempo_bpm = _safe_float(run_events.get("tempo_bpm"), 110.0)
    normalized: List[Dict[str, Any]] = []
    for index, raw in enumerate(event_stream):
        if not isinstance(raw, dict):
            continue
        event_time = _event_time_seconds(raw, tempo_bpm=tempo_bpm)
        if event_time is None:
            continue
        instrument = str(
            raw.get("instrument_id")
            or raw.get("instrumentId")
            or raw.get("instrument")
            or raw.get("drum")
            or ""
        ).strip().lower().replace("-", "_").replace(" ", "_")
        if not instrument:
            continue
        normalized.append(
            {
                "event_index": index,
                "time_sec": event_time,
                "instrument": instrument,
                "velocity": max(1, min(127, _safe_int(raw.get("velocity"), 96))),
            }
        )
    normalized.sort(key=lambda item: (item["time_sec"], item["event_index"]))
    if not normalized:
        raise SampleRendererError("No renderable events found in payload")
    return normalized, tempo_bpm


def _articulation_aliases(instrument: str) -> Iterable[str]:
    normalized = instrument.lower().replace("-", "_").replace(" ", "_")
    yield normalized
    aliases: Dict[str, Sequence[str]] = {
        "snare": ("snare_center", "snare"),
        "snare_ghost": ("snare_ghost", "snare_center", "snare"),
        "snare_rim": ("snare_rimshot", "snare_rim", "snare_center"),
        "rimshot": ("snare_rimshot", "snare_center"),
        "sidestick": ("snare_sidestick", "sidestick", "snare_center"),
        "hihat": ("hihat_closed", "hat", "hihat"),
        "hat": ("hihat_closed", "hat", "hihat"),
        "ride": ("ride_bow", "ride"),
        "crash": ("crash", "crash_1"),
        "tom": ("tom_mid", "tom"),
    }
    for key, candidates in aliases.items():
        if normalized == key or normalized.startswith(key + "_"):
            for candidate in candidates:
                yield candidate
    if "kick" in normalized or normalized in {"bd", "bass_drum"}:
        yield "kick"
    if "snare" in normalized:
        if "ghost" in normalized:
            yield "snare_ghost"
        if "rim" in normalized:
            yield "snare_rimshot"
        yield "snare_center"
        yield "snare"
    if "hat" in normalized or normalized.startswith("hh"):
        if "open" in normalized:
            yield "hihat_open"
        elif "pedal" in normalized:
            yield "hihat_pedal"
        else:
            yield "hihat_closed"
    if "ride" in normalized:
        yield "ride_bell" if "bell" in normalized else "ride_bow"
        yield "ride"
    if any(token in normalized for token in ("crash", "china", "splash")):
        yield normalized
        yield "crash"
    if "tom" in normalized:
        if any(token in normalized for token in ("floor", "low", "tom3", "tom4", "tom5")):
            yield "tom_low"
        elif any(token in normalized for token in ("high", "tom1")):
            yield "tom_high"
        else:
            yield "tom_mid"


def _resolve_articulation(manifest: Mapping[str, Any], instrument: str) -> Tuple[str, Mapping[str, Any]]:
    articulations = manifest.get("articulations")
    if not isinstance(articulations, dict):
        raise SampleRendererError("Kit manifest articulations must be an object")
    seen = set()
    for candidate in _articulation_aliases(instrument):
        if candidate in seen:
            continue
        seen.add(candidate)
        value = articulations.get(candidate)
        if isinstance(value, dict):
            return candidate, value
    raise SampleRendererError(f"Kit manifest has no articulation for instrument '{instrument}'")


def _choose_velocity_layer(layers: Sequence[Any], velocity: int) -> Mapping[str, Any]:
    candidates = [item for item in layers if isinstance(item, dict)]
    if not candidates:
        raise SampleRendererError("Articulation mic has no velocity layers")
    for layer in candidates:
        minimum = _safe_int(layer.get("min"), 1)
        maximum = _safe_int(layer.get("max"), 127)
        if minimum <= velocity <= maximum:
            return layer
    return min(
        candidates,
        key=lambda item: min(
            abs(velocity - _safe_int(item.get("min"), 1)),
            abs(velocity - _safe_int(item.get("max"), 127)),
        ),
    )


def _deterministic_rr_index(
    *,
    seed: int,
    event_index: int,
    articulation_id: str,
    velocity: int,
    count: int,
) -> int:
    if count <= 0:
        raise SampleRendererError("Round-robin sample list is empty")
    basis = f"{seed}|{event_index}|{articulation_id}|{velocity}".encode("utf-8")
    return int(hashlib.sha256(basis).hexdigest()[:12], 16) % count


def _mic_definitions(manifest: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    output: Dict[str, Mapping[str, Any]] = {}
    raw = manifest.get("mics")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                mic_id = str(item.get("id") or "").strip()
                if mic_id:
                    output[mic_id] = item
    return output


def _render(
    *,
    payload: Mapping[str, Any],
    manifest: Mapping[str, Any],
    manifest_uri: str,
    target_rate: int,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    events, tempo_bpm = _normalize_events(payload)
    request = payload.get("render_request") if isinstance(payload.get("render_request"), dict) else {}
    seed = _safe_int(request.get("seed"), 0)
    mic_defs = _mic_definitions(manifest)
    mix_defaults = manifest.get("mixDefaults") if isinstance(manifest.get("mixDefaults"), dict) else {}
    mic_gains = mix_defaults.get("micGainsDb") if isinstance(mix_defaults.get("micGainsDb"), dict) else {}
    master_gain = _db_to_gain(mix_defaults.get("masterGainDb", 0.0))

    sample_cache: Dict[str, np.ndarray] = {}
    selected: List[Tuple[float, np.ndarray, float]] = []
    articulation_counts: Dict[str, int] = {}
    sample_uris_used: List[str] = []

    for event in events:
        articulation_id, articulation = _resolve_articulation(manifest, event["instrument"])
        mics = articulation.get("mics") if isinstance(articulation.get("mics"), dict) else {}
        if not mics:
            raise SampleRendererError(f"Articulation '{articulation_id}' has no mic sample definitions")
        articulation_counts[articulation_id] = articulation_counts.get(articulation_id, 0) + 1

        for mic_id, mic_payload in sorted(mics.items(), key=lambda item: str(item[0])):
            if not isinstance(mic_payload, dict):
                continue
            layers = mic_payload.get("velocityLayers")
            if not isinstance(layers, list):
                raise SampleRendererError(
                    f"Articulation '{articulation_id}' mic '{mic_id}' has no velocityLayers"
                )
            layer = _choose_velocity_layer(layers, event["velocity"])
            round_robin = layer.get("roundRobin")
            if not isinstance(round_robin, list) or not round_robin:
                raise SampleRendererError(
                    f"Articulation '{articulation_id}' mic '{mic_id}' has no roundRobin samples"
                )
            rr_index = _deterministic_rr_index(
                seed=seed,
                event_index=event["event_index"],
                articulation_id=articulation_id,
                velocity=event["velocity"],
                count=len(round_robin),
            )
            sample_uri = _resolve_sample_uri(
                str(round_robin[rr_index]),
                manifest_uri=manifest_uri,
            )
            if sample_uri not in sample_cache:
                raw = _read_uri_bytes(sample_uri)
                decoded, source_rate = _decode_wav(raw)
                sample_cache[sample_uri] = _resample_linear(decoded, source_rate, target_rate)
            sample_audio = sample_cache[sample_uri]
            sample_uris_used.append(sample_uri)

            mic_definition = mic_defs.get(str(mic_id), {})
            gain_db = _safe_float(mic_definition.get("defaultGainDb"), 0.0)
            gain_db += _safe_float(mic_gains.get(str(mic_id)), 0.0)
            gain_db += _safe_float(mic_payload.get("gainDb"), 0.0)
            gain = _db_to_gain(gain_db)
            velocity_gain = 0.45 + 0.55 * (event["velocity"] / 127.0)
            panned = _apply_pan(sample_audio, mic_definition.get("defaultPan", 0.0))
            selected.append((event["time_sec"], panned, gain * velocity_gain))

    if not selected:
        raise SampleRendererError("No manifest samples were selected for the event stream")

    total_frames = max(
        int(round(start * target_rate)) + audio.shape[0]
        for start, audio, _gain in selected
    ) + int(round(target_rate * 0.25))
    mix = np.zeros((max(1, total_frames), 2), dtype=np.float32)
    for start, audio, gain in selected:
        start_frame = max(0, int(round(start * target_rate)))
        end_frame = min(mix.shape[0], start_frame + audio.shape[0])
        if end_frame <= start_frame:
            continue
        mix[start_frame:end_frame] += audio[: end_frame - start_frame] * float(gain)

    mix *= master_gain
    peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    target_peak = float(10.0 ** (-1.0 / 20.0))
    if peak > target_peak and peak > 1e-12:
        mix *= target_peak / peak
    true_peak = float(np.max(np.abs(mix))) if mix.size else 0.0
    true_peak_db = 20.0 * math.log10(max(true_peak, 1e-12))
    rms = float(np.sqrt(np.mean(np.square(mix)))) if mix.size else 0.0
    approximate_lufs = 20.0 * math.log10(max(rms, 1e-12))

    stats = {
        "event_count": len(events),
        "tempo_bpm": tempo_bpm,
        "articulation_counts": articulation_counts,
        "unique_sample_count": len(set(sample_uris_used)),
        "sample_rate_hz": target_rate,
        "true_peak_db": true_peak_db,
        "approximate_lufs": approximate_lufs,
    }
    return mix, stats


def _write_pcm16_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(audio, -1.0, 1.0)
    pcm = np.round(clipped * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())


def _upload_output(
    *,
    wav_path: Path,
    payload: Mapping[str, Any],
    sha256: str,
    renderer_version: str,
    sample_pack_version: str,
) -> str:
    strict_runtime = _app_env() in STRICT_ENVS
    upload_enabled = _env_bool("CALIBRATION_RENDER_UPLOAD_ENABLED", default=strict_runtime)
    if strict_runtime and not upload_enabled:
        raise SampleRendererError("S3 output upload cannot be disabled in a strict runtime")
    if not upload_enabled:
        return str(wav_path.resolve())

    bucket = str(os.getenv("AWS_S3_BUCKET") or "").strip()
    if not bucket:
        raise SampleRendererError("AWS_S3_BUCKET is required for durable render output")
    prefix = str(os.getenv("CALIBRATION_RENDER_OUTPUT_PREFIX", "calibration/v2")).strip("/")
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    request = payload.get("render_request") if isinstance(payload.get("render_request"), dict) else {}
    recipe = request.get("render_recipe") if isinstance(request.get("render_recipe"), dict) else {}
    trial_id = _sanitize_key_part(recipe.get("trial_id"), "unassigned-trial")
    role = _sanitize_key_part(recipe.get("role"), "unknown-role")
    run_id = _sanitize_key_part(job.get("run_id") or run.get("run_id"), "unknown-run")
    key = f"{prefix}/{trial_id}/{role}/{run_id}/{sha256}.wav"

    metadata = {
        "sha256": sha256,
        "renderer-version": renderer_version,
        "sample-pack-version": sample_pack_version,
        "run-id": run_id,
        "trial-id": trial_id,
        "diagnostic-only": "false",
    }
    put_args: Dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Body": wav_path.read_bytes(),
        "ContentType": "audio/wav",
        "Metadata": metadata,
    }
    encryption = str(os.getenv("AWS_S3_SERVER_SIDE_ENCRYPTION", "AES256")).strip()
    if encryption:
        put_args["ServerSideEncryption"] = encryption
    client = _s3_client()
    client.put_object(**put_args)
    head = client.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength") or 0) != wav_path.stat().st_size:
        raise SampleRendererError("Uploaded artifact size does not match local WAV")
    return f"s3://{bucket}/{key}"


def _result_payload(
    *,
    payload: Mapping[str, Any],
    storage_uri: str,
    duration_sec: float,
    loudness_lufs: float,
    sample_pack_version: str,
    renderer_version: str,
    sha256: str,
    manifest_uri: str,
    manifest_sha256: str,
    stats: Mapping[str, Any],
) -> Dict[str, Any]:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    run_id = str(job.get("run_id") or run.get("run_id") or "").strip()
    identity = hashlib.sha256(f"{run_id}|{sha256}".encode("utf-8")).hexdigest()[:16]
    return {
        "artifacts": [
            {
                "artifact_id": f"artifact_{identity}",
                "artifact_type": "candidate_audio",
                "storage_uri": storage_uri,
                "duration_sec": duration_sec,
                "loudness_lufs": loudness_lufs,
                "sample_pack_version": sample_pack_version,
                "render_recipe": {
                    "renderer": "drumtrackai_sample_renderer",
                    "renderer_version": renderer_version,
                    "sample_pack_version": sample_pack_version,
                    "manifest_uri": manifest_uri,
                    "manifest_sha256": manifest_sha256,
                    "sha256": sha256,
                    "true_peak_db": stats.get("true_peak_db"),
                    "sample_rate_hz": stats.get("sample_rate_hz"),
                    "event_count": stats.get("event_count"),
                    "articulation_counts": stats.get("articulation_counts"),
                    "unique_sample_count": stats.get("unique_sample_count"),
                    "diagnostic_only": False,
                    "run_id": run_id,
                },
            }
        ]
    }


def render_request(payload: Mapping[str, Any], *, working_dir: Path) -> Dict[str, Any]:
    manifest_uri = str(
        os.getenv("CALIBRATION_SAMPLE_MANIFEST_URI")
        or (
            payload.get("render_request", {}).get("sample_manifest_uri")
            if isinstance(payload.get("render_request"), dict)
            else ""
        )
        or ""
    ).strip()
    if not manifest_uri:
        raise SampleRendererError("CALIBRATION_SAMPLE_MANIFEST_URI is required")

    manifest, manifest_sha256 = _load_manifest(manifest_uri)
    manifest_version = str(manifest.get("version") or "").strip()
    requested_pack = str(
        (payload.get("job") or {}).get("sample_pack_version")
        if isinstance(payload.get("job"), dict)
        else ""
    ).strip()
    configured_pack = str(os.getenv("CALIBRATION_SAMPLE_PACK_VERSION") or "").strip()
    sample_pack_version = configured_pack or manifest_version or requested_pack
    if not sample_pack_version:
        raise SampleRendererError("Sample-pack version is missing")
    if requested_pack and requested_pack not in {"default", sample_pack_version}:
        raise SampleRendererError(
            f"Render job requested sample pack '{requested_pack}' but renderer is configured for '{sample_pack_version}'"
        )

    target_rate = max(22050, min(192000, _safe_int(os.getenv("CALIBRATION_RENDER_SAMPLE_RATE"), 48000)))
    renderer_version = str(
        os.getenv("CALIBRATION_RENDERER_VERSION")
        or "drumtrackai_sample_renderer_v1"
    ).strip()
    if renderer_version in {"", "unknown"}:
        raise SampleRendererError("CALIBRATION_RENDERER_VERSION must be explicit")

    audio, stats = _render(
        payload=payload,
        manifest=manifest,
        manifest_uri=manifest_uri,
        target_rate=target_rate,
    )
    wav_path = working_dir / f"render_{uuid.uuid4().hex}.wav"
    _write_pcm16_wav(wav_path, audio, target_rate)
    sha256 = _sha256_file(wav_path)
    storage_uri = _upload_output(
        wav_path=wav_path,
        payload=payload,
        sha256=sha256,
        renderer_version=renderer_version,
        sample_pack_version=sample_pack_version,
    )
    duration_sec = float(audio.shape[0]) / float(target_rate)
    return _result_payload(
        payload=payload,
        storage_uri=storage_uri,
        duration_sec=duration_sec,
        loudness_lufs=float(stats["approximate_lufs"]),
        sample_pack_version=sample_pack_version,
        renderer_version=renderer_version,
        sha256=sha256,
        manifest_uri=manifest_uri,
        manifest_sha256=manifest_sha256,
        stats=stats,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibration real-sample renderer")
    parser.add_argument("--input", required=True, help="Worker render-request JSON")
    parser.add_argument("--output", required=True, help="Renderer result JSON")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise SampleRendererError("Render-request root must be an object")
        with tempfile.TemporaryDirectory(prefix="calibration_sample_render_") as temp_dir:
            result = render_request(payload, working_dir=Path(temp_dir))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(result, ensure_ascii=True, default=str),
            encoding="utf-8",
        )
        print(json.dumps(result, ensure_ascii=True, default=str))
        return 0
    except Exception as exc:
        error = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(error, ensure_ascii=True), encoding="utf-8")
        except Exception:
            pass
        print(json.dumps(error, ensure_ascii=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
