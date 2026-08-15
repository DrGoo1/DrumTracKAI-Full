from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import uuid
import wave
from array import array
from pathlib import Path
from typing import Any, Dict, List, Tuple


DEFAULT_SAMPLE_RATE = 96000
DEFAULT_MASTER_GAIN = 0.9


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _extract_events(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], float]:
    run_events = payload.get("run_events") if isinstance(payload.get("run_events"), dict) else {}
    events = run_events.get("event_stream") if isinstance(run_events.get("event_stream"), list) else []
    tempo_bpm = _safe_float(run_events.get("tempo_bpm"), 110.0)

    if not events:
        run_meta = payload.get("run") if isinstance(payload.get("run"), dict) else {}
        metadata = run_meta.get("metadata") if isinstance(run_meta.get("metadata"), dict) else {}
        events = metadata.get("event_stream") if isinstance(metadata.get("event_stream"), list) else []

    normalized: List[Dict[str, Any]] = []
    for item in events:
        if not isinstance(item, dict):
            continue
        time_sec = None
        for key in ("time_sec", "start_sec", "time", "t"):
            if item.get(key) is not None:
                time_sec = _safe_float(item.get(key), 0.0)
                break
        if time_sec is None and item.get("bar_pos_frac") is not None:
            bar_index = _safe_int(item.get("barIndex"), 0)
            frac = _safe_float(item.get("bar_pos_frac"), 0.0)
            beat_sec = 60.0 / max(1e-6, tempo_bpm)
            time_sec = (bar_index * 4.0 + frac * 4.0) * beat_sec
        if time_sec is None:
            continue

        instrument = str(
            item.get("instrument_id")
            or item.get("instrument")
            or item.get("drum")
            or "snare_center"
        ).strip().lower()
        velocity = max(1, min(127, _safe_int(item.get("velocity"), 96)))
        normalized.append(
            {
                "time_sec": max(0.0, time_sec),
                "instrument": instrument,
                "velocity": velocity,
            }
        )

    normalized.sort(key=lambda ev: ev["time_sec"])
    return normalized, tempo_bpm


def _instrument_family(name: str) -> str:
    inst = name.lower()
    if "kick" in inst or "bd" in inst:
        return "kick"
    if "snare" in inst or "rim" in inst or "sidestick" in inst:
        return "snare"
    if "hat" in inst:
        if "open" in inst:
            return "hihat_open"
        if "pedal" in inst:
            return "hihat_pedal"
        return "hihat_closed"
    if "ride" in inst:
        return "ride"
    if "crash" in inst or "splash" in inst or "china" in inst:
        return "crash"
    if "tom" in inst:
        if "floor" in inst or "tom3" in inst or "tom4" in inst:
            return "tom_low"
        if "tom2" in inst:
            return "tom_mid"
        return "tom_high"
    return "perc"


def _noise(rand: random.Random) -> float:
    return rand.uniform(-1.0, 1.0)


def _voice_sample(family: str, t: float, vel: float, rand: random.Random) -> float:
    if family == "kick":
        env = math.exp(-8.0 * t)
        body = math.sin(2.0 * math.pi * (52.0 - 18.0 * min(1.0, t * 6.0)) * t)
        click = math.sin(2.0 * math.pi * 1800.0 * t) * math.exp(-60.0 * t)
        return vel * (0.95 * body * env + 0.15 * click)

    if family == "snare":
        env = math.exp(-14.0 * t)
        tone = math.sin(2.0 * math.pi * 210.0 * t) * math.exp(-22.0 * t)
        noise = _noise(rand) * env
        return vel * (0.74 * noise + 0.26 * tone)

    if family == "hihat_closed":
        env = math.exp(-55.0 * t)
        noise = _noise(rand)
        ring = math.sin(2.0 * math.pi * 7600.0 * t + 0.3 * _noise(rand))
        return vel * (0.65 * noise + 0.35 * ring) * env

    if family == "hihat_open":
        env = math.exp(-7.0 * t)
        noise = _noise(rand)
        ring = math.sin(2.0 * math.pi * 6400.0 * t + 0.2 * _noise(rand))
        return vel * (0.7 * noise + 0.3 * ring) * env

    if family == "hihat_pedal":
        env = math.exp(-22.0 * t)
        noise = _noise(rand)
        return vel * noise * env

    if family == "ride":
        env = math.exp(-3.8 * t)
        metal = (
            0.45 * math.sin(2.0 * math.pi * 2400.0 * t)
            + 0.35 * math.sin(2.0 * math.pi * 3600.0 * t)
            + 0.20 * math.sin(2.0 * math.pi * 5200.0 * t)
        )
        return vel * (0.55 * _noise(rand) + 0.45 * metal) * env

    if family == "crash":
        env = math.exp(-2.5 * t)
        shimmer = (
            0.34 * math.sin(2.0 * math.pi * 2200.0 * t)
            + 0.33 * math.sin(2.0 * math.pi * 3100.0 * t)
            + 0.33 * math.sin(2.0 * math.pi * 4700.0 * t)
        )
        return vel * (0.7 * _noise(rand) + 0.3 * shimmer) * env

    if family == "tom_high":
        env = math.exp(-7.8 * t)
        tone = math.sin(2.0 * math.pi * 190.0 * t)
        return vel * tone * env

    if family == "tom_mid":
        env = math.exp(-7.2 * t)
        tone = math.sin(2.0 * math.pi * 150.0 * t)
        return vel * tone * env

    if family == "tom_low":
        env = math.exp(-6.6 * t)
        tone = math.sin(2.0 * math.pi * 112.0 * t)
        return vel * tone * env

    env = math.exp(-10.0 * t)
    return vel * _noise(rand) * env


