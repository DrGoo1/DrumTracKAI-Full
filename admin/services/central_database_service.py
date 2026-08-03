"""
Central Database Service
=======================
Provides centralized database access for DrumBeats and other database operations.
Handles SQLite connection, CRUD operations, and connection pooling.
"""
import hashlib
import json
import logging
import os
import random
import sqlite3
import statistics
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

try:
    from admin.services.calibration_phase4_sample_mixin import CalibrationPhase4SampleMixin
except Exception:
    class CalibrationPhase4SampleMixin:  # type: ignore[too-few-public-methods]
        pass

try:  # pragma: no cover - optional Qt dependency
    from PySide6.QtCore import QObject, Signal
except Exception:  # pragma: no cover - headless fallback for backend usage
    class QObject:  # type: ignore[too-few-public-methods]
        """Minimal stub to satisfy CentralDatabaseService inheritance."""

        def __init__(self, *args, **kwargs):
            super().__init__()


    class _HeadlessSignal:
        __slots__ = ("_subscribers",)

        def __init__(self) -> None:
            self._subscribers = []

        def connect(self, callback):
            if callable(callback):
                self._subscribers.append(callback)

        def emit(self, *args, **kwargs):
            for callback in list(self._subscribers):
                try:
                    callback(*args, **kwargs)
                except Exception:
                    continue


    class Signal:  # type: ignore[too-few-public-methods]
        """Descriptor-compatible stand-in for Qt signals."""

        def __init__(self, *args, **kwargs):  # noqa: D401 - match Qt signature
            self._attr_name = None

        def __set_name__(self, owner, name):
            self._attr_name = f"__headless_signal_{name}"

        def __get__(self, instance, owner):
            if instance is None:
                return self
            attr_name = self._attr_name or "__headless_signal"
            signal = getattr(instance, attr_name, None)
            if signal is None:
                signal = _HeadlessSignal()
                setattr(instance, attr_name, signal)
            return signal

logger = logging.getLogger(__name__)


_MISSING = object()


@dataclass
class CalibrationRun:
    run_id: str
    drummer_slug: str
    started_at: datetime
    completed_at: Optional[datetime]
    outcome: str
    note_count: Optional[int]
    fills_per_minute: Optional[float]
    within_tolerance_count: Optional[int]
    total_compared: Optional[int]
    delta_summary: Optional[str]
    metadata: Optional[Dict[str, Any]]
    metrics: Optional[Dict[str, Any]]
    comparison: Optional[Dict[str, Any]]
    log_path: Optional[str]


@dataclass
class CalibrationFeedback:
    feedback_id: str
    drummer_slug: str
    rating: int
    comment: Optional[str]
    author: Optional[str]
    submitted_at: datetime


@dataclass
class RunVersion:
    run_id: str
    generator_version: str
    feature_version: str
    rollup_version: str
    sample_pack_version: str
    seed: int
    commit_hash: Optional[str]
    created_at: datetime


@dataclass
class AudioArtifact:
    artifact_id: str
    run_id: Optional[str]
    artifact_type: str
    storage_uri: str
    duration_sec: Optional[float]
    loudness_lufs: Optional[float]
    sample_pack_version: Optional[str]
    render_recipe: Dict[str, Any]
    created_at: datetime


@dataclass
class CalibrationRenderJob:
    job_id: str
    run_id: str
    render_profile_id: str
    sample_pack_version: str
    status: str
    artifact_ids: List[str]
    error_text: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]


@dataclass
class EvaluationSession:
    session_id: str
    reviewer_id: str
    target_drummer_slug: str
    assigned_at: Optional[datetime]
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    app_version: Optional[str]
    notes: Optional[str]
    created_at: datetime


@dataclass
class EvaluationItem:
    item_id: str
    session_id: str
    base_groove_id: str
    target_drummer_slug: str
    reference_artifact_id: Optional[str]
    baseline_run_id: Optional[str]
    candidate_a_run_id: Optional[str]
    candidate_b_run_id: Optional[str]
    ab_mapping: Dict[str, Any]
    eval_mode: str
    created_at: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DrummerBaselineAsset:
    asset_id: str
    base_groove_id: str
    drummer_slug: str
    drummer_fk: Optional[int]
    analysis_id: Optional[str]
    groove_path: Optional[str]
    audio_path: Optional[str]
    tempo_bpm: Optional[float]
    time_signature: Optional[str]
    bars: Optional[int]
    duration_sec: Optional[float]
    source_song_name: Optional[str]
    created_at: datetime
    updated_at: datetime
    groove: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PairwiseJudgment:
    judgment_id: str
    item_id: str
    preferred_candidate: Optional[str]
    closer_to_target: Optional[str]
    better_feel: Optional[str]
    more_musical: Optional[str]
    confidence: Optional[int]
    created_at: datetime


@dataclass
class AttributeRating:
    rating_id: str
    item_id: str
    candidate_label: str
    stylistic_authenticity: Optional[float]
    groove_feel: Optional[float]
    dynamics: Optional[float]
    phrasing: Optional[float]
    kit_balance: Optional[float]
    fill_behavior: Optional[float]
    human_realism: Optional[float]
    overall_usefulness: Optional[float]
    created_at: datetime


class CentralDatabaseService(QObject, CalibrationPhase4SampleMixin):
    """
    Central database service for DrumTracKAI.
    Provides thread-safe database access and CRUD operations.
    """
    # Define signals for database operations
    database_connected = Signal(str)  # db_path
    database_error = Signal(str)  # error_message
    data_changed = Signal(str, str)  # table_name, operation (insert, update, delete)

    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        super().__init__()
        self._db_path = None
        self._connection = None
        self._connections = {}  # Thread-local connections
        self._initialized = False
        self._tables_created = False
        self._schema_cache: Dict[str, set] = {}
        self._write_lock = threading.RLock()
        self._last_ingest_error: str = ""
        self._engine = None
        logger.info("CentralDatabaseService initialized")

    def _set_last_ingest_error(self, msg: str) -> None:
        try:
            self._last_ingest_error = str(msg or "")
        except Exception:
            self._last_ingest_error = ""

    def get_last_ingest_error(self) -> str:
        try:
            return str(self._last_ingest_error or "")
        except Exception:
            return ""

    @staticmethod
    def _parse_datetime(value: Any) -> Optional[datetime]:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            text = str(value).strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1]
            return datetime.fromisoformat(text)
        except Exception:
            return None

    @staticmethod
    def _json_loads(value: Any, *, default: Any = None) -> Any:
        if value is None or value is _MISSING:
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            text = str(value)
        except Exception:
            text = ""
        if not text:
            return default
        try:
            return json.loads(text)
        except Exception:
            return default

    @staticmethod
    def _row_to_dict(row: Any) -> Dict[str, Any]:
        if row is None:
            return {}
        try:
            keys = row.keys()  # type: ignore[attr-defined]
        except Exception:
            keys = None
        if keys:
            try:
                return {key: row[key] for key in keys}
            except Exception:
                pass
        try:
            return dict(row)
        except Exception:
            return {}

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        try:
            if value is None or value is _MISSING:
                return None
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _safe_float(value: Any) -> Optional[float]:
        try:
            if value is None or value is _MISSING:
                return None
            return float(value)
        except Exception:
            return None

    @staticmethod
    def _json_dumps(value: Any) -> Optional[str]:
        if value is None:
            return None
        try:
            return json.dumps(value, default=str)
        except Exception:
            return None

    def _assimilation_score_base(
        self,
        *,
        songs: int,
        artifacts: int,
        stems: int,
        hit_events: int,
        fills: int,
        techniques: int,
    ) -> float:
        """Baseline assimilation richness score capped at 100."""
        try:
            target_songs = 20
            song_score = 0.0
            if target_songs > 0:
                song_score = min(50.0, (float(songs) / float(target_songs)) * 50.0)

            richness = 0.0
            if artifacts > 0:
                richness += 10.0
            if stems >= 6:
                richness += 15.0
            elif stems > 0:
                richness += 7.0
            if hit_events > 0:
                richness += 25.0
            if fills > 0:
                richness += 10.0
            if techniques > 0:
                richness += 10.0

            richness = min(50.0, richness)
            total = song_score + richness
            if total < 0.0:
                return 0.0
            if total > 100.0:
                return 100.0
            return float(total)
        except Exception:
            return 0.0

    def _compute_assimilation_score(
        self,
        *,
        songs: int,
        artifacts: int,
        stems: int,
        hit_events: int,
        fills: int,
        techniques: int,
        pocket_tightness: Optional[float],
        humanness: Optional[float],
    ) -> int:
        """Combine assimilation richness with qualitative bonuses (0-100)."""
        try:
            base = self._assimilation_score_base(
                songs=songs,
                artifacts=artifacts,
                stems=stems,
                hit_events=hit_events,
                fills=fills,
                techniques=techniques,
            )

            bonus = 0.0
            if pocket_tightness is not None:
                bonus += 5.0
                try:
                    bonus += max(0.0, min(1.0, float(pocket_tightness))) * 5.0
                except Exception:
                    pass
            if humanness is not None:
                bonus += 5.0
                try:
                    bonus += max(0.0, min(1.0, float(humanness))) * 5.0
                except Exception:
                    pass

            total = max(0.0, min(100.0, float(base) + bonus))
            return int(round(total))
        except Exception:
            return 0

    def _ensure_phase32_42_columns(self) -> None:
        """Additive migration: add Phase 32-42 derived-feature columns if missing."""
        try:
            conn = self._get_connection()
            cursor = conn.cursor()

            cols = self._table_columns("song_performance_analysis")
            if not cols:
                return

            if "phase32_42_features_json" not in cols:
                cursor.execute("ALTER TABLE song_performance_analysis ADD COLUMN phase32_42_features_json TEXT")
            if "phase32_42_features_version" not in cols:
                cursor.execute("ALTER TABLE song_performance_analysis ADD COLUMN phase32_42_features_version TEXT")

            conn.commit()
        except Exception as e:
            logger.warning(f"Phase32-42 schema migration skipped/failed: {e}")
            try:
                conn = self._get_connection()
                conn.rollback()
            except Exception:
                pass

    def _is_locked_error(self, e: BaseException) -> bool:
        try:
            s = str(e).lower()
            return "database is locked" in s or "database schema is locked" in s or "locked" in s
        except Exception:
            return False

    def _with_write_lock_retry(self, fn, *, attempts: int = 6, base_sleep_s: float = 0.15):
        last_err = None
        with self._write_lock:
            for i in range(int(attempts)):
                try:
                    return fn()
                except sqlite3.OperationalError as e:
                    last_err = e
                    if not self._is_locked_error(e):
                        raise
                    # backoff with jitter
                    sleep_s = float(base_sleep_s) * (1.8 ** i) + (random.random() * 0.05)
                    time.sleep(min(sleep_s, 2.0))
        if last_err is not None:
            raise last_err
        return None

    def _ensure_drummer_exists(self, *, cursor: sqlite3.Cursor, drummer_id: str):
        """Ensure a drummer row exists and return the FK value to store in song_performance_analysis.

        Important: in the shipped admin DB, `drummers.id` is INTEGER primary key and
        `song_performance_analysis.drummer_id` has an FK to `drummers.id`. That means we
        must store the INTEGER PK (as a value) in song_performance_analysis.drummer_id.
        """
        drummer_id = (drummer_id or "").strip()
        if not drummer_id:
            raise ValueError("Missing drummer_id")

        cols = self._table_columns("drummers")
        display_name = drummer_id.replace("_", " ").strip() or drummer_id
        now = datetime.utcnow().isoformat()

        # Canonical admin schema: INTEGER id PK + TEXT drummer_id/display_name
        if "id" in cols and "drummer_id" in cols:
            cursor.execute("SELECT id FROM drummers WHERE drummer_id = ? LIMIT 1", (drummer_id,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                return int(row[0])

            insert_fields = ["drummer_id"]
            insert_values = [drummer_id]
            if "display_name" in cols:
                insert_fields.append("display_name")
                insert_values.append(display_name)
            if "created_at" in cols:
                insert_fields.append("created_at")
                insert_values.append(now)
            if "updated_at" in cols:
                insert_fields.append("updated_at")
                insert_values.append(now)

            cursor.execute(
                f"INSERT INTO drummers ({', '.join(insert_fields)}) VALUES ({', '.join(['?'] * len(insert_values))})",
                tuple(insert_values),
            )
            cursor.execute("SELECT id FROM drummers WHERE drummer_id = ? LIMIT 1", (drummer_id,))
            row = cursor.fetchone()
            if row and row[0] is not None:
                return int(row[0])
            raise RuntimeError("Failed to create drummer row")

        # Legacy schema used by some older tools: TEXT PK id + name
        if "name" in cols:
            cursor.execute(
                """
                INSERT INTO drummers (id, name, description, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (drummer_id, display_name, None, now, now),
            )
            return drummer_id

        if "display_name" in cols and "drummer_id" in cols:
            cursor.execute(
                """
                INSERT INTO drummers (drummer_id, display_name)
                VALUES (?, ?)
                ON CONFLICT(drummer_id) DO NOTHING
                """,
                (drummer_id, display_name),
            )
            return drummer_id

        raise RuntimeError("Unsupported drummers table schema")

    def _get_drummer_fk_by_slug(self, *, cursor: sqlite3.Cursor, drummer_slug: str) -> Optional[int]:
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return None
            cols = self._table_columns("drummers")
            if "id" in cols and "drummer_id" in cols:
                cursor.execute("SELECT id FROM drummers WHERE drummer_id = ? LIMIT 1", (drummer_slug,))
                row = cursor.fetchone()
                if row and row[0] is not None:
                    return int(row[0])
                return None
            return None
        except Exception:
            return None

    def extract_hit_events_for_analysis(
        self,
        *,
        analysis_id: str,
        max_events_per_stem: int = 5000,
    ) -> int:
        """Phase 2: create drum_hit_events rows by running onset detection on each stem wav.

        Returns number of events inserted.
        """
        try:
            self._set_last_ingest_error("")
            analysis_id = (analysis_id or "").strip()
            if not analysis_id:
                return 0

            try:
                import numpy as np  # type: ignore
                import librosa  # type: ignore
            except Exception as e:
                msg = f"Phase 2 requires librosa/numpy: {e}"
                logger.error(msg)
                self._set_last_ingest_error(msg)
                self.database_error.emit(msg)
                return 0

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    spa = conn_pg.execute(
                        text(
                            """
                            SELECT analysis_id, drummer_id, mvsep_output_dir, tempo_bpm, time_signature
                            FROM public.song_performance_analysis
                            WHERE analysis_id = :aid
                            LIMIT 1
                            """
                        ),
                        {"aid": analysis_id},
                    ).first()
                cursor = None  # not used in PG path
            else:
                conn = self._get_connection()
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT analysis_id, drummer_id, mvsep_output_dir, tempo_bpm, time_signature
                    FROM song_performance_analysis
                    WHERE analysis_id = ?
                    LIMIT 1
                    """,
                    (analysis_id,),
                )
                spa = cursor.fetchone()
            if not spa:
                return 0

            drummer_fk = spa[1]
            mvsep_output_dir = spa[2] or ""
            tempo_bpm = spa[3]
            time_signature = spa[4] or ""

            try:
                tempo_bpm = float(tempo_bpm) if isinstance(tempo_bpm, (int, float)) else None
            except Exception:
                tempo_bpm = None

            beats_per_bar = 4
            try:
                if isinstance(time_signature, str) and "/" in time_signature:
                    beats_per_bar = int(str(time_signature).split("/")[0].strip() or "4")
            except Exception:
                beats_per_bar = 4

            if not tempo_bpm or tempo_bpm <= 0:
                # Reasonable fallback: prevents division by zero and keeps grid fields usable.
                tempo_bpm = 120.0

            sec_per_beat = 60.0 / float(tempo_bpm)

            def _map_stem_to_instrument(stem: str) -> str:
                s = str(stem or "").lower()
                if any(k in s for k in ["kick", "bd", "bassdrum", "bass_drum", "kik"]):
                    return "kick"
                if any(k in s for k in ["snare", "sd", "snr"]):
                    return "snare"
                if any(k in s for k in ["hihat", "hi_hat", "hat", "hh"]):
                    return "hihat"
                if "ride" in s:
                    return "ride"
                if "crash" in s:
                    return "crash"
                if "tom" in s:
                    return "tom"
                if any(k in s for k in ["cym", "cymbal", "oh", "overhead"]):
                    return "cymbal"
                if any(k in s for k in ["perc", "shaker", "tamb", "clap", "fx"]):
                    return "perc"
                return str(stem or "stem")

            def _subdivision_from_fraction(frac: float) -> str:
                # Nearest 16th note grid
                targets = [0.0, 0.25, 0.5, 0.75]
                labels = ["1", "e", "&", "a"]
                best_i = 0
                best_d = 999.0
                for i, t in enumerate(targets):
                    d = abs(float(frac) - float(t))
                    if d < best_d:
                        best_d = d
                        best_i = i
                return labels[best_i]

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    stems = conn_pg.execute(
                        text("SELECT stem_name, file_path FROM public.stem_artifacts WHERE analysis_id = :aid"),
                        {"aid": analysis_id},
                    ).fetchall() or []
            else:
                cursor.execute(
                    "SELECT stem_name, file_path FROM stem_artifacts WHERE analysis_id = ?",
                    (analysis_id,),
                )
                stems = cursor.fetchall() or []
            # Filter to existing files
            try:
                stems = [(sn, fp) for (sn, fp) in stems if fp and os.path.exists(str(fp))]
            except Exception:
                pass
            if (not stems) and mvsep_output_dir and os.path.isdir(str(mvsep_output_dir)):
                try:
                    candidates = [p for p in os.listdir(str(mvsep_output_dir)) if p.lower().endswith(".wav")]
                    stems = [(os.path.splitext(p)[0], os.path.join(str(mvsep_output_dir), p)) for p in candidates]
                except Exception:
                    stems = []

            try:
                inst_like = []
                drums_mix = []
                for sn, fp in stems:
                    s = str(sn or "").lower()
                    if any(k in s for k in ["bass", "vocal", "other", "residual", "track", "mix"]):
                        # non-drum stems
                        if any(k in s for k in ["drums", "drumsep_drums"]):
                            drums_mix.append((sn, fp))
                        continue
                    inst_like.append((sn, fp))
                if inst_like:
                    stems = inst_like
                elif drums_mix:
                    stems = [drums_mix[0]]
                else:
                    # As a last resort, keep original list
                    pass
            except Exception:
                pass

            now = datetime.utcnow().isoformat()

            def _do_write_sqlite() -> int:
                cursor.execute("DELETE FROM drum_hit_events WHERE analysis_id = ?", (analysis_id,))
                inserted = 0
                for stem_name, wav_path in stems:
                    wav_path = str(wav_path or "")
                    if not wav_path or not os.path.exists(wav_path):
                        continue

                    try:
                        y, sr = librosa.load(wav_path, sr=22050, mono=True)
                        if y is None or len(y) < 2048:
                            continue
                        hop_length = 512
                        o_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)
                        onset_frames = librosa.onset.onset_detect(
                            onset_envelope=o_env,
                            sr=sr,
                            hop_length=hop_length,
                            backtrack=False,
                            units="frames",
                        )
                        if onset_frames is None:
                            continue
                        onset_frames = np.asarray(onset_frames, dtype=int)
                        if onset_frames.size == 0:
                            continue
                        if int(max_events_per_stem) > 0 and onset_frames.size > int(max_events_per_stem):
                            onset_frames = onset_frames[: int(max_events_per_stem)]

                        onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop_length)
                        onset_times = np.asarray(onset_times, dtype=float)

                        # strength + normalized velocity (per stem)
                        o_env = np.asarray(o_env, dtype=float) if o_env is not None else np.asarray([], dtype=float)
                        max_env = float(np.max(o_env)) if o_env.size else 0.0
                        instrument = _map_stem_to_instrument(stem_name)

                        for idx, t in enumerate(onset_times):
                            frame = int(onset_frames[idx]) if idx < onset_frames.size else None
                            strength = None
                            vel = None
                            try:
                                if frame is not None and o_env.size and 0 <= frame < int(o_env.size):
                                    strength = float(o_env[frame])
                                    if max_env > 0:
                                        vel = float(strength / max_env)
                            except Exception:
                                strength = None
                                vel = None

                            beat_pos = float(t) / float(sec_per_beat) if sec_per_beat > 0 else 0.0
                            beat_index = int(beat_pos) if beat_pos >= 0 else 0
                            bar_index = int(beat_index // int(beats_per_bar)) if int(beats_per_bar) > 0 else 0
                            frac = beat_pos - float(beat_index)
                            subdiv = _subdivision_from_fraction(frac)

                            # timing offset vs nearest 16th grid
                            grid_16 = round(beat_pos * 4.0) / 4.0
                            grid_t = float(grid_16) * float(sec_per_beat)
                            timing_offset_ms = (float(t) - float(grid_t)) * 1000.0

                            cursor.execute(
                                """
                                INSERT INTO drum_hit_events (
                                    event_id, analysis_id, drummer_id, song_id,
                                    instrument, component,
                                    onset_time_sec, onset_strength, velocity_est,
                                    beat_index, bar_index, subdivision, timing_offset_ms,
                                    is_ghost, is_accent, is_flams_like, is_roll_like,
                                    created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    str(uuid.uuid4()),
                                    analysis_id,
                                    drummer_fk,
                                    None,
                                    instrument,
                                    None,
                                    float(t),
                                    strength,
                                    vel,
                                    int(beat_index),
                                    int(bar_index),
                                    str(subdiv),
                                    float(timing_offset_ms),
                                    None,
                                    None,
                                    None,
                                    None,
                                    now,
                                ),
                            )
                            inserted += 1
                    except Exception:
                        continue

                conn.commit()
                return inserted
            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(text("DELETE FROM public.drum_hit_events WHERE analysis_id = :aid"), {"aid": analysis_id})
                    inserted = 0
                    for stem_name, wav_path in stems:
                        wav_path = str(wav_path or "")
                        if not wav_path or not os.path.exists(wav_path):
                            continue
                        try:
                            y, sr = librosa.load(wav_path, sr=22050, mono=True)
                            if y is None or len(y) < 2048:
                                continue
                            hop_length = 512
                            o_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)
                            onset_frames = librosa.onset.onset_detect(
                                onset_envelope=o_env,
                                sr=sr,
                                hop_length=hop_length,
                                backtrack=False,
                                units="frames",
                            )
                            if onset_frames is None:
                                continue
                            onset_frames = np.asarray(onset_frames, dtype=int)
                            if onset_frames.size == 0:
                                continue
                            if int(max_events_per_stem) > 0 and onset_frames.size > int(max_events_per_stem):
                                onset_frames = onset_frames[: int(max_events_per_stem)]

                            onset_times = librosa.frames_to_time(onset_frames, sr=sr, hop_length=hop_length)
                            onset_times = np.asarray(onset_times, dtype=float)
                        except Exception:
                            continue

                        o_env = np.asarray(o_env, dtype=float) if o_env is not None else np.asarray([], dtype=float)
                        max_env = float(np.max(o_env)) if o_env.size else 0.0
                        instrument = _map_stem_to_instrument(stem_name)

                        for idx, t in enumerate(onset_times):
                            frame = int(onset_frames[idx]) if idx < onset_frames.size else None
                            strength = None
                            vel = None
                            try:
                                if frame is not None and o_env.size and 0 <= frame < int(o_env.size):
                                    strength = float(o_env[frame])
                                    if max_env > 0:
                                        vel = float(strength / max_env)
                            except Exception:
                                strength = None
                                vel = None

                            beat_pos = float(t) / float(sec_per_beat) if sec_per_beat > 0 else 0.0
                            beat_index = int(beat_pos) if beat_pos >= 0 else 0
                            bar_index = int(beat_index // int(beats_per_bar)) if int(beats_per_bar) > 0 else 0
                            frac = beat_pos - float(beat_index)
                            subdiv = _subdivision_from_fraction(frac)

                            grid_16 = round(beat_pos * 4.0) / 4.0
                            grid_t = float(grid_16) * float(sec_per_beat)
                            timing_offset_ms = (float(t) - float(grid_t)) * 1000.0

                            conn_pg.execute(
                                text(
                                    """
                                    INSERT INTO public.drum_hit_events (
                                        event_id, analysis_id, drummer_id, song_id,
                                        instrument, component,
                                        onset_time_sec, onset_strength, velocity_est,
                                        beat_index, bar_index, subdivision, timing_offset_ms,
                                        is_ghost, is_accent, is_flams_like, is_roll_like,
                                        created_at
                                    ) VALUES (
                                        :event_id, :analysis_id, :drummer_id, :song_id,
                                        :instrument, :component,
                                        :onset_time_sec, :onset_strength, :velocity_est,
                                        :beat_index, :bar_index, :subdivision, :timing_offset_ms,
                                        :is_ghost, :is_accent, :is_flams_like, :is_roll_like,
                                        NOW()
                                    )
                                    """
                                ),
                                {
                                    "event_id": str(uuid.uuid4()),
                                    "analysis_id": analysis_id,
                                    "drummer_id": str(drummer_fk),
                                    "song_id": None,
                                    "instrument": instrument,
                                    "component": None,
                                    "onset_time_sec": float(t),
                                    "onset_strength": strength,
                                    "velocity_est": vel,
                                    "beat_index": int(beat_index),
                                    "bar_index": int(bar_index),
                                    "subdivision": str(subdiv),
                                    "timing_offset_ms": float(timing_offset_ms),
                                    "is_ghost": None,
                                    "is_accent": None,
                                    "is_flams_like": None,
                                    "is_roll_like": None,
                                },
                            )
                            inserted += 1
                if inserted > 0:
                    self.data_changed.emit("drum_hit_events", "insert")
                return inserted

            # SQLite path
            inserted = int(self._with_write_lock_retry(_do_write_sqlite) or 0)
            if inserted > 0:
                self.data_changed.emit("drum_hit_events", "insert")
            return inserted
        except Exception as e:
            msg = f"Phase 2 hit event extraction failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return 0

    def run_phase2_hit_event_extraction_for_drummer(
        self,
        *,
        drummer_slug: str,
        max_events_per_stem: int = 5000,
    ) -> Dict[str, Any]:
        """Run Phase 2 for all ingested analyses of a drummer identified by folder slug (e.g., stewart_copeland)."""
        out = {"drummer_slug": drummer_slug, "analyses": 0, "events": 0, "analysis_errors": 0, "failed_analyses": []}
        try:
            self._set_last_ingest_error("")
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return out

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text("SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d ORDER BY created_at DESC"),
                        {"d": drummer_slug},
                    ).fetchall() or []
            else:
                conn = self._get_connection()
                cursor = conn.cursor()
                drummer_fk = self._get_drummer_fk_by_slug(cursor=cursor, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    # Ensure row exists then look up again
                    self._ensure_drummer_exists(cursor=cursor, drummer_id=drummer_slug)
                    conn.commit()
                    drummer_fk = self._get_drummer_fk_by_slug(cursor=cursor, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    return out
                cursor.execute(
                    "SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ? ORDER BY created_at DESC",
                    (drummer_fk,),
                )
                rows = cursor.fetchall() or []
            out["analyses"] = len(rows)
            total_events = 0
            failed_analyses: List[Dict[str, Any]] = []
            for r in rows:
                aid = r[0] if isinstance(r, (tuple, list)) else r.analysis_id
                n = int(
                    self.extract_hit_events_for_analysis(analysis_id=aid, max_events_per_stem=max_events_per_stem) or 0
                )
                err = self.get_last_ingest_error()
                if err:
                    failed_analyses.append({"analysis_id": str(aid), "error": err})
                    self._set_last_ingest_error("")
                total_events += n
                try:
                    print(f"[Phase2] {drummer_slug} analysis={aid} events={n}", flush=True)
                except Exception:
                    pass
            out["events"] = total_events
            out["analysis_errors"] = len(failed_analyses)
            out["failed_analyses"] = failed_analyses
            if failed_analyses:
                self._set_last_ingest_error(f"Phase 2 run had {len(failed_analyses)} analysis failures")
            return out
        except Exception as e:
            msg = f"Phase 2 run failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def compute_phase32_42_features_for_analysis(self, *, analysis_id: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {"analysis_id": analysis_id, "updated": False}
        try:
            analysis_id = (analysis_id or "").strip()
            if not analysis_id:
                return out

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    row = conn_pg.execute(
                        text("SELECT drummer_id FROM public.song_performance_analysis WHERE analysis_id = :aid LIMIT 1"),
                        {"aid": analysis_id},
                    ).first()
                    drummer_fk = row[0] if row else None
            else:
                conn = self._get_connection()
                cur = conn.cursor()
                cur.execute(
                    "SELECT drummer_id FROM song_performance_analysis WHERE analysis_id = ? LIMIT 1",
                    (analysis_id,),
                )
                row = cur.fetchone()
                drummer_fk = row[0] if row else None

            rollup: Dict[str, Any] = {}
            try:
                if drummer_fk is not None:
                    rollup = self.compute_drummer_profile_rollup(drummer_fk=drummer_fk)
            except Exception:
                rollup = {}

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    event_rows = conn_pg.execute(
                        text(
                            """
                            SELECT onset_time_sec, instrument, timing_offset_ms, velocity_est, onset_strength,
                                   is_ghost, is_accent, component
                            FROM public.drum_hit_events
                            WHERE analysis_id = :aid
                            ORDER BY onset_time_sec ASC
                            """
                        ),
                        {"aid": analysis_id},
                    ).fetchall() or []
            else:
                cur.execute(
                    """
                    SELECT onset_time_sec, instrument, timing_offset_ms, velocity_est, onset_strength,
                           is_ghost, is_accent, component
                    FROM drum_hit_events
                    WHERE analysis_id = ?
                    ORDER BY onset_time_sec ASC
                    """,
                    (analysis_id,),
                )
                event_rows = cur.fetchall() or []

            from admin.services.phase32_42_feature_extractor import build_phase32_42_features_json

            payload_json = build_phase32_42_features_json(event_rows=event_rows, rollup=rollup)
            version = "phase32_42_offline_v1"
            now = datetime.utcnow().isoformat()

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            UPDATE public.song_performance_analysis
                            SET phase32_42_features_json = :pj,
                                phase32_42_features_version = :ver,
                                updated_at = NOW()
                            WHERE analysis_id = :aid
                            """
                        ),
                        {"pj": payload_json, "ver": version, "aid": analysis_id},
                    )
            else:
                conn = self._get_connection()
                cur = conn.cursor()
                def _do_write() -> None:
                    cur.execute(
                        """
                        UPDATE song_performance_analysis
                        SET phase32_42_features_json = ?,
                            phase32_42_features_version = ?,
                            updated_at = ?
                        WHERE analysis_id = ?
                        """,
                        (payload_json, version, now, analysis_id),
                    )
                    conn.commit()
                self._with_write_lock_retry(_do_write)
            out["updated"] = True
            out["phase32_42_features_version"] = version
            self.data_changed.emit("song_performance_analysis", "update")
            return out
        except Exception as e:
            msg = f"Phase32-42 feature recompute failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def run_phase32_42_features_for_drummer(self, *, drummer_slug: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {"drummer_slug": drummer_slug, "analyses": 0, "updated": 0, "analysis_errors": 0, "failed_analyses": []}
        try:
            self._set_last_ingest_error("")
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return out

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text(
                            """
                            SELECT analysis_id
                            FROM public.song_performance_analysis
                            WHERE CAST(drummer_id AS TEXT) = CAST(:did AS TEXT)
                            ORDER BY created_at DESC
                            """
                        ),
                        {"did": drummer_slug},
                    ).fetchall() or []
            else:
                conn = self._get_connection()
                cur = conn.cursor()
                drummer_fk = self._get_drummer_fk_by_slug(cursor=cur, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    self._ensure_drummer_exists(cursor=cur, drummer_id=drummer_slug)
                    conn.commit()
                    drummer_fk = self._get_drummer_fk_by_slug(cursor=cur, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    return out

                cur.execute(
                    "SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ? ORDER BY created_at DESC",
                    (drummer_fk,),
                )
                rows = cur.fetchall() or []
            out["analyses"] = len(rows)

            updated = 0
            failed_analyses: List[Dict[str, Any]] = []
            for (aid,) in rows:
                res = self.compute_phase32_42_features_for_analysis(analysis_id=str(aid))
                if res.get("updated"):
                    updated += 1
                err = self.get_last_ingest_error()
                if err:
                    failed_analyses.append({"analysis_id": str(aid), "error": err})
                    self._set_last_ingest_error("")

            out["updated"] = int(updated)
            out["analysis_errors"] = len(failed_analyses)
            out["failed_analyses"] = failed_analyses
            if failed_analyses:
                self._set_last_ingest_error(f"Phase32-42 run had {len(failed_analyses)} analysis failures")
            return out
        except Exception as e:
            msg = f"Phase32-42 run failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def _get_drummer_slug_by_fk(self, *, cursor: sqlite3.Cursor, drummer_fk: int) -> str:
        try:
            cols = self._table_columns("drummers")
            if "id" in cols and "drummer_id" in cols:
                cursor.execute("SELECT drummer_id FROM drummers WHERE id = ? LIMIT 1", (int(drummer_fk),))
                row = cursor.fetchone()
                if row and row[0]:
                    return str(row[0]).strip()
        except Exception:
            pass
        return str(drummer_fk)

    def infer_persona_from_rollup(self, *, rollup: Dict[str, Any]) -> Dict[str, Any]:
        try:
            pocket = rollup.get("pocket_tightness")
            human = rollup.get("humanness")
            fills_per_min = rollup.get("fills_per_min")
            inst_shares = rollup.get("instrument_shares") or {}

            tags: List[str] = []
            if pocket is not None:
                try:
                    p = float(pocket)
                    if p >= 0.70:
                        tags.append("tight")
                    elif p <= 0.35:
                        tags.append("loose")
                    else:
                        tags.append("balanced")
                except Exception:
                    pass

            if human is not None:
                try:
                    h = float(human)
                    if h >= 0.65:
                        tags.append("human")
                    elif h <= 0.35:
                        tags.append("machine")
                except Exception:
                    pass

            if fills_per_min is not None:
                try:
                    fpm = float(fills_per_min)
                    if fpm >= 1.25:
                        tags.append("fill_heavy")
                    elif fpm <= 0.35:
                        tags.append("fill_light")
                except Exception:
                    pass

            try:
                ride_share = float(inst_shares.get("ride") or 0.0) if isinstance(inst_shares, dict) else 0.0
                if ride_share >= 0.18:
                    tags.append("ride_led")
            except Exception:
                pass
            try:
                hat_share = float(inst_shares.get("hihat") or 0.0) if isinstance(inst_shares, dict) else 0.0
                if hat_share >= 0.22:
                    tags.append("hat_led")
            except Exception:
                pass

            if not tags:
                tags = ["balanced"]

            label = "_".join(tags[:4])
            confidence = 0.55 + (0.1 * min(3, max(0, len(tags) - 1)))
            confidence = max(0.0, min(0.95, float(confidence)))

            return {"label": label, "confidence": confidence, "tags": tags}
        except Exception:
            return {"label": "balanced", "confidence": 0.5, "tags": ["balanced"]}

    def generate_preset_from_rollup(self, *, drummer_slug: str, rollup: Dict[str, Any], persona: Dict[str, Any]) -> Dict[str, Any]:
        deltas: Dict[str, Any] = {}
        policies: Dict[str, Any] = {"source": "phase6", "persona": persona, "sentient_drummer": True}

        pocket = rollup.get("pocket_tightness")
        human = rollup.get("humanness")
        fills_per_min = rollup.get("fills_per_min")

        humanize = 0.5
        try:
            if pocket is not None:
                humanize = 0.2 + (1.0 - max(0.0, min(1.0, float(pocket)))) * 0.8
        except Exception:
            humanize = 0.5
        humanize = max(0.0, min(1.0, float(humanize)))

        ghost = 0.35
        try:
            if human is not None:
                ghost = 0.15 + max(0.0, min(1.0, float(human))) * 0.55
        except Exception:
            ghost = 0.35
        ghost = max(0.0, min(1.0, float(ghost)))

        fill_density = 0.35
        try:
            if fills_per_min is not None:
                fill_density = max(0.0, min(1.0, float(fills_per_min) / 2.0))
        except Exception:
            fill_density = 0.35

        swing = 0.15

        deltas["humanizeAmount"] = float(humanize)
        deltas["ghostNoteAmount"] = float(ghost)
        deltas["swingAmount"] = float(swing)
        deltas["fillDensity"] = float(fill_density)

        preset_id = f"phase6_{drummer_slug}".lower()
        name = f"{drummer_slug} (Phase 6)"

        return {
            "preset_id": preset_id,
            "profile_type": "drummer",
            "name": name,
            "tier": "generated",
            "deltas": deltas,
            "policies": policies,
            "source_type": "phase6_rollup",
            "source_song_name": None,
            "source_ref": drummer_slug,
        }

    def export_drummer_profile_json(
        self,
        *,
        drummer_slug: str,
        rollup: Dict[str, Any],
        persona: Dict[str, Any],
        preset: Dict[str, Any],
    ) -> str:
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return ""

            project_root = Path(__file__).resolve().parents[2]
            out_dir = project_root / "database" / "drummer_profiles_generated" / drummer_slug
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "drummer_profile.json"

            payload = {
                "drummer_id": drummer_slug,
                "generated_at": datetime.utcnow().isoformat(),
                "sentient_drummer": True,
                "persona": persona,
                "preset": {
                    "preset_id": preset.get("preset_id"),
                    "profile_type": preset.get("profile_type"),
                    "tier": preset.get("tier"),
                    "deltas": preset.get("deltas") or {},
                    "policies": preset.get("policies") or {},
                },
                "rollup": rollup,
            }
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            return str(out_path)
        except Exception:
            return ""

    def run_phase6_persona_preset_export_for_drummer(self, *, drummer_slug: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "drummer_slug": drummer_slug,
            "saved_rollup": False,
            "persona": {},
            "preset_saved": False,
            "preset_id": None,
            "export_path": "",
        }
        try:
            self._set_last_ingest_error("")
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return out

            if getattr(self, "_engine", None) is not None:
                drummer_fk = drummer_slug
                rollup = self.compute_drummer_profile_rollup(drummer_fk=drummer_slug)
            else:
                conn = self._get_connection()
                cur = conn.cursor()
                drummer_fk = self._get_drummer_fk_by_slug(cursor=cur, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    self._ensure_drummer_exists(cursor=cur, drummer_id=drummer_slug)
                    conn.commit()
                    drummer_fk = self._get_drummer_fk_by_slug(cursor=cur, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    return out
                rollup = self.compute_drummer_profile_rollup(drummer_fk=int(drummer_fk))

            persona = self.infer_persona_from_rollup(rollup=rollup)
            preset = self.generate_preset_from_rollup(drummer_slug=drummer_slug, rollup=rollup, persona=persona)

            preset_saved = self.upsert_drummer_preset(
                preset_id=str(preset.get("preset_id") or ""),
                profile_type=str(preset.get("profile_type") or "drummer"),
                name=str(preset.get("name") or ""),
                tier=str(preset.get("tier") or "generated"),
                deltas=preset.get("deltas") if isinstance(preset.get("deltas"), dict) else {},
                policies=preset.get("policies") if isinstance(preset.get("policies"), dict) else {},
                source_type=str(preset.get("source_type") or "phase6_rollup"),
                source_song_name=None,
                source_ref=str(preset.get("source_ref") or drummer_slug),
            )

            export_path = self.export_drummer_profile_json(
                drummer_slug=drummer_slug,
                rollup=rollup,
                persona=persona,
                preset=preset,
            )

            rollup = dict(rollup or {})
            rollup["persona"] = persona
            rollup["preset_id"] = preset.get("preset_id")
            rollup["export_path"] = export_path
            if getattr(self, "_engine", None) is not None:
                rollup_saved = self.upsert_drummer_profile_rollup(
                    drummer_fk=drummer_slug,
                    rollup=rollup,
                    rollup_version="phase6_v1",
                )
            else:
                rollup_saved = self.upsert_drummer_profile_rollup(
                    drummer_fk=int(drummer_fk),
                    rollup=rollup,
                    rollup_version="phase6_v1",
                )

            out["saved_rollup"] = bool(rollup_saved)
            out["persona"] = persona
            out["preset_saved"] = bool(preset_saved)
            out["preset_id"] = preset.get("preset_id")
            out["export_path"] = export_path
            return out
        except Exception as e:
            msg = f"Phase 6 run failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def _compute_drummer_confidence(
        self,
        *,
        songs: int,
        hits: int,
        bars: int,
        section_diversity: int,
        fills: int,
        cymbal_evidence_hits: int,
    ) -> Dict[str, Any]:
        minimums = {
            "songs": 3,
            "hits": 2000,
            "bars": 120,
            "section_diversity": 3,
            "fills": 20,
            "cymbal_evidence_hits": 200,
        }
        observed = {
            "songs": int(songs or 0),
            "hits": int(hits or 0),
            "bars": int(bars or 0),
            "section_diversity": int(section_diversity or 0),
            "fills": int(fills or 0),
            "cymbal_evidence_hits": int(cymbal_evidence_hits or 0),
        }

        ratios: Dict[str, float] = {}
        for key, minimum in minimums.items():
            val = float(observed.get(key) or 0)
            den = float(max(1, minimum))
            ratios[key] = max(0.0, min(1.0, val / den))

        score = float(sum(ratios.values()) / float(len(ratios)))
        missing = [key for key, ratio in ratios.items() if ratio < 1.0]
        return {
            "score": score,
            "minimums": minimums,
            "observed": observed,
            "coverage": ratios,
            "missing_signals": missing,
            "status": "ready" if score >= 0.8 else ("developing" if score >= 0.5 else "limited"),
            "message": "Limited source material - personality transfer may be approximate."
            if score < 0.5
            else "",
        }

    def compute_drummer_profile_rollup(self, *, drummer_fk: int) -> Dict[str, Any]:
        rollup: Dict[str, Any] = {
            "drummer_id": str(drummer_fk),
            "songs": 0,
            "hits": 0,
            "instrument_counts": {},
            "instrument_shares": {},
            "fills": 0,
            "fills_per_min": None,
            "techniques": 0,
            "technique_breakdown": {},
            "timing_std_ms": None,
            "pocket_tightness": None,
            "humanness": None,
            "velocity_mean": None,
            "velocity_std": None,
            "confidence": {},
        }
        if getattr(self, "_engine", None) is not None:
            with self._engine.connect() as conn_pg:
                d = str(drummer_fk)
                try:
                    row = conn_pg.execute(
                        text("SELECT COUNT(DISTINCT analysis_id) FROM public.song_performance_analysis WHERE drummer_id = :d"),
                        {"d": d},
                    ).first()
                    rollup["songs"] = int((row or (0,))[0] or 0)
                except Exception:
                    rollup["songs"] = 0

                try:
                    row = conn_pg.execute(
                        text(
                            "SELECT COUNT(1) FROM public.drum_hit_events WHERE analysis_id IN (SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d)"
                        ),
                        {"d": d},
                    ).first()
                    rollup["hits"] = int((row or (0,))[0] or 0)
                except Exception:
                    rollup["hits"] = 0

                try:
                    res = conn_pg.execute(
                        text(
                            "SELECT instrument, COUNT(1) FROM public.drum_hit_events WHERE analysis_id IN (SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d) GROUP BY instrument ORDER BY COUNT(1) DESC"
                        ),
                        {"d": d},
                    ).fetchall() or []
                except Exception:
                    res = []
                inst_counts: Dict[str, int] = {}
                for inst, n in res:
                    inst_counts[str(inst or "")] = int(n or 0)
                rollup["instrument_counts"] = inst_counts
                total_inst = float(sum(inst_counts.values()) or 0)
                if total_inst > 0:
                    rollup["instrument_shares"] = {k: float(v) / total_inst for k, v in inst_counts.items()}

                try:
                    row = conn_pg.execute(
                        text(
                            "SELECT COUNT(1) FROM public.fill_events WHERE analysis_id IN (SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d)"
                        ),
                        {"d": d},
                    ).first()
                    rollup["fills"] = int((row or (0,))[0] or 0)
                except Exception:
                    rollup["fills"] = 0

                try:
                    row = conn_pg.execute(
                        text(
                            "SELECT COUNT(1) FROM public.technique_events WHERE analysis_id IN (SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d)"
                        ),
                        {"d": d},
                    ).first()
                    rollup["techniques"] = int((row or (0,))[0] or 0)
                except Exception:
                    rollup["techniques"] = 0

                try:
                    res = conn_pg.execute(
                        text(
                            "SELECT technique_name, COUNT(1) FROM public.technique_events WHERE analysis_id IN (SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d) GROUP BY technique_name ORDER BY COUNT(1) DESC"
                        ),
                        {"d": d},
                    ).fetchall() or []
                except Exception:
                    res = []
                tb: Dict[str, int] = {}
                for name, n in res:
                    tb[str(name or "")] = int(n or 0)
                rollup["technique_breakdown"] = tb

                try:
                    row = conn_pg.execute(
                        text(
                            "SELECT AVG(groove_micro_timing_variance), AVG(groove_pocket_tightness), AVG(humanness_score) FROM public.song_performance_analysis WHERE drummer_id = :d"
                        ),
                        {"d": d},
                    ).first() or (None, None, None)
                except Exception:
                    row = (None, None, None)
                try:
                    rollup["timing_std_ms"] = None if row[0] is None else float(row[0])
                except Exception:
                    rollup["timing_std_ms"] = None
                try:
                    rollup["pocket_tightness"] = None if row[1] is None else float(row[1])
                except Exception:
                    rollup["pocket_tightness"] = None
                try:
                    rollup["humanness"] = None if row[2] is None else float(row[2])
                except Exception:
                    rollup["humanness"] = None

                try:
                    res = conn_pg.execute(
                        text("SELECT dynamics_json FROM public.song_performance_analysis WHERE drummer_id = :d"),
                        {"d": d},
                    ).fetchall() or []
                except Exception:
                    res = []
                vel_means: List[float] = []
                vel_stds: List[float] = []
                for (dj,) in res:
                    try:
                        dyn = json.loads(dj) if isinstance(dj, str) and dj.strip() else None
                        if isinstance(dyn, dict):
                            if dyn.get("velocity_mean") is not None:
                                vel_means.append(float(dyn.get("velocity_mean")))
                            if dyn.get("velocity_std") is not None:
                                vel_stds.append(float(dyn.get("velocity_std")))
                    except Exception:
                        continue
                if vel_means:
                    rollup["velocity_mean"] = float(sum(vel_means) / float(len(vel_means)))
                if vel_stds:
                    rollup["velocity_std"] = float(sum(vel_stds) / float(len(vel_stds)))

                try:
                    res = conn_pg.execute(
                        text("SELECT duration_sec FROM public.song_performance_analysis WHERE drummer_id = :d"),
                        {"d": d},
                    ).fetchall() or []
                except Exception:
                    res = []
                total_min = 0.0
                for (dur,) in res:
                    try:
                        if dur is not None and float(dur) > 0:
                            total_min += float(dur) / 60.0
                    except Exception:
                        continue
                if total_min > 0:
                    rollup["fills_per_min"] = float(rollup["fills"]) / total_min

                try:
                    row = conn_pg.execute(
                        text(
                            "SELECT COUNT(DISTINCT bar_index) FROM public.drum_hit_events WHERE analysis_id IN (SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d)"
                        ),
                        {"d": d},
                    ).first()
                    bars = int((row or (0,))[0] or 0)
                except Exception:
                    bars = 0

                try:
                    row = conn_pg.execute(
                        text(
                            "SELECT COUNT(DISTINCT section_label) FROM public.drummer_phrase_features WHERE drummer_id = :d AND COALESCE(section_label, '') <> ''"
                        ),
                        {"d": d},
                    ).first()
                    section_diversity = int((row or (0,))[0] or 0)
                except Exception:
                    section_diversity = 0

                try:
                    row = conn_pg.execute(
                        text(
                            "SELECT COUNT(1) FROM public.drum_hit_events WHERE analysis_id IN (SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d) AND instrument IN ('hihat','ride','crash','hh')"
                        ),
                        {"d": d},
                    ).first()
                    cymbal_evidence_hits = int((row or (0,))[0] or 0)
                except Exception:
                    cymbal_evidence_hits = 0

                rollup["confidence"] = self._compute_drummer_confidence(
                    songs=int(rollup.get("songs") or 0),
                    hits=int(rollup.get("hits") or 0),
                    bars=bars,
                    section_diversity=section_diversity,
                    fills=int(rollup.get("fills") or 0),
                    cymbal_evidence_hits=cymbal_evidence_hits,
                )
                rollup["drummer_id"] = d
            return rollup
        conn = self._get_connection()
        cur = conn.cursor()

        cur.execute(
            "SELECT COUNT(DISTINCT analysis_id) FROM song_performance_analysis WHERE drummer_id = ?",
            (drummer_fk,),
        )
        rollup["songs"] = int((cur.fetchone() or [0])[0] or 0)

        cur.execute(
            """
            SELECT COUNT(1)
            FROM drum_hit_events
            WHERE analysis_id IN (
                SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ?
            )
            """,
            (drummer_fk,),
        )
        rollup["hits"] = int((cur.fetchone() or [0])[0] or 0)

        cur.execute(
            """
            SELECT instrument, COUNT(1)
            FROM drum_hit_events
            WHERE analysis_id IN (
                SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ?
            )
            GROUP BY instrument
            ORDER BY COUNT(1) DESC
            """,
            (drummer_fk,),
        )
        inst_counts: Dict[str, int] = {}
        for inst, n in cur.fetchall() or []:
            inst_counts[str(inst or "")] = int(n or 0)
        rollup["instrument_counts"] = inst_counts
        total_inst = float(sum(inst_counts.values()) or 0)
        if total_inst > 0:
            rollup["instrument_shares"] = {k: float(v) / total_inst for k, v in inst_counts.items()}

        cur.execute(
            """
            SELECT COUNT(1)
            FROM fill_events
            WHERE analysis_id IN (
                SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ?
            )
            """,
            (drummer_fk,),
        )
        rollup["fills"] = int((cur.fetchone() or [0])[0] or 0)

        cur.execute(
            """
            SELECT COUNT(1)
            FROM technique_events
            WHERE analysis_id IN (
                SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ?
            )
            """,
            (drummer_fk,),
        )
        rollup["techniques"] = int((cur.fetchone() or [0])[0] or 0)

        cur.execute(
            """
            SELECT technique_name, COUNT(1)
            FROM technique_events
            WHERE analysis_id IN (
                SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ?
            )
            GROUP BY technique_name
            ORDER BY COUNT(1) DESC
            """,
            (drummer_fk,),
        )
        tb: Dict[str, int] = {}
        for name, n in cur.fetchall() or []:
            tb[str(name or "")] = int(n or 0)
        rollup["technique_breakdown"] = tb

        cur.execute(
            """
            SELECT
                AVG(groove_micro_timing_variance),
                AVG(groove_pocket_tightness),
                AVG(humanness_score)
            FROM song_performance_analysis
            WHERE drummer_id = ?
            """,
            (drummer_fk,),
        )
        row = cur.fetchone() or (None, None, None)
        try:
            rollup["timing_std_ms"] = None if row[0] is None else float(row[0])
        except Exception:
            rollup["timing_std_ms"] = None
        try:
            rollup["pocket_tightness"] = None if row[1] is None else float(row[1])
        except Exception:
            rollup["pocket_tightness"] = None
        try:
            rollup["humanness"] = None if row[2] is None else float(row[2])
        except Exception:
            rollup["humanness"] = None

        cur.execute(
            "SELECT dynamics_json FROM song_performance_analysis WHERE drummer_id = ?",
            (drummer_fk,),
        )
        vel_means: List[float] = []
        vel_stds: List[float] = []
        for (dj,) in cur.fetchall() or []:
            try:
                d = json.loads(dj) if isinstance(dj, str) and dj.strip() else None
                if isinstance(d, dict):
                    if d.get("velocity_mean") is not None:
                        vel_means.append(float(d.get("velocity_mean")))
                    if d.get("velocity_std") is not None:
                        vel_stds.append(float(d.get("velocity_std")))
            except Exception:
                continue
        if vel_means:
            rollup["velocity_mean"] = float(sum(vel_means) / float(len(vel_means)))
        if vel_stds:
            rollup["velocity_std"] = float(sum(vel_stds) / float(len(vel_stds)))

        cur.execute(
            "SELECT duration_sec FROM song_performance_analysis WHERE drummer_id = ?",
            (drummer_fk,),
        )
        total_min = 0.0
        for (dur,) in cur.fetchall() or []:
            try:
                if dur is not None and float(dur) > 0:
                    total_min += float(dur) / 60.0
            except Exception:
                continue
        if total_min > 0:
            rollup["fills_per_min"] = float(rollup["fills"]) / total_min

        cur.execute(
            "SELECT COUNT(DISTINCT bar_index) FROM drum_hit_events WHERE analysis_id IN (SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ?)",
            (drummer_fk,),
        )
        bars = int((cur.fetchone() or [0])[0] or 0)

        cur.execute(
            "SELECT COUNT(DISTINCT section_label) FROM drummer_phrase_features WHERE drummer_id = ? AND COALESCE(section_label, '') <> ''",
            (drummer_fk,),
        )
        section_diversity = int((cur.fetchone() or [0])[0] or 0)

        cur.execute(
            "SELECT COUNT(1) FROM drum_hit_events WHERE analysis_id IN (SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ?) AND instrument IN ('hihat', 'ride', 'crash', 'hh')",
            (drummer_fk,),
        )
        cymbal_evidence_hits = int((cur.fetchone() or [0])[0] or 0)

        rollup["confidence"] = self._compute_drummer_confidence(
            songs=int(rollup.get("songs") or 0),
            hits=int(rollup.get("hits") or 0),
            bars=bars,
            section_diversity=section_diversity,
            fills=int(rollup.get("fills") or 0),
            cymbal_evidence_hits=cymbal_evidence_hits,
        )

        return rollup

    def _upsert_profile_row(
        self,
        *,
        table: str,
        profile_id: str,
        drummer_fk: int,
        payload: Dict[str, Any],
    ) -> bool:
        try:
            table_name = str(table or "").strip()
            if not table_name:
                return False
            columns = [
                col
                for col in self._table_columns(table_name)
                if col not in {"id", "drummer_id", "created_at"}
            ]
            if not columns:
                return False

            now = datetime.utcnow().isoformat()
            values: List[Any] = []
            for col in columns:
                val = payload.get(col)
                if isinstance(val, (dict, list)):
                    val = self._json_dumps(val)
                values.append(val)
            if getattr(self, "_engine", None) is not None:
                col_list = ["id", "drummer_id"] + columns + ["created_at"]
                updates = ", ".join([f"{col}=EXCLUDED.{col}" for col in columns] + ["drummer_id=EXCLUDED.drummer_id"])
                params = {"id": profile_id, "drummer_id": str(drummer_fk)}
                for i, col in enumerate(columns):
                    params[col] = values[i]
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            f"""
                            INSERT INTO public.{table_name} ({', '.join(col_list)})
                            VALUES (:id, :drummer_id, {', '.join(':' + c for c in columns)}, NOW())
                            ON CONFLICT (id) DO UPDATE SET {updates}
                            """
                        ),
                        params,
                    )
                self.data_changed.emit(table_name, "upsert")
                return True

            conn = self._get_connection()
            cur = conn.cursor()
            placeholders = ", ".join(["?"] * (3 + len(columns)))
            col_list_sql = ", ".join(["id", "drummer_id"] + columns + ["created_at"])
            updates = ", ".join([f"{col}=excluded.{col}" for col in columns])
            sql = f"""
                INSERT INTO {table_name} ({col_list_sql})
                VALUES ({placeholders})
                ON CONFLICT(id) DO UPDATE SET
                    drummer_id=excluded.drummer_id,
                    {updates}
            """

            cur.execute(sql, [profile_id, drummer_fk, *values, now])
            conn.commit()
            self.data_changed.emit(table_name, "upsert")
            return True
        except Exception as e:
            logger.error(f"Error upserting profile row into {table}: {e}")
            self.database_error.emit(f"Error upserting profile row into {table}: {e}")
            return False

    def upsert_drummer_personality_embedding(
        self,
        *,
        embedding_id: str,
        drummer_fk: int,
        model_version: str,
        embedding_vector: List[float],
        confidence_score: float,
        source_song_count: int,
        source_hit_count: int,
        timing_weight: float,
        dynamics_weight: float,
        fill_weight: float,
        cymbal_weight: float,
        coordination_weight: float,
        phrase_weight: float,
    ) -> bool:
        payload = {
            "model_version": str(model_version or "v1").strip() or "v1",
            "embedding_dim": int(len(embedding_vector or [])),
            "embedding_vector_json": self._json_dumps(list(embedding_vector or [])) or "[]",
            "source_song_count": int(source_song_count or 0),
            "source_hit_count": int(source_hit_count or 0),
            "confidence_score": float(confidence_score or 0.0),
            "timing_weight": float(timing_weight or 0.0),
            "dynamics_weight": float(dynamics_weight or 0.0),
            "fill_weight": float(fill_weight or 0.0),
            "cymbal_weight": float(cymbal_weight or 0.0),
            "coordination_weight": float(coordination_weight or 0.0),
            "phrase_weight": float(phrase_weight or 0.0),
        }
        return self._upsert_profile_row(
            table="drummer_personality_embeddings",
            profile_id=str(embedding_id or str(uuid.uuid4())),
            drummer_fk=drummer_fk,
            payload=payload,
        )

    def log_generated_transform_audit(
        self,
        *,
        audit_id: str,
        target_drummer_fk: Optional[int],
        generation_run_id: Optional[str],
        personality_embedding_id: Optional[str],
        source_similarity_score: Optional[float],
        target_similarity_score: Optional[float],
        human_feasibility_score: Optional[float],
        groove_preservation_score: Optional[float],
        before_features: Optional[Dict[str, Any]] = None,
        after_features: Optional[Dict[str, Any]] = None,
        transform_delta: Optional[Dict[str, Any]] = None,
        source_track_id: Optional[str] = None,
    ) -> bool:
        try:
            conn = self._get_connection()
            cur = conn.cursor()
            now = datetime.utcnow().isoformat()
            cur.execute(
                """
                INSERT INTO generated_drummer_transform_audits (
                    id, source_track_id, target_drummer_id, generation_run_id,
                    personality_embedding_id, source_similarity_score, target_similarity_score,
                    human_feasibility_score, groove_preservation_score,
                    before_features_json, after_features_json, transform_delta_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_track_id=excluded.source_track_id,
                    target_drummer_id=excluded.target_drummer_id,
                    generation_run_id=excluded.generation_run_id,
                    personality_embedding_id=excluded.personality_embedding_id,
                    source_similarity_score=excluded.source_similarity_score,
                    target_similarity_score=excluded.target_similarity_score,
                    human_feasibility_score=excluded.human_feasibility_score,
                    groove_preservation_score=excluded.groove_preservation_score,
                    before_features_json=excluded.before_features_json,
                    after_features_json=excluded.after_features_json,
                    transform_delta_json=excluded.transform_delta_json
                """,
                (
                    str(audit_id or str(uuid.uuid4())),
                    source_track_id,
                    int(target_drummer_fk) if target_drummer_fk is not None else None,
                    generation_run_id,
                    personality_embedding_id,
                    self._safe_float(source_similarity_score),
                    self._safe_float(target_similarity_score),
                    self._safe_float(human_feasibility_score),
                    self._safe_float(groove_preservation_score),
                    self._json_dumps(before_features or {}),
                    self._json_dumps(after_features or {}),
                    self._json_dumps(transform_delta or {}),
                    now,
                ),
            )
            conn.commit()
            self.data_changed.emit("generated_drummer_transform_audits", "upsert")
            return True
        except Exception as e:
            logger.error(f"Error logging transform audit: {e}")
            self.database_error.emit(f"Error logging transform audit: {e}")
            return False

    def upsert_app_user_role(self, *, user_id: str, role: str) -> bool:
        try:
            user_id = (user_id or "").strip()
            role = (role or "").strip()
            if not user_id or not role:
                return False
            conn = self._get_connection()
            cur = conn.cursor()
            now = datetime.utcnow().isoformat()
            cur.execute(
                """
                INSERT INTO app_user_roles (user_id, role, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET role=excluded.role
                """,
                (user_id, role, now),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error upserting app_user_role: {e}")
            self.database_error.emit(f"Error upserting app_user_role: {e}")
            return False

    def map_user_to_drummer(self, *, user_id: str, drummer_id: str) -> bool:
        try:
            user_id = (user_id or "").strip()
            drummer_id = (drummer_id or "").strip()
            if not user_id or not drummer_id:
                return False
            conn = self._get_connection()
            cur = conn.cursor()
            now = datetime.utcnow().isoformat()
            cur.execute(
                """
                INSERT INTO user_drummer_map (user_id, drummer_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, drummer_id) DO NOTHING
                """,
                (user_id, drummer_id, now),
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error mapping user to drummer: {e}")
            self.database_error.emit(f"Error mapping user to drummer: {e}")
            return False

    def user_has_access_to_drummer(self, *, user_id: str, drummer_id: str) -> bool:
        try:
            user_id = (user_id or "").strip()
            drummer_id = (drummer_id or "").strip()
            if not user_id or not drummer_id:
                return False
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1 FROM user_drummer_map WHERE user_id = ? AND drummer_id = ? LIMIT 1
                """,
                (user_id, drummer_id),
            )
            row = cur.fetchone()
            if row:
                return True
            # admin override
            cur.execute(
                """
                SELECT 1 FROM app_user_roles WHERE user_id = ? AND role = 'qa_admin' LIMIT 1
                """,
                (user_id,),
            )
            return bool(cur.fetchone())
        except Exception as e:
            logger.error(f"Error checking access: {e}")
            self.database_error.emit(f"Error checking access: {e}")
            return False

    def log_calibration_audit(
        self,
        *,
        actor_user_id: Optional[str],
        drummer_id: Optional[str],
        run_id: Optional[str],
        action: str,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        try:
            audit_id = str(uuid.uuid4())
            conn = self._get_connection()
            cur = conn.cursor()
            now = datetime.utcnow().isoformat()
            cur.execute(
                """
                INSERT INTO calibration_audit_log (
                    id, actor_user_id, drummer_id, run_id, action, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    (actor_user_id or None),
                    (drummer_id or None),
                    (run_id or None),
                    (action or "").strip(),
                    self._json_dumps(payload or {}),
                    now,
                ),
            )
            conn.commit()
            return audit_id
        except Exception as e:
            logger.error(f"Error logging calibration audit: {e}")
            self.database_error.emit(f"Error logging calibration audit: {e}")
            return None

    def create_analysis_job(self, *, drummer_id: str, input_json: Optional[Dict[str, Any]] = None) -> Optional[str]:
        try:
            job_id = str(uuid.uuid4())
            conn = self._get_connection()
            cur = conn.cursor()
            now = datetime.utcnow().isoformat()
            cur.execute(
                """
                INSERT INTO analysis_jobs (
                    id, drummer_id, status, input_json, result_json, error_text, created_at, updated_at
                ) VALUES (?, ?, 'queued', ?, NULL, NULL, ?, ?)
                """,
                (job_id, (drummer_id or "").strip(), self._json_dumps(input_json or {}), now, now),
            )
            conn.commit()
            return job_id
        except Exception as e:
            logger.error(f"Error creating analysis job: {e}")
            self.database_error.emit(f"Error creating analysis job: {e}")
            return None

    def get_analysis_job(self, *, job_id: str) -> Optional[Dict[str, Any]]:
        try:
            job_id = (job_id or "").strip()
            if not job_id:
                return None
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, drummer_id, status, input_json, result_json, error_text, created_at, updated_at
                FROM analysis_jobs WHERE id = ? LIMIT 1
                """,
                (job_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "id": row["id"],
                "drummer_id": row["drummer_id"],
                "status": row["status"],
                "input_json": row["input_json"],
                "result_json": row["result_json"],
                "error_text": row["error_text"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        except Exception as e:
            logger.error(f"Error fetching analysis job: {e}")
            self.database_error.emit(f"Error fetching analysis job: {e}")
            return None

    def update_analysis_job_status(
        self,
        *,
        job_id: str,
        status: str,
        result_json: Optional[Dict[str, Any]] = None,
        error_text: Optional[str] = None,
    ) -> bool:
        try:
            job_id = (job_id or "").strip()
            status = (status or "").strip()
            if not job_id or not status:
                return False
            conn = self._get_connection()
            cur = conn.cursor()
            now = datetime.utcnow().isoformat()
            cur.execute(
                """
                UPDATE analysis_jobs
                SET status = ?, result_json = COALESCE(?, result_json), error_text = COALESCE(?, error_text), updated_at = ?
                WHERE id = ?
                """,
                (status, self._json_dumps(result_json or None), (error_text or None), now, job_id),
            )
            conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            logger.error(f"Error updating analysis job: {e}")
            self.database_error.emit(f"Error updating analysis job: {e}")
            return False

    @staticmethod
    def _tokenize_profile_id(value: Any) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return "unknown"
        out_chars: List[str] = []
        for ch in text:
            if ch.isalnum():
                out_chars.append(ch)
            else:
                out_chars.append("_")
        token = "".join(out_chars)
        while "__" in token:
            token = token.replace("__", "_")
        return token.strip("_") or "unknown"

    @staticmethod
    def _basic_stats(values: List[float]) -> Dict[str, float]:
        if not values:
            return {"mean": 0.0, "std": 0.0, "skew": 0.0}
        mean_val = float(sum(values) / float(len(values)))
        if len(values) <= 1:
            return {"mean": mean_val, "std": 0.0, "skew": 0.0}
        var = float(sum((v - mean_val) ** 2 for v in values) / float(len(values)))
        std = float(var ** 0.5)
        if std <= 1e-9:
            skew = 0.0
        else:
            skew = float(sum(((v - mean_val) / std) ** 3 for v in values) / float(len(values)))
        return {"mean": mean_val, "std": std, "skew": skew}

    @staticmethod
    def _histogram(values: List[float], *, bins: int = 12) -> Dict[str, Any]:
        if not values:
            return {"bins": [], "counts": []}
        n_bins = max(3, int(bins))
        v_min = float(min(values))
        v_max = float(max(values))
        if abs(v_max - v_min) < 1e-9:
            return {"bins": [v_min, v_max], "counts": [len(values)]}
        width = (v_max - v_min) / float(n_bins)
        edges = [v_min + (width * i) for i in range(n_bins + 1)]
        counts = [0 for _ in range(n_bins)]
        for v in values:
            idx = int((float(v) - v_min) / width)
            if idx < 0:
                idx = 0
            if idx >= n_bins:
                idx = n_bins - 1
            counts[idx] += 1
        return {"bins": edges, "counts": counts}

    def run_phase7_assimilation_profiles_for_drummer(self, *, drummer_slug: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "drummer_slug": drummer_slug,
            "saved": False,
            "drummer_fk": None,
            "profiles": {},
            "embedding": {},
        }
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return out
            if getattr(self, "_engine", None) is not None:
                drummer_fk = drummer_slug
                out["drummer_fk"] = drummer_fk

                rollup = self.compute_drummer_profile_rollup(drummer_fk=drummer_fk)
                conf = rollup.get("confidence") if isinstance(rollup.get("confidence"), dict) else {}
                confidence_score = float(conf.get("score") or 0.0)

                section_labels = ["intro", "verse", "prechorus", "chorus", "bridge", "solo", "outro"]

                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text(
                            """
                            SELECT analysis_id, song_id, tempo_bpm, time_signature, duration_sec
                            FROM public.song_performance_analysis
                            WHERE drummer_id = :d
                            ORDER BY created_at DESC
                            """
                        ),
                        {"d": drummer_fk},
                    ).fetchall() or []
                analyses = [
                    {
                        "analysis_id": r[0],
                        "song_id": r[1],
                        "tempo_bpm": r[2],
                        "time_signature": r[3],
                        "duration_sec": r[4],
                    }
                    for r in rows
                ]

                def _ensure_song_id(analysis_id: str, current_song_id: Any) -> Optional[str]:
                    sid = str(current_song_id or "").strip()
                    if sid:
                        return sid
                    synthetic_id = f"synthetic_{analysis_id}"
                    try:
                        with self._engine.begin() as conn_pg:
                            conn_pg.execute(
                                text(
                                    """
                                    INSERT INTO public.songs (
                                        id, title, artist, album, year, genre, duration, file_path, drummer_id, created_at, updated_at
                                    ) VALUES (
                                        :id, :title, NULL, NULL, NULL, :genre, NULL, NULL, :drummer_id, NOW(), NOW()
                                    )
                                    ON CONFLICT (id) DO UPDATE SET
                                        title = EXCLUDED.title,
                                        duration = COALESCE(public.songs.duration, EXCLUDED.duration),
                                        updated_at = NOW()
                                    """
                                ),
                                {
                                    "id": synthetic_id,
                                    "title": f"Assimilation Source {analysis_id[:8]}",
                                    "genre": "unknown",
                                    "drummer_id": drummer_fk,
                                },
                            )
                            conn_pg.execute(
                                text("UPDATE public.song_performance_analysis SET song_id = :sid WHERE analysis_id = :aid"),
                                {"sid": synthetic_id, "aid": analysis_id},
                            )
                        return synthetic_id
                    except Exception:
                        return None

                with self._engine.begin() as conn_pg:
                    for table in (
                        "drummer_phrase_features",
                        "drummer_microtiming_profiles",
                        "drummer_dynamic_profiles",
                        "drummer_cymbal_language",
                        "drummer_limb_coordination",
                        "drummer_fill_behavior",
                    ):
                        conn_pg.execute(text(f"DELETE FROM public.{table} WHERE drummer_id = :d"), {"d": drummer_fk})

                with self._engine.connect() as conn_pg:
                    hit_res = conn_pg.execute(
                        text(
                            """
                            SELECT e.analysis_id, e.instrument, e.component, e.onset_time_sec, e.velocity_est,
                                   e.timing_offset_ms, e.bar_index, e.subdivision, e.is_ghost, e.is_accent
                            FROM public.drum_hit_events e
                            JOIN public.song_performance_analysis s ON s.analysis_id = e.analysis_id
                            WHERE s.drummer_id = :d
                            ORDER BY e.analysis_id, e.onset_time_sec ASC
                            """
                        ),
                        {"d": drummer_fk},
                    ).fetchall() or []
                    hit_rows: List[Dict[str, Any]] = [
                        {
                            "analysis_id": r[0],
                            "instrument": r[1],
                            "component": r[2],
                            "onset_time_sec": r[3],
                            "velocity_est": r[4],
                            "timing_offset_ms": r[5],
                            "bar_index": r[6],
                            "subdivision": r[7],
                            "is_ghost": r[8],
                            "is_accent": r[9],
                        }
                        for r in hit_res
                    ]
                    fill_res = conn_pg.execute(
                        text(
                            """
                            SELECT e.analysis_id, e.start_time_sec, e.end_time_sec, e.start_bar_index, e.hit_count, e.instruments_json
                            FROM public.fill_events e
                            JOIN public.song_performance_analysis s ON s.analysis_id = e.analysis_id
                            WHERE s.drummer_id = :d
                            ORDER BY e.analysis_id, e.start_time_sec ASC
                            """
                        ),
                        {"d": drummer_fk},
                    ).fetchall() or []
                    fill_rows: List[Dict[str, Any]] = [
                        {
                            "analysis_id": r[0],
                            "start_time_sec": r[1],
                            "end_time_sec": r[2],
                            "start_bar_index": r[3],
                            "hit_count": r[4],
                            "instruments_json": r[5],
                        }
                        for r in fill_res
                    ]
            else:
                conn = self._get_connection()
                cur = conn.cursor()
                drummer_fk = self._get_drummer_fk_by_slug(cursor=cur, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    self._ensure_drummer_exists(cursor=cur, drummer_id=drummer_slug)
                    conn.commit()
                    drummer_fk = self._get_drummer_fk_by_slug(cursor=cur, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    return out
                drummer_fk = int(drummer_fk)
                out["drummer_fk"] = drummer_fk

                rollup = self.compute_drummer_profile_rollup(drummer_fk=drummer_fk)
                conf = rollup.get("confidence") if isinstance(rollup.get("confidence"), dict) else {}
                confidence_score = float(conf.get("score") or 0.0)

                section_labels = ["intro", "verse", "prechorus", "chorus", "bridge", "solo", "outro"]

                cur.execute(
                    """
                    SELECT analysis_id, song_id, tempo_bpm, time_signature, duration_sec
                    FROM song_performance_analysis
                    WHERE drummer_id = ?
                    ORDER BY created_at DESC
                    """,
                    (drummer_fk,),
                )
                analyses = cur.fetchall() or []
                analysis_ids = [str(r["analysis_id"]).strip() for r in analyses if r["analysis_id"]]

                def _ensure_song_id(analysis_id: str, current_song_id: Any) -> Optional[str]:
                    sid = str(current_song_id or "").strip()
                    if sid:
                        return sid
                    synthetic_id = f"synthetic_{analysis_id}"
                    now = datetime.utcnow().isoformat()
                    try:
                        cur.execute(
                            """
                            INSERT INTO songs (id, title, artist, album, year, genre, duration, file_path, drummer_id, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET
                                title=excluded.title,
                                duration=COALESCE(songs.duration, excluded.duration),
                                updated_at=excluded.updated_at
                            """,
                            (
                                synthetic_id,
                                f"Assimilation Source {analysis_id[:8]}",
                                None,
                                None,
                                None,
                                "unknown",
                                None,
                                None,
                                drummer_fk,
                                now,
                                now,
                            ),
                        )
                        cur.execute(
                            "UPDATE song_performance_analysis SET song_id = ? WHERE analysis_id = ?",
                            (synthetic_id, analysis_id),
                        )
                        conn.commit()
                        return synthetic_id
                    except Exception:
                        return None

                for table in (
                    "drummer_phrase_features",
                    "drummer_microtiming_profiles",
                    "drummer_dynamic_profiles",
                    "drummer_cymbal_language",
                    "drummer_limb_coordination",
                    "drummer_fill_behavior",
                ):
                    try:
                        cur.execute(f"DELETE FROM {table} WHERE drummer_id = ?", (drummer_fk,))
                    except Exception:
                        continue
                conn.commit()

                hit_rows: List[Dict[str, Any]] = []
                fill_rows: List[Dict[str, Any]] = []
                if analysis_ids:
                    placeholders = ", ".join(["?"] * len(analysis_ids))
                    cur.execute(
                        f"""
                        SELECT analysis_id, instrument, component, onset_time_sec, velocity_est,
                               timing_offset_ms, bar_index, subdivision, is_ghost, is_accent
                        FROM drum_hit_events
                        WHERE analysis_id IN ({placeholders})
                        ORDER BY analysis_id, onset_time_sec ASC
                        """,
                        tuple(analysis_ids),
                    )
                    hit_rows = [self._row_to_dict(r) for r in (cur.fetchall() or [])]

                    cur.execute(
                        f"""
                        SELECT analysis_id, start_time_sec, end_time_sec, start_bar_index, hit_count, instruments_json
                        FROM fill_events
                        WHERE analysis_id IN ({placeholders})
                        ORDER BY analysis_id, start_time_sec ASC
                        """,
                        tuple(analysis_ids),
                    )
                    fill_rows = [self._row_to_dict(r) for r in (cur.fetchall() or [])]

            analysis_hits: Dict[str, List[Dict[str, Any]]] = {}
            for row in hit_rows:
                aid = str(row.get("analysis_id") or "").strip()
                if aid:
                    analysis_hits.setdefault(aid, []).append(row)

            analysis_fills: Dict[str, List[Dict[str, Any]]] = {}
            for row in fill_rows:
                aid = str(row.get("analysis_id") or "").strip()
                if aid:
                    analysis_fills.setdefault(aid, []).append(row)

            phrase_saved = 0
            phrase_status = "saved"
            for analysis_row in analyses:
                analysis_id = str(analysis_row["analysis_id"] or "").strip()
                if not analysis_id:
                    continue
                song_id = _ensure_song_id(analysis_id, analysis_row["song_id"])
                if not song_id:
                    phrase_status = "save_failed"
                    continue
                hits = analysis_hits.get(analysis_id, [])
                if not hits:
                    continue
                bars_present = sorted({int(h.get("bar_index") or 0) for h in hits if h.get("bar_index") is not None})
                if not bars_present:
                    bars_present = [0]
                max_bar = max(bars_present)
                phrase_len = 4
                total_phrases = max(1, (max_bar // phrase_len) + 1)
                for phrase_idx in range(total_phrases):
                    start_bar = phrase_idx * phrase_len
                    end_bar = start_bar + phrase_len
                    phrase_events = [h for h in hits if start_bar <= int(h.get("bar_index") or 0) < end_bar]
                    if not phrase_events:
                        continue

                    density_curve: List[float] = []
                    accent_curve: List[float] = []
                    signatures: List[str] = []
                    for bar in range(start_bar, end_bar):
                        bar_events = [h for h in phrase_events if int(h.get("bar_index") or 0) == bar]
                        density_curve.append(float(len(bar_events)))
                        bar_vel = [float(h.get("velocity_est")) for h in bar_events if h.get("velocity_est") is not None]
                        accent_curve.append(float(sum(bar_vel) / len(bar_vel)) if bar_vel else 0.0)
                        inst_counts: Dict[str, int] = {}
                        for ev in bar_events:
                            inst = str(ev.get("instrument") or "unknown")
                            inst_counts[inst] = int(inst_counts.get(inst) or 0) + 1
                        signatures.append(self._json_dumps(inst_counts) or "{}")

                    sig_counts: Dict[str, int] = {}
                    for sig in signatures:
                        sig_counts[sig] = int(sig_counts.get(sig) or 0) + 1
                    repetition = float(max(sig_counts.values()) / max(1, len(signatures))) if sig_counts else 0.0
                    mutation = 1.0 - repetition
                    energy_start = accent_curve[0] if accent_curve else 0.0
                    energy_end = accent_curve[-1] if accent_curve else 0.0
                    energy_slope = float((energy_end - energy_start) / max(1, phrase_len))
                    section_label = section_labels[phrase_idx % len(section_labels)]

                    payload = {
                        "analysis_id": analysis_id,
                        "song_id": song_id,
                        "section_label": section_label,
                        "phrase_index": phrase_idx,
                        "phrase_length_bars": phrase_len,
                        "bar_position_in_phrase": 0,
                        "energy_start": energy_start,
                        "energy_end": energy_end,
                        "energy_slope": energy_slope,
                        "pattern_repetition_score": repetition,
                        "pattern_mutation_rate": mutation,
                        "density_curve_json": density_curve,
                        "accent_curve_json": accent_curve,
                    }
                    pid = f"phrase_{drummer_fk}_{analysis_id}_{phrase_idx}"
                    if self._upsert_profile_row(
                        table="drummer_phrase_features",
                        profile_id=pid,
                        drummer_fk=drummer_fk,
                        payload=payload,
                    ):
                        phrase_saved += 1

            fill_windows: Dict[str, List[Tuple[float, float]]] = {}
            for row in fill_rows:
                aid = str(row.get("analysis_id") or "").strip()
                if not aid:
                    continue
                try:
                    st = float(row.get("start_time_sec") or 0.0)
                    en = float(row.get("end_time_sec") or st)
                except Exception:
                    continue
                fill_windows.setdefault(aid, []).append((st, en))

            micro_groups: Dict[Tuple[str, str, str], List[float]] = {}
            for row in hit_rows:
                try:
                    offset = float(row.get("timing_offset_ms")) if row.get("timing_offset_ms") is not None else None
                except Exception:
                    offset = None
                if offset is None:
                    continue
                aid = str(row.get("analysis_id") or "").strip()
                inst = self._tokenize_profile_id(row.get("instrument") or "unknown")
                subdiv = self._tokenize_profile_id(row.get("subdivision") or "unknown")
                ctx = "groove"
                if bool(row.get("is_ghost")):
                    ctx = "ghost"
                elif bool(row.get("is_accent")):
                    ctx = "accent"
                else:
                    try:
                        t = float(row.get("onset_time_sec") or 0.0)
                    except Exception:
                        t = None
                    if t is not None:
                        for ws, we in fill_windows.get(aid, []):
                            if ws <= t <= we:
                                ctx = "fill"
                                break
                micro_groups.setdefault((inst, subdiv, ctx), []).append(offset)

            micro_saved = 0
            for (inst, subdiv, ctx), values in micro_groups.items():
                stats = self._basic_stats(values)
                early = float(sum(1 for v in values if v < 0.0) / max(1, len(values)))
                late = float(sum(1 for v in values if v > 0.0) / max(1, len(values)))
                if stats["mean"] > 2.0:
                    pocket_bias = "behind"
                elif stats["mean"] < -2.0:
                    pocket_bias = "ahead"
                elif abs(stats["mean"]) <= 1.0:
                    pocket_bias = "centered"
                else:
                    pocket_bias = "mixed"
                payload = {
                    "instrument": inst,
                    "subdivision": subdiv,
                    "mean_offset_ms": stats["mean"],
                    "std_offset_ms": stats["std"],
                    "skew_offset_ms": stats["skew"],
                    "early_hit_probability": early,
                    "late_hit_probability": late,
                    "pocket_bias": pocket_bias,
                    "context_label": ctx,
                    "histogram_json": self._histogram(values),
                }
                pid = f"micro_{drummer_fk}_{inst}_{subdiv}_{ctx}"
                if self._upsert_profile_row(
                    table="drummer_microtiming_profiles",
                    profile_id=pid,
                    drummer_fk=drummer_fk,
                    payload=payload,
                ):
                    micro_saved += 1

            dynamic_groups: Dict[str, List[float]] = {}
            dynamic_ghost: Dict[str, int] = {}
            dynamic_accent: Dict[str, int] = {}
            dynamic_total: Dict[str, int] = {}
            accent_grid: Dict[str, Dict[str, int]] = {}
            for row in hit_rows:
                inst = self._tokenize_profile_id(row.get("instrument") or "unknown")
                try:
                    vel = float(row.get("velocity_est")) if row.get("velocity_est") is not None else None
                except Exception:
                    vel = None
                if vel is not None:
                    dynamic_groups.setdefault(inst, []).append(vel)
                dynamic_total[inst] = int(dynamic_total.get(inst) or 0) + 1
                if bool(row.get("is_ghost")):
                    dynamic_ghost[inst] = int(dynamic_ghost.get(inst) or 0) + 1
                if bool(row.get("is_accent")):
                    dynamic_accent[inst] = int(dynamic_accent.get(inst) or 0) + 1
                subdiv = self._tokenize_profile_id(row.get("subdivision") or "unknown")
                bucket = accent_grid.setdefault(inst, {})
                if bool(row.get("is_accent")):
                    bucket[subdiv] = int(bucket.get(subdiv) or 0) + 1

            dynamics_saved = 0
            for inst, velocities in dynamic_groups.items():
                stats = self._basic_stats(velocities)
                total_inst = int(dynamic_total.get(inst) or len(velocities) or 1)
                ghost_prob = float((dynamic_ghost.get(inst) or 0) / max(1, total_inst))
                accent_prob = float((dynamic_accent.get(inst) or 0) / max(1, total_inst))
                payload = {
                    "instrument": inst,
                    "velocity_mean": stats["mean"],
                    "velocity_std": stats["std"],
                    "velocity_skew": stats["skew"],
                    "ghost_note_probability": ghost_prob,
                    "accent_probability": accent_prob,
                    "ghost_to_accent_ratio": float(ghost_prob / max(1e-6, accent_prob)),
                    "accent_grid_json": accent_grid.get(inst) or {},
                    "velocity_histogram_json": self._histogram(velocities),
                    "phrase_dynamic_curve_json": [stats["mean"]],
                }
                pid = f"dyn_{drummer_fk}_{inst}"
                if self._upsert_profile_row(
                    table="drummer_dynamic_profiles",
                    profile_id=pid,
                    drummer_fk=drummer_fk,
                    payload=payload,
                ):
                    dynamics_saved += 1

            cym_total = len(hit_rows)
            hihat_total = 0
            hihat_open = 0
            hihat_pedal = 0
            hihat_bark = 0
            ride_total = 0
            ride_bell = 0
            crash_total = 0
            crash_downbeat = 0
            transition_crash = 0
            crash_times: List[float] = []
            section_crash_counts: Dict[str, int] = {}
            total_minutes = 0.0
            for row in analyses:
                try:
                    dur = float(row["duration_sec"]) if row["duration_sec"] is not None else 0.0
                except Exception:
                    dur = 0.0
                if dur > 0:
                    total_minutes += dur / 60.0

            for row in hit_rows:
                inst = str(row.get("instrument") or "").lower()
                comp = str(row.get("component") or "").lower()
                subdiv = str(row.get("subdivision") or "").lower()
                try:
                    onset = float(row.get("onset_time_sec") or 0.0)
                except Exception:
                    onset = 0.0
                if inst in {"hihat", "hh"}:
                    hihat_total += 1
                    if "open" in comp:
                        hihat_open += 1
                    if "pedal" in comp or "foot" in comp:
                        hihat_pedal += 1
                    if "bark" in comp:
                        hihat_bark += 1
                if inst == "ride":
                    ride_total += 1
                    if "bell" in comp:
                        ride_bell += 1
                if inst == "crash":
                    crash_total += 1
                    crash_times.append(onset)
                    if subdiv in {"0", "1", "downbeat", "quarter"}:
                        crash_downbeat += 1
                    aid = str(row.get("analysis_id") or "").strip()
                    is_transition = False
                    for ws, we in fill_windows.get(aid, []):
                        if (ws - 0.25) <= onset <= (we + 0.25):
                            is_transition = True
                            break
                    if is_transition:
                        transition_crash += 1
                    section = section_labels[int(row.get("bar_index") or 0) % len(section_labels)]
                    section_crash_counts[section] = int(section_crash_counts.get(section) or 0) + 1

            decay_spacing_score = 0.0
            if len(crash_times) > 1:
                diffs = [max(0.0, crash_times[i + 1] - crash_times[i]) for i in range(len(crash_times) - 1)]
                if diffs:
                    avg_spacing = float(sum(diffs) / len(diffs))
                    decay_spacing_score = max(0.0, min(1.0, avg_spacing / 1.5))

            cym_payload = {
                "hihat_closed_ratio": float((hihat_total - hihat_open) / max(1, hihat_total)),
                "hihat_open_ratio": float(hihat_open / max(1, hihat_total)),
                "hihat_pedal_ratio": float(hihat_pedal / max(1, hihat_total)),
                "hihat_bark_probability": float(hihat_bark / max(1, hihat_total)),
                "ride_usage_ratio": float(ride_total / max(1, cym_total)),
                "ride_bell_probability": float(ride_bell / max(1, ride_total)),
                "crash_frequency_per_min": float(crash_total / max(1e-6, total_minutes)),
                "crash_on_downbeat_probability": float(crash_downbeat / max(1, crash_total)),
                "crash_on_transition_probability": float(transition_crash / max(1, crash_total)),
                "cymbal_decay_spacing_score": decay_spacing_score,
                "cymbal_density_curve_json": section_crash_counts,
            }
            cym_saved = self._upsert_profile_row(
                table="drummer_cymbal_language",
                profile_id=f"cym_{drummer_fk}",
                drummer_fk=drummer_fk,
                payload=cym_payload,
            )

            simultaneous_matrix: Dict[str, int] = {}
            timeslots: Dict[Tuple[str, float], List[str]] = {}
            kick_hits = 0
            snare_hits = 0
            hat_hits = 0
            ks = 0
            kh = 0
            sh = 0
            offbeat = 0
            for row in hit_rows:
                aid = str(row.get("analysis_id") or "").strip()
                try:
                    onset = round(float(row.get("onset_time_sec") or 0.0), 3)
                except Exception:
                    continue
                inst = self._tokenize_profile_id(row.get("instrument") or "unknown")
                timeslots.setdefault((aid, onset), []).append(inst)
                if inst == "kick":
                    kick_hits += 1
                if inst == "snare":
                    snare_hits += 1
                if inst in {"hihat", "hh"}:
                    hat_hits += 1
                subdiv = str(row.get("subdivision") or "").lower()
                if subdiv not in {"0", "1", "downbeat", "quarter"}:
                    offbeat += 1

            infeasible_slots = 0
            common_patterns: Dict[str, int] = {}
            for _, insts in timeslots.items():
                uniq = sorted(set(insts))
                if len(uniq) > 4:
                    infeasible_slots += 1
                pattern_key = "+".join(uniq)
                common_patterns[pattern_key] = int(common_patterns.get(pattern_key) or 0) + 1
                if "kick" in uniq and "snare" in uniq:
                    ks += 1
                if "kick" in uniq and ("hihat" in uniq or "hh" in uniq):
                    kh += 1
                if "snare" in uniq and ("hihat" in uniq or "hh" in uniq):
                    sh += 1
                if len(uniq) > 1:
                    for i in range(len(uniq)):
                        for j in range(i + 1, len(uniq)):
                            pair = f"{uniq[i]}|{uniq[j]}"
                            simultaneous_matrix[pair] = int(simultaneous_matrix.get(pair) or 0) + 1

            slot_count = len(timeslots)
            limb_payload = {
                "simultaneous_hit_matrix_json": simultaneous_matrix,
                "kick_snare_dependency": float(ks / max(1, kick_hits)),
                "kick_hat_dependency": float(kh / max(1, kick_hits)),
                "snare_hat_dependency": float(sh / max(1, snare_hits)),
                "independence_score": max(0.0, min(1.0, 1.0 - (float(ks + kh + sh) / max(1, slot_count * 3)))),
                "syncopation_score": float(offbeat / max(1, len(hit_rows))),
                "limb_feasibility_violation_rate": float(infeasible_slots / max(1, slot_count)),
                "common_limb_patterns_json": dict(sorted(common_patterns.items(), key=lambda kv: kv[1], reverse=True)[:20]),
            }
            limb_saved = self._upsert_profile_row(
                table="drummer_limb_coordination",
                profile_id=f"limb_{drummer_fk}",
                drummer_fk=drummer_fk,
                payload=limb_payload,
            )

            fill_groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
            for row in fill_rows:
                aid = str(row.get("analysis_id") or "").strip()
                bar_idx = int(row.get("start_bar_index") or 0)
                section_label = section_labels[bar_idx % len(section_labels)]
                mod8 = (bar_idx + 1) % 8
                if mod8 == 0:
                    phrase_pos = "end_of_8"
                elif mod8 == 4:
                    phrase_pos = "end_of_4"
                elif ((bar_idx + 1) % 2) == 0:
                    phrase_pos = "end_of_2"
                else:
                    phrase_pos = "pre_downbeat"

                key = (section_label, phrase_pos)
                g = fill_groups.setdefault(
                    key,
                    {
                        "count": 0,
                        "lengths": [],
                        "densities": [],
                        "tom": 0,
                        "snare": 0,
                        "kick": 0,
                        "cymbal_exit": 0,
                    },
                )
                g["count"] += 1
                try:
                    st = float(row.get("start_time_sec") or 0.0)
                    en = float(row.get("end_time_sec") or st)
                except Exception:
                    st, en = 0.0, 0.0
                g["lengths"].append(max(0.0, en - st))
                try:
                    hit_count = float(row.get("hit_count") or 0.0)
                except Exception:
                    hit_count = 0.0
                g["densities"].append(hit_count / max(1e-6, (en - st)))
                inst_json = row.get("instruments_json")
                try:
                    insts = json.loads(inst_json) if isinstance(inst_json, str) and inst_json.strip() else []
                except Exception:
                    insts = []
                inst_set = {self._tokenize_profile_id(x) for x in (insts or [])}
                if "tom" in inst_set or "toms" in inst_set:
                    g["tom"] += 1
                if "snare" in inst_set:
                    g["snare"] += 1
                if "kick" in inst_set:
                    g["kick"] += 1
                if crash_total > 0:
                    for h in analysis_hits.get(aid, []):
                        inst = str(h.get("instrument") or "").lower()
                        if inst != "crash":
                            continue
                        try:
                            ht = float(h.get("onset_time_sec") or 0.0)
                        except Exception:
                            continue
                        if (en - 0.15) <= ht <= (en + 0.35):
                            g["cymbal_exit"] += 1
                            break

            fill_saved = 0
            for (section_label, phrase_pos), g in fill_groups.items():
                stats_len = self._basic_stats([float(v) for v in g.get("lengths") or []])
                density_mean = float(sum(g.get("densities") or [0.0]) / max(1, len(g.get("densities") or [])))
                count = int(g.get("count") or 0)
                payload = {
                    "section_label": section_label,
                    "phrase_position": phrase_pos,
                    "fill_probability": float(min(1.0, count / 8.0)),
                    "fill_length_mean_beats": stats_len["mean"],
                    "fill_length_std_beats": stats_len["std"],
                    "fill_density_mean": density_mean,
                    "tom_usage_probability": float(g.get("tom", 0) / max(1, count)),
                    "snare_fill_probability": float(g.get("snare", 0) / max(1, count)),
                    "kick_fill_probability": float(g.get("kick", 0) / max(1, count)),
                    "cymbal_exit_probability": float(g.get("cymbal_exit", 0) / max(1, count)),
                    "triplet_fill_probability": 0.0,
                    "linear_fill_probability": 0.5,
                    "rudimental_fill_probability": 0.25,
                    "common_fill_shapes_json": {"count": count},
                }
                pid = f"fill_{drummer_fk}_{self._tokenize_profile_id(section_label)}_{self._tokenize_profile_id(phrase_pos)}"
                if self._upsert_profile_row(
                    table="drummer_fill_behavior",
                    profile_id=pid,
                    drummer_fk=drummer_fk,
                    payload=payload,
                ):
                    fill_saved += 1

            embedding_vector = [0.0] * 128
            embedding_vector[0] = self._safe_float(rollup.get("timing_std_ms")) or 0.0
            embedding_vector[1] = self._safe_float(rollup.get("velocity_mean")) or 0.0
            embedding_vector[2] = self._safe_float(rollup.get("velocity_std")) or 0.0
            embedding_vector[3] = self._safe_float(rollup.get("fills_per_min")) or 0.0
            embedding_vector[4] = float(cym_payload.get("ride_usage_ratio") or 0.0)
            embedding_vector[5] = float(cym_payload.get("crash_frequency_per_min") or 0.0)
            embedding_vector[6] = float(limb_payload.get("independence_score") or 0.0)
            embedding_vector[7] = float(limb_payload.get("syncopation_score") or 0.0)
            embedding_vector[8] = float(confidence_score)
            embedding_vector[9] = float(phrase_saved)

            seed_source = self._json_dumps(
                {
                    "drummer_fk": drummer_fk,
                    "rollup": rollup,
                    "cymbal": cym_payload,
                    "limb": limb_payload,
                    "counts": {
                        "phrase": phrase_saved,
                        "micro": micro_saved,
                        "dyn": dynamics_saved,
                        "fill": fill_saved,
                    },
                }
            ) or ""
            digest = hashlib.sha256(seed_source.encode("utf-8", errors="ignore")).hexdigest()
            for i in range(10, 128):
                pair = digest[(2 * ((i - 10) % (len(digest) // 2))):(2 * ((i - 10) % (len(digest) // 2))) + 2]
                try:
                    embedding_vector[i] = (int(pair, 16) / 255.0) - 0.5
                except Exception:
                    embedding_vector[i] = 0.0

            embedding_saved = self.upsert_drummer_personality_embedding(
                embedding_id=f"emb_{drummer_fk}",
                drummer_fk=drummer_fk,
                model_version="phase7_v1",
                embedding_vector=embedding_vector,
                confidence_score=confidence_score,
                source_song_count=int(rollup.get("songs") or 0),
                source_hit_count=int(rollup.get("hits") or 0),
                timing_weight=0.2,
                dynamics_weight=0.2,
                fill_weight=0.15,
                cymbal_weight=0.15,
                coordination_weight=0.15,
                phrase_weight=0.15,
            )

            out["profiles"] = {
                "phrase": bool(phrase_saved > 0),
                "phrase_count": phrase_saved,
                "phrase_status": phrase_status if phrase_saved == 0 else "saved",
                "microtiming": bool(micro_saved > 0),
                "microtiming_count": micro_saved,
                "dynamics": bool(dynamics_saved > 0),
                "dynamics_count": dynamics_saved,
                "cymbal": cym_saved,
                "limb": limb_saved,
                "fill": bool(fill_saved > 0),
                "fill_count": fill_saved,
            }
            out["embedding"] = {
                "saved": embedding_saved,
                "dim": len(embedding_vector),
                "model_version": "phase7_v1",
            }
            required = [micro_saved > 0, dynamics_saved > 0, bool(cym_saved), bool(limb_saved), fill_saved > 0, bool(embedding_saved)]
            out["saved"] = all(bool(v) for v in required)
            return out
        except Exception as e:
            msg = f"Phase 7 assimilation profiling failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def upsert_drummer_profile_rollup(
        self,
        *,
        drummer_fk: int,
        rollup: Dict[str, Any],
        rollup_version: str = "phase5_v1",
    ) -> bool:
        try:
            now = datetime.utcnow().isoformat()
            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.drummer_profile_rollups (
                                rollup_id, drummer_id, rollup_version, rollup_json, created_at, updated_at
                            ) VALUES (:rid, :did, :ver, :rjson, NOW(), NOW())
                            ON CONFLICT (drummer_id) DO UPDATE SET
                                rollup_version = EXCLUDED.rollup_version,
                                rollup_json = EXCLUDED.rollup_json,
                                updated_at = NOW()
                            """
                        ),
                        {
                            "rid": str(uuid.uuid4()),
                            "did": str(drummer_fk),
                            "ver": (rollup_version or "").strip() or None,
                            "rjson": json.dumps(rollup or {}, default=str),
                        },
                    )
            else:
                conn = self._get_connection()
                cur = conn.cursor()

                def _do_write() -> None:
                    cur.execute(
                        """
                        INSERT INTO drummer_profile_rollups (
                            rollup_id, drummer_id, rollup_version, rollup_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(drummer_id) DO UPDATE SET
                            rollup_version=excluded.rollup_version,
                            rollup_json=excluded.rollup_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            str(uuid.uuid4()),
                            int(drummer_fk),
                            (rollup_version or "").strip() or None,
                            json.dumps(rollup or {}, default=str),
                            now,
                            now,
                        ),
                    )
                    conn.commit()

                self._with_write_lock_retry(_do_write)
            self.data_changed.emit("drummer_profile_rollups", "upsert")
            return True
        except Exception as e:
            msg = f"Phase 5 rollup upsert failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return False

    def get_drummer_profile_rollup(self, *, drummer_slug: str) -> Optional[Dict[str, Any]]:
        """Fetch the last saved drummer_profile_rollups.rollup_json for a slug/id without recomputing.
        Works for both Postgres and SQLite schemas. Returns a dict or None if not present.
        """
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return None
            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    row = conn_pg.execute(
                        text(
                            "SELECT rollup_json FROM public.drummer_profile_rollups WHERE CAST(drummer_id AS TEXT) = CAST(:did AS TEXT) LIMIT 1"
                        ),
                        {"did": str(drummer_slug)},
                    ).first()
                if not row:
                    return None
                try:
                    value = row[0]
                except Exception:
                    try:
                        value = row["rollup_json"]  # type: ignore[index]
                    except Exception:
                        value = None
                return self._json_loads(value, default={}) or {}
            # SQLite path
            conn = self._get_connection()
            cur = conn.cursor()
            cur.execute(
                """
                SELECT rollup_json
                FROM drummer_profile_rollups
                WHERE CAST(drummer_id AS TEXT) = CAST(? AS TEXT)
                LIMIT 1
                """,
                (drummer_slug,),
            )
            row = cur.fetchone()
            if not row:
                return None
            try:
                value = row[0]
            except Exception:
                value = None
            return self._json_loads(value, default={}) or {}
        except sqlite3.OperationalError:
            return None
        except Exception as e:
            logger.error(f"Error fetching drummer_profile_rollup for {drummer_slug}: {str(e)}")
            self.database_error.emit(f"Error fetching drummer_profile_rollup: {str(e)}")
            return None

    def run_phase5_profile_rollup_for_drummer(self, *, drummer_slug: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {"drummer_slug": drummer_slug, "saved": False, "rollup": {}}
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return out
            if getattr(self, "_engine", None) is not None:
                rollup = self.compute_drummer_profile_rollup(drummer_fk=drummer_slug)
                saved = self.upsert_drummer_profile_rollup(drummer_fk=drummer_slug, rollup=rollup)
            else:
                conn = self._get_connection()
                cur = conn.cursor()
                drummer_fk = self._get_drummer_fk_by_slug(cursor=cur, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    self._ensure_drummer_exists(cursor=cur, drummer_id=drummer_slug)
                    conn.commit()
                    drummer_fk = self._get_drummer_fk_by_slug(cursor=cur, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    return out

                rollup = self.compute_drummer_profile_rollup(drummer_fk=int(drummer_fk))
                saved = self.upsert_drummer_profile_rollup(drummer_fk=int(drummer_fk), rollup=rollup)
            phase7 = self.run_phase7_assimilation_profiles_for_drummer(drummer_slug=drummer_slug)
            out["saved"] = bool(saved)
            out["rollup"] = rollup
            out["phase7"] = phase7
            return out
        except Exception as e:
            msg = f"Phase 5 run failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def extract_fills_and_techniques_for_analysis(
        self,
        *,
        analysis_id: str,
        fill_window_sec: float = 0.75,
        fill_min_hits: int = 12,
        flam_max_gap_ms: float = 70.0,
        roll_max_gap_ms: float = 110.0,
        roll_min_hits: int = 5,
    ) -> Dict[str, Any]:
        """Phase 3 (baseline): derive fill_events and technique_events from hit-event sequences."""
        out = {"analysis_id": analysis_id, "fills": 0, "techniques": 0}
        try:
            self._set_last_ingest_error("")
            analysis_id = (analysis_id or "").strip()
            if not analysis_id:
                return out

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    spa = conn_pg.execute(
                        text(
                            "SELECT analysis_id, drummer_id, tempo_bpm, time_signature FROM public.song_performance_analysis WHERE analysis_id = :aid LIMIT 1"
                        ),
                        {"aid": analysis_id},
                    ).first()
                cursor = None
            else:
                conn = self._get_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT analysis_id, drummer_id, tempo_bpm, time_signature FROM song_performance_analysis WHERE analysis_id = ? LIMIT 1",
                    (analysis_id,),
                )
                spa = cursor.fetchone()
            if not spa:
                return out

            drummer_fk = spa[1]
            tempo_bpm = spa[2]
            time_signature = spa[3] or ""
            try:
                tempo_bpm = float(tempo_bpm) if isinstance(tempo_bpm, (int, float)) else None
            except Exception:
                tempo_bpm = None
            if not tempo_bpm or tempo_bpm <= 0:
                tempo_bpm = 120.0
            sec_per_beat = 60.0 / float(tempo_bpm)

            beats_per_bar = 4
            try:
                if isinstance(time_signature, str) and "/" in time_signature:
                    beats_per_bar = int(str(time_signature).split("/")[0].strip() or "4")
            except Exception:
                beats_per_bar = 4

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text(
                            """
                            SELECT instrument, onset_time_sec, velocity_est
                            FROM public.drum_hit_events
                            WHERE analysis_id = :aid
                            ORDER BY onset_time_sec ASC
                            """
                        ),
                        {"aid": analysis_id},
                    ).fetchall() or []
            else:
                cursor.execute(
                    """
                    SELECT instrument, onset_time_sec, velocity_est
                    FROM drum_hit_events
                    WHERE analysis_id = ?
                    ORDER BY onset_time_sec ASC
                    """,
                    (analysis_id,),
                )
                rows = cursor.fetchall() or []
            if not rows:
                return out

            hits = []
            for r in rows:
                inst = str(r[0] or "")
                try:
                    t = float(r[1])
                except Exception:
                    continue
                vel = r[2]
                try:
                    vel = float(vel) if isinstance(vel, (int, float)) else None
                except Exception:
                    vel = None
                hits.append((inst, t, vel))

            song_len_sec = 0.0
            try:
                song_len_sec = float(hits[-1][1]) - float(hits[0][1])
                if song_len_sec < 0:
                    song_len_sec = 0.0
            except Exception:
                song_len_sec = 0.0

            now = datetime.utcnow().isoformat()

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(text("DELETE FROM public.fill_events WHERE analysis_id = :aid"), {"aid": analysis_id})
                    conn_pg.execute(text("DELETE FROM public.technique_events WHERE analysis_id = :aid"), {"aid": analysis_id})

                    fills_inserted = 0
                    tech_inserted = 0
                    technique_breakdown: Dict[str, int] = {}

                    n = len(hits)
                    i = 0
                    while i < n:
                        t0 = hits[i][1]
                        j = i
                        tom_snare = 0
                        insts = set()
                        while j < n and (hits[j][1] - t0) <= float(fill_window_sec):
                            inst = hits[j][0]
                            insts.add(inst)
                            if inst in ("tom", "snare"):
                                tom_snare += 1
                            j += 1

                        window_hits = j - i
                        looks_like_fill = window_hits >= int(fill_min_hits) and (tom_snare >= 3 or ("tom" in insts and tom_snare >= 2))
                        if looks_like_fill:
                            fs = float(t0)
                            fe = float(hits[j - 1][1]) if j - 1 >= i else fs
                            if (fe - fs) >= 0.25:
                                beat_index = int(fs / sec_per_beat) if sec_per_beat > 0 else None
                                bar_index = int((beat_index or 0) // int(beats_per_bar)) if beats_per_bar > 0 else None
                                conn_pg.execute(
                                    text(
                                        """
                                        INSERT INTO public.fill_events (
                                            fill_id, analysis_id, drummer_id, song_id,
                                            start_time_sec, end_time_sec,
                                            start_bar_index, end_bar_index,
                                            hit_count, instruments_json,
                                            density_per_sec, classification,
                                            created_at
                                        ) VALUES (
                                            :fill_id, :analysis_id, :drummer_id, :song_id,
                                            :start_time_sec, :end_time_sec,
                                            :start_bar_index, :end_bar_index,
                                            :hit_count, :instruments_json,
                                            :density_per_sec, :classification,
                                            NOW()
                                        )
                                        """
                                    ),
                                    {
                                        "fill_id": str(uuid.uuid4()),
                                        "analysis_id": analysis_id,
                                        "drummer_id": str(drummer_fk),
                                        "song_id": None,
                                        "start_time_sec": fs,
                                        "end_time_sec": fe,
                                        "start_bar_index": bar_index,
                                        "end_bar_index": bar_index,
                                        "hit_count": int(window_hits),
                                        "instruments_json": json.dumps(sorted(list(insts))),
                                        "density_per_sec": float(window_hits) / max(0.001, (fe - fs)),
                                        "classification": "density_fill",
                                    },
                                )
                                fills_inserted += 1
                            i = max(i + 1, j)
                        else:
                            i += 1

                    snare_times = [t for inst, t, _ in hits if inst == "snare"]
                    snare_times.sort()

                    for k in range(1, len(snare_times)):
                        gap_ms = (float(snare_times[k]) - float(snare_times[k - 1])) * 1000.0
                        if 0.0 < gap_ms <= float(flam_max_gap_ms):
                            conn_pg.execute(
                                text(
                                    """
                                    INSERT INTO public.technique_events (
                                        technique_event_id, analysis_id, drummer_id, song_id,
                                        start_time_sec, end_time_sec,
                                        technique_type, technique_name,
                                        confidence, details_json,
                                        created_at
                                    ) VALUES (
                                        :technique_event_id, :analysis_id, :drummer_id, :song_id,
                                        :start_time_sec, :end_time_sec,
                                        :technique_type, :technique_name,
                                        :confidence, :details_json,
                                        NOW()
                                    )
                                    """
                                ),
                                {
                                    "technique_event_id": str(uuid.uuid4()),
                                    "analysis_id": analysis_id,
                                    "drummer_id": str(drummer_fk),
                                    "song_id": None,
                                    "start_time_sec": float(snare_times[k - 1]),
                                    "end_time_sec": float(snare_times[k]),
                                    "technique_type": "rudiment",
                                    "technique_name": "flam_like",
                                    "confidence": 0.55,
                                    "details_json": json.dumps({"gap_ms": gap_ms}),
                                },
                            )
                            tech_inserted += 1
                            technique_breakdown["flam_like"] = int(technique_breakdown.get("flam_like") or 0) + 1

                    run_start = 0
                    for k in range(1, len(snare_times) + 1):
                        if k == len(snare_times):
                            end_run = k
                        else:
                            gap_ms = (float(snare_times[k]) - float(snare_times[k - 1])) * 1000.0
                            end_run = None if gap_ms <= float(roll_max_gap_ms) else k

                        if end_run is not None:
                            run_len = end_run - run_start
                            if run_len >= int(roll_min_hits):
                                s = float(snare_times[run_start])
                                e = float(snare_times[end_run - 1])
                                conn_pg.execute(
                                    text(
                                        """
                                        INSERT INTO public.technique_events (
                                            technique_event_id, analysis_id, drummer_id, song_id,
                                            start_time_sec, end_time_sec,
                                            technique_type, technique_name,
                                            confidence, details_json,
                                            created_at
                                        ) VALUES (
                                            :technique_event_id, :analysis_id, :drummer_id, :song_id,
                                            :start_time_sec, :end_time_sec,
                                            :technique_type, :technique_name,
                                            :confidence, :details_json,
                                            NOW()
                                        )
                                        """
                                    ),
                                    {
                                        "technique_event_id": str(uuid.uuid4()),
                                        "analysis_id": analysis_id,
                                        "drummer_id": str(drummer_fk),
                                        "song_id": None,
                                        "start_time_sec": s,
                                        "end_time_sec": e,
                                        "technique_type": "rudiment",
                                        "technique_name": "roll_like",
                                        "confidence": 0.6,
                                        "details_json": json.dumps({"hit_count": int(run_len)}),
                                    },
                                )
                                tech_inserted += 1
                                technique_breakdown["roll_like"] = int(technique_breakdown.get("roll_like") or 0) + 1
                            run_start = end_run

                    fills_per_min = 0.0
                    try:
                        denom = max(1e-6, float(song_len_sec) / 60.0)
                        fills_per_min = float(fills_inserted) / denom
                    except Exception:
                        fills_per_min = 0.0

                out["fills"] = int(fills_inserted)
                out["techniques"] = int(tech_inserted)
                out["fills_per_min"] = float(fills_per_min)
                out["technique_breakdown"] = technique_breakdown
                if out["fills"] > 0:
                    self.data_changed.emit("fill_events", "insert")
                if out["techniques"] > 0:
                    self.data_changed.emit("technique_events", "insert")
                return out

            def _do_write() -> Dict[str, Any]:
                # De-dupe baseline extraction
                cursor.execute("DELETE FROM fill_events WHERE analysis_id = ?", (analysis_id,))
                cursor.execute("DELETE FROM technique_events WHERE analysis_id = ?", (analysis_id,))

                fills_inserted = 0
                tech_inserted = 0

                # -------- FILL detection (simple density heuristic) --------
                # Identify windows with many hits, biased toward tom/snare activity.
                n = len(hits)
                i = 0
                in_fill = False
                fill_start = None
                fill_end = None
                fill_hit_count = 0
                fill_instruments = set()

                while i < n:
                    t0 = hits[i][1]
                    j = i
                    tom_snare = 0
                    insts = set()
                    while j < n and (hits[j][1] - t0) <= float(fill_window_sec):
                        inst = hits[j][0]
                        insts.add(inst)
                        if inst in ("tom", "snare"):
                            tom_snare += 1
                        j += 1

                    window_hits = j - i
                    # Stricter fill rule:
                    # - must be dense
                    # - must have meaningful tom/snare presence
                    # - skip overlapping windows by jumping i->j when a fill is started
                    looks_like_fill = window_hits >= int(fill_min_hits) and (tom_snare >= 3 or ("tom" in insts and tom_snare >= 2))
                    if looks_like_fill:
                        fs = float(t0)
                        fe = float(hits[j - 1][1]) if j - 1 >= i else fs
                        # Minimum duration prevents ultra-short spikes from being recorded as a fill.
                        if (fe - fs) >= 0.25:
                            beat_index = int(fs / sec_per_beat) if sec_per_beat > 0 else None
                            bar_index = int((beat_index or 0) // int(beats_per_bar)) if beats_per_bar > 0 else None
                            cursor.execute(
                                """
                                INSERT INTO fill_events (
                                    fill_id, analysis_id, drummer_id, song_id,
                                    start_time_sec, end_time_sec,
                                    start_bar_index, end_bar_index,
                                    hit_count, instruments_json,
                                    density_per_sec, classification,
                                    created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    str(uuid.uuid4()),
                                    analysis_id,
                                    drummer_fk,
                                    None,
                                    fs,
                                    fe,
                                    bar_index,
                                    bar_index,
                                    int(window_hits),
                                    json.dumps(sorted(list(insts))),
                                    float(window_hits) / max(0.001, (fe - fs)),
                                    "density_fill",
                                    now,
                                ),
                            )
                            fills_inserted += 1
                        i = max(i + 1, j)
                    else:
                        i += 1

                # -------- Technique detection (baseline heuristics) --------
                # Flam: two snare hits within flam_max_gap_ms
                # Roll: >= roll_min_hits snare hits where each gap <= roll_max_gap_ms
                snare_times = [t for inst, t, _ in hits if inst == "snare"]
                snare_times.sort()

                technique_breakdown: Dict[str, int] = {}

                # Flams
                for k in range(1, len(snare_times)):
                    gap_ms = (float(snare_times[k]) - float(snare_times[k - 1])) * 1000.0
                    if 0.0 < gap_ms <= float(flam_max_gap_ms):
                        cursor.execute(
                            """
                            INSERT INTO technique_events (
                                technique_event_id, analysis_id, drummer_id, song_id,
                                start_time_sec, end_time_sec,
                                technique_type, technique_name,
                                confidence, details_json,
                                created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                str(uuid.uuid4()),
                                analysis_id,
                                drummer_fk,
                                None,
                                float(snare_times[k - 1]),
                                float(snare_times[k]),
                                "rudiment",
                                "flam_like",
                                0.55,
                                json.dumps({"gap_ms": gap_ms}),
                                now,
                            ),
                        )
                        tech_inserted += 1
                        technique_breakdown["flam_like"] = int(technique_breakdown.get("flam_like") or 0) + 1

                # Rolls
                run_start = 0
                for k in range(1, len(snare_times) + 1):
                    if k == len(snare_times):
                        end_run = k
                    else:
                        gap_ms = (float(snare_times[k]) - float(snare_times[k - 1])) * 1000.0
                        end_run = None if gap_ms <= float(roll_max_gap_ms) else k

                    if end_run is not None:
                        run_len = end_run - run_start
                        if run_len >= int(roll_min_hits):
                            s = float(snare_times[run_start])
                            e = float(snare_times[end_run - 1])
                            cursor.execute(
                                """
                                INSERT INTO technique_events (
                                    technique_event_id, analysis_id, drummer_id, song_id,
                                    start_time_sec, end_time_sec,
                                    technique_type, technique_name,
                                    confidence, details_json,
                                    created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    str(uuid.uuid4()),
                                    analysis_id,
                                    drummer_fk,
                                    None,
                                    s,
                                    e,
                                    "rudiment",
                                    "roll_like",
                                    0.6,
                                    json.dumps({"hit_count": int(run_len)}),
                                    now,
                                ),
                            )
                            tech_inserted += 1
                            technique_breakdown["roll_like"] = int(technique_breakdown.get("roll_like") or 0) + 1
                        run_start = end_run

                conn.commit()
                fills_per_min = 0.0
                try:
                    denom = max(1e-6, float(song_len_sec) / 60.0)
                    fills_per_min = float(fills_inserted) / denom
                except Exception:
                    fills_per_min = 0.0

                return {
                    "fills": int(fills_inserted),
                    "techniques": int(tech_inserted),
                    "fills_per_min": float(fills_per_min),
                    "technique_breakdown": technique_breakdown,
                }

            res = self._with_write_lock_retry(_do_write) or {}
            out["fills"] = int(res.get("fills") or 0)
            out["techniques"] = int(res.get("techniques") or 0)
            if out["fills"] > 0:
                self.data_changed.emit("fill_events", "insert")
            if out["techniques"] > 0:
                self.data_changed.emit("technique_events", "insert")
            return out
        except Exception as e:
            msg = f"Phase 3 extraction failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def run_phase3_fills_and_techniques_for_drummer(
        self,
        *,
        drummer_slug: str,
    ) -> Dict[str, Any]:
        out = {
            "drummer_slug": drummer_slug,
            "analyses": 0,
            "fills": 0,
            "techniques": 0,
            "avg_fills_per_min": 0.0,
            "technique_breakdown": {},
        }
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return out
            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text("SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d ORDER BY created_at DESC"),
                        {"d": drummer_slug},
                    ).fetchall() or []
            else:
                conn = self._get_connection()
                cursor = conn.cursor()
                drummer_fk = self._get_drummer_fk_by_slug(cursor=cursor, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    self._ensure_drummer_exists(cursor=cursor, drummer_id=drummer_slug)
                    conn.commit()
                    drummer_fk = self._get_drummer_fk_by_slug(cursor=cursor, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    return out

                cursor.execute(
                    "SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ? ORDER BY created_at DESC",
                    (drummer_fk,),
                )
                rows = cursor.fetchall() or []
            out["analyses"] = len(rows)
            fills = 0
            tech = 0
            fills_per_min_vals: List[float] = []
            breakdown: Dict[str, int] = {}
            for r in rows:
                aid = r[0]
                res = self.extract_fills_and_techniques_for_analysis(analysis_id=aid)
                fills += int((res or {}).get("fills") or 0)
                tech += int((res or {}).get("techniques") or 0)

                try:
                    fpm = float((res or {}).get("fills_per_min") or 0.0)
                    if fpm > 0:
                        fills_per_min_vals.append(fpm)
                except Exception:
                    pass

                try:
                    tb = (res or {}).get("technique_breakdown") or {}
                    if isinstance(tb, dict):
                        for k, v in tb.items():
                            try:
                                breakdown[str(k)] = int(breakdown.get(str(k)) or 0) + int(v or 0)
                            except Exception:
                                continue
                except Exception:
                    pass
            out["fills"] = fills
            out["techniques"] = tech

            if fills_per_min_vals:
                out["avg_fills_per_min"] = float(sum(fills_per_min_vals) / float(len(fills_per_min_vals)))
            out["technique_breakdown"] = breakdown
            return out
        except Exception as e:
            msg = f"Phase 3 run failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def extract_microtiming_and_dynamics_for_analysis(
        self,
        *,
        analysis_id: str,
    ) -> Dict[str, Any]:
        """Phase 4 (baseline): derive microtiming + dynamics summaries from drum_hit_events.

        Writes into song_performance_analysis:
        - groove_micro_timing_variance
        - groove_pocket_tightness
        - groove_swing_factor (currently None unless a reliable estimate exists)
        - humanness_score
        - hit_counts_json
        - hit_density_json
        - dynamics_json
        """

        out: Dict[str, Any] = {
            "analysis_id": analysis_id,
            "updated": False,
            "total_hits": 0,
            "timing_std_ms": None,
            "timing_mean_ms": None,
            "velocity_mean": None,
            "velocity_std": None,
            "pocket_tightness": None,
            "micro_timing_variance": None,
            "humanness_score": None,
        }
        try:
            self._set_last_ingest_error("")
            analysis_id = (analysis_id or "").strip()
            if not analysis_id:
                return out

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    spa = conn_pg.execute(
                        text("SELECT tempo_bpm, duration_sec FROM public.song_performance_analysis WHERE analysis_id = :aid LIMIT 1"),
                        {"aid": analysis_id},
                    ).first()
            else:
                conn = self._get_connection()
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT tempo_bpm, duration_sec FROM song_performance_analysis WHERE analysis_id = ? LIMIT 1",
                    (analysis_id,),
                )
                spa = cursor.fetchone()
            if not spa:
                return out

            tempo_bpm = spa[0]
            duration_sec = spa[1]
            try:
                tempo_bpm = float(tempo_bpm) if isinstance(tempo_bpm, (int, float)) else None
            except Exception:
                tempo_bpm = None
            try:
                duration_sec = float(duration_sec) if isinstance(duration_sec, (int, float)) else None
            except Exception:
                duration_sec = None

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text(
                            """
                            SELECT instrument, onset_time_sec, timing_offset_ms, velocity_est
                            FROM public.drum_hit_events
                            WHERE analysis_id = :aid
                            ORDER BY onset_time_sec ASC
                            """
                        ),
                        {"aid": analysis_id},
                    ).fetchall() or []
            else:
                cursor.execute(
                    """
                    SELECT instrument, onset_time_sec, timing_offset_ms, velocity_est
                    FROM drum_hit_events
                    WHERE analysis_id = ?
                    ORDER BY onset_time_sec ASC
                    """,
                    (analysis_id,),
                )
                rows = cursor.fetchall() or []
            if not rows:
                return out

            offsets: List[float] = []
            velocities: List[float] = []
            hit_counts: Dict[str, int] = {}
            first_t = None
            last_t = None

            for inst, t, off_ms, vel in rows:
                inst_s = str(inst or "")
                hit_counts[inst_s] = int(hit_counts.get(inst_s) or 0) + 1

                try:
                    t = float(t)
                    if first_t is None:
                        first_t = t
                    last_t = t
                except Exception:
                    pass

                try:
                    if off_ms is not None:
                        offsets.append(float(off_ms))
                except Exception:
                    pass

                try:
                    if vel is not None:
                        velocities.append(float(vel))
                except Exception:
                    pass

            total_hits = int(sum(hit_counts.values()))
            out["total_hits"] = total_hits

            if duration_sec is None:
                try:
                    if first_t is not None and last_t is not None and float(last_t) > float(first_t):
                        duration_sec = float(last_t) - float(first_t)
                except Exception:
                    duration_sec = None

            # ---- microtiming stats ----
            timing_mean_ms = None
            timing_std_ms = None
            if offsets:
                try:
                    timing_mean_ms = float(statistics.fmean(offsets))
                except Exception:
                    timing_mean_ms = None
                try:
                    timing_std_ms = float(statistics.pstdev(offsets)) if len(offsets) >= 2 else 0.0
                except Exception:
                    timing_std_ms = None

            # ---- dynamics stats ----
            vel_mean = None
            vel_std = None
            if velocities:
                try:
                    vel_mean = float(statistics.fmean(velocities))
                except Exception:
                    vel_mean = None
                try:
                    vel_std = float(statistics.pstdev(velocities)) if len(velocities) >= 2 else 0.0
                except Exception:
                    vel_std = None

            # Pocket tightness in [0, 1], decreasing with timing std.
            pocket_tightness = None
            micro_var = None
            if timing_std_ms is not None:
                micro_var = float(timing_std_ms)
                # Smooth decay so values remain informative above ~30ms.
                # 0ms => 1.0, ~20ms => 0.45, ~36ms => 0.23, ~60ms => 0.09
                try:
                    pocket_tightness = float(2.718281828459045 ** (-float(timing_std_ms) / 25.0))
                except Exception:
                    pocket_tightness = None
                if pocket_tightness is not None:
                    pocket_tightness = max(0.0, min(1.0, pocket_tightness))

            # Humanness is a simple composite of timing variation + dynamics variation.
            # (This is intentionally lightweight; can be refined later.)
            humanness = None
            try:
                t_component = 0.0
                if timing_std_ms is not None:
                    # Favor some variation, penalize too-tight and too-loose.
                    # Peak around ~12ms.
                    t_component = max(0.0, 1.0 - (abs(float(timing_std_ms) - 12.0) / 28.0))
                v_component = 0.0
                if vel_std is not None:
                    # Peak around ~0.18 assuming velocity_est is ~0..1.
                    v_component = max(0.0, 1.0 - (abs(float(vel_std) - 0.18) / 0.30))
                humanness = max(0.0, min(1.0, (0.65 * t_component) + (0.35 * v_component)))
            except Exception:
                humanness = None

            # Basic hit density: hits per second and per beat (if tempo exists).
            hits_per_sec = None
            hits_per_beat = None
            try:
                if duration_sec and duration_sec > 0:
                    hits_per_sec = float(total_hits) / float(duration_sec)
            except Exception:
                hits_per_sec = None
            try:
                if duration_sec and duration_sec > 0 and tempo_bpm and tempo_bpm > 0:
                    beats = float(duration_sec) / (60.0 / float(tempo_bpm))
                    if beats > 0:
                        hits_per_beat = float(total_hits) / beats
            except Exception:
                hits_per_beat = None

            now = datetime.utcnow().isoformat()

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            UPDATE public.song_performance_analysis
                            SET
                                groove_swing_factor = COALESCE(groove_swing_factor, NULL),
                                groove_pocket_tightness = :pocket,
                                groove_micro_timing_variance = :microvar,
                                humanness_score = :human,
                                total_hits = :total_hits,
                                hit_counts_json = :hit_counts,
                                hit_density_json = :hit_density,
                                dynamics_json = :dyn,
                                updated_at = NOW()
                            WHERE analysis_id = :aid
                            """
                        ),
                        {
                            "pocket": float(pocket_tightness) if pocket_tightness is not None else None,
                            "microvar": float(micro_var) if micro_var is not None else None,
                            "human": float(humanness) if humanness is not None else None,
                            "total_hits": int(total_hits),
                            "hit_counts": json.dumps(hit_counts, default=str),
                            "hit_density": json.dumps(
                                {
                                    "duration_sec": float(duration_sec) if isinstance(duration_sec, (int, float)) else None,
                                    "hits_per_sec": hits_per_sec,
                                    "hits_per_beat": hits_per_beat,
                                },
                                default=str,
                            ),
                            "dyn": json.dumps(
                                {
                                    "velocity_mean": vel_mean,
                                    "velocity_std": vel_std,
                                    "timing_mean_ms": timing_mean_ms,
                                    "timing_std_ms": timing_std_ms,
                                },
                                default=str,
                            ),
                            "aid": analysis_id,
                        },
                    )
            else:
                def _do_write() -> None:
                    cursor.execute(
                        """
                        UPDATE song_performance_analysis
                        SET
                            groove_swing_factor = COALESCE(groove_swing_factor, NULL),
                            groove_pocket_tightness = ?,
                            groove_micro_timing_variance = ?,
                            humanness_score = ?,
                            total_hits = ?,
                            hit_counts_json = ?,
                            hit_density_json = ?,
                            dynamics_json = ?,
                            updated_at = ?
                        WHERE analysis_id = ?
                        """,
                        (
                            float(pocket_tightness) if pocket_tightness is not None else None,
                            float(micro_var) if micro_var is not None else None,
                            float(humanness) if humanness is not None else None,
                            int(total_hits),
                            json.dumps(hit_counts, default=str),
                            json.dumps(
                                {
                                    "duration_sec": float(duration_sec) if isinstance(duration_sec, (int, float)) else None,
                                    "hits_per_sec": hits_per_sec,
                                    "hits_per_beat": hits_per_beat,
                                },
                                default=str,
                            ),
                            json.dumps(
                                {
                                    "velocity_mean": vel_mean,
                                    "velocity_std": vel_std,
                                    "timing_mean_ms": timing_mean_ms,
                                    "timing_std_ms": timing_std_ms,
                                },
                                default=str,
                            ),
                            now,
                            analysis_id,
                        ),
                    )
                    conn.commit()
                self._with_write_lock_retry(_do_write)
            out["updated"] = True
            out["timing_std_ms"] = timing_std_ms
            out["timing_mean_ms"] = timing_mean_ms
            out["velocity_mean"] = vel_mean
            out["velocity_std"] = vel_std
            out["pocket_tightness"] = pocket_tightness
            out["micro_timing_variance"] = micro_var
            out["humanness_score"] = humanness
            self.data_changed.emit("song_performance_analysis", "update")
            return out
        except Exception as e:
            msg = f"Phase 4 extraction failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def run_phase4_microtiming_and_dynamics_for_drummer(
        self,
        *,
        drummer_slug: str,
    ) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "drummer_slug": drummer_slug,
            "analyses": 0,
            "updated": 0,
            "avg_timing_std_ms": None,
            "avg_pocket_tightness": None,
            "avg_humanness_score": None,
        }
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return out
            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text("SELECT analysis_id FROM public.song_performance_analysis WHERE drummer_id = :d ORDER BY created_at DESC"),
                        {"d": drummer_slug},
                    ).fetchall() or []
            else:
                conn = self._get_connection()
                cursor = conn.cursor()
                drummer_fk = self._get_drummer_fk_by_slug(cursor=cursor, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    self._ensure_drummer_exists(cursor=cursor, drummer_id=drummer_slug)
                    conn.commit()
                    drummer_fk = self._get_drummer_fk_by_slug(cursor=cursor, drummer_slug=drummer_slug)
                if drummer_fk is None:
                    return out

                cursor.execute(
                    "SELECT analysis_id FROM song_performance_analysis WHERE drummer_id = ? ORDER BY created_at DESC",
                    (drummer_fk,),
                )
                rows = cursor.fetchall() or []
            out["analyses"] = len(rows)

            updated = 0
            stds: List[float] = []
            pockets: List[float] = []
            humans: List[float] = []
            for r in rows:
                aid = r[0] if isinstance(r, (tuple, list)) else r.analysis_id
                res = self.extract_microtiming_and_dynamics_for_analysis(analysis_id=aid)
                if res.get("updated"):
                    updated += 1
                try:
                    v = res.get("timing_std_ms")
                    if v is not None:
                        stds.append(float(v))
                except Exception:
                    pass
                try:
                    v = res.get("pocket_tightness")
                    if v is not None:
                        pockets.append(float(v))
                except Exception:
                    pass
                try:
                    v = res.get("humanness_score")
                    if v is not None:
                        humans.append(float(v))
                except Exception:
                    pass

            out["updated"] = int(updated)
            if stds:
                out["avg_timing_std_ms"] = float(sum(stds) / float(len(stds)))
            if pockets:
                out["avg_pocket_tightness"] = float(sum(pockets) / float(len(pockets)))
            if humans:
                out["avg_humanness_score"] = float(sum(humans) / float(len(humans)))
            return out
        except Exception as e:
            msg = f"Phase 4 run failed: {e}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return out

    def _table_columns(self, table_name: str) -> set:
        if table_name in self._schema_cache:
            return self._schema_cache[table_name]
        try:
            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    res = conn_pg.execute(
                        text(
                            """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_name = :tbl
                              AND table_schema IN ('public', 'drumtrackai')
                            """
                        ),
                        {"tbl": table_name},
                    )
                    cols = {row[0] for row in res}
                self._schema_cache[table_name] = cols
                return cols
            conn = self._get_connection()
            cursor = conn.cursor()
            rows = cursor.execute(f"PRAGMA table_info({table_name})").fetchall()
            cols = {row[1] for row in rows} if rows else set()
            self._schema_cache[table_name] = cols
            return cols
        except Exception:
            self._schema_cache[table_name] = set()
            return set()

    def _ensure_postgres_schema(self) -> None:
        if getattr(self, "_engine", None) is None:
            return
        stmts = [
            "CREATE SCHEMA IF NOT EXISTS drumtrackai",
            """
            CREATE TABLE IF NOT EXISTS public.drummers (
                id TEXT PRIMARY KEY,
                drummer_id TEXT UNIQUE,
                slug TEXT UNIQUE,
                display_name TEXT,
                name TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.songs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT,
                album TEXT,
                year INTEGER,
                genre TEXT,
                duration DOUBLE PRECISION,
                file_path TEXT,
                drummer_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.song_performance_analysis (
                analysis_id TEXT PRIMARY KEY,
                song_id TEXT,
                drummer_id TEXT,
                source_file TEXT,
                mvsep_output_dir TEXT,
                tempo_bpm DOUBLE PRECISION,
                tempo_confidence DOUBLE PRECISION,
                time_signature TEXT,
                duration_sec DOUBLE PRECISION,
                groove_swing_factor DOUBLE PRECISION,
                groove_pocket_tightness DOUBLE PRECISION,
                groove_micro_timing_variance DOUBLE PRECISION,
                rhythmic_complexity DOUBLE PRECISION,
                syncopation_level DOUBLE PRECISION,
                humanness_score DOUBLE PRECISION,
                total_hits INTEGER,
                hit_counts_json TEXT,
                hit_density_json TEXT,
                dynamics_json TEXT,
                fills_summary_json TEXT,
                rudiments_summary_json TEXT,
                techniques_json TEXT,
                stem_files_used_json TEXT,
                raw_analysis_json TEXT,
                analysis_version TEXT,
                phase32_42_features_json TEXT,
                phase32_42_features_version TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (song_id) REFERENCES public.songs(id) ON DELETE SET NULL,
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drum_hit_events (
                event_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                instrument TEXT NOT NULL,
                component TEXT,
                onset_time_sec DOUBLE PRECISION NOT NULL,
                onset_strength DOUBLE PRECISION,
                velocity_est DOUBLE PRECISION,
                beat_index INTEGER,
                bar_index INTEGER,
                subdivision TEXT,
                timing_offset_ms DOUBLE PRECISION,
                is_ghost INTEGER,
                is_accent INTEGER,
                is_flams_like INTEGER,
                is_roll_like INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (analysis_id) REFERENCES public.song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.fill_events (
                fill_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                start_time_sec DOUBLE PRECISION NOT NULL,
                end_time_sec DOUBLE PRECISION NOT NULL,
                start_bar_index INTEGER,
                end_bar_index INTEGER,
                hit_count INTEGER,
                instruments_json TEXT,
                density_per_sec DOUBLE PRECISION,
                classification TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (analysis_id) REFERENCES public.song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.technique_events (
                technique_event_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                start_time_sec DOUBLE PRECISION,
                end_time_sec DOUBLE PRECISION,
                technique_type TEXT NOT NULL,
                technique_name TEXT,
                confidence DOUBLE PRECISION,
                details_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (analysis_id) REFERENCES public.song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.analysis_artifacts (
                artifact_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                artifact_role TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_format TEXT,
                file_size_bytes BIGINT,
                file_mtime_epoch DOUBLE PRECISION,
                sha256 TEXT,
                extractor_name TEXT,
                extractor_version TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (analysis_id) REFERENCES public.song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.stem_artifacts (
                stem_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                stem_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size_bytes BIGINT,
                file_mtime_epoch DOUBLE PRECISION,
                sha256 TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (analysis_id) REFERENCES public.song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drummer_phrase_features (
                id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT NOT NULL,
                song_id TEXT NOT NULL,
                section_label TEXT,
                phrase_index INTEGER,
                phrase_length_bars INTEGER,
                bar_position_in_phrase INTEGER,
                energy_start DOUBLE PRECISION,
                energy_end DOUBLE PRECISION,
                energy_slope DOUBLE PRECISION,
                pattern_repetition_score DOUBLE PRECISION,
                pattern_mutation_rate DOUBLE PRECISION,
                density_curve_json TEXT,
                accent_curve_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (analysis_id) REFERENCES public.song_performance_analysis(analysis_id) ON DELETE CASCADE,
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE CASCADE,
                FOREIGN KEY (song_id) REFERENCES public.songs(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drummer_microtiming_profiles (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                instrument TEXT,
                subdivision TEXT,
                mean_offset_ms DOUBLE PRECISION,
                std_offset_ms DOUBLE PRECISION,
                skew_offset_ms DOUBLE PRECISION,
                early_hit_probability DOUBLE PRECISION,
                late_hit_probability DOUBLE PRECISION,
                pocket_bias TEXT,
                context_label TEXT,
                histogram_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drummer_dynamic_profiles (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                instrument TEXT,
                velocity_mean DOUBLE PRECISION,
                velocity_std DOUBLE PRECISION,
                velocity_skew DOUBLE PRECISION,
                ghost_note_probability DOUBLE PRECISION,
                accent_probability DOUBLE PRECISION,
                ghost_to_accent_ratio DOUBLE PRECISION,
                accent_grid_json TEXT,
                velocity_histogram_json TEXT,
                phrase_dynamic_curve_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drummer_cymbal_language (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                hihat_closed_ratio DOUBLE PRECISION,
                hihat_open_ratio DOUBLE PRECISION,
                hihat_pedal_ratio DOUBLE PRECISION,
                hihat_bark_probability DOUBLE PRECISION,
                ride_usage_ratio DOUBLE PRECISION,
                ride_bell_probability DOUBLE PRECISION,
                crash_frequency_per_min DOUBLE PRECISION,
                crash_on_downbeat_probability DOUBLE PRECISION,
                crash_on_transition_probability DOUBLE PRECISION,
                cymbal_decay_spacing_score DOUBLE PRECISION,
                cymbal_density_curve_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drummer_limb_coordination (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                simultaneous_hit_matrix_json TEXT,
                kick_snare_dependency DOUBLE PRECISION,
                kick_hat_dependency DOUBLE PRECISION,
                snare_hat_dependency DOUBLE PRECISION,
                independence_score DOUBLE PRECISION,
                syncopation_score DOUBLE PRECISION,
                limb_feasibility_violation_rate DOUBLE PRECISION,
                common_limb_patterns_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drummer_fill_behavior (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                section_label TEXT,
                phrase_position TEXT,
                fill_probability DOUBLE PRECISION,
                fill_length_mean_beats DOUBLE PRECISION,
                fill_length_std_beats DOUBLE PRECISION,
                fill_density_mean DOUBLE PRECISION,
                tom_usage_probability DOUBLE PRECISION,
                snare_fill_probability DOUBLE PRECISION,
                kick_fill_probability DOUBLE PRECISION,
                cymbal_exit_probability DOUBLE PRECISION,
                triplet_fill_probability DOUBLE PRECISION,
                linear_fill_probability DOUBLE PRECISION,
                rudimental_fill_probability DOUBLE PRECISION,
                common_fill_shapes_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drummer_personality_embeddings (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                model_version TEXT NOT NULL,
                embedding_dim INTEGER NOT NULL,
                embedding_vector_json TEXT NOT NULL,
                source_song_count INTEGER,
                source_hit_count INTEGER,
                confidence_score DOUBLE PRECISION,
                timing_weight DOUBLE PRECISION,
                dynamics_weight DOUBLE PRECISION,
                fill_weight DOUBLE PRECISION,
                cymbal_weight DOUBLE PRECISION,
                coordination_weight DOUBLE PRECISION,
                phrase_weight DOUBLE PRECISION,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drummer_profile_rollups (
                rollup_id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL UNIQUE,
                rollup_version TEXT,
                rollup_json TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.source_videos (
                url TEXT PRIMARY KEY,
                platform TEXT DEFAULT 'youtube',
                title TEXT,
                channel TEXT,
                duration_sec INTEGER,
                drummer_id TEXT,
                status TEXT,
                downloaded_path TEXT,
                tags_json TEXT,
                metadata_json TEXT,
                song_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (drummer_id) REFERENCES public.drummers(id) ON DELETE SET NULL,
                FOREIGN KEY (song_id) REFERENCES public.songs(id) ON DELETE SET NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.calibration_adjustments (
                drummer_slug TEXT PRIMARY KEY,
                adjustments_json TEXT NOT NULL,
                metadata_json TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.calibration_runs (
                run_id TEXT PRIMARY KEY,
                drummer_slug TEXT NOT NULL,
                started_at TIMESTAMPTZ DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                outcome TEXT NOT NULL,
                within_tolerance_count INTEGER,
                total_compared INTEGER,
                delta_summary TEXT,
                note_count INTEGER,
                fills_per_minute DOUBLE PRECISION
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.calibration_feedback (
                feedback_id TEXT PRIMARY KEY,
                drummer_slug TEXT NOT NULL,
                run_id TEXT,
                author TEXT,
                rating INTEGER NOT NULL,
                comment TEXT,
                metadata_json TEXT,
                submitted_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.reviewer_profiles (
                reviewer_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                expertise_level TEXT,
                primary_styles_json TEXT NOT NULL DEFAULT '[]',
                years_experience INTEGER,
                weighting_factor DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.evaluation_sessions (
                session_id TEXT PRIMARY KEY,
                reviewer_id TEXT NOT NULL,
                target_drummer_slug TEXT NOT NULL,
                assigned_at TIMESTAMPTZ,
                started_at TIMESTAMPTZ,
                completed_at TIMESTAMPTZ,
                app_version TEXT,
                notes TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (reviewer_id) REFERENCES public.reviewer_profiles(reviewer_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.evaluation_items (
                item_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                target_drummer_slug TEXT NOT NULL,
                base_groove_id TEXT NOT NULL,
                reference_artifact_id TEXT,
                baseline_run_id TEXT,
                candidate_a_run_id TEXT,
                candidate_b_run_id TEXT,
                eval_mode TEXT,
                ab_mapping_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.pairwise_judgments (
                judgment_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                preferred_candidate TEXT,
                closer_to_target TEXT,
                better_feel TEXT,
                more_musical TEXT,
                confidence INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.attribute_ratings (
                rating_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                candidate_label TEXT NOT NULL,
                stylistic_authenticity INTEGER,
                groove_feel INTEGER,
                dynamics INTEGER,
                phrasing INTEGER,
                kit_balance INTEGER,
                fill_behavior INTEGER,
                human_realism INTEGER,
                overall_usefulness INTEGER,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.rubric_tags (
                tag_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                candidate_label TEXT NOT NULL,
                tag_name TEXT NOT NULL,
                tag_value TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (item_id) REFERENCES public.evaluation_items(item_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.run_versions (
                run_id TEXT PRIMARY KEY,
                generator_version TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                rollup_version TEXT NOT NULL,
                sample_pack_version TEXT NOT NULL,
                seed INTEGER NOT NULL,
                commit_hash TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.drummer_presets (
                preset_id TEXT PRIMARY KEY,
                profile_type TEXT NOT NULL,
                name TEXT NOT NULL,
                tier TEXT NOT NULL,
                deltas_json TEXT,
                policies_json TEXT,
                source_type TEXT,
                source_song_name TEXT,
                source_ref TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.audio_artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT,
                artifact_type TEXT NOT NULL,
                storage_uri TEXT NOT NULL,
                duration_sec DOUBLE PRECISION,
                loudness_lufs DOUBLE PRECISION,
                sample_pack_version TEXT,
                render_recipe_json TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.learning_updates (
                update_id TEXT PRIMARY KEY,
                model_family TEXT NOT NULL,
                previous_version TEXT,
                new_version TEXT NOT NULL,
                training_window TEXT,
                summary_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.training_sessions (
                session_id TEXT PRIMARY KEY,
                session_data TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.model_versions (
                version_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                model_path TEXT NOT NULL,
                sophistication_score DOUBLE PRECISION NOT NULL,
                capabilities TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (session_id) REFERENCES public.training_sessions(session_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS public.performance_benchmarks (
                benchmark_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                test_name TEXT NOT NULL,
                test_results TEXT NOT NULL,
                score DOUBLE PRECISION NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                FOREIGN KEY (session_id) REFERENCES public.training_sessions(session_id)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_spa_drummer ON public.song_performance_analysis(drummer_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_hits_analysis ON public.drum_hit_events(analysis_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_hits_drummer_instrument ON public.drum_hit_events(drummer_id, instrument)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_fills_analysis ON public.fill_events(analysis_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_tech_analysis ON public.technique_events(analysis_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_phrase_features_drummer ON public.drummer_phrase_features(drummer_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_microtiming_drummer_context ON public.drummer_microtiming_profiles(drummer_id, instrument, context_label)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_dynamics_drummer_instrument ON public.drummer_dynamic_profiles(drummer_id, instrument)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_cymbal_language_drummer ON public.drummer_cymbal_language(drummer_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_limb_coordination_drummer ON public.drummer_limb_coordination(drummer_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_fill_behavior_drummer_section ON public.drummer_fill_behavior(drummer_id, section_label, phrase_position)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_embeddings_drummer_version ON public.drummer_personality_embeddings(drummer_id, model_version)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_source_videos_drummer_status ON public.source_videos(drummer_id, status)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_eval_sessions_reviewer ON public.evaluation_sessions(reviewer_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_eval_items_session ON public.evaluation_items(session_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_pairwise_item ON public.pairwise_judgments(item_id)
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_attr_item ON public.attribute_ratings(item_id)
            """,
        ]
        with self._engine.begin() as conn:
            skip_idx = str(os.getenv("DB_SKIP_INDEXES", "")).strip().lower() in ("1", "true", "yes", "y")
            for sql in stmts:
                if skip_idx and sql.strip().upper().startswith("CREATE INDEX"):
                    continue
                conn.execute(text(sql))
        self._schema_cache.clear()

    @classmethod
    def get_instance(cls):
        """Get the singleton instance"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def initialize(self, db_path=None) -> bool:
        """
        Initialize the database connection.
        
        Args:
            db_path: Legacy parameter retained for compatibility.
            
        Returns:
            bool: True if successful
        """
        try:
            if self._initialized:
                logger.warning("Database already initialized")
                return True
            
            db_backend = str(os.getenv("DB_BACKEND", "")).strip().lower()
            db_url = str(os.getenv("DATABASE_URL", "")).strip()
            postgres_configured = db_backend in {"postgres", "postgresql"} or db_url.lower().startswith("postgres")
            if not postgres_configured:
                msg = "CentralDatabaseService requires Postgres configuration (set DB_BACKEND=postgres and DATABASE_URL)"
                logger.error(msg)
                try:
                    self.database_error.emit(msg)
                except Exception:
                    pass
                return False
            if db_backend in {"postgres", "postgresql"} and not db_url.lower().startswith("postgres"):
                msg = "DB_BACKEND is postgres but DATABASE_URL is missing or not a postgres URL"
                logger.error(msg)
                try:
                    self.database_error.emit(msg)
                except Exception:
                    pass
                return False
            if db_backend in {"postgres", "postgresql"} or db_url.lower().startswith("postgres"):
                try:
                    max_overflow = int(str(os.getenv("DB_MAX_OVERFLOW", "5")).strip() or "5")
                except Exception:
                    max_overflow = 5
                try:
                    self._engine = create_engine(
                        db_url,
                        pool_pre_ping=True,
                        pool_size=5,
                        max_overflow=max_overflow,
                        future=True,
                    )
                    with self._engine.connect() as conn:
                        conn.execute(text("SELECT 1"))
                        try:
                            info_row = conn.execute(
                                text(
                                    "SELECT current_user, current_database(), version()"
                                )
                            ).first()
                            sp_row = conn.execute(text("SHOW search_path")).first()
                            logger.info(
                                f"DB connected: user={info_row[0]} db={info_row[1]} search_path={sp_row[0]}"
                            )
                        except Exception as _:
                            pass
                except Exception as e:
                    logger.error(f"Failed to initialize Postgres engine: {str(e)}")
                    try:
                        self.database_error.emit(f"Failed to initialize Postgres engine: {str(e)}")
                    except Exception:
                        pass
                    return False
                else:
                    # Ensure required schema/tables exist in Postgres
                    try:
                        self._ensure_postgres_schema()
                    except Exception as se:
                        logger.warning(f"Postgres schema ensure failed (continuing): {se}")
                        try:
                            self.database_error.emit(f"Postgres schema ensure failed: {se}")
                        except Exception:
                            pass
                    self._db_path = db_url or "postgres"
                    self._initialized = True
                    self.database_connected.emit(self._db_path)
                    logger.info(f"Database initialized successfully at {self._db_path}")
                    return True

            # Set default path if not provided
            if db_path is None:
                # First, honor an explicit environment override so backend
                # services and the admin GUI can share the same canonical DB
                # (e.g. the rich admin/analysis DB at admin/drumtrackai.db).
                env_path = os.getenv("DRUMTRACKAI_DB_PATH")
                if env_path:
                    db_path = env_path
                else:
                    # Prefer project-local DBs when running from a repo checkout.
                    # This prevents the admin UI from silently connecting to a
                    # fresh per-user DB with no drummers/beats.
                    try:
                        project_root = Path(__file__).resolve().parents[2]
                    except Exception:
                        project_root = None

                    candidates: List[Path] = []
                    if project_root:
                        candidates.extend([
                            project_root / "admin" / "drumtrackai.db",
                            project_root / "admin" / "admin" / "drumtrackai.db",
                            project_root / "admin" / "data" / "drum_training.db",
                            project_root / "admin" / "admin" / "data" / "drum_training.db",
                        ])

                    selected = None
                    for candidate in candidates:
                        try:
                            if candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0:
                                selected = candidate
                                break
                        except Exception:
                            continue

                    if selected is not None:
                        db_path = str(selected)
                    else:
                        # Fallback to the original per-user location.
                        home = Path.home()
                        db_dir = home / "DrumTracKAI" / "database"
                        db_dir.mkdir(parents=True, exist_ok=True)
                        db_path = str(db_dir / "drum_tracks.db")
                
            logger.info(f"Initializing database at: {db_path}")
            self._db_path = db_path
            
            # Create initial connection
            self._get_connection()
            
            # Create tables if they don't exist
            self._create_tables()

            # Schema migrations / compatibility
            self._ensure_phase32_42_columns()
            
            self._initialized = True
            self.database_connected.emit(db_path)
            logger.info(f"Database initialized successfully at {db_path}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to initialize database: {str(e)}")
            self.database_error.emit(f"Failed to initialize database: {str(e)}")
            return False

    def _sha256_file(self, file_path: str, max_bytes: int = 0) -> str:
        try:
            p = str(file_path)
            h = hashlib.sha256()
            with open(p, "rb") as f:
                if max_bytes and int(max_bytes) > 0:
                    h.update(f.read(int(max_bytes)))
                else:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
            return h.hexdigest()
        except Exception:
            return ""

    def upsert_song_performance_analysis(
        self,
        *,
        analysis_id: str,
        drummer_id: str,
        song_id: Optional[str] = None,
        source_file: str = "",
        mvsep_output_dir: str = "",
        analysis_json: Optional[Dict[str, Any]] = None,
        stem_files_used: Optional[Dict[str, Any]] = None,
        analysis_version: str = "",
    ) -> bool:
        try:
            self._set_last_ingest_error("")
            analysis_id = (analysis_id or "").strip()
            drummer_id = (drummer_id or "").strip()
            if not analysis_id or not drummer_id:
                return False

            now = datetime.utcnow().isoformat()
            analysis_json = analysis_json if isinstance(analysis_json, dict) else {}
            stem_files_used = stem_files_used if isinstance(stem_files_used, dict) else {}

            tempo_bpm = analysis_json.get("tempo")
            tempo_confidence = analysis_json.get("tempo_confidence")
            duration_sec = analysis_json.get("duration")
            total_hits = analysis_json.get("total_hits")

            groove = analysis_json.get("groove_analysis") if isinstance(analysis_json.get("groove_analysis"), dict) else {}
            swing_factor = groove.get("swing_factor")
            pocket_tightness = groove.get("pocket_tightness")
            micro_var = groove.get("micro_timing_variance")
            rhythmic_complexity = groove.get("rhythmic_complexity")
            syncopation_level = groove.get("syncopation_level")
            humanness_score = groove.get("humanness_score")

            raw_analysis_json = json.dumps(analysis_json, default=str)
            stem_files_used_json = json.dumps(stem_files_used, default=str)

            if getattr(self, "_engine", None) is not None:
                params = {
                    "analysis_id": analysis_id,
                    "song_id": song_id,
                    "drummer_pk": drummer_id,
                    "source_file": source_file,
                    "mvsep_output_dir": mvsep_output_dir,
                    "tempo_bpm": float(tempo_bpm) if isinstance(tempo_bpm, (int, float)) else None,
                    "tempo_confidence": float(tempo_confidence) if isinstance(tempo_confidence, (int, float)) else None,
                    "time_signature": (analysis_json.get("time_signature") if isinstance(analysis_json.get("time_signature"), str) else None),
                    "duration_sec": float(duration_sec) if isinstance(duration_sec, (int, float)) else None,
                    "groove_swing_factor": float(swing_factor) if isinstance(swing_factor, (int, float)) else None,
                    "groove_pocket_tightness": float(pocket_tightness) if isinstance(pocket_tightness, (int, float)) else None,
                    "groove_micro_timing_variance": float(micro_var) if isinstance(micro_var, (int, float)) else None,
                    "rhythmic_complexity": float(rhythmic_complexity) if isinstance(rhythmic_complexity, (int, float)) else None,
                    "syncopation_level": float(syncopation_level) if isinstance(syncopation_level, (int, float)) else None,
                    "humanness_score": float(humanness_score) if isinstance(humanness_score, (int, float)) else None,
                    "total_hits": int(total_hits) if isinstance(total_hits, int) else None,
                    "stem_files_used_json": stem_files_used_json,
                    "raw_analysis_json": raw_analysis_json,
                    "analysis_version": (analysis_version or "").strip() or None,
                }
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.drummers (id, drummer_id, slug, display_name, name)
                            VALUES (:drummer_pk, :drummer_pk, :drummer_pk, :drummer_pk, :drummer_pk)
                            ON CONFLICT (id) DO NOTHING
                            """
                        ),
                        {"drummer_pk": drummer_id},
                    )
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.song_performance_analysis (
                                analysis_id, song_id, drummer_id, source_file, mvsep_output_dir,
                                tempo_bpm, tempo_confidence, time_signature, duration_sec,
                                groove_swing_factor, groove_pocket_tightness, groove_micro_timing_variance,
                                rhythmic_complexity, syncopation_level, humanness_score,
                                total_hits, stem_files_used_json, raw_analysis_json, analysis_version, created_at, updated_at
                            ) VALUES (
                                :analysis_id, :song_id, :drummer_pk, :source_file, :mvsep_output_dir,
                                :tempo_bpm, :tempo_confidence, :time_signature, :duration_sec,
                                :groove_swing_factor, :groove_pocket_tightness, :groove_micro_timing_variance,
                                :rhythmic_complexity, :syncopation_level, :humanness_score,
                                :total_hits, :stem_files_used_json, :raw_analysis_json, :analysis_version, NOW(), NOW()
                            )
                            ON CONFLICT (analysis_id) DO UPDATE SET
                                song_id=EXCLUDED.song_id,
                                drummer_id=EXCLUDED.drummer_id,
                                source_file=EXCLUDED.source_file,
                                mvsep_output_dir=EXCLUDED.mvsep_output_dir,
                                tempo_bpm=EXCLUDED.tempo_bpm,
                                tempo_confidence=EXCLUDED.tempo_confidence,
                                time_signature=EXCLUDED.time_signature,
                                duration_sec=EXCLUDED.duration_sec,
                                groove_swing_factor=EXCLUDED.groove_swing_factor,
                                groove_pocket_tightness=EXCLUDED.groove_pocket_tightness,
                                groove_micro_timing_variance=EXCLUDED.groove_micro_timing_variance,
                                rhythmic_complexity=EXCLUDED.rhythmic_complexity,
                                syncopation_level=EXCLUDED.syncopation_level,
                                humanness_score=EXCLUDED.humanness_score,
                                total_hits=EXCLUDED.total_hits,
                                stem_files_used_json=EXCLUDED.stem_files_used_json,
                                raw_analysis_json=EXCLUDED.raw_analysis_json,
                                analysis_version=EXCLUDED.analysis_version,
                                updated_at=NOW()
                            """
                        ),
                        params,
                    )
                self.data_changed.emit("song_performance_analysis", "upsert")
                return True

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write():
                drummer_fk = self._ensure_drummer_exists(cursor=cursor, drummer_id=drummer_id)
                cursor.execute(
                    """
                INSERT INTO song_performance_analysis (
                    analysis_id, song_id, drummer_id, source_file, mvsep_output_dir,
                    tempo_bpm, tempo_confidence, duration_sec,
                    groove_swing_factor, groove_pocket_tightness, groove_micro_timing_variance,
                    rhythmic_complexity, syncopation_level, humanness_score,
                    total_hits,
                    stem_files_used_json, raw_analysis_json,
                    analysis_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(analysis_id) DO UPDATE SET
                    song_id=excluded.song_id,
                    drummer_id=excluded.drummer_id,
                    source_file=excluded.source_file,
                    mvsep_output_dir=excluded.mvsep_output_dir,
                    tempo_bpm=excluded.tempo_bpm,
                    tempo_confidence=excluded.tempo_confidence,
                    duration_sec=excluded.duration_sec,
                    groove_swing_factor=excluded.groove_swing_factor,
                    groove_pocket_tightness=excluded.groove_pocket_tightness,
                    groove_micro_timing_variance=excluded.groove_micro_timing_variance,
                    rhythmic_complexity=excluded.rhythmic_complexity,
                    syncopation_level=excluded.syncopation_level,
                    humanness_score=excluded.humanness_score,
                    total_hits=excluded.total_hits,
                    stem_files_used_json=excluded.stem_files_used_json,
                    raw_analysis_json=excluded.raw_analysis_json,
                    analysis_version=excluded.analysis_version,
                    updated_at=excluded.updated_at
                """,
                    (
                    analysis_id,
                    song_id,
                    drummer_fk,
                    source_file,
                    mvsep_output_dir,
                    float(tempo_bpm) if isinstance(tempo_bpm, (int, float)) else None,
                    float(tempo_confidence) if isinstance(tempo_confidence, (int, float)) else None,
                    float(duration_sec) if isinstance(duration_sec, (int, float)) else None,
                    float(swing_factor) if isinstance(swing_factor, (int, float)) else None,
                    float(pocket_tightness) if isinstance(pocket_tightness, (int, float)) else None,
                    float(micro_var) if isinstance(micro_var, (int, float)) else None,
                    float(rhythmic_complexity) if isinstance(rhythmic_complexity, (int, float)) else None,
                    float(syncopation_level) if isinstance(syncopation_level, (int, float)) else None,
                    float(humanness_score) if isinstance(humanness_score, (int, float)) else None,
                    int(total_hits) if isinstance(total_hits, int) else None,
                    stem_files_used_json,
                    raw_analysis_json,
                    (analysis_version or "").strip() or None,
                    now,
                    now,
                    ),
                )
                conn.commit()

            self._with_write_lock_retry(_do_write)
            self.data_changed.emit("song_performance_analysis", "upsert")
            return True
        except sqlite3.OperationalError as e:
            msg = f"Error upserting song_performance_analysis: {str(e)}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return False
        except Exception as e:
            msg = f"Error upserting song_performance_analysis: {str(e)}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return False

    def ingest_processed_stems_song_folder(
        self,
        *,
        drummer_id: str,
        song_folder: str,
        compute_hashes: bool = False,
        hash_max_bytes: int = 0,
        analysis_version: str = "baseline_v1",
    ) -> Optional[str]:
        try:
            self._set_last_ingest_error("")
            drummer_id = (drummer_id or "").strip()
            song_folder = str(song_folder or "").strip()
            if not drummer_id or not song_folder:
                self._set_last_ingest_error("Missing drummer_id or song_folder")
                return None
            if not os.path.isdir(song_folder):
                self._set_last_ingest_error(f"Not a directory: {song_folder}")
                return None

            drum_analysis_path = os.path.join(song_folder, "drum_analysis.json")
            if not os.path.exists(drum_analysis_path):
                self._set_last_ingest_error(f"Missing drum_analysis.json: {song_folder}")
                return None

            analysis_json = {}
            try:
                with open(drum_analysis_path, "r", encoding="utf-8") as f:
                    analysis_json = json.load(f)
            except Exception:
                analysis_json = {}

            stem_files_used = {}
            try:
                stem_files_used = analysis_json.get("stem_files_used") if isinstance(analysis_json, dict) else {}
            except Exception:
                stem_files_used = {}
            if not isinstance(stem_files_used, dict):
                stem_files_used = {}

            # Resolve stem file paths against current song_folder if archived paths differ
            resolved_stems: Dict[str, Any] = {}
            try:
                for stem_name, p in stem_files_used.items():
                    p = str(p or "").strip()
                    found = ""
                    if p and os.path.exists(p):
                        found = p
                    else:
                        base = os.path.basename(p) if p else ""
                        cand1 = os.path.join(song_folder, base) if base else ""
                        cand2 = os.path.join(song_folder, "drumsep_components", base) if base else ""
                        if cand1 and os.path.exists(cand1):
                            found = cand1
                        elif cand2 and os.path.exists(cand2):
                            found = cand2
                    if found:
                        resolved_stems[stem_name] = found
            except Exception:
                resolved_stems = {}
            if not resolved_stems:
                # Fallback: scan common locations under song_folder
                try:
                    for root in (song_folder, os.path.join(song_folder, "drumsep_components")):
                        if not os.path.isdir(root):
                            continue
                        for fn in os.listdir(root):
                            if not fn.lower().endswith(".wav"):
                                continue
                            name = os.path.splitext(fn)[0]
                            resolved_stems[name] = os.path.join(root, fn)
                except Exception:
                    pass
            try:
                ds_dir = os.path.join(song_folder, "drumsep_components")
                if os.path.isdir(ds_dir):
                    for fn in os.listdir(ds_dir):
                        if not fn.lower().endswith(".wav"):
                            continue
                        name = os.path.splitext(fn)[0]
                        path = os.path.join(ds_dir, fn)
                        if name not in resolved_stems and os.path.exists(path):
                            resolved_stems[name] = path
            except Exception:
                pass

            analysis_id = str(uuid.uuid4())
            source_file = ""
            try:
                candidates = [p for p in os.listdir(song_folder) if p.lower().endswith((".mp3", ".wav", ".flac", ".m4a"))]
                if candidates:
                    source_file = os.path.join(song_folder, sorted(candidates)[0])
            except Exception:
                source_file = ""

            source_file_storage = source_file
            stem_storage_paths: Dict[str, Any] = {}
            storage_bucket = str(os.getenv("AWS_S3_BUCKET") or "").strip()
            storage_region = str(os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "").strip()
            storage_client = None
            upload_cache: Dict[str, str] = {}
            if getattr(self, "_engine", None) is not None and storage_bucket and storage_region:
                try:
                    import boto3  # type: ignore

                    storage_client = boto3.client("s3", region_name=storage_region)
                except Exception as storage_exc:
                    logger.warning("ingest_processed_stems_song_folder S3 client unavailable: %s", storage_exc)

            def _to_cloud_storage_uri(local_path: str, *, category: str) -> str:
                value = str(local_path or "").strip()
                if not value:
                    return ""
                lower = value.lower()
                if lower.startswith(("https://", "http://", "s3://", "supabase://", "gs://", "r2://")):
                    return value
                if not os.path.exists(value) or not os.path.isfile(value):
                    return value
                cached = upload_cache.get(value)
                if cached:
                    return cached
                if storage_client is None:
                    return value
                safe_name = os.path.basename(value)
                key = f"drummers/{drummer_id}/assimilation/{analysis_id}/{category}/{safe_name}"
                try:
                    storage_client.upload_file(value, storage_bucket, key)
                    cloud_uri = f"s3://{storage_bucket}/{key}"
                    upload_cache[value] = cloud_uri
                    return cloud_uri
                except Exception as upload_exc:
                    logger.warning("ingest_processed_stems_song_folder upload failed path=%s err=%s", value, upload_exc)
                    return value

            if source_file:
                source_file_storage = _to_cloud_storage_uri(source_file, category="source")
            for stem_name, stem_path in resolved_stems.items():
                stem_storage_paths[stem_name] = _to_cloud_storage_uri(str(stem_path), category=f"stem_{stem_name}")
            stem_files_used_for_db = stem_storage_paths or resolved_stems

            song_title = os.path.basename(song_folder).strip()
            try:
                meta_path = os.path.join(song_folder, "song_meta.json")
                if os.path.exists(meta_path):
                    with open(meta_path, "r", encoding="utf-8") as mf:
                        m = json.load(mf)
                    t = (m.get("title") or m.get("song_name") or "").strip()
                    if t:
                        song_title = t
            except Exception:
                pass
            try:
                duration_val = analysis_json.get("duration") if isinstance(analysis_json, dict) else None
                duration_val = float(duration_val) if isinstance(duration_val, (int, float)) else None
            except Exception:
                duration_val = None

            if getattr(self, "_engine", None) is not None:
                ok = self.upsert_song_performance_analysis(
                    analysis_id=analysis_id,
                    drummer_id=drummer_id,
                    song_id=None,
                    source_file=source_file_storage,
                    mvsep_output_dir=song_folder,
                    analysis_json=analysis_json,
                    stem_files_used=stem_files_used_for_db,
                    analysis_version=analysis_version,
                )
                if not ok:
                    return None
                now_iso = datetime.utcnow().isoformat()
                with self._engine.begin() as conn_pg:
                    try:
                        sid = str(uuid.uuid4())
                        conn_pg.execute(
                            text(
                                """
                                INSERT INTO public.songs (
                                    id, title, artist, album, year, genre, duration, file_path, drummer_id, created_at, updated_at
                                ) VALUES (
                                    :id, :title, NULL, NULL, NULL, NULL, :duration, :file_path, :drummer_id, NOW(), NOW()
                                ) ON CONFLICT (id) DO NOTHING
                                """
                            ),
                            {
                                "id": sid,
                                "title": song_title or None,
                                "duration": duration_val,
                                "file_path": source_file_storage or None,
                                "drummer_id": drummer_id,
                            },
                        )
                        conn_pg.execute(
                            text("UPDATE public.song_performance_analysis SET song_id = :sid WHERE analysis_id = :aid"),
                            {"sid": sid, "aid": analysis_id},
                        )
                    except Exception:
                        pass
                    def _insert_artifact_pg(role: str, path: str, fmt: str = "", *, storage_path: Optional[str] = None):
                        try:
                            storage_value = str(storage_path if storage_path is not None else path or "").strip()
                            if not storage_value:
                                return
                            local_path = str(path or "").strip()
                            stat = os.stat(local_path) if local_path and os.path.exists(local_path) else None
                            sha = ""
                            if compute_hashes and stat is not None:
                                sha = self._sha256_file(local_path, max_bytes=hash_max_bytes)
                            conn_pg.execute(
                                text(
                                    """
                                    INSERT INTO public.analysis_artifacts (
                                        artifact_id, analysis_id, drummer_id, song_id,
                                        artifact_role, file_path, file_format,
                                        file_size_bytes, file_mtime_epoch, sha256,
                                        extractor_name, extractor_version,
                                        created_at
                                    ) VALUES (
                                        :artifact_id, :analysis_id, :drummer_id, :song_id,
                                        :artifact_role, :file_path, :file_format,
                                        :file_size_bytes, :file_mtime_epoch, :sha256,
                                        :extractor_name, :extractor_version,
                                        :created_at
                                    )
                                    """
                                ),
                                {
                                    "artifact_id": str(uuid.uuid4()),
                                    "analysis_id": analysis_id,
                                    "drummer_id": drummer_id,
                                    "song_id": None,
                                    "artifact_role": role,
                                    "file_path": storage_value,
                                    "file_format": (fmt or "").strip() or None,
                                    "file_size_bytes": int(stat.st_size) if stat is not None else None,
                                    "file_mtime_epoch": float(stat.st_mtime) if stat is not None else None,
                                    "sha256": sha or None,
                                    "extractor_name": "processed_stems_ingest",
                                    "extractor_version": (analysis_version or "").strip() or None,
                                    "created_at": now_iso,
                                },
                            )
                        except Exception:
                            return

                    _insert_artifact_pg("drum_analysis_json", drum_analysis_path, "json")
                    prof = os.path.join(song_folder, "drummer_profile.json")
                    _insert_artifact_pg("drummer_profile_json", prof, "json")
                    if source_file:
                        _insert_artifact_pg(
                            "source_audio",
                            source_file,
                            Path(source_file).suffix.lower().lstrip("."),
                            storage_path=source_file_storage,
                        )

                    for stem_name, path in resolved_stems.items():
                        try:
                            if not path or not os.path.exists(path):
                                continue
                            stat = os.stat(path)
                            sha = ""
                            if compute_hashes:
                                sha = self._sha256_file(path, max_bytes=hash_max_bytes)
                            storage_path = str(stem_storage_paths.get(stem_name) or path)
                            conn_pg.execute(
                                text(
                                    """
                                    INSERT INTO public.stem_artifacts (
                                        stem_id, analysis_id, drummer_id, song_id,
                                        stem_name, file_path,
                                        file_size_bytes, file_mtime_epoch, sha256,
                                        created_at
                                    ) VALUES (
                                        :stem_id, :analysis_id, :drummer_id, :song_id,
                                        :stem_name, :file_path,
                                        :file_size_bytes, :file_mtime_epoch, :sha256,
                                        :created_at
                                    )
                                    """
                                ),
                                {
                                    "stem_id": str(uuid.uuid4()),
                                    "analysis_id": analysis_id,
                                    "drummer_id": drummer_id,
                                    "song_id": None,
                                    "stem_name": str(stem_name),
                                    "file_path": storage_path,
                                    "file_size_bytes": int(stat.st_size),
                                    "file_mtime_epoch": float(stat.st_mtime),
                                    "sha256": sha or None,
                                    "created_at": now_iso,
                                },
                            )
                        except Exception:
                            continue
                self.data_changed.emit("analysis_artifacts", "insert")
                self.data_changed.emit("stem_artifacts", "insert")
                return analysis_id

            conn = self._get_connection()
            cursor = conn.cursor()
            drummer_fk = self._ensure_drummer_exists(cursor=cursor, drummer_id=drummer_id)
            conn.commit()

            ok = self.upsert_song_performance_analysis(
                analysis_id=analysis_id,
                drummer_id=drummer_id,
                song_id=None,
                source_file=source_file,
                mvsep_output_dir=song_folder,
                analysis_json=analysis_json,
                stem_files_used=resolved_stems,
                analysis_version=analysis_version,
            )
            if not ok:
                return None

            now = datetime.utcnow().isoformat()
            conn = self._get_connection()
            cursor = conn.cursor()

            def _insert_artifact(role: str, path: str, fmt: str = ""):
                try:
                    if not path or not os.path.exists(path):
                        return
                    stat = os.stat(path)
                    sha = ""
                    if compute_hashes:
                        sha = self._sha256_file(path, max_bytes=hash_max_bytes)
                    cursor.execute(
                        """
                        INSERT INTO analysis_artifacts (
                            artifact_id, analysis_id, drummer_id, song_id,
                            artifact_role, file_path, file_format,
                            file_size_bytes, file_mtime_epoch, sha256,
                            extractor_name, extractor_version,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            analysis_id,
                            drummer_fk,
                            None,
                            role,
                            str(path),
                            (fmt or "").strip() or None,
                            int(stat.st_size),
                            float(stat.st_mtime),
                            sha or None,
                            "processed_stems_ingest",
                            (analysis_version or "").strip() or None,
                            now,
                        ),
                    )
                except Exception:
                    return

            _insert_artifact("drum_analysis_json", drum_analysis_path, "json")
            prof = os.path.join(song_folder, "drummer_profile.json")
            _insert_artifact("drummer_profile_json", prof, "json")

            for stem_name, path in resolved_stems.items():
                try:
                    if not path or not os.path.exists(path):
                        continue
                    stat = os.stat(path)
                    sha = ""
                    if compute_hashes:
                        sha = self._sha256_file(path, max_bytes=hash_max_bytes)
                    cursor.execute(
                        """
                        INSERT INTO stem_artifacts (
                            stem_id, analysis_id, drummer_id, song_id,
                            stem_name, file_path,
                            file_size_bytes, file_mtime_epoch, sha256,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid.uuid4()),
                            analysis_id,
                            drummer_fk,
                            None,
                            str(stem_name),
                            str(path),
                            int(stat.st_size),
                            float(stat.st_mtime),
                            sha or None,
                            now,
                        ),
                    )
                except Exception:
                    continue

            def _commit_all():
                conn.commit()

            self._with_write_lock_retry(_commit_all)
            try:
                sid = str(uuid.uuid4())
                cursor.execute(
                    """
                    INSERT INTO songs (
                        id, title, artist, album, year, genre, duration, file_path, drummer_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        sid,
                        song_title or None,
                        None,
                        None,
                        None,
                        None,
                        float(duration_val) if duration_val is not None else None,
                        source_file or None,
                        int(drummer_fk) if drummer_fk is not None else None,
                        now,
                        now,
                    ),
                )
                cursor.execute(
                    "UPDATE song_performance_analysis SET song_id = ? WHERE analysis_id = ?",
                    (sid, analysis_id),
                )
                conn.commit()
            except Exception:
                pass
            self.data_changed.emit("analysis_artifacts", "insert")
            self.data_changed.emit("stem_artifacts", "insert")
            return analysis_id
        except sqlite3.OperationalError as e:
            msg = f"Error ingesting processed stems folder: {str(e)}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return None
        except Exception as e:
            msg = f"Error ingesting processed stems folder: {str(e)}"
            logger.error(msg)
            self._set_last_ingest_error(msg)
            self.database_error.emit(msg)
            return None

    def list_drummer_presets(self, profile_type: str) -> List[Dict[str, Any]]:
        try:
            profile_type = (profile_type or "").strip().lower()
            if not profile_type:
                return []

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text(
                            """
                            SELECT preset_id, profile_type, name, tier,
                                   deltas_json, policies_json,
                                   source_type, source_song_name, source_ref
                            FROM public.drummer_presets
                            WHERE profile_type = :profile_type
                            ORDER BY tier DESC, name
                            """
                        ),
                        {"profile_type": profile_type},
                    ).mappings().all()

                out: List[Dict[str, Any]] = []
                for row in rows:
                    deltas = {}
                    policies = {}
                    try:
                        deltas = json.loads(row.get("deltas_json")) if row.get("deltas_json") else {}
                    except Exception:
                        deltas = {}
                    try:
                        policies = json.loads(row.get("policies_json")) if row.get("policies_json") else {}
                    except Exception:
                        policies = {}
                    out.append(
                        {
                            "preset_id": row.get("preset_id"),
                            "profile_type": row.get("profile_type"),
                            "name": row.get("name"),
                            "tier": row.get("tier"),
                            "deltas": deltas,
                            "policies": policies,
                            "source_type": row.get("source_type"),
                            "source_song_name": row.get("source_song_name"),
                            "source_ref": row.get("source_ref"),
                        }
                    )
                return out

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT preset_id, profile_type, name, tier,
                       deltas_json, policies_json,
                       source_type, source_song_name, source_ref
                FROM drummer_presets
                WHERE profile_type = ?
                ORDER BY tier DESC, name
                """,
                (profile_type,),
            )
            rows = cursor.fetchall()
            out: List[Dict[str, Any]] = []
            for row in rows:
                deltas = {}
                policies = {}
                try:
                    deltas = json.loads(row[4]) if row[4] else {}
                except Exception:
                    deltas = {}
                try:
                    policies = json.loads(row[5]) if row[5] else {}
                except Exception:
                    policies = {}
                out.append(
                    {
                        "preset_id": row[0],
                        "profile_type": row[1],
                        "name": row[2],
                        "tier": row[3],
                        "deltas": deltas,
                        "policies": policies,
                        "source_type": row[6],
                        "source_song_name": row[7],
                        "source_ref": row[8],
                    }
                )
            return out
        except sqlite3.OperationalError as e:
            logger.warning(f"drummer_presets table not available: {e}")
            return []
        except Exception as e:
            logger.error(f"Error listing drummer presets: {str(e)}")
            self.database_error.emit(f"Error listing drummer presets: {str(e)}")
            return []

    def get_drummer_preset(self, preset_id: str) -> Optional[Dict[str, Any]]:
        try:
            preset_id = (preset_id or "").strip()
            if not preset_id:
                return None

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    row = conn_pg.execute(
                        text(
                            """
                            SELECT preset_id, profile_type, name, tier,
                                   deltas_json, policies_json,
                                   source_type, source_song_name, source_ref
                            FROM public.drummer_presets
                            WHERE preset_id = :preset_id
                            LIMIT 1
                            """
                        ),
                        {"preset_id": preset_id},
                    ).mappings().first()

                if not row:
                    return None

                deltas = {}
                policies = {}
                try:
                    deltas = json.loads(row.get("deltas_json")) if row.get("deltas_json") else {}
                except Exception:
                    deltas = {}
                try:
                    policies = json.loads(row.get("policies_json")) if row.get("policies_json") else {}
                except Exception:
                    policies = {}

                return {
                    "preset_id": row.get("preset_id"),
                    "profile_type": row.get("profile_type"),
                    "name": row.get("name"),
                    "tier": row.get("tier"),
                    "deltas": deltas,
                    "policies": policies,
                    "source_type": row.get("source_type"),
                    "source_song_name": row.get("source_song_name"),
                    "source_ref": row.get("source_ref"),
                }

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT preset_id, profile_type, name, tier,
                       deltas_json, policies_json,
                       source_type, source_song_name, source_ref
                FROM drummer_presets
                WHERE preset_id = ?
                LIMIT 1
                """,
                (preset_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            deltas = {}
            policies = {}
            try:
                deltas = json.loads(row[4]) if row[4] else {}
            except Exception:
                deltas = {}
            try:
                policies = json.loads(row[5]) if row[5] else {}
            except Exception:
                policies = {}

            return {
                "preset_id": row[0],
                "profile_type": row[1],
                "name": row[2],
                "tier": row[3],
                "deltas": deltas,
                "policies": policies,
                "source_type": row[6],
                "source_song_name": row[7],
                "source_ref": row[8],
            }
        except sqlite3.OperationalError as e:
            logger.warning(f"drummer_presets table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error getting drummer preset {preset_id}: {str(e)}")
            self.database_error.emit(f"Error getting drummer preset: {str(e)}")
            return None

    def upsert_drummer_preset(
        self,
        preset_id: str,
        profile_type: str,
        name: str,
        tier: str,
        deltas: Optional[Dict[str, Any]] = None,
        policies: Optional[Dict[str, Any]] = None,
        source_type: Optional[str] = None,
        source_song_name: Optional[str] = None,
        source_ref: Optional[str] = None,
    ) -> bool:
        try:
            preset_id = (preset_id or "").strip()
            profile_type = (profile_type or "").strip().lower()
            name = (name or "").strip()
            tier = (tier or "").strip().lower()

            if not preset_id or not profile_type or not name or not tier:
                return False

            deltas_json = json.dumps(deltas or {})
            policies_json = json.dumps(policies or {})

            now = datetime.utcnow().isoformat()
            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.drummer_presets (
                                preset_id, profile_type, name, tier,
                                deltas_json, policies_json,
                                source_type, source_song_name, source_ref,
                                created_at, updated_at
                            ) VALUES (
                                :preset_id, :profile_type, :name, :tier,
                                :deltas_json, :policies_json,
                                :source_type, :source_song_name, :source_ref,
                                NOW(), NOW()
                            )
                            ON CONFLICT (preset_id) DO UPDATE SET
                                profile_type = EXCLUDED.profile_type,
                                name = EXCLUDED.name,
                                tier = EXCLUDED.tier,
                                deltas_json = EXCLUDED.deltas_json,
                                policies_json = EXCLUDED.policies_json,
                                source_type = EXCLUDED.source_type,
                                source_song_name = EXCLUDED.source_song_name,
                                source_ref = EXCLUDED.source_ref,
                                updated_at = NOW()
                            """
                        ),
                        {
                            "preset_id": preset_id,
                            "profile_type": profile_type,
                            "name": name,
                            "tier": tier,
                            "deltas_json": deltas_json,
                            "policies_json": policies_json,
                            "source_type": source_type,
                            "source_song_name": source_song_name,
                            "source_ref": source_ref,
                        },
                    )
                return True

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO drummer_presets (
                    preset_id, profile_type, name, tier,
                    deltas_json, policies_json,
                    source_type, source_song_name, source_ref,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(preset_id) DO UPDATE SET
                    profile_type=excluded.profile_type,
                    name=excluded.name,
                    tier=excluded.tier,
                    deltas_json=excluded.deltas_json,
                    policies_json=excluded.policies_json,
                    source_type=excluded.source_type,
                    source_song_name=excluded.source_song_name,
                    source_ref=excluded.source_ref,
                    updated_at=excluded.updated_at
                """,
                (
                    preset_id,
                    profile_type,
                    name,
                    tier,
                    deltas_json,
                    policies_json,
                    source_type,
                    source_song_name,
                    source_ref,
                    now,
                    now,
                ),
            )
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            logger.warning(f"drummer_presets table not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Error upserting drummer preset: {str(e)}")
            self.database_error.emit(f"Error upserting drummer preset: {str(e)}")
            return False

    def upsert_source_video(
        self,
        *,
        drummer_id: Optional[str],
        url: str,
        title: Optional[str] = None,
        channel: Optional[str] = None,
        duration_sec: Optional[int] = None,
        status: Optional[str] = None,
        downloaded_path: Optional[str] = None,
        tags: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        song_id: Optional[str] = None,
    ) -> bool:
        try:
            if getattr(self, "_engine", None) is None:
                return False
            params = {
                "url": (url or "").strip(),
                "title": (title or None),
                "channel": (channel or None),
                "duration_sec": int(duration_sec) if isinstance(duration_sec, int) else None,
                "drummer_id": (drummer_id or None),
                "status": (status or None),
                "downloaded_path": (downloaded_path or None),
                "tags": json.dumps(tags or {}),
                "metadata": json.dumps(metadata or {}),
                "song_id": (song_id or None),
            }
            if not params["url"]:
                return False
            stmt = text(
                """
                INSERT INTO public.source_videos (
                    url, platform, title, channel, duration_sec, drummer_id, status,
                    downloaded_path, tags_json, metadata_json, song_id, created_at, updated_at
                ) VALUES (
                    :url, 'youtube', :title, :channel, :duration_sec, :drummer_id, :status,
                    :downloaded_path, :tags, :metadata, :song_id, NOW(), NOW()
                )
                ON CONFLICT (url) DO UPDATE SET
                    title=EXCLUDED.title,
                    channel=EXCLUDED.channel,
                    duration_sec=EXCLUDED.duration_sec,
                    drummer_id=EXCLUDED.drummer_id,
                    status=EXCLUDED.status,
                    downloaded_path=EXCLUDED.downloaded_path,
                    tags_json=EXCLUDED.tags_json,
                    metadata_json=EXCLUDED.metadata_json,
                    song_id=COALESCE(EXCLUDED.song_id, public.source_videos.song_id),
                    updated_at=NOW()
                """
            )
            with self._engine.begin() as conn:
                conn.execute(stmt, params)
            return True
        except Exception as e:
            msg = f"Error upserting source video: {str(e)}"
            logger.error(msg)
            self.database_error.emit(msg)
            return False

    def _get_connection(self) -> sqlite3.Connection:
        """
        Get a thread-local database connection.
        
        Returns:
            sqlite3.Connection: SQLite connection object
        """
        thread_id = threading.get_ident()
        if thread_id not in self._connections or self._connections[thread_id] is None:
            if self._db_path is None:
                raise ValueError("Database path not set. Call initialize() first.")
                
            conn = sqlite3.connect(self._db_path, timeout=10.0, check_same_thread=False)
            # Enable foreign keys
            conn.execute("PRAGMA foreign_keys = ON")
            # Make the GUI more robust against concurrent access.
            # WAL allows concurrent readers while a writer is active.
            # busy_timeout makes SQLite wait a bit instead of raising 'database is locked'.
            try:
                conn.execute("PRAGMA journal_mode = WAL")
            except Exception:
                pass
            try:
                conn.execute("PRAGMA busy_timeout = 5000")
            except Exception:
                pass
            # Configure for dictionary results
            conn.row_factory = sqlite3.Row
            self._connections[thread_id] = conn
            
        return self._connections[thread_id]

    def _create_tables(self) -> None:
        """Create database tables if they don't exist"""
        if self._tables_created:
            return
            
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            # Create drummers table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''')
            
            # Create songs table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS songs (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT,
                album TEXT,
                year INTEGER,
                genre TEXT,
                duration REAL,
                file_path TEXT,
                drummer_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')
            
            # Create drum_beats table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drum_beats (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                file_path TEXT,
                song_id TEXT,
                drummer_id TEXT,
                bpm REAL,
                time_signature TEXT,
                complexity REAL,
                energy REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE,
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')
            
            # Create processing_metadata table
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS processing_metadata (
                id TEXT PRIMARY KEY,
                entity_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                process_type TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata TEXT,  -- JSON string
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''')
            # Note: drummer_personas lives in the admin DB and is created by
            # admin/tools/init_drummer_personas_table.py. We don't create it
            # here to avoid surprising frontends that use a different DB
            # layout, but we *do* provide read helpers below if it exists.

            # Admin-only mapping of public DrumTracKAI drummer categories to
            # analysis personas & default knob settings. This lives in the same
            # DB so both the admin tools and backend can share it.
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_category_mappings (
                category_id TEXT PRIMARY KEY,
                display_name TEXT,
                primary_persona_id TEXT,
                backup_persona_ids_json TEXT,
                default_humanize REAL,
                default_swing REAL,
                default_chorus_ride_pref REAL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_presets (
                preset_id TEXT PRIMARY KEY,
                profile_type TEXT NOT NULL,
                name TEXT NOT NULL,
                tier TEXT NOT NULL,
                deltas_json TEXT,
                policies_json TEXT,
                source_type TEXT,
                source_song_name TEXT,
                source_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS run_versions (
                run_id TEXT PRIMARY KEY,
                generator_version TEXT NOT NULL,
                feature_version TEXT NOT NULL,
                rollup_version TEXT NOT NULL,
                sample_pack_version TEXT NOT NULL,
                seed INTEGER NOT NULL,
                commit_hash TEXT,
                created_at TEXT NOT NULL
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS audio_artifacts (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT,
                artifact_type TEXT NOT NULL,
                storage_uri TEXT NOT NULL,
                duration_sec REAL,
                loudness_lufs REAL,
                sample_pack_version TEXT,
                render_recipe_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES calibration_runs(run_id)
            )
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_audio_artifacts_run_id ON audio_artifacts(run_id)
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS calibration_run_events (
                run_id TEXT PRIMARY KEY,
                drummer_slug TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'dcsm_json',
                event_stream_json TEXT NOT NULL DEFAULT '[]',
                tempo_bpm REAL,
                time_signature_json TEXT,
                bars INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS calibration_render_jobs (
                job_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                render_profile_id TEXT NOT NULL,
                sample_pack_version TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                artifact_ids_json TEXT NOT NULL DEFAULT '[]',
                error_text TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                FOREIGN KEY (run_id) REFERENCES calibration_runs(run_id)
            )
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_calibration_render_jobs_run_id ON calibration_render_jobs(run_id)
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS reviewer_profiles (
                reviewer_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                expertise_level TEXT,
                primary_styles_json TEXT NOT NULL DEFAULT '[]',
                years_experience INTEGER,
                weighting_factor REAL NOT NULL DEFAULT 1.0,
                created_at TEXT NOT NULL
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS evaluation_sessions (
                session_id TEXT PRIMARY KEY,
                reviewer_id TEXT NOT NULL,
                target_drummer_slug TEXT NOT NULL,
                assigned_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                app_version TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (reviewer_id) REFERENCES reviewer_profiles(reviewer_id)
            )
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_eval_sessions_reviewer ON evaluation_sessions(reviewer_id)
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS evaluation_items (
                item_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                base_groove_id TEXT NOT NULL,
                target_drummer_slug TEXT NOT NULL,
                reference_artifact_id TEXT,
                baseline_run_id TEXT,
                candidate_a_run_id TEXT,
                candidate_b_run_id TEXT,
                ab_mapping_json TEXT NOT NULL DEFAULT '{}',
                eval_mode TEXT NOT NULL DEFAULT 'AB',
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES evaluation_sessions(session_id)
            )
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_eval_items_session ON evaluation_items(session_id)
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS pairwise_judgments (
                judgment_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                preferred_candidate TEXT,
                closer_to_target TEXT,
                better_feel TEXT,
                more_musical TEXT,
                confidence INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (item_id) REFERENCES evaluation_items(item_id)
            )
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_pairwise_item ON pairwise_judgments(item_id)
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS attribute_ratings (
                rating_id TEXT PRIMARY KEY,
                item_id TEXT NOT NULL,
                candidate_label TEXT NOT NULL,
                stylistic_authenticity REAL,
                groove_feel REAL,
                dynamics REAL,
                phrasing REAL,
                kit_balance REAL,
                fill_behavior REAL,
                human_realism REAL,
                overall_usefulness REAL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (item_id) REFERENCES evaluation_items(item_id)
            )
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_attr_item ON attribute_ratings(item_id)
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS song_performance_analysis (
                analysis_id TEXT PRIMARY KEY,
                song_id TEXT,
                drummer_id TEXT,
                source_file TEXT,
                mvsep_output_dir TEXT,
                tempo_bpm REAL,
                tempo_confidence REAL,
                time_signature TEXT,
                duration_sec REAL,
                groove_swing_factor REAL,
                groove_pocket_tightness REAL,
                groove_micro_timing_variance REAL,
                rhythmic_complexity REAL,
                syncopation_level REAL,
                humanness_score REAL,
                total_hits INTEGER,
                hit_counts_json TEXT,
                hit_density_json TEXT,
                dynamics_json TEXT,
                fills_summary_json TEXT,
                rudiments_summary_json TEXT,
                techniques_json TEXT,
                stem_files_used_json TEXT,
                raw_analysis_json TEXT,
                analysis_version TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE SET NULL,
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE SET NULL
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drum_hit_events (
                event_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                instrument TEXT NOT NULL,
                component TEXT,
                onset_time_sec REAL NOT NULL,
                onset_strength REAL,
                velocity_est REAL,
                beat_index INTEGER,
                bar_index INTEGER,
                subdivision TEXT,
                timing_offset_ms REAL,
                is_ghost INTEGER,
                is_accent INTEGER,
                is_flams_like INTEGER,
                is_roll_like INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (analysis_id) REFERENCES song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS fill_events (
                fill_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                start_time_sec REAL NOT NULL,
                end_time_sec REAL NOT NULL,
                start_bar_index INTEGER,
                end_bar_index INTEGER,
                hit_count INTEGER,
                instruments_json TEXT,
                density_per_sec REAL,
                classification TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (analysis_id) REFERENCES song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS technique_events (
                technique_event_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                start_time_sec REAL,
                end_time_sec REAL,
                technique_type TEXT NOT NULL,
                technique_name TEXT,
                confidence REAL,
                details_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (analysis_id) REFERENCES song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_profile_rollups (
                rollup_id TEXT PRIMARY KEY,
                drummer_id INTEGER NOT NULL UNIQUE,
                rollup_version TEXT,
                rollup_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS analysis_artifacts (
                artifact_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                artifact_role TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_format TEXT,
                file_size_bytes INTEGER,
                file_mtime_epoch REAL,
                sha256 TEXT,
                extractor_name TEXT,
                extractor_version TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (analysis_id) REFERENCES song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS stem_artifacts (
                stem_id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT,
                song_id TEXT,
                stem_name TEXT NOT NULL,
                file_path TEXT NOT NULL,
                file_size_bytes INTEGER,
                file_mtime_epoch REAL,
                sha256 TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (analysis_id) REFERENCES song_performance_analysis(analysis_id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_phrase_features (
                id TEXT PRIMARY KEY,
                analysis_id TEXT NOT NULL,
                drummer_id TEXT NOT NULL,
                song_id TEXT NOT NULL,
                section_label TEXT,
                phrase_index INTEGER,
                phrase_length_bars INTEGER,
                bar_position_in_phrase INTEGER,
                energy_start REAL,
                energy_end REAL,
                energy_slope REAL,
                pattern_repetition_score REAL,
                pattern_mutation_rate REAL,
                density_curve_json TEXT,
                accent_curve_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (analysis_id) REFERENCES song_performance_analysis(analysis_id) ON DELETE CASCADE,
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE,
                FOREIGN KEY (song_id) REFERENCES songs(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_microtiming_profiles (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                instrument TEXT,
                subdivision TEXT,
                mean_offset_ms REAL,
                std_offset_ms REAL,
                skew_offset_ms REAL,
                early_hit_probability REAL,
                late_hit_probability REAL,
                pocket_bias TEXT,
                context_label TEXT,
                histogram_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_dynamic_profiles (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                instrument TEXT,
                velocity_mean REAL,
                velocity_std REAL,
                velocity_skew REAL,
                ghost_note_probability REAL,
                accent_probability REAL,
                ghost_to_accent_ratio REAL,
                accent_grid_json TEXT,
                velocity_histogram_json TEXT,
                phrase_dynamic_curve_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_cymbal_language (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                hihat_closed_ratio REAL,
                hihat_open_ratio REAL,
                hihat_pedal_ratio REAL,
                hihat_bark_probability REAL,
                ride_usage_ratio REAL,
                ride_bell_probability REAL,
                crash_frequency_per_min REAL,
                crash_on_downbeat_probability REAL,
                crash_on_transition_probability REAL,
                cymbal_decay_spacing_score REAL,
                cymbal_density_curve_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_limb_coordination (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                simultaneous_hit_matrix_json TEXT,
                kick_snare_dependency REAL,
                kick_hat_dependency REAL,
                snare_hat_dependency REAL,
                independence_score REAL,
                syncopation_score REAL,
                limb_feasibility_violation_rate REAL,
                common_limb_patterns_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_fill_behavior (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                section_label TEXT,
                phrase_position TEXT,
                fill_probability REAL,
                fill_length_mean_beats REAL,
                fill_length_std_beats REAL,
                fill_density_mean REAL,
                tom_usage_probability REAL,
                snare_fill_probability REAL,
                kick_fill_probability REAL,
                cymbal_exit_probability REAL,
                triplet_fill_probability REAL,
                linear_fill_probability REAL,
                rudimental_fill_probability REAL,
                common_fill_shapes_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS drummer_personality_embeddings (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                model_version TEXT NOT NULL,
                embedding_dim INTEGER NOT NULL,
                embedding_vector_json TEXT NOT NULL,
                source_song_count INTEGER,
                source_hit_count INTEGER,
                confidence_score REAL,
                timing_weight REAL,
                dynamics_weight REAL,
                fill_weight REAL,
                cymbal_weight REAL,
                coordination_weight REAL,
                phrase_weight REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS generated_drummer_transform_audits (
                id TEXT PRIMARY KEY,
                source_track_id TEXT,
                target_drummer_id TEXT,
                generation_run_id TEXT,
                personality_embedding_id TEXT,
                source_similarity_score REAL,
                target_similarity_score REAL,
                human_feasibility_score REAL,
                groove_preservation_score REAL,
                before_features_json TEXT,
                after_features_json TEXT,
                transform_delta_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (target_drummer_id) REFERENCES drummers(id) ON DELETE SET NULL,
                FOREIGN KEY (personality_embedding_id) REFERENCES drummer_personality_embeddings(id) ON DELETE SET NULL
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS calibration_adjustments (
                drummer_slug TEXT PRIMARY KEY,
                adjustments_json TEXT NOT NULL,
                metadata_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS calibration_runs (
                run_id TEXT PRIMARY KEY,
                drummer_slug TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                outcome TEXT NOT NULL,
                note_count INTEGER,
                fills_per_minute REAL,
                within_tolerance_count INTEGER,
                total_compared INTEGER,
                delta_summary TEXT,
                metadata_json TEXT,
                metrics_json TEXT,
                comparison_json TEXT,
                log_path TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS calibration_feedback (
                feedback_id TEXT PRIMARY KEY,
                drummer_slug TEXT NOT NULL,
                rating INTEGER NOT NULL,
                comment TEXT,
                author TEXT,
                submitted_at TEXT NOT NULL,
                metadata_json TEXT
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS app_user_roles (
                user_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_drummer_map (
                user_id TEXT NOT NULL,
                drummer_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (user_id, drummer_id),
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS calibration_audit_log (
                id TEXT PRIMARY KEY,
                actor_user_id TEXT,
                drummer_id TEXT,
                run_id TEXT,
                action TEXT NOT NULL,
                payload_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS analysis_jobs (
                id TEXT PRIMARY KEY,
                drummer_id TEXT NOT NULL,
                status TEXT NOT NULL,
                input_json TEXT,
                result_json TEXT,
                error_text TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                FOREIGN KEY (drummer_id) REFERENCES drummers(id) ON DELETE CASCADE
            )
            ''')

            cursor.execute('CREATE INDEX IF NOT EXISTS idx_spa_drummer_id ON song_performance_analysis(drummer_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_spa_song_id ON song_performance_analysis(song_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_hit_analysis_time ON drum_hit_events(analysis_id, onset_time_sec)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_hit_drummer_instrument_time ON drum_hit_events(drummer_id, instrument, onset_time_sec)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_fill_analysis_time ON fill_events(analysis_id, start_time_sec)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_tech_analysis_type ON technique_events(analysis_id, technique_type)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_rollups_drummer_id ON drummer_profile_rollups(drummer_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_artifacts_analysis_role ON analysis_artifacts(analysis_id, artifact_role)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_stems_analysis_name ON stem_artifacts(analysis_id, stem_name)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_phrase_features_analysis ON drummer_phrase_features(analysis_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_phrase_features_drummer ON drummer_phrase_features(drummer_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_microtiming_drummer_context ON drummer_microtiming_profiles(drummer_id, instrument, context_label)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_dynamics_drummer_instrument ON drummer_dynamic_profiles(drummer_id, instrument)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_cymbal_language_drummer ON drummer_cymbal_language(drummer_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_limb_coordination_drummer ON drummer_limb_coordination(drummer_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_fill_behavior_drummer_section ON drummer_fill_behavior(drummer_id, section_label, phrase_position)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_embeddings_drummer_version ON drummer_personality_embeddings(drummer_id, model_version)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_transform_audits_target_run ON generated_drummer_transform_audits(target_drummer_id, generation_run_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_calibration_runs_slug ON calibration_runs(drummer_slug)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_calibration_runs_start ON calibration_runs(started_at)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_calibration_feedback_slug ON calibration_feedback(drummer_slug)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_drummer_map_user ON user_drummer_map(user_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_drummer_map_drummer ON user_drummer_map(drummer_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_analysis_jobs_drummer_time ON analysis_jobs(drummer_id, created_at)')

            conn.commit()
            self._tables_created = True
            logger.info("Database tables created successfully")
            
        except Exception as e:
            logger.error(f"Failed to create database tables: {str(e)}")
            self.database_error.emit(f"Failed to create database tables: {str(e)}")
            raise

    # CRUD operations for drummers
    def get_drummers(self) -> List[Dict]:
        """
        Get all drummers from the database.
        
        Returns:
            List[Dict]: List of drummer records
        """
        try:
            cols = self._table_columns("drummers")
            results: List[Dict] = []
            if getattr(self, "_engine", None) is not None and cols:
                if "display_name" in cols:
                    q = text('SELECT * FROM public.drummers ORDER BY display_name')
                elif "name" in cols:
                    q = text('SELECT * FROM public.drummers ORDER BY name')
                else:
                    q = text('SELECT * FROM public.drummers ORDER BY id')
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(q).mappings().all()
                results = [dict(row) for row in rows]
            elif getattr(self, "_engine", None) is None and cols:
                conn = self._get_connection()
                cursor = conn.cursor()
                if "display_name" in cols:
                    cursor.execute('SELECT * FROM drummers ORDER BY display_name')
                elif "name" in cols:
                    cursor.execute('SELECT * FROM drummers ORDER BY name')
                else:
                    cursor.execute('SELECT * FROM drummers ORDER BY id')
                rows = cursor.fetchall()
                results = [dict(row) for row in rows]
            if results:
                return results

            # Prefer real drummers derived from style-vector ingestion.
            # This is the authoritative "real drummers" list in v1.1.17.
            try:
                vec_cols = self._table_columns("drummer_style_vectors")
                if vec_cols and "drummer_id" in vec_cols and "drummer_name" in vec_cols:
                    if getattr(self, "_engine", None) is not None:
                        with self._engine.connect() as conn:
                            res = conn.execute(
                                text(
                                    "SELECT DISTINCT drummer_id, drummer_name FROM public.drummer_style_vectors "
                                    "WHERE drummer_name IS NOT NULL AND TRIM(drummer_name) <> '' "
                                    "ORDER BY drummer_name"
                                )
                            )
                            vec_rows = res.all()
                    else:
                        conn = self._get_connection()
                        cursor = conn.cursor()
                        cursor.execute(
                            'SELECT DISTINCT drummer_id, drummer_name FROM drummer_style_vectors '
                            'WHERE drummer_name IS NOT NULL AND TRIM(drummer_name) != "" '
                            'ORDER BY drummer_name'
                        )
                        vec_rows = cursor.fetchall()
                    if vec_rows:
                        return [
                            {
                                "id": row[0],
                                "drummer_id": row[0],
                                "display_name": row[1],
                                "name": row[1],
                                "source": "drummer_style_vectors",
                            }
                            for row in vec_rows
                        ]
            except Exception:
                pass

            # Fallback: many v1.1.x admin DBs use drummer_personas/drummer_profiles
            # instead of the simple drummers table.
            try:
                persona_cols = self._table_columns("drummer_personas")
                if persona_cols:
                    if getattr(self, "_engine", None) is not None:
                        with self._engine.connect() as conn:
                            res = conn.execute(text('SELECT persona_id, display_name, archetypes_json, style_json, created_at, updated_at FROM public.drummer_personas ORDER BY display_name'))
                            persona_rows = res.all()
                    else:
                        conn = self._get_connection()
                        cursor = conn.cursor()
                        cursor.execute('SELECT persona_id, display_name, archetypes_json, style_json, created_at, updated_at FROM drummer_personas ORDER BY display_name')
                        persona_rows = cursor.fetchall()
                    return [
                        {
                            "id": row[0],
                            "drummer_id": row[0],
                            "display_name": row[1],
                            "name": row[1],
                            "archetypes_json": row[2],
                            "style_json": row[3],
                            "created_at": row[4],
                            "updated_at": row[5],
                            "source": "drummer_personas",
                        }
                        for row in persona_rows
                    ]
            except Exception:
                pass

            try:
                profile_cols = self._table_columns("drummer_profiles")
                if profile_cols:
                    if getattr(self, "_engine", None) is not None:
                        with self._engine.connect() as conn:
                            res = conn.execute(text('SELECT drummer_id, COALESCE(display_name, name) as display_name, category, era FROM public.drummer_profiles ORDER BY display_name'))
                            profile_rows = res.all()
                    else:
                        conn = self._get_connection()
                        cursor = conn.cursor()
                        cursor.execute('SELECT drummer_id, COALESCE(display_name, name) as display_name, category, era FROM drummer_profiles ORDER BY display_name')
                        profile_rows = cursor.fetchall()
                    return [
                        {
                            "id": row[0],
                            "drummer_id": row[0],
                            "display_name": row[1],
                            "name": row[1],
                            "category": row[2],
                            "era": row[3],
                            "source": "drummer_profiles",
                        }
                        for row in profile_rows
                    ]
            except Exception:
                pass

            return []
        except Exception as e:
            logger.error(f"Error getting drummers: {str(e)}")
            self.database_error.emit(f"Error getting drummers: {str(e)}")
            return []

    def get_drummer(self, drummer_id: str) -> Optional[Dict]:
        """
        Get a drummer by ID.
        
        Args:
            drummer_id: The ID of the drummer
            
        Returns:
            Dict or None: Drummer record or None if not found
        """
        try:
            cols = self._table_columns("drummers")
            if cols:
                if getattr(self, "_engine", None) is not None:
                    q = text('SELECT * FROM public.drummers WHERE drummer_id = :id') if "drummer_id" in cols else text('SELECT * FROM public.drummers WHERE id = :id')
                    with self._engine.connect() as conn_pg:
                        row = conn_pg.execute(q, {"id": drummer_id}).mappings().first()
                    if row:
                        return dict(row)
                else:
                    conn = self._get_connection()
                    cursor = conn.cursor()
                    if "drummer_id" in cols:
                        cursor.execute('SELECT * FROM drummers WHERE drummer_id = ?', (drummer_id,))
                    else:
                        cursor.execute('SELECT * FROM drummers WHERE id = ?', (drummer_id,))
                    row = cursor.fetchone()
                    if row:
                        return dict(row)

            vec_cols = self._table_columns("drummer_style_vectors")
            if vec_cols and "drummer_id" in vec_cols and "drummer_name" in vec_cols:
                if getattr(self, "_engine", None) is not None:
                    with self._engine.connect() as conn:
                        res = conn.execute(
                            text('SELECT DISTINCT drummer_id, drummer_name FROM public.drummer_style_vectors WHERE drummer_id = :id LIMIT 1'),
                            {"id": drummer_id},
                        )
                        vrow = res.first()
                else:
                    conn = self._get_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        'SELECT DISTINCT drummer_id, drummer_name FROM drummer_style_vectors WHERE drummer_id = ? LIMIT 1',
                        (drummer_id,),
                    )
                    vrow = cursor.fetchone()
                if vrow:
                    return {
                        "id": vrow[0],
                        "drummer_id": vrow[0],
                        "display_name": vrow[1],
                        "name": vrow[1],
                        "source": "drummer_style_vectors",
                    }

            persona_cols = self._table_columns("drummer_personas")
            if persona_cols:
                if getattr(self, "_engine", None) is not None:
                    with self._engine.connect() as conn:
                        res = conn.execute(
                            text('SELECT persona_id, display_name, archetypes_json, style_json, created_at, updated_at FROM public.drummer_personas WHERE persona_id = :id'),
                            {"id": drummer_id},
                        )
                        prow = res.first()
                else:
                    conn = self._get_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        'SELECT persona_id, display_name, archetypes_json, style_json, created_at, updated_at FROM drummer_personas WHERE persona_id = ?',
                        (drummer_id,),
                    )
                    prow = cursor.fetchone()
                if prow:
                    return {
                        "id": prow[0],
                        "drummer_id": prow[0],
                        "display_name": prow[1],
                        "name": prow[1],
                        "archetypes_json": prow[2],
                        "style_json": prow[3],
                        "created_at": prow[4],
                        "updated_at": prow[5],
                        "source": "drummer_personas",
                    }

            profile_cols = self._table_columns("drummer_profiles")
            if profile_cols:
                if getattr(self, "_engine", None) is not None:
                    with self._engine.connect() as conn:
                        res = conn.execute(
                            text('SELECT drummer_id, COALESCE(display_name, name) as display_name, category, era, styles FROM public.drummer_profiles WHERE drummer_id = :id'),
                            {"id": drummer_id},
                        )
                        pr = res.first()
                else:
                    conn = self._get_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        'SELECT drummer_id, COALESCE(display_name, name) as display_name, category, era, styles FROM drummer_profiles WHERE drummer_id = ?',
                        (drummer_id,),
                    )
                    pr = cursor.fetchone()
                if pr:
                    return {
                        "id": pr[0],
                        "drummer_id": pr[0],
                        "display_name": pr[1],
                        "name": pr[1],
                        "category": pr[2],
                        "era": pr[3],
                        "styles": pr[4],
                        "source": "drummer_profiles",
                    }

            return None
        except Exception as e:
            logger.error(f"Error getting drummer {drummer_id}: {str(e)}")
            self.database_error.emit(f"Error getting drummer: {str(e)}")
            return None

    # ---- Calibration persistence helpers ---------------------------------

    def get_calibration_adjustments(self, drummer_slug: str) -> Optional[Dict[str, Any]]:
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return None

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    row = conn_pg.execute(
                        text(
                            """
                            SELECT drummer_slug, adjustments_json, metadata_json, created_at, updated_at
                            FROM public.calibration_adjustments
                            WHERE drummer_slug = :drummer_slug
                            LIMIT 1
                            """
                        ),
                        {"drummer_slug": drummer_slug},
                    ).mappings().first()
                if not row:
                    return None
                return {
                    "drummer_slug": row.get("drummer_slug"),
                    "adjustments": self._json_loads(row.get("adjustments_json"), default={}) or {},
                    "metadata": self._json_loads(row.get("metadata_json"), default={}),
                    "created_at": self._parse_datetime(row.get("created_at")),
                    "updated_at": self._parse_datetime(row.get("updated_at")),
                }

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT drummer_slug, adjustments_json, metadata_json, created_at, updated_at
                FROM calibration_adjustments
                WHERE drummer_slug = ?
                LIMIT 1
                """,
                (drummer_slug,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            return {
                "drummer_slug": row["drummer_slug"],
                "adjustments": self._json_loads(row["adjustments_json"], default={}) or {},
                "metadata": self._json_loads(row["metadata_json"], default={}),
                "created_at": self._parse_datetime(row["created_at"]),
                "updated_at": self._parse_datetime(row["updated_at"]),
            }
        except sqlite3.OperationalError as e:
            logger.warning(f"calibration_adjustments table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error getting calibration adjustments for {drummer_slug}: {str(e)}")
            self.database_error.emit(f"Error getting calibration adjustments: {str(e)}")
            return None

    def upsert_calibration_adjustments(
        self,
        *,
        drummer_slug: str,
        adjustments: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return False

            now = datetime.utcnow().isoformat()
            adjustments_json = self._json_dumps(adjustments or {}) or "{}"
            metadata_json = self._json_dumps(metadata or {})

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.calibration_adjustments (
                                drummer_slug, adjustments_json, metadata_json, created_at, updated_at
                            ) VALUES (
                                :drummer_slug, :adjustments_json, :metadata_json, :created_at, :updated_at
                            )
                            ON CONFLICT (drummer_slug) DO UPDATE SET
                                adjustments_json = EXCLUDED.adjustments_json,
                                metadata_json = EXCLUDED.metadata_json,
                                updated_at = EXCLUDED.updated_at
                            """
                        ),
                        {
                            "drummer_slug": drummer_slug,
                            "adjustments_json": adjustments_json,
                            "metadata_json": metadata_json,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                self.data_changed.emit("calibration_adjustments", "upsert")
                return True

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> bool:
                cursor.execute(
                    """
                    INSERT INTO calibration_adjustments (
                        drummer_slug, adjustments_json, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(drummer_slug) DO UPDATE SET
                        adjustments_json = excluded.adjustments_json,
                        metadata_json = excluded.metadata_json,
                        updated_at = excluded.updated_at
                    """,
                    (drummer_slug, adjustments_json, metadata_json, now, now),
                )
                conn.commit()
                return True

            self._with_write_lock_retry(_do_write)
            self.data_changed.emit("calibration_adjustments", "upsert")
            return True
        except sqlite3.OperationalError as e:
            logger.warning(f"calibration_adjustments table not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Error upserting calibration adjustments for {drummer_slug}: {str(e)}")
            self.database_error.emit(f"Error upserting calibration adjustments: {str(e)}")
            return False

    def _row_to_calibration_run(self, row: sqlite3.Row) -> Optional[CalibrationRun]:
        if not row:
            return None

        def _to_int(value: Any) -> Optional[int]:
            try:
                return int(value)
            except Exception:
                return None

        def _to_float(value: Any) -> Optional[float]:
            try:
                return float(value)
            except Exception:
                return None

        try:
            row_dict = {key: row[key] for key in row.keys()}
        except Exception:
            try:
                row_dict = dict(row)
            except Exception:
                row_dict = {}

        metadata_json = row_dict.get("metadata_json", _MISSING)
        metrics_json = row_dict.get("metrics_json", _MISSING)
        comparison_json = row_dict.get("comparison_json", _MISSING)

        return CalibrationRun(
            run_id=str(row_dict.get("run_id", "")),
            drummer_slug=str(row_dict.get("drummer_slug", "")),
            started_at=self._parse_datetime(row_dict.get("started_at")) or datetime.utcnow(),
            completed_at=self._parse_datetime(row_dict.get("completed_at")),
            outcome=str(row_dict.get("outcome", "unknown") or "unknown"),
            note_count=_to_int(row_dict.get("note_count")),
            fills_per_minute=_to_float(row_dict.get("fills_per_minute")),
            within_tolerance_count=_to_int(row_dict.get("within_tolerance_count")),
            total_compared=_to_int(row_dict.get("total_compared")),
            delta_summary=row_dict.get("delta_summary"),
            metadata=(self._json_loads(metadata_json, default={}) or {}) if metadata_json is not _MISSING else None,
            metrics=(self._json_loads(metrics_json, default={}) or {}) if metrics_json is not _MISSING else None,
            comparison=(self._json_loads(comparison_json, default={}) or {}) if comparison_json is not _MISSING else None,
            log_path=row_dict.get("log_path"),
        )

    def _row_to_calibration_feedback(self, row: sqlite3.Row) -> Optional[CalibrationFeedback]:
        if not row:
            return None

        try:
            row_dict = {key: row[key] for key in row.keys()}
        except Exception:
            try:
                row_dict = dict(row)
            except Exception:
                row_dict = {}

        submitted_at = self._parse_datetime(row_dict.get("submitted_at")) or datetime.utcnow()
        metadata_json = row_dict.get("metadata_json", _MISSING)

        return CalibrationFeedback(
            feedback_id=str(row_dict.get("feedback_id", "")),
            drummer_slug=str(row_dict.get("drummer_slug", "")),
            rating=int(row_dict.get("rating", 0) or 0),
            comment=row_dict.get("comment"),
            author=row_dict.get("author"),
            submitted_at=submitted_at,
            metadata=(self._json_loads(metadata_json, default={}) or {}) if metadata_json is not _MISSING else None,
        )

    def _row_to_run_version(self, row: sqlite3.Row) -> Optional[RunVersion]:
        data = self._row_to_dict(row)
        if not data:
            return None
        return RunVersion(
            run_id=str(data.get("run_id", "")),
            generator_version=str(data.get("generator_version", "")),
            feature_version=str(data.get("feature_version", "")),
            rollup_version=str(data.get("rollup_version", "")),
            sample_pack_version=str(data.get("sample_pack_version", "")),
            seed=int(data.get("seed", 0) or 0),
            commit_hash=data.get("commit_hash"),
            created_at=self._parse_datetime(data.get("created_at")) or datetime.utcnow(),
        )

    def _row_to_audio_artifact(self, row: sqlite3.Row) -> Optional[AudioArtifact]:
        data = self._row_to_dict(row)
        if not data:
            return None
        return AudioArtifact(
            artifact_id=str(data.get("artifact_id", "")),
            run_id=data.get("run_id"),
            artifact_type=str(data.get("artifact_type", "")),
            storage_uri=str(data.get("storage_uri", "")),
            duration_sec=self._safe_float(data.get("duration_sec")),
            loudness_lufs=self._safe_float(data.get("loudness_lufs")),
            sample_pack_version=data.get("sample_pack_version"),
            render_recipe=self._json_loads(data.get("render_recipe_json"), default={}) or {},
            created_at=self._parse_datetime(data.get("created_at")) or datetime.utcnow(),
        )

    def _row_to_calibration_render_job(self, row: sqlite3.Row) -> Optional[CalibrationRenderJob]:
        data = self._row_to_dict(row)
        if not data:
            return None
        return CalibrationRenderJob(
            job_id=str(data.get("job_id", "")),
            run_id=str(data.get("run_id", "")),
            render_profile_id=str(data.get("render_profile_id", "")),
            sample_pack_version=str(data.get("sample_pack_version", "")),
            status=str(data.get("status", "queued") or "queued"),
            artifact_ids=self._json_loads(data.get("artifact_ids_json"), default=[]) or [],
            error_text=data.get("error_text"),
            created_at=self._parse_datetime(data.get("created_at")) or datetime.utcnow(),
            started_at=self._parse_datetime(data.get("started_at")),
            completed_at=self._parse_datetime(data.get("completed_at")),
        )

    def _row_to_evaluation_session(self, row: sqlite3.Row) -> Optional[EvaluationSession]:
        data = self._row_to_dict(row)
        if not data:
            return None
        return EvaluationSession(
            session_id=str(data.get("session_id", "")),
            reviewer_id=str(data.get("reviewer_id", "")),
            target_drummer_slug=str(data.get("target_drummer_slug", "")),
            assigned_at=self._parse_datetime(data.get("assigned_at")),
            started_at=self._parse_datetime(data.get("started_at")),
            completed_at=self._parse_datetime(data.get("completed_at")),
            app_version=data.get("app_version"),
            notes=data.get("notes"),
            created_at=self._parse_datetime(data.get("created_at")) or datetime.utcnow(),
        )

    def _row_to_evaluation_item(self, row: sqlite3.Row) -> Optional[EvaluationItem]:
        data = self._row_to_dict(row)
        if not data:
            return None
        return EvaluationItem(
            item_id=str(data.get("item_id", "")),
            session_id=str(data.get("session_id", "")),
            base_groove_id=str(data.get("base_groove_id", "")),
            target_drummer_slug=str(data.get("target_drummer_slug", "")),
            reference_artifact_id=data.get("reference_artifact_id"),
            baseline_run_id=data.get("baseline_run_id"),
            candidate_a_run_id=data.get("candidate_a_run_id"),
            candidate_b_run_id=data.get("candidate_b_run_id"),
            ab_mapping=self._json_loads(data.get("ab_mapping_json"), default={}) or {},
            eval_mode=str(data.get("eval_mode") or "AB"),
            created_at=self._parse_datetime(data.get("created_at")) or datetime.utcnow(),
        )

    def _row_to_pairwise_judgment(self, row: sqlite3.Row) -> Optional[PairwiseJudgment]:
        data = self._row_to_dict(row)
        if not data:
            return None
        return PairwiseJudgment(
            judgment_id=str(data.get("judgment_id", "")),
            item_id=str(data.get("item_id", "")),
            preferred_candidate=data.get("preferred_candidate"),
            closer_to_target=data.get("closer_to_target"),
            better_feel=data.get("better_feel"),
            more_musical=data.get("more_musical"),
            confidence=self._safe_int(data.get("confidence")),
            created_at=self._parse_datetime(data.get("created_at")) or datetime.utcnow(),
        )

    def _row_to_attribute_rating(self, row: sqlite3.Row) -> Optional[AttributeRating]:
        data = self._row_to_dict(row)
        if not data:
            return None
        return AttributeRating(
            rating_id=str(data.get("rating_id", "")),
            item_id=str(data.get("item_id", "")),
            candidate_label=str(data.get("candidate_label", "")),
            stylistic_authenticity=self._safe_float(data.get("stylistic_authenticity")),
            groove_feel=self._safe_float(data.get("groove_feel")),
            dynamics=self._safe_float(data.get("dynamics")),
            phrasing=self._safe_float(data.get("phrasing")),
            kit_balance=self._safe_float(data.get("kit_balance")),
            fill_behavior=self._safe_float(data.get("fill_behavior")),
            human_realism=self._safe_float(data.get("human_realism")),
            overall_usefulness=self._safe_float(data.get("overall_usefulness")),
            created_at=self._parse_datetime(data.get("created_at")) or datetime.utcnow(),
        )

    # ---- Phase 2 run version helpers ---------------------------------

    def upsert_run_version(
        self,
        *,
        run_id: str,
        generator_version: str,
        feature_version: str,
        rollup_version: str,
        sample_pack_version: str,
        seed: int,
        commit_hash: Optional[str] = None,
    ) -> bool:
        try:
            run_id = (run_id or "").strip()
            if not run_id:
                return False

            now = datetime.utcnow().isoformat()
            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.run_versions (
                                run_id, generator_version, feature_version, rollup_version,
                                sample_pack_version, seed, commit_hash, created_at
                            ) VALUES (
                                :run_id, :generator_version, :feature_version, :rollup_version,
                                :sample_pack_version, :seed, :commit_hash, NOW()
                            )
                            ON CONFLICT (run_id) DO UPDATE SET
                                generator_version = EXCLUDED.generator_version,
                                feature_version = EXCLUDED.feature_version,
                                rollup_version = EXCLUDED.rollup_version,
                                sample_pack_version = EXCLUDED.sample_pack_version,
                                seed = EXCLUDED.seed,
                                commit_hash = EXCLUDED.commit_hash,
                                created_at = NOW()
                            """
                        ),
                        {
                            "run_id": run_id,
                            "generator_version": generator_version,
                            "feature_version": feature_version,
                            "rollup_version": rollup_version,
                            "sample_pack_version": sample_pack_version,
                            "seed": int(seed),
                            "commit_hash": commit_hash,
                        },
                    )
                self.data_changed.emit("run_versions", "upsert")
                return True

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> bool:
                cursor.execute(
                    """
                    INSERT INTO run_versions (
                        run_id, generator_version, feature_version, rollup_version,
                        sample_pack_version, seed, commit_hash, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        generator_version = excluded.generator_version,
                        feature_version = excluded.feature_version,
                        rollup_version = excluded.rollup_version,
                        sample_pack_version = excluded.sample_pack_version,
                        seed = excluded.seed,
                        commit_hash = excluded.commit_hash
                    """,
                    (
                        run_id,
                        generator_version,
                        feature_version,
                        rollup_version,
                        sample_pack_version,
                        int(seed),
                        commit_hash,
                        now,
                    ),
                )
                conn.commit()
                return True

            result = bool(self._with_write_lock_retry(_do_write))
            if result:
                self.data_changed.emit("run_versions", "upsert")
            return result
        except sqlite3.OperationalError as e:
            logger.warning(f"run_versions table not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Error upserting run version {run_id}: {str(e)}")
            self.database_error.emit(f"Error upserting run version: {str(e)}")
            return False

    def get_run_version(self, *, run_id: str) -> Optional[RunVersion]:
        try:
            run_id = (run_id or "").strip()
            if not run_id:
                return None

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT run_id, generator_version, feature_version, rollup_version,
                       sample_pack_version, seed, commit_hash, created_at
                FROM run_versions
                WHERE run_id = ?
                """,
                (run_id,),
            )
            row = cursor.fetchone()
            return self._row_to_run_version(row) if row else None
        except sqlite3.OperationalError as e:
            logger.warning(f"run_versions table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching run version {run_id}: {str(e)}")
            self.database_error.emit(f"Error fetching run version: {str(e)}")
            return None

    def log_audio_artifact(
        self,
        *,
        run_id: Optional[str],
        artifact_type: str,
        storage_uri: str,
        duration_sec: Optional[float] = None,
        loudness_lufs: Optional[float] = None,
        sample_pack_version: Optional[str] = None,
        render_recipe: Optional[Dict[str, Any]] = None,
        artifact_id: Optional[str] = None,
    ) -> Optional[str]:
        try:
            artifact_type = (artifact_type or "").strip()
            storage_uri = (storage_uri or "").strip()
            if not artifact_type or not storage_uri:
                return None

            artifact_id = (artifact_id or f"art_{uuid.uuid4().hex[:12]}").strip()
            now = datetime.utcnow().isoformat()
            render_json = self._json_dumps(render_recipe) if render_recipe is not None else "{}"

            if getattr(self, "_engine", None) is not None:
                params = {
                    "artifact_id": artifact_id,
                    "run_id": run_id,
                    "artifact_type": artifact_type,
                    "storage_uri": storage_uri,
                    "duration_sec": self._safe_float(duration_sec),
                    "loudness_lufs": self._safe_float(loudness_lufs),
                    "sample_pack_version": sample_pack_version,
                    "render_recipe_json": render_json,
                }
                try:
                    with self._engine.begin() as conn_pg:
                        conn_pg.execute(
                            text(
                                """
                                INSERT INTO public.audio_artifacts (
                                    artifact_id, run_id, artifact_type, storage_uri,
                                    duration_sec, loudness_lufs, sample_pack_version,
                                    render_recipe_json, created_at
                                ) VALUES (
                                    :artifact_id, :run_id, :artifact_type, :storage_uri,
                                    :duration_sec, :loudness_lufs, :sample_pack_version,
                                    :render_recipe_json, NOW()
                                )
                                ON CONFLICT (artifact_id) DO UPDATE SET
                                    run_id = EXCLUDED.run_id,
                                    artifact_type = EXCLUDED.artifact_type,
                                    storage_uri = EXCLUDED.storage_uri,
                                    duration_sec = EXCLUDED.duration_sec,
                                    loudness_lufs = EXCLUDED.loudness_lufs,
                                    sample_pack_version = EXCLUDED.sample_pack_version,
                                    render_recipe_json = EXCLUDED.render_recipe_json,
                                    created_at = NOW()
                                """
                            ),
                            params,
                        )
                except Exception as pg_write_exc:
                    if "created_at" not in str(pg_write_exc):
                        raise
                    with self._engine.begin() as conn_pg:
                        conn_pg.execute(
                            text(
                                """
                                INSERT INTO public.audio_artifacts (
                                    artifact_id, run_id, artifact_type, storage_uri,
                                    duration_sec, loudness_lufs, sample_pack_version,
                                    render_recipe_json
                                ) VALUES (
                                    :artifact_id, :run_id, :artifact_type, :storage_uri,
                                    :duration_sec, :loudness_lufs, :sample_pack_version,
                                    :render_recipe_json
                                )
                                ON CONFLICT (artifact_id) DO UPDATE SET
                                    run_id = EXCLUDED.run_id,
                                    artifact_type = EXCLUDED.artifact_type,
                                    storage_uri = EXCLUDED.storage_uri,
                                    duration_sec = EXCLUDED.duration_sec,
                                    loudness_lufs = EXCLUDED.loudness_lufs,
                                    sample_pack_version = EXCLUDED.sample_pack_version,
                                    render_recipe_json = EXCLUDED.render_recipe_json
                                """
                            ),
                            params,
                        )
                self.data_changed.emit("audio_artifacts", "upsert")
                return artifact_id

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> str:
                cursor.execute(
                    """
                    INSERT INTO audio_artifacts (
                        artifact_id, run_id, artifact_type, storage_uri,
                        duration_sec, loudness_lufs, sample_pack_version,
                        render_recipe_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(artifact_id) DO UPDATE SET
                        run_id = excluded.run_id,
                        artifact_type = excluded.artifact_type,
                        storage_uri = excluded.storage_uri,
                        duration_sec = excluded.duration_sec,
                        loudness_lufs = excluded.loudness_lufs,
                        sample_pack_version = excluded.sample_pack_version,
                        render_recipe_json = excluded.render_recipe_json,
                        created_at = excluded.created_at
                    """,
                    (
                        artifact_id,
                        run_id,
                        artifact_type,
                        storage_uri,
                        self._safe_float(duration_sec),
                        self._safe_float(loudness_lufs),
                        sample_pack_version,
                        render_json,
                        now,
                    ),
                )
                conn.commit()
                return artifact_id

            result = self._with_write_lock_retry(_do_write)
            if result:
                self.data_changed.emit("audio_artifacts", "upsert")
            return str(result) if result else None
        except sqlite3.OperationalError as e:
            logger.warning(f"audio_artifacts table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error logging audio artifact for run {run_id}: {str(e)}")
            self.database_error.emit(f"Error logging audio artifact: {str(e)}")
            return None

    def get_audio_artifacts_for_run(self, *, run_id: str) -> List[AudioArtifact]:
        artifacts: List[AudioArtifact] = []
        try:
            run_id = (run_id or "").strip()
            if not run_id:
                return artifacts

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    try:
                        rows = conn_pg.execute(
                            text(
                                """
                                SELECT artifact_id, run_id, artifact_type, storage_uri, duration_sec,
                                       loudness_lufs, sample_pack_version, render_recipe_json, created_at
                                FROM public.audio_artifacts
                                WHERE run_id = :run_id
                                ORDER BY created_at DESC
                                """
                            ),
                            {"run_id": run_id},
                        ).mappings().all()
                    except Exception as pg_read_exc:
                        if "created_at" not in str(pg_read_exc):
                            raise
                        rows = conn_pg.execute(
                            text(
                                """
                                SELECT artifact_id, run_id, artifact_type, storage_uri, duration_sec,
                                       loudness_lufs, sample_pack_version, render_recipe_json
                                FROM public.audio_artifacts
                                WHERE run_id = :run_id
                                ORDER BY artifact_id DESC
                                """
                            ),
                            {"run_id": run_id},
                        ).mappings().all()

                for data in rows:
                    artifact = self._row_to_audio_artifact(data)
                    if artifact:
                        artifacts.append(artifact)
                return artifacts

            conn = self._get_connection()
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    SELECT artifact_id, run_id, artifact_type, storage_uri, duration_sec,
                           loudness_lufs, sample_pack_version, render_recipe_json, created_at
                    FROM audio_artifacts
                    WHERE run_id = ?
                    ORDER BY created_at DESC
                    """,
                    (run_id,),
                )
            except sqlite3.OperationalError as sqlite_read_exc:
                if "created_at" not in str(sqlite_read_exc).lower():
                    raise
                cursor.execute(
                    """
                    SELECT artifact_id, run_id, artifact_type, storage_uri, duration_sec,
                           loudness_lufs, sample_pack_version, render_recipe_json
                    FROM audio_artifacts
                    WHERE run_id = ?
                    ORDER BY artifact_id DESC
                    """,
                    (run_id,),
                )
            for row in cursor.fetchall() or []:
                artifact = self._row_to_audio_artifact(row)
                if artifact:
                    artifacts.append(artifact)
            return artifacts
        except sqlite3.OperationalError as e:
            logger.warning(f"audio_artifacts table not available: {e}")
            return artifacts
        except Exception as e:
            logger.error(f"Error fetching audio artifacts for run {run_id}: {str(e)}")
            self.database_error.emit(f"Error fetching audio artifacts: {str(e)}")
            return artifacts

    def upsert_reviewer_profile(
        self,
        *,
        reviewer_id: str,
        display_name: str,
        expertise_level: Optional[str] = None,
        primary_styles: Optional[List[str]] = None,
        years_experience: Optional[int] = None,
        weighting_factor: float = 1.0,
    ) -> bool:
        try:
            reviewer_id = (reviewer_id or "").strip()
            display_name = (display_name or "").strip()
            if not reviewer_id or not display_name:
                return False

            now = datetime.utcnow().isoformat()
            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.reviewer_profiles (
                                reviewer_id, display_name, expertise_level,
                                primary_styles_json, years_experience,
                                weighting_factor, created_at
                            ) VALUES (
                                :reviewer_id, :display_name, :expertise_level,
                                :primary_styles_json, :years_experience,
                                :weighting_factor, NOW()
                            )
                            ON CONFLICT (reviewer_id) DO UPDATE SET
                                display_name = EXCLUDED.display_name,
                                expertise_level = EXCLUDED.expertise_level,
                                primary_styles_json = EXCLUDED.primary_styles_json,
                                years_experience = EXCLUDED.years_experience,
                                weighting_factor = EXCLUDED.weighting_factor
                            """
                        ),
                        {
                            "reviewer_id": reviewer_id,
                            "display_name": display_name,
                            "expertise_level": expertise_level,
                            "primary_styles_json": self._json_dumps(primary_styles or []) or "[]",
                            "years_experience": self._safe_int(years_experience),
                            "weighting_factor": float(weighting_factor),
                        },
                    )
                self.data_changed.emit("reviewer_profiles", "upsert")
                return True

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> bool:
                cursor.execute(
                    """
                    INSERT INTO reviewer_profiles (
                        reviewer_id, display_name, expertise_level,
                        primary_styles_json, years_experience,
                        weighting_factor, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(reviewer_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        expertise_level = excluded.expertise_level,
                        primary_styles_json = excluded.primary_styles_json,
                        years_experience = excluded.years_experience,
                        weighting_factor = excluded.weighting_factor
                    """,
                    (
                        reviewer_id,
                        display_name,
                        expertise_level,
                        self._json_dumps(primary_styles or []) or "[]",
                        self._safe_int(years_experience),
                        float(weighting_factor),
                        now,
                    ),
                )
                conn.commit()
                return True

            result = bool(self._with_write_lock_retry(_do_write))
            if result:
                self.data_changed.emit("reviewer_profiles", "upsert")
            return result
        except sqlite3.OperationalError as e:
            logger.warning(f"reviewer_profiles table not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Error upserting reviewer profile {reviewer_id}: {str(e)}")
            self.database_error.emit(f"Error upserting reviewer profile: {str(e)}")
            return False

    def create_evaluation_session(
        self,
        *,
        reviewer_id: str,
        target_drummer_slug: str,
        app_version: Optional[str] = None,
        notes: Optional[str] = None,
        assigned_at: Optional[datetime] = None,
    ) -> Optional[str]:
        try:
            reviewer_id = (reviewer_id or "").strip()
            target_drummer_slug = (target_drummer_slug or "").strip()
            if not reviewer_id or not target_drummer_slug:
                return None

            session_id = f"sess_{uuid.uuid4().hex[:12]}"
            assigned_iso = (assigned_at or datetime.utcnow()).isoformat()
            now = datetime.utcnow().isoformat()

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.evaluation_sessions (
                                session_id, reviewer_id, target_drummer_slug,
                                assigned_at, started_at, completed_at,
                                app_version, notes, created_at
                            ) VALUES (
                                :session_id, :reviewer_id, :target_drummer_slug,
                                :assigned_at, NULL, NULL,
                                :app_version, :notes, NOW()
                            )
                            """
                        ),
                        {
                            "session_id": session_id,
                            "reviewer_id": reviewer_id,
                            "target_drummer_slug": target_drummer_slug,
                            "assigned_at": assigned_iso,
                            "app_version": app_version,
                            "notes": notes,
                        },
                    )
                self.data_changed.emit("evaluation_sessions", "insert")
                return session_id

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> str:
                cursor.execute(
                    """
                    INSERT INTO evaluation_sessions (
                        session_id, reviewer_id, target_drummer_slug,
                        assigned_at, started_at, completed_at,
                        app_version, notes, created_at
                    ) VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                    """,
                    (
                        session_id,
                        reviewer_id,
                        target_drummer_slug,
                        assigned_iso,
                        app_version,
                        notes,
                        now,
                    ),
                )
                conn.commit()
                return session_id

            result = self._with_write_lock_retry(_do_write)
            if result:
                self.data_changed.emit("evaluation_sessions", "insert")
            return str(result) if result else None
        except sqlite3.OperationalError as e:
            logger.warning(f"evaluation_sessions table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error creating evaluation session for {reviewer_id}: {str(e)}")
            self.database_error.emit(f"Error creating evaluation session: {str(e)}")
            return None

    def start_evaluation_session(self, *, session_id: str) -> bool:
        try:
            session_id = (session_id or "").strip()
            if not session_id:
                return False

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    result = conn_pg.execute(
                        text(
                            """
                            UPDATE public.evaluation_sessions
                            SET started_at = COALESCE(started_at, NOW())
                            WHERE session_id = :session_id
                            """
                        ),
                        {"session_id": session_id},
                    )
                changed = bool((result.rowcount or 0) > 0)
                if changed:
                    self.data_changed.emit("evaluation_sessions", "update")
                return changed

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> bool:
                cursor.execute(
                    "UPDATE evaluation_sessions SET started_at = COALESCE(started_at, ?) WHERE session_id = ?",
                    (datetime.utcnow().isoformat(), session_id),
                )
                conn.commit()
                return cursor.rowcount > 0

            result = bool(self._with_write_lock_retry(_do_write))
            if result:
                self.data_changed.emit("evaluation_sessions", "update")
            return result
        except sqlite3.OperationalError as e:
            logger.warning(f"evaluation_sessions table not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Error starting evaluation session {session_id}: {str(e)}")
            self.database_error.emit(f"Error starting evaluation session: {str(e)}")
            return False

    def complete_evaluation_session(self, *, session_id: str) -> bool:
        try:
            session_id = (session_id or "").strip()
            if not session_id:
                return False

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    result = conn_pg.execute(
                        text(
                            """
                            UPDATE public.evaluation_sessions
                            SET completed_at = COALESCE(completed_at, NOW())
                            WHERE session_id = :session_id
                            """
                        ),
                        {"session_id": session_id},
                    )
                changed = bool((result.rowcount or 0) > 0)
                if changed:
                    self.data_changed.emit("evaluation_sessions", "update")
                return changed

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> bool:
                cursor.execute(
                    "UPDATE evaluation_sessions SET completed_at = COALESCE(completed_at, ?) WHERE session_id = ?",
                    (datetime.utcnow().isoformat(), session_id),
                )
                conn.commit()
                return cursor.rowcount > 0

            result = bool(self._with_write_lock_retry(_do_write))
            if result:
                self.data_changed.emit("evaluation_sessions", "update")
            return result
        except sqlite3.OperationalError as e:
            logger.warning(f"evaluation_sessions table not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Error completing evaluation session {session_id}: {str(e)}")
            self.database_error.emit(f"Error completing evaluation session: {str(e)}")
            return False

    def get_next_evaluation_session(
        self,
        *,
        reviewer_id: Optional[str] = None,
        target_drummer_slug: Optional[str] = None,
    ) -> Optional[EvaluationSession]:
        try:
            if getattr(self, "_engine", None) is not None:
                clauses = ["started_at IS NULL", "completed_at IS NULL"]
                params: Dict[str, Any] = {}
                if reviewer_id:
                    clauses.append("reviewer_id = :reviewer_id")
                    params["reviewer_id"] = (reviewer_id or "").strip()
                if target_drummer_slug:
                    clauses.append("target_drummer_slug = :target_drummer_slug")
                    params["target_drummer_slug"] = (target_drummer_slug or "").strip()
                query = (
                    "SELECT * FROM public.evaluation_sessions WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY assigned_at ASC NULLS LAST, created_at ASC LIMIT 1"
                )
                with self._engine.connect() as conn_pg:
                    row = conn_pg.execute(text(query), params).mappings().first()
                return self._row_to_evaluation_session(row) if row else None

            clauses: List[str] = []
            params_sqlite: List[Any] = []
            if reviewer_id:
                clauses.append("reviewer_id = ?")
                params_sqlite.append((reviewer_id or "").strip())
            if target_drummer_slug:
                clauses.append("target_drummer_slug = ?")
                params_sqlite.append((target_drummer_slug or "").strip())
            clauses.extend(["started_at IS NULL", "completed_at IS NULL"])

            query = "SELECT * FROM evaluation_sessions"
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY assigned_at ASC, created_at ASC LIMIT 1"

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(query, tuple(params_sqlite))
            row = cursor.fetchone()
            return self._row_to_evaluation_session(row) if row else None
        except sqlite3.OperationalError as e:
            logger.warning(f"evaluation_sessions table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching next evaluation session: {str(e)}")
            self.database_error.emit(f"Error fetching evaluation session: {str(e)}")
            return None

    def create_evaluation_item(
        self,
        *,
        session_id: str,
        base_groove_id: str,
        target_drummer_slug: str,
        reference_artifact_id: Optional[str] = None,
        baseline_run_id: Optional[str] = None,
        candidate_a_run_id: Optional[str] = None,
        candidate_b_run_id: Optional[str] = None,
        eval_mode: str = "AB",
        ab_mapping: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        try:
            session_id = (session_id or "").strip()
            base_groove_id = (base_groove_id or "").strip()
            target_drummer_slug = (target_drummer_slug or "").strip()
            if not session_id or not base_groove_id or not target_drummer_slug:
                return None

            item_id = f"item_{uuid.uuid4().hex[:12]}"
            now = datetime.utcnow().isoformat()

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.evaluation_items (
                                item_id, session_id, base_groove_id, target_drummer_slug,
                                reference_artifact_id, baseline_run_id, candidate_a_run_id,
                                candidate_b_run_id, ab_mapping_json, eval_mode, created_at
                            ) VALUES (
                                :item_id, :session_id, :base_groove_id, :target_drummer_slug,
                                :reference_artifact_id, :baseline_run_id, :candidate_a_run_id,
                                :candidate_b_run_id, :ab_mapping_json, :eval_mode, NOW()
                            )
                            """
                        ),
                        {
                            "item_id": item_id,
                            "session_id": session_id,
                            "base_groove_id": base_groove_id,
                            "target_drummer_slug": target_drummer_slug,
                            "reference_artifact_id": reference_artifact_id,
                            "baseline_run_id": baseline_run_id,
                            "candidate_a_run_id": candidate_a_run_id,
                            "candidate_b_run_id": candidate_b_run_id,
                            "ab_mapping_json": self._json_dumps(ab_mapping or {}) or "{}",
                            "eval_mode": eval_mode or "AB",
                        },
                    )
                self.data_changed.emit("evaluation_items", "insert")
                return item_id

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> str:
                cursor.execute(
                    """
                    INSERT INTO evaluation_items (
                        item_id, session_id, base_groove_id, target_drummer_slug,
                        reference_artifact_id, baseline_run_id, candidate_a_run_id,
                        candidate_b_run_id, ab_mapping_json, eval_mode, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item_id,
                        session_id,
                        base_groove_id,
                        target_drummer_slug,
                        reference_artifact_id,
                        baseline_run_id,
                        candidate_a_run_id,
                        candidate_b_run_id,
                        self._json_dumps(ab_mapping or {}) or "{}",
                        eval_mode or "AB",
                        now,
                    ),
                )
                conn.commit()
                return item_id

            result = self._with_write_lock_retry(_do_write)
            if result:
                self.data_changed.emit("evaluation_items", "insert")
            return str(result) if result else None
        except sqlite3.OperationalError as e:
            logger.warning(f"evaluation_items table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error creating evaluation item for session {session_id}: {str(e)}")
            self.database_error.emit(f"Error creating evaluation item: {str(e)}")
            return None

    def upsert_calibration_run_events(
        self,
        *,
        run_id: str,
        drummer_slug: str,
        event_stream: List[Dict[str, Any]],
        source_type: str = "dcsm_json",
        tempo_bpm: Optional[float] = None,
        time_signature: Optional[Dict[str, Any]] = None,
        bars: Optional[int] = None,
    ) -> bool:
        try:
            run_id = (run_id or "").strip()
            drummer_slug = (drummer_slug or "").strip()
            if not run_id or not drummer_slug:
                return False

            event_stream_json = self._json_dumps(event_stream or []) or "[]"
            time_signature_json = self._json_dumps(time_signature or {}) or "{}"

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.calibration_run_events (
                                run_id, drummer_slug, source_type,
                                event_stream_json, tempo_bpm, time_signature_json, bars,
                                created_at, updated_at
                            ) VALUES (
                                :run_id, :drummer_slug, :source_type,
                                :event_stream_json, :tempo_bpm, :time_signature_json, :bars,
                                NOW(), NOW()
                            )
                            ON CONFLICT (run_id) DO UPDATE SET
                                drummer_slug = EXCLUDED.drummer_slug,
                                source_type = EXCLUDED.source_type,
                                event_stream_json = EXCLUDED.event_stream_json,
                                tempo_bpm = EXCLUDED.tempo_bpm,
                                time_signature_json = EXCLUDED.time_signature_json,
                                bars = EXCLUDED.bars,
                                updated_at = NOW()
                            """
                        ),
                        {
                            "run_id": run_id,
                            "drummer_slug": drummer_slug,
                            "source_type": (source_type or "dcsm_json").strip() or "dcsm_json",
                            "event_stream_json": event_stream_json,
                            "tempo_bpm": self._safe_float(tempo_bpm),
                            "time_signature_json": time_signature_json,
                            "bars": self._safe_int(bars),
                        },
                    )
                self.data_changed.emit("calibration_run_events", "upsert")
                return True

            now = datetime.utcnow().isoformat()
            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> bool:
                cursor.execute(
                    """
                    INSERT INTO calibration_run_events (
                        run_id, drummer_slug, source_type,
                        event_stream_json, tempo_bpm, time_signature_json, bars,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        drummer_slug = excluded.drummer_slug,
                        source_type = excluded.source_type,
                        event_stream_json = excluded.event_stream_json,
                        tempo_bpm = excluded.tempo_bpm,
                        time_signature_json = excluded.time_signature_json,
                        bars = excluded.bars,
                        updated_at = excluded.updated_at
                    """,
                    (
                        run_id,
                        drummer_slug,
                        (source_type or "dcsm_json").strip() or "dcsm_json",
                        event_stream_json,
                        self._safe_float(tempo_bpm),
                        time_signature_json,
                        self._safe_int(bars),
                        now,
                        now,
                    ),
                )
                conn.commit()
                return True

            result = bool(self._with_write_lock_retry(_do_write))
            if result:
                self.data_changed.emit("calibration_run_events", "upsert")
            return result
        except sqlite3.OperationalError as e:
            logger.warning(f"calibration_run_events table not available: {e}")
            return False
        except Exception as e:
            logger.error(f"Error upserting calibration run events for {run_id}: {str(e)}")
            self.database_error.emit(f"Error upserting calibration run events: {str(e)}")
            return False

    def log_calibration_render_job(
        self,
        *,
        run_id: str,
        render_profile_id: str,
        sample_pack_version: str,
        status: str = "queued",
        artifact_ids: Optional[List[str]] = None,
        error_text: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Optional[str]:
        try:
            run_id = (run_id or "").strip()
            render_profile_id = (render_profile_id or "").strip()
            sample_pack_version = (sample_pack_version or "").strip()
            status_val = (status or "queued").strip() or "queued"
            if not run_id or not render_profile_id or not sample_pack_version:
                return None

            job_id = (job_id or f"rjob_{uuid.uuid4().hex[:12]}").strip()
            artifact_ids_json = self._json_dumps(artifact_ids or []) or "[]"
            now = datetime.utcnow().isoformat()

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.calibration_render_jobs (
                                job_id, run_id, render_profile_id, sample_pack_version,
                                status, artifact_ids_json, error_text,
                                created_at, started_at, completed_at
                            ) VALUES (
                                :job_id, :run_id, :render_profile_id, :sample_pack_version,
                                :status, :artifact_ids_json, :error_text,
                                NOW(), NULL, NULL
                            )
                            ON CONFLICT (job_id) DO UPDATE SET
                                run_id = EXCLUDED.run_id,
                                render_profile_id = EXCLUDED.render_profile_id,
                                sample_pack_version = EXCLUDED.sample_pack_version,
                                status = EXCLUDED.status,
                                artifact_ids_json = EXCLUDED.artifact_ids_json,
                                error_text = EXCLUDED.error_text
                            """
                        ),
                        {
                            "job_id": job_id,
                            "run_id": run_id,
                            "render_profile_id": render_profile_id,
                            "sample_pack_version": sample_pack_version,
                            "status": status_val,
                            "artifact_ids_json": artifact_ids_json,
                            "error_text": error_text,
                        },
                    )
                self.data_changed.emit("calibration_render_jobs", "upsert")
                return job_id

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> str:
                cursor.execute(
                    """
                    INSERT INTO calibration_render_jobs (
                        job_id, run_id, render_profile_id, sample_pack_version,
                        status, artifact_ids_json, error_text,
                        created_at, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    ON CONFLICT(job_id) DO UPDATE SET
                        run_id = excluded.run_id,
                        render_profile_id = excluded.render_profile_id,
                        sample_pack_version = excluded.sample_pack_version,
                        status = excluded.status,
                        artifact_ids_json = excluded.artifact_ids_json,
                        error_text = excluded.error_text
                    """,
                    (
                        job_id,
                        run_id,
                        render_profile_id,
                        sample_pack_version,
                        status_val,
                        artifact_ids_json,
                        error_text,
                        now,
                    ),
                )
                conn.commit()
                return job_id

            result = self._with_write_lock_retry(_do_write)
            if result:
                self.data_changed.emit("calibration_render_jobs", "upsert")
            return str(result) if result else None
        except sqlite3.OperationalError as e:
            logger.warning(f"calibration_render_jobs table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error logging calibration render job for run {run_id}: {str(e)}")
            self.database_error.emit(f"Error logging calibration render job: {str(e)}")
            return None

    def get_calibration_run_events_payload(self, *, run_id: str) -> Optional[Dict[str, Any]]:
        try:
            run_id = (run_id or "").strip()
            if not run_id:
                return None
            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    row = conn_pg.execute(
                        text(
                            """
                            SELECT run_id, drummer_slug, source_type,
                                   event_stream_json, tempo_bpm, time_signature_json, bars,
                                   created_at, updated_at
                            FROM public.calibration_run_events
                            WHERE run_id = :run_id
                            LIMIT 1
                            """
                        ),
                        {"run_id": run_id},
                    ).mappings().first()
                if not row:
                    return None
                return {
                    "run_id": str(row.get("run_id") or ""),
                    "drummer_slug": str(row.get("drummer_slug") or ""),
                    "source_type": str(row.get("source_type") or "dcsm_json"),
                    "event_stream": self._json_loads(row.get("event_stream_json"), default=[]) or [],
                    "tempo_bpm": self._safe_float(row.get("tempo_bpm")),
                    "time_signature": self._json_loads(row.get("time_signature_json"), default={}) or {},
                    "bars": self._safe_int(row.get("bars")),
                    "created_at": self._parse_datetime(row.get("created_at")),
                    "updated_at": self._parse_datetime(row.get("updated_at")),
                }
            conn = self._get_connection()
            row = conn.execute(
                "SELECT run_id, drummer_slug, source_type, event_stream_json, tempo_bpm, time_signature_json, bars, created_at, updated_at FROM calibration_run_events WHERE run_id = ? LIMIT 1",
                (run_id,),
            ).fetchone()
            data = self._row_to_dict(row) if row else None
            if not data:
                return None
            return {
                "run_id": str(data.get("run_id") or ""),
                "drummer_slug": str(data.get("drummer_slug") or ""),
                "source_type": str(data.get("source_type") or "dcsm_json"),
                "event_stream": self._json_loads(data.get("event_stream_json"), default=[]) or [],
                "tempo_bpm": self._safe_float(data.get("tempo_bpm")),
                "time_signature": self._json_loads(data.get("time_signature_json"), default={}) or {},
                "bars": self._safe_int(data.get("bars")),
            }
        except Exception as exc:
            logger.error("Error fetching calibration run events for %s: %s", run_id, exc)
            return None

    def list_calibration_render_jobs(
        self,
        *,
        statuses: Optional[List[str]] = None,
        limit: int = 25,
    ) -> List[CalibrationRenderJob]:
        status_values = [str(value or "").strip().lower() for value in (statuses or []) if str(value or "").strip()]
        lim = max(1, int(limit))
        jobs: List[CalibrationRenderJob] = []
        try:
            if getattr(self, "_engine", None) is not None:
                params: Dict[str, Any] = {"limit": lim}
                where_sql = ""
                if status_values:
                    placeholders = ", ".join(f":status_{idx}" for idx, _ in enumerate(status_values))
                    where_sql = f"WHERE lower(status) IN ({placeholders})"
                    params.update({f"status_{idx}": value for idx, value in enumerate(status_values)})
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text(
                            f"""
                            SELECT job_id, run_id, render_profile_id, sample_pack_version,
                                   status, artifact_ids_json, error_text,
                                   created_at, started_at, completed_at
                            FROM public.calibration_render_jobs
                            {where_sql}
                            ORDER BY created_at ASC
                            LIMIT :limit
                            """
                        ),
                        params,
                    ).mappings().all()
                return [item for row in rows if (item := self._row_to_calibration_render_job(row))]

            conn = self._get_connection()
            where_sql = ""
            params_sqlite: List[Any] = []
            if status_values:
                where_sql = "WHERE lower(status) IN ({})".format(", ".join("?" for _ in status_values))
                params_sqlite.extend(status_values)
            params_sqlite.append(lim)
            rows = conn.execute(
                f"SELECT job_id, run_id, render_profile_id, sample_pack_version, status, artifact_ids_json, error_text, created_at, started_at, completed_at FROM calibration_render_jobs {where_sql} ORDER BY created_at ASC LIMIT ?",
                tuple(params_sqlite),
            ).fetchall()
            return [item for row in rows if (item := self._row_to_calibration_render_job(row))]
        except Exception as exc:
            logger.error("Error listing calibration render jobs: %s", exc)
            return jobs

    def claim_calibration_render_job(
        self,
        *,
        job_id: str,
        from_statuses: Optional[List[str]] = None,
    ) -> bool:
        job_id = (job_id or "").strip()
        statuses = [str(value or "").strip().lower() for value in (from_statuses or ["queued", "retry"]) if str(value or "").strip()]
        if not job_id or not statuses:
            return False
        try:
            if getattr(self, "_engine", None) is not None:
                placeholders = ", ".join(f":status_{idx}" for idx, _ in enumerate(statuses))
                params: Dict[str, Any] = {"job_id": job_id}
                params.update({f"status_{idx}": value for idx, value in enumerate(statuses)})
                with self._engine.begin() as conn_pg:
                    result = conn_pg.execute(
                        text(
                            f"""
                            UPDATE public.calibration_render_jobs
                            SET status = 'running', started_at = COALESCE(started_at, NOW()), completed_at = NULL
                            WHERE job_id = :job_id AND lower(status) IN ({placeholders})
                            """
                        ),
                        params,
                    )
                return int(getattr(result, "rowcount", 0) or 0) > 0
            conn = self._get_connection()
            result = conn.execute(
                f"UPDATE calibration_render_jobs SET status = 'running', started_at = COALESCE(started_at, ?), completed_at = NULL WHERE job_id = ? AND lower(status) IN ({', '.join('?' for _ in statuses)})",
                (datetime.utcnow().isoformat(), job_id, *statuses),
            )
            conn.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0
        except Exception as exc:
            logger.error("Error claiming calibration render job %s: %s", job_id, exc)
            return False

    def update_calibration_render_job_status(
        self,
        *,
        job_id: str,
        status: str,
        artifact_ids: Optional[List[str]] = None,
        error_text: Optional[str] = None,
    ) -> bool:
        job_id = (job_id or "").strip()
        status_value = (status or "").strip().lower()
        if not job_id or not status_value:
            return False
        artifact_ids_json = self._json_dumps(artifact_ids or []) or "[]"
        terminal = status_value in {"completed", "failed", "canceled"}
        try:
            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    result = conn_pg.execute(
                        text(
                            """
                            UPDATE public.calibration_render_jobs
                            SET status = :status,
                                artifact_ids_json = :artifact_ids_json,
                                error_text = :error_text,
                                started_at = CASE WHEN started_at IS NULL AND :status = 'running' THEN NOW() ELSE started_at END,
                                completed_at = CASE WHEN :terminal THEN NOW() ELSE NULL END
                            WHERE job_id = :job_id
                            """
                        ),
                        {
                            "job_id": job_id,
                            "status": status_value,
                            "artifact_ids_json": artifact_ids_json,
                            "error_text": error_text,
                            "terminal": terminal,
                        },
                    )
                return int(getattr(result, "rowcount", 0) or 0) > 0
            conn = self._get_connection()
            now = datetime.utcnow().isoformat()
            result = conn.execute(
                "UPDATE calibration_render_jobs SET status = ?, artifact_ids_json = ?, error_text = ?, started_at = CASE WHEN started_at IS NULL AND ? = 'running' THEN ? ELSE started_at END, completed_at = CASE WHEN ? = 1 THEN ? ELSE NULL END WHERE job_id = ?",
                (status_value, artifact_ids_json, error_text, status_value, now, 1 if terminal else 0, now, job_id),
            )
            conn.commit()
            return int(getattr(result, "rowcount", 0) or 0) > 0
        except Exception as exc:
            logger.error("Error updating calibration render job %s: %s", job_id, exc)
            return False

    def get_evaluation_item(self, *, item_id: str) -> Optional[EvaluationItem]:
        try:
            item_id = (item_id or "").strip()
            if not item_id:
                return None

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    row = conn_pg.execute(
                        text(
                            """
                            SELECT *
                            FROM public.evaluation_items
                            WHERE item_id = :item_id
                            LIMIT 1
                            """
                        ),
                        {"item_id": item_id},
                    ).mappings().first()
                return self._row_to_evaluation_item(row) if row else None

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM evaluation_items WHERE item_id = ? LIMIT 1",
                (item_id,),
            )
            row = cursor.fetchone()
            return self._row_to_evaluation_item(row) if row else None
        except sqlite3.OperationalError as e:
            logger.warning(f"evaluation_items table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching evaluation item {item_id}: {str(e)}")
            self.database_error.emit(f"Error fetching evaluation item: {str(e)}")
            return None

    def log_pairwise_judgment(
        self,
        *,
        item_id: str,
        preferred_candidate: Optional[str] = None,
        closer_to_target: Optional[str] = None,
        better_feel: Optional[str] = None,
        more_musical: Optional[str] = None,
        confidence: Optional[int] = None,
    ) -> Optional[str]:
        try:
            item_id = (item_id or "").strip()
            if not item_id:
                return None

            judgment_id = f"judge_{uuid.uuid4().hex[:12]}"
            now = datetime.utcnow().isoformat()

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    row = conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.pairwise_judgments (
                                judgment_id, item_id, preferred_candidate,
                                closer_to_target, better_feel, more_musical,
                                confidence, created_at
                            ) VALUES (
                                :judgment_id, :item_id, :preferred_candidate,
                                :closer_to_target, :better_feel, :more_musical,
                                :confidence, NOW()
                            )
                            RETURNING judgment_id
                            """
                        ),
                        {
                            "judgment_id": judgment_id,
                            "item_id": item_id,
                            "preferred_candidate": preferred_candidate,
                            "closer_to_target": closer_to_target,
                            "better_feel": better_feel,
                            "more_musical": more_musical,
                            "confidence": self._safe_int(confidence),
                        },
                    ).first()
                stored_id = str(row[0]) if row else judgment_id
                self.data_changed.emit("pairwise_judgments", "insert")
                return stored_id

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> str:
                cursor.execute(
                    """
                    INSERT INTO pairwise_judgments (
                        judgment_id, item_id, preferred_candidate,
                        closer_to_target, better_feel, more_musical,
                        confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        judgment_id,
                        item_id,
                        preferred_candidate,
                        closer_to_target,
                        better_feel,
                        more_musical,
                        self._safe_int(confidence),
                        now,
                    ),
                )
                conn.commit()
                return judgment_id

            result = self._with_write_lock_retry(_do_write)
            if result:
                self.data_changed.emit("pairwise_judgments", "insert")
            return str(result) if result else None
        except sqlite3.OperationalError as e:
            logger.warning(f"pairwise_judgments table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error logging pairwise judgment for item {item_id}: {str(e)}")
            self.database_error.emit(f"Error logging pairwise judgment: {str(e)}")
            return None

    def log_attribute_rating(
        self,
        *,
        item_id: str,
        candidate_label: str,
        stylistic_authenticity: Optional[float] = None,
        groove_feel: Optional[float] = None,
        dynamics: Optional[float] = None,
        phrasing: Optional[float] = None,
        kit_balance: Optional[float] = None,
        fill_behavior: Optional[float] = None,
        human_realism: Optional[float] = None,
        overall_usefulness: Optional[float] = None,
    ) -> Optional[str]:
        try:
            item_id = (item_id or "").strip()
            candidate_label = (candidate_label or "").strip()
            if not item_id or not candidate_label:
                return None

            rating_id = f"rate_{uuid.uuid4().hex[:12]}"
            now = datetime.utcnow().isoformat()
            values = {
                "stylistic_authenticity": self._safe_int(round(stylistic_authenticity)) if stylistic_authenticity is not None else None,
                "groove_feel": self._safe_int(round(groove_feel)) if groove_feel is not None else None,
                "dynamics": self._safe_int(round(dynamics)) if dynamics is not None else None,
                "phrasing": self._safe_int(round(phrasing)) if phrasing is not None else None,
                "kit_balance": self._safe_int(round(kit_balance)) if kit_balance is not None else None,
                "fill_behavior": self._safe_int(round(fill_behavior)) if fill_behavior is not None else None,
                "human_realism": self._safe_int(round(human_realism)) if human_realism is not None else None,
                "overall_usefulness": self._safe_int(round(overall_usefulness)) if overall_usefulness is not None else None,
            }

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    row = conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.attribute_ratings (
                                rating_id, item_id, candidate_label,
                                stylistic_authenticity, groove_feel, dynamics,
                                phrasing, kit_balance, fill_behavior,
                                human_realism, overall_usefulness, created_at
                            ) VALUES (
                                :rating_id, :item_id, :candidate_label,
                                :stylistic_authenticity, :groove_feel, :dynamics,
                                :phrasing, :kit_balance, :fill_behavior,
                                :human_realism, :overall_usefulness, NOW()
                            )
                            RETURNING rating_id
                            """
                        ),
                        {
                            "rating_id": rating_id,
                            "item_id": item_id,
                            "candidate_label": candidate_label,
                            **values,
                        },
                    ).first()
                stored_id = str(row[0]) if row else rating_id
                self.data_changed.emit("attribute_ratings", "insert")
                return stored_id

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> str:
                cursor.execute(
                    """
                    INSERT INTO attribute_ratings (
                        rating_id, item_id, candidate_label,
                        stylistic_authenticity, groove_feel, dynamics,
                        phrasing, kit_balance, fill_behavior,
                        human_realism, overall_usefulness, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rating_id,
                        item_id,
                        candidate_label,
                        values["stylistic_authenticity"],
                        values["groove_feel"],
                        values["dynamics"],
                        values["phrasing"],
                        values["kit_balance"],
                        values["fill_behavior"],
                        values["human_realism"],
                        values["overall_usefulness"],
                        now,
                    ),
                )
                conn.commit()
                return rating_id

            result = self._with_write_lock_retry(_do_write)
            if result:
                self.data_changed.emit("attribute_ratings", "insert")
            return str(result) if result else None
        except sqlite3.OperationalError as e:
            logger.warning(f"attribute_ratings table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error logging attribute rating for item {item_id}: {str(e)}")
            self.database_error.emit(f"Error logging attribute rating: {str(e)}")
            return None

    def log_calibration_run(
        self,
        *,
        drummer_slug: str,
        outcome: str,
        started_at: Optional[datetime] = None,
        completed_at: Optional[datetime] = None,
        note_count: Optional[int] = None,
        fills_per_minute: Optional[float] = None,
        within_tolerance_count: Optional[int] = None,
        total_compared: Optional[int] = None,
        delta_summary: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        metrics: Optional[Dict[str, Any]] = None,
        comparison: Optional[Dict[str, Any]] = None,
        log_path: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> Optional[str]:
        try:
            drummer_slug = (drummer_slug or "").strip()
            outcome = (outcome or "").strip() or "unknown"
            if not drummer_slug:
                return None

            run_id = (run_id or str(uuid.uuid4())).strip()
            started_at = started_at or datetime.utcnow()
            completed_iso = completed_at.isoformat() if completed_at else None

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.calibration_runs (
                                run_id, drummer_slug, started_at, completed_at, outcome,
                                note_count, fills_per_minute, within_tolerance_count,
                                total_compared, delta_summary, metadata_json, metrics_json,
                                comparison_json, log_path
                            ) VALUES (
                                :run_id, :drummer_slug, :started_at, :completed_at, :outcome,
                                :note_count, :fills_per_minute, :within_tolerance_count,
                                :total_compared, :delta_summary, :metadata_json, :metrics_json,
                                :comparison_json, :log_path
                            )
                            ON CONFLICT (run_id) DO UPDATE SET
                                drummer_slug = EXCLUDED.drummer_slug,
                                started_at = EXCLUDED.started_at,
                                completed_at = EXCLUDED.completed_at,
                                outcome = EXCLUDED.outcome,
                                note_count = EXCLUDED.note_count,
                                fills_per_minute = EXCLUDED.fills_per_minute,
                                within_tolerance_count = EXCLUDED.within_tolerance_count,
                                total_compared = EXCLUDED.total_compared,
                                delta_summary = EXCLUDED.delta_summary,
                                metadata_json = EXCLUDED.metadata_json,
                                metrics_json = EXCLUDED.metrics_json,
                                comparison_json = EXCLUDED.comparison_json,
                                log_path = EXCLUDED.log_path
                            """
                        ),
                        {
                            "run_id": run_id,
                            "drummer_slug": drummer_slug,
                            "started_at": started_at.isoformat(),
                            "completed_at": completed_iso,
                            "outcome": outcome,
                            "note_count": note_count,
                            "fills_per_minute": fills_per_minute,
                            "within_tolerance_count": within_tolerance_count,
                            "total_compared": total_compared,
                            "delta_summary": delta_summary,
                            "metadata_json": self._json_dumps(metadata),
                            "metrics_json": self._json_dumps(metrics),
                            "comparison_json": self._json_dumps(comparison),
                            "log_path": log_path,
                        },
                    )
                self.data_changed.emit("calibration_runs", "upsert")
                return run_id

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> str:
                cursor.execute(
                    """
                    INSERT INTO calibration_runs (
                        run_id, drummer_slug, started_at, completed_at, outcome,
                        note_count, fills_per_minute, within_tolerance_count,
                        total_compared, delta_summary, metadata_json, metrics_json,
                        comparison_json, log_path
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id) DO UPDATE SET
                        drummer_slug = excluded.drummer_slug,
                        started_at = excluded.started_at,
                        completed_at = excluded.completed_at,
                        outcome = excluded.outcome,
                        note_count = excluded.note_count,
                        fills_per_minute = excluded.fills_per_minute,
                        within_tolerance_count = excluded.within_tolerance_count,
                        total_compared = excluded.total_compared,
                        delta_summary = excluded.delta_summary,
                        metadata_json = excluded.metadata_json,
                        metrics_json = excluded.metrics_json,
                        comparison_json = excluded.comparison_json,
                        log_path = excluded.log_path
                    """,
                    (
                        run_id,
                        drummer_slug,
                        started_at.isoformat(),
                        completed_iso,
                        outcome,
                        note_count,
                        fills_per_minute,
                        within_tolerance_count,
                        total_compared,
                        delta_summary,
                        self._json_dumps(metadata),
                        self._json_dumps(metrics),
                        self._json_dumps(comparison),
                        log_path,
                    ),
                )
                conn.commit()
                return run_id

            result = self._with_write_lock_retry(_do_write)
            self.data_changed.emit("calibration_runs", "upsert")
            return str(result)
        except sqlite3.OperationalError as e:
            logger.warning(f"calibration_runs table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error logging calibration run for {drummer_slug}: {str(e)}")
            self.database_error.emit(f"Error logging calibration run: {str(e)}")
            return None

    def get_calibration_run(self, *, run_id: str) -> Optional[CalibrationRun]:
        try:
            run_id = (run_id or "").strip()
            if not run_id:
                return None

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    row = conn_pg.execute(
                        text(
                            """
                            SELECT run_id, drummer_slug, started_at, completed_at, outcome,
                                   note_count, fills_per_minute, within_tolerance_count,
                                   total_compared, delta_summary, metadata_json,
                                   metrics_json, comparison_json, log_path
                            FROM public.calibration_runs
                            WHERE run_id = :run_id
                            LIMIT 1
                            """
                        ),
                        {"run_id": run_id},
                    ).mappings().first()
                return self._row_to_calibration_run(row) if row else None

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT run_id, drummer_slug, started_at, completed_at, outcome,
                       note_count, fills_per_minute, within_tolerance_count,
                       total_compared, delta_summary, metadata_json,
                       metrics_json, comparison_json, log_path
                FROM calibration_runs
                WHERE run_id = ?
                LIMIT 1
                """,
                (run_id,),
            )
            row = cursor.fetchone()
            return self._row_to_calibration_run(row) if row else None
        except sqlite3.OperationalError as e:
            logger.warning(f"calibration_runs table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error fetching calibration run {run_id}: {str(e)}")
            self.database_error.emit(f"Error fetching calibration run: {str(e)}")
            return None

    def get_calibration_runs(
        self,
        *,
        drummer_slug: str,
        limit: int = 25,
        include_metrics: bool = True,
    ) -> List[CalibrationRun]:
        runs: List[CalibrationRun] = []
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return runs

            if getattr(self, "_engine", None) is not None:
                select_columns = [
                    "run_id",
                    "drummer_slug",
                    "started_at",
                    "completed_at",
                    "outcome",
                    "note_count",
                    "fills_per_minute",
                    "within_tolerance_count",
                    "total_compared",
                    "delta_summary",
                    "log_path",
                ]
                if include_metrics:
                    select_columns.extend(["metadata_json", "metrics_json", "comparison_json"])

                query = f"""
                SELECT {', '.join(select_columns)}
                FROM public.calibration_runs
                WHERE drummer_slug = :drummer_slug
                ORDER BY started_at DESC
                LIMIT :limit
                """
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text(query),
                        {"drummer_slug": drummer_slug, "limit": max(1, int(limit))},
                    ).mappings().all()

                for row in rows:
                    run = self._row_to_calibration_run(row)
                    if run:
                        runs.append(run)
                return runs

            conn = self._get_connection()
            cursor = conn.cursor()

            select_columns = [
                "run_id",
                "drummer_slug",
                "started_at",
                "completed_at",
                "outcome",
                "note_count",
                "fills_per_minute",
                "within_tolerance_count",
                "total_compared",
                "delta_summary",
                "log_path",
            ]
            if include_metrics:
                select_columns.extend(["metadata_json", "metrics_json", "comparison_json"])

            cursor.execute(
                f"""
                SELECT {', '.join(select_columns)}
                FROM calibration_runs
                WHERE drummer_slug = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (drummer_slug, max(1, int(limit))),
            )
            rows = cursor.fetchall() or []

            for row in rows:
                run = self._row_to_calibration_run(row)
                if run:
                    runs.append(run)

            return runs
        except sqlite3.OperationalError as e:
            logger.warning(f"calibration_runs table not available: {e}")
            return runs
        except Exception as e:
            logger.error(f"Error getting calibration runs for {drummer_slug}: {str(e)}")
            self.database_error.emit(f"Error getting calibration runs: {str(e)}")
            return runs

    def get_latest_calibration_run(self, *, drummer_slug: str) -> Optional[CalibrationRun]:
        try:
            items = self.get_calibration_runs(drummer_slug=drummer_slug, limit=1, include_metrics=True)
            return items[0] if items else None
        except Exception as e:
            logger.error(f"Error fetching latest calibration run for {drummer_slug}: {str(e)}")
            self.database_error.emit(f"Error fetching latest calibration run: {str(e)}")
            return None

    def log_calibration_feedback(
        self,
        *,
        drummer_slug: str,
        rating: int,
        comment: Optional[str] = None,
        author: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        submitted_at: Optional[datetime] = None,
        feedback_id: Optional[str] = None,
    ) -> Optional[str]:
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return None

            try:
                rating_value = int(rating)
            except Exception:
                rating_value = 0
            rating_value = max(min(rating_value, 10), 0)

            feedback_id = (feedback_id or str(uuid.uuid4())).strip()
            submitted_at = submitted_at or datetime.utcnow()
            metadata_json = self._json_dumps(metadata) if metadata else None

            if getattr(self, "_engine", None) is not None:
                with self._engine.begin() as conn_pg:
                    conn_pg.execute(
                        text(
                            """
                            INSERT INTO public.calibration_feedback (
                                feedback_id, drummer_slug, rating, comment, author,
                                submitted_at, metadata_json
                            ) VALUES (
                                :feedback_id, :drummer_slug, :rating, :comment, :author,
                                :submitted_at, :metadata_json
                            )
                            ON CONFLICT (feedback_id) DO UPDATE SET
                                drummer_slug = EXCLUDED.drummer_slug,
                                rating = EXCLUDED.rating,
                                comment = EXCLUDED.comment,
                                author = EXCLUDED.author,
                                submitted_at = EXCLUDED.submitted_at,
                                metadata_json = EXCLUDED.metadata_json
                            """
                        ),
                        {
                            "feedback_id": feedback_id,
                            "drummer_slug": drummer_slug,
                            "rating": rating_value,
                            "comment": comment,
                            "author": author,
                            "submitted_at": submitted_at.isoformat(),
                            "metadata_json": metadata_json,
                        },
                    )
                self.data_changed.emit("calibration_feedback", "upsert")
                return feedback_id

            conn = self._get_connection()
            cursor = conn.cursor()

            def _do_write() -> str:
                cursor.execute(
                    """
                    INSERT INTO calibration_feedback (
                        feedback_id, drummer_slug, rating, comment, author,
                        submitted_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feedback_id) DO UPDATE SET
                        drummer_slug = excluded.drummer_slug,
                        rating = excluded.rating,
                        comment = excluded.comment,
                        author = excluded.author,
                        submitted_at = excluded.submitted_at,
                        metadata_json = excluded.metadata_json
                    """,
                    (
                        feedback_id,
                        drummer_slug,
                        rating_value,
                        comment,
                        author,
                        submitted_at.isoformat(),
                        metadata_json,
                    ),
                )
                conn.commit()
                return feedback_id

            result = self._with_write_lock_retry(_do_write)
            self.data_changed.emit("calibration_feedback", "upsert")
            return str(result)
        except sqlite3.OperationalError as e:
            logger.warning(f"calibration_feedback table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error logging calibration feedback for {drummer_slug}: {str(e)}")
            self.database_error.emit(f"Error logging calibration feedback: {str(e)}")
            return None

    def get_calibration_feedback(
        self,
        *,
        drummer_slug: str,
        limit: int = 50,
    ) -> List[CalibrationFeedback]:
        feedback: List[CalibrationFeedback] = []
        try:
            drummer_slug = (drummer_slug or "").strip()
            if not drummer_slug:
                return feedback

            if getattr(self, "_engine", None) is not None:
                with self._engine.connect() as conn_pg:
                    rows = conn_pg.execute(
                        text(
                            """
                            SELECT feedback_id, drummer_slug, rating, comment, author,
                                   submitted_at, metadata_json
                            FROM public.calibration_feedback
                            WHERE drummer_slug = :drummer_slug
                            ORDER BY submitted_at DESC
                            LIMIT :limit
                            """
                        ),
                        {"drummer_slug": drummer_slug, "limit": max(1, int(limit))},
                    ).mappings().all()
                for row in rows:
                    item = self._row_to_calibration_feedback(row)
                    if item:
                        feedback.append(item)
                return feedback

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT feedback_id, drummer_slug, rating, comment, author,
                       submitted_at, metadata_json
                FROM calibration_feedback
                WHERE drummer_slug = ?
                ORDER BY submitted_at DESC
                LIMIT ?
                """,
                (drummer_slug, max(1, int(limit))),
            )
            for row in cursor.fetchall() or []:
                item = self._row_to_calibration_feedback(row)
                if item:
                    feedback.append(item)

            return feedback
        except sqlite3.OperationalError as e:
            logger.warning(f"calibration_feedback table not available: {e}")
            return feedback
        except Exception as e:
            logger.error(f"Error getting calibration feedback for {drummer_slug}: {str(e)}")
            self.database_error.emit(f"Error getting calibration feedback: {str(e)}")
            return feedback

    def get_latest_calibration_feedback(self, *, drummer_slug: str) -> Optional[CalibrationFeedback]:
        items = self.get_calibration_feedback(drummer_slug=drummer_slug, limit=1)
        return items[0] if items else None

    def add_drummer(self, name: str, description: str = "") -> Optional[str]:
        """
        Add a new drummer to the database.
        
        Args:
            name: The name of the drummer
            description: Optional description
            
        Returns:
            str or None: The ID of the new drummer or None if failed
        """
        try:
            drummer_id = str(uuid.uuid4())
            now = datetime.now().isoformat()

            conn = self._get_connection()
            cursor = conn.cursor()

            cols = self._table_columns("drummers")
            if "name" in cols:
                cursor.execute(
                    'INSERT INTO drummers (id, name, description, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
                    (drummer_id, name, description, now, now)
                )
            elif "display_name" in cols and "drummer_id" in cols:
                cursor.execute(
                    'INSERT INTO drummers (drummer_id, display_name) VALUES (?, ?)',
                    (drummer_id, name)
                )
            else:
                raise RuntimeError("Unsupported drummers table schema")
            conn.commit()
            
            self.data_changed.emit('drummers', 'insert')
            logger.info(f"Added new drummer: {name} (ID: {drummer_id})")
            return drummer_id
            
        except Exception as e:
            logger.error(f"Error adding drummer {name}: {str(e)}")
            self.database_error.emit(f"Error adding drummer: {str(e)}")
            return None

    def update_drummer(self, drummer_id: str, data: Dict) -> bool:
        """
        Update a drummer's information.
        
        Args:
            drummer_id: The ID of the drummer to update
            data: Dictionary with fields to update
            
        Returns:
            bool: True if successful
        """
        try:
            cols = self._table_columns("drummers")
            if "name" in cols:
                valid_fields = {'name', 'description'}
            else:
                valid_fields = {'display_name', 'real_name', 'tagline', 'bio', 'youtube_channel', 'photo_url', 'source'}
            update_data = {k: v for k, v in data.items() if k in valid_fields}
            
            if not update_data:
                logger.warning("No valid fields to update for drummer")
                return False
                
            # Add updated_at timestamp
            if "updated_at" in cols:
                update_data['updated_at'] = datetime.now().isoformat()
            
            # Build the SQL query
            field_str = ', '.join([f"{field} = ?" for field in update_data.keys()])
            values = list(update_data.values()) + [drummer_id]
            
            conn = self._get_connection()
            cursor = conn.cursor()
            if "drummer_id" in cols:
                cursor.execute(f"UPDATE drummers SET {field_str} WHERE drummer_id = ?", values)
            else:
                cursor.execute(f"UPDATE drummers SET {field_str} WHERE id = ?", values)
            conn.commit()
            
            self.data_changed.emit('drummers', 'update')
            logger.info(f"Updated drummer ID: {drummer_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error updating drummer {drummer_id}: {str(e)}")
            self.database_error.emit(f"Error updating drummer: {str(e)}")
            return False

    def delete_drummer(self, drummer_id: str) -> bool:
        """
        Delete a drummer from the database.
        
        Args:
            drummer_id: The ID of the drummer to delete
            
        Returns:
            bool: True if successful
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cols = self._table_columns("drummers")
            if "drummer_id" in cols:
                cursor.execute('DELETE FROM drummers WHERE drummer_id = ?', (drummer_id,))
            else:
                cursor.execute('DELETE FROM drummers WHERE id = ?', (drummer_id,))
            conn.commit()
            
            self.data_changed.emit('drummers', 'delete')
            logger.info(f"Deleted drummer ID: {drummer_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error deleting drummer {drummer_id}: {str(e)}")
            self.database_error.emit(f"Error deleting drummer: {str(e)}")
            return False

    # ---- Drummer personas (admin DB integration) ---------------------

    def get_drummer_persona(self, persona_id: str) -> Optional[Dict[str, Any]]:
        """Load a single drummer persona by ID from drummer_personas.

        Returns a dict with keys:
        - persona_id
        - display_name
        - archetypes (list[str])
        - style (dict of aggregated style metrics)
        or None if not found or table missing.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT persona_id, display_name, archetypes_json, style_json
                FROM drummer_personas
                WHERE persona_id = ?
                """,
                (persona_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None

            archetypes = json.loads(row["archetypes_json"]) if row["archetypes_json"] else []
            style = json.loads(row["style_json"]) if row["style_json"] else {}
            return {
                "persona_id": row["persona_id"],
                "display_name": row["display_name"],
                "archetypes": archetypes,
                "style": style,
            }
        except sqlite3.OperationalError as e:
            # Likely table does not exist in this DB; fail soft.
            logger.warning(f"drummer_personas table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error getting drummer persona {persona_id}: {str(e)}")
            self.database_error.emit(f"Error getting drummer persona: {str(e)}")
            return None

    def get_all_drummer_personas(self) -> List[Dict[str, Any]]:
        """Return all drummer personas as a list of dicts.

        See get_drummer_persona for the dict shape.
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT persona_id, display_name, archetypes_json, style_json
                FROM drummer_personas
                ORDER BY display_name
                """
            )
            rows = cursor.fetchall()
            personas: List[Dict[str, Any]] = []
            for row in rows:
                archetypes = json.loads(row["archetypes_json"]) if row["archetypes_json"] else []
                style = json.loads(row["style_json"]) if row["style_json"] else {}
                personas.append(
                    {
                        "persona_id": row["persona_id"],
                        "display_name": row["display_name"],
                        "archetypes": archetypes,
                        "style": style,
                    }
                )
            return personas
        except sqlite3.OperationalError as e:
            logger.warning(f"drummer_personas table not available: {e}")
            return []
        except Exception as e:
            logger.error(f"Error getting drummer personas: {str(e)}")
            self.database_error.emit(f"Error getting drummer personas: {str(e)}")
            return []

    # ---- Drummer category mappings (admin-only) ----------------------

    def get_drummer_category_mapping(self, category_id: str) -> Optional[Dict[str, Any]]:
        """Return mapping for a public drummer category, if defined.

        Shape:
          {
            "category_id": str,
            "display_name": str,
            "primary_persona_id": str,
            "backup_persona_ids": [str],
            "default_humanize": float | None,
            "default_swing": float | None,
            "default_chorus_ride_pref": float | None,
          }
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''SELECT category_id, display_name, primary_persona_id,
                          backup_persona_ids_json, default_humanize,
                          default_swing, default_chorus_ride_pref
                   FROM drummer_category_mappings
                   WHERE category_id = ?''',
                (category_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            backups = []
            if row[3]:
                try:
                    backups = json.loads(row[3])
                except Exception:
                    backups = []
            return {
                "category_id": row[0],
                "display_name": row[1],
                "primary_persona_id": row[2],
                "backup_persona_ids": backups,
                "default_humanize": row[4],
                "default_swing": row[5],
                "default_chorus_ride_pref": row[6],
            }
        except sqlite3.OperationalError as e:
            logger.warning(f"drummer_category_mappings table not available: {e}")
            return None
        except Exception as e:
            logger.error(f"Error getting drummer category mapping {category_id}: {str(e)}")
            self.database_error.emit(f"Error getting drummer category mapping: {str(e)}")
            return None

    def upsert_drummer_category_mapping(
        self,
        category_id: str,
        display_name: str,
        primary_persona_id: str,
        backup_persona_ids: Optional[List[str]] = None,
        default_humanize: Optional[float] = None,
        default_swing: Optional[float] = None,
        default_chorus_ride_pref: Optional[float] = None,
    ) -> bool:
        """Insert or update a mapping from category_id -> persona + defaults."""
        try:
            now = datetime.now().isoformat()
            backups_json = json.dumps(backup_persona_ids or [])

            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO drummer_category_mappings (
                       category_id, display_name, primary_persona_id,
                       backup_persona_ids_json, default_humanize,
                       default_swing, default_chorus_ride_pref,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(category_id) DO UPDATE SET
                       display_name = excluded.display_name,
                       primary_persona_id = excluded.primary_persona_id,
                       backup_persona_ids_json = excluded.backup_persona_ids_json,
                       default_humanize = excluded.default_humanize,
                       default_swing = excluded.default_swing,
                       default_chorus_ride_pref = excluded.default_chorus_ride_pref,
                       updated_at = excluded.updated_at
                ''',
                (
                    category_id,
                    display_name,
                    primary_persona_id,
                    backups_json,
                    default_humanize,
                    default_swing,
                    default_chorus_ride_pref,
                    now,
                    now,
                ),
            )
            conn.commit()
            logger.info(f"Upserted drummer_category_mapping for {category_id} -> {primary_persona_id}")
            return True
        except Exception as e:
            logger.error(f"Error upserting drummer category mapping for {category_id}: {str(e)}")
            self.database_error.emit(f"Error upserting drummer category mapping: {str(e)}")
            return False

    # CRUD operations for songs
    def get_songs(self, drummer_id: Optional[str] = None) -> List[Dict]:
        """
        Get songs from the database, optionally filtered by drummer.
        
        Args:
            drummer_id: Optional drummer ID to filter by
            
        Returns:
            List[Dict]: List of song records
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            if drummer_id:
                cursor.execute('SELECT * FROM songs WHERE drummer_id = ? ORDER BY title', (drummer_id,))
            else:
                cursor.execute('SELECT * FROM songs ORDER BY title')
                
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except Exception as e:
            logger.error(f"Error getting songs: {str(e)}")
            self.database_error.emit(f"Error getting songs: {str(e)}")
            return []

    def get_song(self, song_id: str) -> Optional[Dict]:
        """
        Get a song by ID.
        
        Args:
            song_id: The ID of the song
            
        Returns:
            Dict or None: Song record or None if not found
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM songs WHERE id = ?', (song_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
            
        except Exception as e:
            logger.error(f"Error getting song {song_id}: {str(e)}")
            self.database_error.emit(f"Error getting song: {str(e)}")
            return None

    def add_song(self, title: str, file_path: str = None, drummer_id: str = None, metadata: Dict = None) -> Optional[str]:
        """
        Add a new song to the database.
        
        Args:
            title: The title of the song
            file_path: Path to the audio file
            drummer_id: Optional ID of the associated drummer
            metadata: Optional additional metadata (artist, album, etc.)
            
        Returns:
            str or None: The ID of the new song or None if failed
        """
        try:
            song_id = str(uuid.uuid4())
            now = datetime.now().isoformat()
            
            # Extract metadata if provided
            if metadata is None:
                metadata = {}
                
            artist = metadata.get('artist', '')
            album = metadata.get('album', '')
            year = metadata.get('year', None)
            genre = metadata.get('genre', '')
            duration = metadata.get('duration', None)
            
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO songs 
                   (id, title, artist, album, year, genre, duration, file_path, drummer_id, created_at, updated_at) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (song_id, title, artist, album, year, genre, duration, file_path, drummer_id, now, now)
            )
            conn.commit()
            
            self.data_changed.emit('songs', 'insert')
            logger.info(f"Added new song: {title} (ID: {song_id})")
            return song_id
            
        except Exception as e:
            logger.error(f"Error adding song {title}: {str(e)}")
            self.database_error.emit(f"Error adding song: {str(e)}")
            return None

    # CRUD operations for drum beats
    def get_drum_beats(self, drummer_id: Optional[str] = None, song_id: Optional[str] = None) -> List[Dict]:
        """
        Get drum beats from the database, optionally filtered by drummer or song.
        
        Args:
            drummer_id: Optional drummer ID to filter by
            song_id: Optional song ID to filter by
            
        Returns:
            List[Dict]: List of drum beat records
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            
            query = 'SELECT * FROM drum_beats'
            params = []
            
            if drummer_id and song_id:
                query += ' WHERE drummer_id = ? AND song_id = ?'
                params = [drummer_id, song_id]
            elif drummer_id:
                query += ' WHERE drummer_id = ?'
                params = [drummer_id]
            elif song_id:
                query += ' WHERE song_id = ?'
                params = [song_id]
                
            cols = self._table_columns("drum_beats")
            query += ' ORDER BY name' if 'name' in cols else ' ORDER BY id'
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            results = [dict(row) for row in rows]
            if results:
                return results

            # Fallback: if drum_beats table is empty, show beats from the local DrumBeats folder
            try:
                project_root = Path(__file__).resolve().parents[2]
                beats_dir = project_root / "DrumBeats"
                if beats_dir.exists() and beats_dir.is_dir():
                    synthetic: List[Dict[str, Any]] = []
                    for wav in sorted(beats_dir.glob("*.wav")):
                        synthetic.append({
                            "id": wav.stem,
                            "name": wav.stem.replace("_", " "),
                            "description": "(filesystem)",
                            "file_path": str(wav),
                            "song_id": None,
                            "drummer_id": None,
                            "bpm": None,
                            "time_signature": None,
                            "complexity": None,
                            "energy": None,
                        })
                    return synthetic
            except Exception:
                pass

            return []
        except Exception as e:
            logger.error(f"Error getting drum beats: {str(e)}")
            self.database_error.emit(f"Error getting drum beats: {str(e)}")
            return []

    def get_drum_beat(self, beat_id: str) -> Optional[Dict]:
        """
        Get a drum beat by ID.
        
        Args:
            beat_id: The ID of the drum beat
            
        Returns:
            Dict or None: Drum beat record or None if not found
        """
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM drum_beats WHERE id = ?', (beat_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
            
        except Exception as e:
            logger.error(f"Error getting drum beat {beat_id}: {str(e)}")
            self.database_error.emit(f"Error getting drum beat: {str(e)}")
            return None

    def add_drum_beat(self, name: str, file_path: str = None, drummer_id: str = None, song_id: str = None, metadata: Dict = None) -> Optional[str]:
        """
        Add a new drum beat to the database.
        
        Args:
            name: The name of the drum beat
            file_path: Path to the audio file
            drummer_id: Optional ID of the associated drummer
            song_id: Optional ID of the associated song
            metadata: Optional additional metadata (bpm, complexity, etc.)
            
        Returns:
            str or None: The ID of the new drum beat or None if failed
        """
        try:
            beat_id = str(uuid.uuid4())
            now = datetime.now().isoformat()
            
            # Extract metadata if provided
            if metadata is None:
                metadata = {}
                
            description = metadata.get('description', '')
            bpm = metadata.get('bpm', None)
            time_signature = metadata.get('time_signature', '')
            complexity = metadata.get('complexity', None)
            energy = metadata.get('energy', None)
            
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO drum_beats 
                   (id, name, description, file_path, song_id, drummer_id, bpm, time_signature, complexity, energy, created_at, updated_at) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (beat_id, name, description, file_path, song_id, drummer_id, bpm, time_signature, complexity, energy, now, now)
            )
            conn.commit()
            
            self.data_changed.emit('drum_beats', 'insert')
            logger.info(f"Added new drum beat: {name} (ID: {beat_id})")
            return beat_id
            
        except Exception as e:
            logger.error(f"Error adding drum beat {name}: {str(e)}")
            self.database_error.emit(f"Error adding drum beat: {str(e)}")
            return None
            
    # Function to get the singleton instance
    @staticmethod
    def get_database():
        """Get the singleton database instance"""
        return CentralDatabaseService.get_instance()

# Singleton access function
def get_database_service() -> CentralDatabaseService:
    """Get the singleton database service instance"""
    return CentralDatabaseService.get_instance()