def _voice_length_sec(family: str) -> float:
    if family in {"kick", "snare", "hihat_closed", "hihat_pedal", "tom_high", "tom_mid", "tom_low"}:
        return 0.45
    if family == "hihat_open":
        return 1.2
    if family == "ride":
        return 1.6
    if family == "crash":
        return 2.6
    return 0.7


def _render_wav(*, events: List[Dict[str, Any]], sample_rate: int, out_path: Path) -> float:
    if not events:
        raise RuntimeError("No renderable events found in payload")

    max_t = max(float(ev["time_sec"]) for ev in events)
    total_sec = max(2.0, max_t + 3.0)
    total_frames = int(total_sec * sample_rate)
    mono = array("f", [0.0] * total_frames)

    for idx, ev in enumerate(events):
        t0 = float(ev["time_sec"])
        family = _instrument_family(str(ev["instrument"]))
        vel = float(ev["velocity"]) / 127.0
        gain = 0.12 + (vel * 0.88)

        seed_key = f"{idx}|{family}|{ev['velocity']}|{t0:.6f}".encode("utf-8")
        seed_int = int(hashlib.sha256(seed_key).hexdigest()[:12], 16)
        rand = random.Random(seed_int)

        start = int(t0 * sample_rate)
        voice_frames = int(_voice_length_sec(family) * sample_rate)
        end = min(total_frames, start + voice_frames)
        for i in range(start, end):
            local_t = (i - start) / float(sample_rate)
            mono[i] += _voice_sample(family, local_t, gain, rand)

    peak = max(abs(v) for v in mono) if mono else 1.0
    if peak > 1e-8:
        scale = (DEFAULT_MASTER_GAIN / peak) if peak > DEFAULT_MASTER_GAIN else 1.0
    else:
        scale = 1.0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out_path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = array("h")
        for sample in mono:
            val = max(-1.0, min(1.0, sample * scale))
            i16 = int(round(val * 32767.0))
            frames.append(i16)
            frames.append(i16)
        wf.writeframes(frames.tobytes())

    return float(total_frames) / float(sample_rate)


def _build_output_payload(*, wav_path: Path, payload: Dict[str, Any], duration_sec: float, sample_rate: int) -> Dict[str, Any]:
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    render_request = payload.get("render_request") if isinstance(payload.get("render_request"), dict) else {}

    run_id = str(job.get("run_id") or run.get("run_id") or "")
    artifact_id = f"artifact_{uuid.uuid4().hex[:12]}"
    sample_pack_version = str(job.get("sample_pack_version") or render_request.get("sample_pack_version") or "default")

    return {
        "artifacts": [
            {
                "artifact_id": artifact_id,
                "artifact_type": "candidate_audio",
                "storage_uri": str(wav_path.resolve()),
                "duration_sec": duration_sec,
                "loudness_lufs": None,
                "sample_pack_version": sample_pack_version,
                "render_recipe": {
                    "renderer": "calibration_procedural_renderer_v1",
                    "sample_rate_hz": sample_rate,
                    "bit_depth": 16,
                    "run_id": run_id,
                },
            }
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibration procedural renderer")
    parser.add_argument("--input", required=True, help="Path to render request JSON")
    parser.add_argument("--output", required=True, help="Path to render result JSON")
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    sample_rate = max(22050, int(args.sample_rate))

    payload = json.loads(input_path.read_text(encoding="utf-8-sig"))
    events, _tempo = _extract_events(payload)

    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    run = payload.get("run") if isinstance(payload.get("run"), dict) else {}
    run_id = str(job.get("run_id") or run.get("run_id") or "unknown_run").strip() or "unknown_run"
    job_id = str(job.get("job_id") or f"rjob_{uuid.uuid4().hex[:10]}").strip()

    repo_root = Path(__file__).resolve().parents[1]
    artifact_dir = repo_root / "backend" / "artifacts" / "calibration" / "rendered" / run_id
    wav_path = artifact_dir / f"{job_id}.wav"

    duration = _render_wav(events=events, sample_rate=sample_rate, out_path=wav_path)
    result = _build_output_payload(wav_path=wav_path, payload=payload, duration_sec=duration, sample_rate=sample_rate)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=True, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
