import json
import hashlib
import hmac
import logging
import os
import time
import sqlite3
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Set

import numpy as np
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict

from backend.calibration_api import router as calibration_router
from backend.app.assimilation.api.routes_drummer_generation import router as assimilation_generation_router
try:
    from backend.app.assimilation.api.routes_drummer_profiles import router as assimilation_profiles_router
except Exception:  # pragma: no cover - optional legacy router may be absent
    assimilation_profiles_router = None

try:
    from backend.drummerbrain.performance_spec_sentient import build_sentient_instrument_profile
except Exception:  # pragma: no cover - keep llm_service bootable if backend package is absent
    build_sentient_instrument_profile = None

try:
    from backend.drummerbrain.song_roadmap_sentient import build_song_roadmap_section_overrides
except Exception:  # pragma: no cover - keep llm_service bootable if backend package is absent
    build_song_roadmap_section_overrides = None

try:
    from backend.drummerbrain.phrase_selection_sentient import (
        choose_phrase_shape_from_family,
        select_phrase_families,
    )
except Exception:  # pragma: no cover - optional additive patch
    choose_phrase_shape_from_family = None
    select_phrase_families = None

try:
    from backend.drummerbrain.phrase_retrieval_sentient import retrieve_phrase_assets
except Exception:  # pragma: no cover - optional additive patch
    retrieve_phrase_assets = None

try:
    from backend.drummerbrain.phrase_event_loader_sentient import build_phrase_event_pattern
except Exception:  # pragma: no cover - optional additive patch
    build_phrase_event_pattern = None

try:
    from backend.drummerbrain.performance_to_dcsm_sentient import build_dcsm_payload_from_sentient_spec
except Exception:  # pragma: no cover - optional additive patch
    build_dcsm_payload_from_sentient_spec = None

try:
    from backend.drummerbrain.render_take_sentient import build_sentient_take_bundle
except Exception:  # pragma: no cover - optional additive patch
    build_sentient_take_bundle = None

try:
    # Optional helper to render plugin MIDI from note events
    from backend.render_to_plugin_midi import render_articulated_notes_to_midi
except Exception:  # pragma: no cover - keep service bootable without plugin helper
    render_articulated_notes_to_midi = None

try:
    from backend.drummerbrain.sentient_request_routing import (
        has_sentient_profile,
        normalize_generate_drums_payload,
    )
except Exception:  # pragma: no cover - optional additive patch
    has_sentient_profile = None
    normalize_generate_drums_payload = None

try:
    from backend.drummerbrain.sentient_profile_registry import build_sentient_profile_response
except Exception:  # pragma: no cover - optional additive patch
    build_sentient_profile_response = None

try:
    from backend.drummerbrain.section_sentient_overrides import (
        build_section_profile_map,
        derive_orchestration_bias,
        derive_time_feel,
        derive_transition_bias,
        has_sentient_identity,
        resolve_section_profile,
    )
except Exception:  # pragma: no cover - optional additive patch
    build_section_profile_map = None
    derive_orchestration_bias = None
    derive_time_feel = None
    derive_transition_bias = None
    has_sentient_identity = None
    resolve_section_profile = None

try:
    from backend.drummerbrain.section_asset_scoring_sentient import derive_section_asset_scoring
except Exception:  # pragma: no cover - optional additive patch
    derive_section_asset_scoring = None

try:
    from backend.groove_catalog import GrooveCatalog
except Exception:  # pragma: no cover - optional additive patch
    GrooveCatalog = None

try:
    from admin.services.central_database_service import CentralDatabaseService
except Exception:  # pragma: no cover - optional additive patch
    CentralDatabaseService = None  # type: ignore[assignment]

try:
    from backend.services.calibration_render_service import CalibrationRenderService, RenderRequest
except Exception:  # pragma: no cover - optional additive patch
    CalibrationRenderService = None  # type: ignore[assignment]
    RenderRequest = None  # type: ignore[assignment]

try:
    from backend.services.calibration_candidate_generator import generate_candidate_run
except Exception:  # pragma: no cover - optional additive patch
    generate_candidate_run = None

try:
    import torch
    import torch.nn as nn
    TORCH_AVAILABLE = True
except Exception:
    torch = None
    nn = None
    TORCH_AVAILABLE = False

try:
    import onnxruntime as ort
    ONNX_AVAILABLE = True
except Exception:
    ort = None
    ONNX_AVAILABLE = False

try:
    import onnx  # noqa: F401
    ONNX_EXPORT_AVAILABLE = True
except Exception:
    onnx = None
    ONNX_EXPORT_AVAILABLE = False

# ------------------------------------------------------------
# Minimal helpers for UI compatibility (drummers/grooves)
# ------------------------------------------------------------

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _candidate_profile_roots() -> List[Path]:
    roots: List[Path] = []
    env = str(os.getenv("SENTIENT_PROFILE_ROOTS", "").strip())
    if env:
        for raw in env.split(os.pathsep):
            try:
                p = Path(raw).expanduser().resolve()
                if p.exists():
                    roots.append(p)
            except Exception:
                continue
    base = _repo_root()
    for rel in (
        Path("database/drummer_profiles_generated"),
        Path("backend/database/drummer_profiles_generated"),
        Path("admin/database/drummer_profiles_generated"),
    ):
        try:
            p = (base / rel).resolve()
            if p.exists():
                roots.append(p)
        except Exception:
            continue
    # de-dup
    seen: Set[str] = set()
    out: List[Path] = []
    for r in roots:
        s = str(r)
        if s in seen:
            continue
        seen.add(s)
        out.append(r)
    return out


def _scan_profile_slugs(max_items: int = 512) -> List[str]:
    slugs: List[str] = []
    seen: Set[str] = set()
    for root in _candidate_profile_roots():
        try:
            for child in root.iterdir():
                if len(slugs) >= max_items:
                    break
                try:
                    if child.is_dir():
                        if (child / "drummer_profile.json").exists():
                            slug = child.name.strip()
                            if slug and slug not in seen:
                                seen.add(slug)
                                slugs.append(slug)
                    elif child.is_file() and child.suffix.lower() == ".json":
                        slug = child.stem.strip()
                        if slug and slug not in seen:
                            seen.add(slug)
                            slugs.append(slug)
                except Exception:
                    continue
        except Exception:
            continue
    return slugs


def _title_from_slug(slug: str) -> str:
    s = str(slug or "").replace("_", " ").replace("-", " ")
    return " ".join(w for w in s.split() if w).title() or str(slug)


def _resolve_admin_db_path_for_listing() -> Optional[Path]:
    env = str(os.getenv("DRUMTRACKAI_DB_PATH", "").strip())
    if env:
        p = Path(env)
        if p.exists():
            return p
    cand = _repo_root() / "admin" / "drumtrackai.db"
    return cand if cand.exists() else None


def _split_env_paths(raw: str) -> List[str]:
    parts: List[str] = []
    for chunk in str(raw or "").replace(";", os.pathsep).replace(",", os.pathsep).split(os.pathsep):
        c = chunk.strip()
        if c:
            parts.append(c)
    return parts


def _groove_manifest_candidates() -> List[Path]:
    out: List[Path] = []
    for env_name in ("DTK_GROOVE_MANIFEST_PATHS", "DTK_GROOVE_MANIFEST_PATH", "GROOVE_CATALOG_MANIFESTS", "GROOVE_CATALOG_MANIFEST"):
        for part in _split_env_paths(os.getenv(env_name, "")):
            try:
                p = Path(part)
                if p.exists():
                    out.append(p)
            except Exception:
                continue
    if not out:
        base = _repo_root()
        for rel in (
            Path("admin/training/egmd_phrase_manifest.jsonl"),
            Path("llm_training_project/training_datasets/egmd_phrase_select_train.jsonl"),
        ):
            try:
                p = (base / rel).resolve()
                if p.exists():
                    out.append(p)
            except Exception:
                continue
    return out


@lru_cache(maxsize=1)
def _get_groove_catalog() -> Optional[Any]:
    if GrooveCatalog is None:
        return None
    paths = _groove_manifest_candidates()
    if not paths:
        return None
    try:
        if len(paths) == 1:
            return GrooveCatalog(paths[0])
        return GrooveCatalog(paths)
    except Exception:
        return None


def _egmd_training_manifest_path() -> Optional[Path]:
    # Prefer explicit env overrides first
    for env_name in ("DTK_EGMD_TRAINING_JSONL", "EGMD_TRAINING_JSONL"):
        raw = str(os.getenv(env_name, "").strip())
        if raw:
            try:
                p = Path(raw).expanduser().resolve()
                if p.exists():
                    return p
            except Exception:
                continue
    # Fallback to repository path
    p = _repo_root() / "llm_training_project" / "training_datasets" / "egmd_phrase_select_train.jsonl"
    try:
        return p if p.exists() else None
    except Exception:
        return None


def _iter_training_jsonl(path: Path):
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                try:
                    obj = json.loads((line or "").strip())
                except Exception:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except Exception:
        return


def _egmd_style_groups_from_training(limit: int = 200) -> List[str]:
    p = _egmd_training_manifest_path()
    if not p:
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for obj in _iter_training_jsonl(p):
        try:
            if str(obj.get("task") or "").strip() != "select_phrase":
                continue
            inp = obj.get("input") or {}
            sg = str(inp.get("style_group") or "").strip().lower()
            if not sg:
                # Try derive from output.midi_path filename
                outp = obj.get("output") or {}
                mp = str(outp.get("midi_path") or "").strip().lower()
                if mp:
                    base = os.path.basename(mp)
                    # pattern: <num>_<style>_<bpm>_(beat|groove|fill)_...
                    try:
                        parts = base.split("_")
                        if len(parts) >= 2:
                            sg = parts[1]
                    except Exception:
                        sg = ""
            if sg and sg not in seen:
                seen.add(sg)
                out.append(sg)
                if len(out) >= max(1, int(limit)):
                    break
        except Exception:
            continue
    return out


def _egmd_phrases_from_training(style_group: Optional[str], limit: int = 50) -> List[Dict[str, Any]]:
    p = _egmd_training_manifest_path()
    if not p:
        return []
    want_sg = str(style_group or "").strip().lower()
    out: List[Dict[str, Any]] = []
    for obj in _iter_training_jsonl(p):
        try:
            if str(obj.get("task") or "").strip() != "select_phrase":
                continue
            inp = obj.get("input") or {}
            sg = str(inp.get("style_group") or "").strip().lower()
            if want_sg and sg != want_sg:
                continue
            outp = obj.get("output") or {}
            pid = outp.get("phrase_id")
            mp = outp.get("midi_path")
            ap = outp.get("audio_path")
            items = {
                "phrase_id": int(pid) if pid is not None else None,
                "midi_path": mp,
                "audio_path": ap,
                "style_group": sg or None,
                "title": None,
                "source": "egmd",
                "bars": None,
                "tempo_bpm": (outp.get("measured") or {}).get("tempo"),
            }
            out.append(items)
            if len(out) >= max(1, int(limit)):
                break
        except Exception:
            continue
    return out


def _list_db_drummers() -> List[Dict[str, Any]]:
    p = _resolve_admin_db_path_for_listing()
    if not p:
        return []
    try:
        conn = sqlite3.connect(str(p))
        cur = conn.cursor()
        drummers: Dict[str, Dict[str, Any]] = {}
        try:
            cur.execute("SELECT drummer_id, display_name FROM drummers")
            for r in cur.fetchall() or []:
                slug = str(r[0] or "").strip()
                name = str(r[1] or _title_from_slug(slug)).strip()
                if not slug:
                    continue
                drummers[slug] = {"id": slug, "display_name": name, "genre_tags": [], "style": None}
        except Exception:
            pass
        # Populate genre/style tags with best-effort schema support
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {str(r[0]) for r in cur.fetchall() or []}
        except Exception:
            tables = set()

        try:
            if "drummer_category_assignments" in tables and "categories" in tables:
                cur.execute(
                    """
                    SELECT a.drummer_id, c.label
                    FROM drummer_category_assignments a
                    LEFT JOIN categories c ON c.category_id = a.category_id
                    ORDER BY a.drummer_id
                    """
                )
                rows = cur.fetchall() or []
                for r in rows:
                    did = str(r[0] or "").strip()
                    lab = str(r[1] or "").strip()
                    if not did or did not in drummers:
                        continue
                    if lab:
                        tags = drummers[did].setdefault("genre_tags", [])
                        if lab not in tags:
                            tags.append(lab)
                        if not drummers[did].get("style"):
                            drummers[did]["style"] = lab.lower().replace(" ", "_")
            if "drummer_genres" in tables:
                cur.execute(
                    """
                    SELECT drummer_id, genre
                    FROM drummer_genres
                    ORDER BY drummer_id
                    """
                )
                rows = cur.fetchall() or []
                for r in rows:
                    did = str(r[0] or "").strip()
                    lab = str(r[1] or "").strip()
                    if not did or did not in drummers:
                        continue
                    if lab:
                        tags = drummers[did].setdefault("genre_tags", [])
                        if lab not in tags:
                            tags.append(lab)
                        if not drummers[did].get("style"):
                            drummers[did]["style"] = lab.lower().replace(" ", "_")
            if "drummer_profiles" in tables:
                # Augment styles and category from drummer_profiles
                try:
                    cur.execute(
                        """
                        SELECT drummer_id, styles, category
                        FROM drummer_profiles
                        ORDER BY drummer_id
                        """
                    )
                    rows = cur.fetchall() or []
                except Exception:
                    rows = []
                for r in rows:
                    did = str(r[0] or "").strip()
                    if not did or did not in drummers:
                        continue
                    styles_raw = str(r[1] or "").strip()
                    category_raw = str(r[2] or "").strip()
                    labels: List[str] = []
                    if styles_raw:
                        parsed = None
                        try:
                            parsed = json.loads(styles_raw)
                        except Exception:
                            parsed = None
                        if isinstance(parsed, list):
                            labels = [str(x or "").strip() for x in parsed if str(x or "").strip()]
                        elif isinstance(parsed, str) and parsed.strip():
                            tmp = styles_raw.replace("|", ",").replace(";", ",")
                            labels = [s.strip() for s in tmp.split(",") if s.strip()]
                    if labels:
                        tags = drummers[did].setdefault("genre_tags", [])
                        for lab in labels:
                            if lab and lab not in tags:
                                tags.append(lab)
                            if not drummers[did].get("style") and lab:
                                norm = "_".join([w for w in lab.lower().replace("/", " ").replace(",", " ").replace("-", " ").split() if w])
                                drummers[did]["style"] = norm or lab.lower()
                            if not drummers[did].get("profileType") and lab:
                                drummers[did]["profileType"] = lab
                    if category_raw:
                        tags = drummers[did].setdefault("genre_tags", [])
                        if category_raw not in tags:
                            tags.append(category_raw)
                        if not drummers[did].get("style"):
                            normc = "_".join([w for w in category_raw.lower().replace("/", " ").replace(",", " ").replace("-", " ").split() if w])
                            drummers[did]["style"] = normc or category_raw.lower()
                        if not drummers[did].get("profileType"):
                            drummers[did]["profileType"] = category_raw
        except Exception:
            pass

        try:
            conn.close()
        except Exception:
            pass
        # Post-process fallbacks
        try:
            for did, drow in drummers.items():
                style = drow.get("style")
                tags = drow.get("genre_tags") or []
                ptype = drow.get("profileType")
                # Prefer first tag if style is missing
                if not style and tags:
                    lab = str(tags[0] or "").strip()
                    if lab:
                        norm = "_".join([w for w in lab.lower().replace("/", " ").replace(",", " ").replace("-", " ").split() if w])
                        drow["style"] = norm or lab.lower()
                # Use profileType when style still missing
                if not drow.get("style") and ptype:
                    lab = str(ptype or "").strip()
                    if lab:
                        norm = "_".join([w for w in lab.lower().replace("/", " ").replace(",", " ").replace("-", " ").split() if w])
                        drow["style"] = norm or lab.lower()
                # Final default
                if not drow.get("style"):
                    drow["style"] = "rock"
        except Exception:
            pass
        return list(drummers.values())
    except Exception:
        return []


def _list_styles(include_admin: bool = False) -> List[str]:
    p = _resolve_admin_db_path_for_listing()
    out: List[str] = []
    if not p:
        return out
    try:
        conn = sqlite3.connect(str(p))
        cur = conn.cursor()
        tables: Set[str] = set()
        try:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {str(r[0]) for r in cur.fetchall() or []}
        except Exception:
            tables = set()
        # Allow env override for admin styles when flag not passed
        try:
            if not include_admin:
                inc = str(os.getenv("INCLUDE_ADMIN_STYLES", "")).strip().lower()
                include_admin = inc not in {"", "0", "false", "no"}
        except Exception:
            include_admin = include_admin

        # Preferred: categories table with human-readable labels
        if "categories" in tables:
            try:
                cur.execute("SELECT label FROM categories WHERE TRIM(label) <> '' ORDER BY lower(label)")
                for r in cur.fetchall() or []:
                    lab = str(r[0] or "").strip()
                    if lab:
                        out.append(lab)
            except Exception:
                pass

        # Add display names from drummer_category_mappings when present
        if "drummer_category_mappings" in tables:
            admin_flag_exists = False
            try:
                cur.execute("PRAGMA table_info(drummer_category_mappings)")
                cols = [str(c[1]).strip().lower() for c in cur.fetchall() or []]
                admin_flag_exists = "is_admin_only" in cols
            except Exception:
                admin_flag_exists = False
            try:
                if admin_flag_exists and not include_admin:
                    cur.execute("SELECT DISTINCT display_name FROM drummer_category_mappings WHERE TRIM(display_name) <> '' AND COALESCE(is_admin_only,0)=0 ORDER BY lower(display_name)")
                else:
                    cur.execute("SELECT DISTINCT display_name FROM drummer_category_mappings WHERE TRIM(display_name) <> '' ORDER BY lower(display_name)")
                for r in cur.fetchall() or []:
                    lab = str(r[0] or "").strip()
                    if lab:
                        out.append(lab)
            except Exception:
                pass
            # Fallback to category_id tokens if display_name is not populated
            try:
                if admin_flag_exists and not include_admin:
                    cur.execute("SELECT DISTINCT category_id FROM drummer_category_mappings WHERE TRIM(category_id) <> '' AND COALESCE(is_admin_only,0)=0 ORDER BY lower(category_id)")
                else:
                    cur.execute("SELECT DISTINCT category_id FROM drummer_category_mappings WHERE TRIM(category_id) <> '' ORDER BY lower(category_id)")
                for r in cur.fetchall() or []:
                    lab = str(r[0] or "").strip()
                    if lab:
                        out.append(lab)
            except Exception:
                pass

        # Augment with drummer_profiles (styles list and category)
        if "drummer_profiles" in tables:
            try:
                cur.execute("SELECT styles, category FROM drummer_profiles")
                rows = cur.fetchall() or []
            except Exception:
                rows = []
            for sraw, cat in rows:
                styles_raw = str(sraw or "").strip()
                if styles_raw:
                    parsed = None
                    try:
                        parsed = json.loads(styles_raw)
                    except Exception:
                        parsed = None
                    if isinstance(parsed, list):
                        for x in parsed:
                            lab = str(x or "").strip()
                            if lab:
                                out.append(lab)
                    else:
                        tmp = styles_raw.replace("|", ",").replace(";", ",")
                        for tok in [t.strip() for t in tmp.split(",") if t.strip()]:
                            out.append(tok)
                cat_s = str(cat or "").strip()
                if cat_s:
                    out.append(cat_s)

        # Add genres when table is present
        if "drummer_genres" in tables:
            try:
                cur.execute("SELECT DISTINCT genre FROM drummer_genres WHERE TRIM(genre) <> '' ORDER BY lower(genre)")
                for r in cur.fetchall() or []:
                    lab = str(r[0] or "").strip()
                    if lab:
                        out.append(lab)
            except Exception:
                pass
        try:
            conn.close()
        except Exception:
            pass
    except Exception:
        return []
    # De-duplicate while preserving order (case-insensitive)
    # Provide a minimal default set when DB yields no styles
    if not out:
        out = [
            "Rock",
            "Funk",
            "Jazz",
            "Pop",
            "Blues",
            "Shuffle",
            "Latin",
            "Metal",
        ]
    seen: Set[str] = set()
    ordered: List[str] = []
    for s in out:
        k = str(s or "").strip()
        if not k:
            continue
        low = k.lower()
        if low in seen:
            continue
        seen.add(low)
        ordered.append(k)
    return ordered

# ------------------------------------------------------------
# Version & configuration
# ------------------------------------------------------------

API_VERSION = "1.2.0"

# INFERENCE_BACKEND: "auto" | "torch" | "onnx"
INFERENCE_BACKEND = str(os.getenv("INFERENCE_BACKEND", "auto")).strip().lower()

# Caching bins (for UI dragging; tune as needed)
CACHE_ENABLED = str(os.getenv("HUMANIZE_CACHE_ENABLED", "1")).strip() not in {"0", "false", "no"}
CACHE_TEMPO_BIN = float(os.getenv("HUMANIZE_CACHE_TEMPO_BIN", "1.0"))          # bpm bin size
CACHE_COMPLEXITY_BIN = float(os.getenv("HUMANIZE_CACHE_COMPLEXITY_BIN", "0.02"))  # 0..1 bin size

# Structured logging
LOG_LEVEL = str(os.getenv("LOG_LEVEL", "INFO")).upper()
SERVICE_NAME = str(os.getenv("SERVICE_NAME", "drumtracai-humanize")).strip()

# ------------------------------------------------------------
# Logging setup (Step 4: structured logging)
# ------------------------------------------------------------

logger = logging.getLogger(SERVICE_NAME)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
logger.setLevel(LOG_LEVEL)

def log_event(event: str, **fields: Any) -> None:
    payload = {"ts": time.time(), "service": SERVICE_NAME, "event": event, **fields}
    try:
        logger.info(json.dumps(payload, separators=(",", ":"), default=str))
    except Exception:
        # Last resort: avoid crashing on logging
        logger.info(f"{event} {fields}")

# ------------------------------------------------------------
# Defaults & constants
# ------------------------------------------------------------

DEFAULT_HUMANIZATION_PARAMS: Dict[str, float] = {
    "timing_variance": 0.06,
    "timing_drift": 0.03,
    "groove_consistency": 0.75,
    "swing_factor": 0.08,
    "velocity_variance": 0.22,
    "ghost_note_frequency": 0.22,
    "velocity_humanization": 0.55,
    "hihat_variation": 0.45,
    "kick_snare_relationship": 0.55,
}

PARAM_KEYS: List[str] = [
    "timing_variance",
    "timing_drift",
    "groove_consistency",
    "swing_factor",
    "velocity_variance",
    "ghost_note_frequency",
    "velocity_humanization",
    "hihat_variation",
    "kick_snare_relationship",
]

_STYLE_TO_INT = {
    "rock": 0,
    "funk": 1,
    "jazz": 2,
    "latin": 3,
    "metal": 4,
    "pop": 5,
    "live_recording": 6,
    "shuffle": 7,
    "blues": 8,
}

def _style_to_int(style: Optional[str]) -> int:
    return int(_STYLE_TO_INT.get(str(style or "rock").strip().lower(), 0))

def _legacy_notes_to_plugin_notes_for_midi(legacy_notes: List[Dict[str, Any]], *, ppq: int, tempo_bpm: float) -> List[Dict[str, Any]]:
    try:
        ticks_per_second = (float(ppq) * float(tempo_bpm)) / 60.0
    except Exception:
        ticks_per_second = float(ppq) * 2.0
    out: List[Dict[str, Any]] = []
    for note in legacy_notes or []:
        if not isinstance(note, dict):
            continue
        try:
            t_sec = float(note.get("time", 0.0) or 0.0)
        except Exception:
            t_sec = 0.0
        try:
            length_sec = float(note.get("length") or note.get("duration") or 0.10)
        except Exception:
            length_sec = 0.10
        t0 = int(round(max(0.0, t_sec) * ticks_per_second))
        t1 = max(t0 + 1, int(round((max(0.0, t_sec) + max(0.01, length_sec)) * ticks_per_second)))
        pitch = int(note.get("note") or 38)
        vel = int(note.get("velocity") or 96)
        drum = str(note.get("drum") or "")
        out.append({
            "t0": t0,
            "t1": t1,
            "pitch": pitch,
            "vel": vel,
            "chan": 9,
            "articulationId": drum or None,
        })
    return out

def _ensure_midi_base64_on_payload(payload: Dict[str, Any], cfg: Dict[str, Any]) -> None:
    try:
        midi_b64 = None
        # Prefer plugin_render output when available
        if isinstance(payload.get("plugin_render"), dict):
            pr = payload["plugin_render"]
            midi_b64 = pr.get("midi_base64") or pr.get("midi_smf_base64") or pr.get("midi_b64")
        # Fallback: synthesize minimal SMF from legacy midi_notes
        if not midi_b64 and callable(render_articulated_notes_to_midi):
            tempos = cfg.get("tempos") or []
            if isinstance(tempos, list) and tempos:
                try:
                    tempo_bpm = float(sum(float(t or 0.0) for t in tempos) / max(len(tempos), 1))
                except Exception:
                    tempo_bpm = 120.0
            else:
                try:
                    tempo_bpm = float(cfg.get("tempo") or 120.0)
                except Exception:
                    tempo_bpm = 120.0
            ppq = int((payload.get("drum_track") or {}).get("resolution_ppq") or 960)
            notes = _legacy_notes_to_plugin_notes_for_midi(payload.get("midi_notes") or [], ppq=ppq, tempo_bpm=tempo_bpm)
            out = render_articulated_notes_to_midi({
                "plugin": str(cfg.get("pluginTarget") or cfg.get("plugin") or "jamstix"),
                "advancedArticulations": bool(cfg.get("advancedArticulations", False)),
                "ppq": ppq,
                "tempo_bpm": tempo_bpm,
                "notes": notes,
            }) if notes else None
            if isinstance(out, dict):
                midi_b64 = out.get("midi_base64") or out.get("midi_smf_base64") or out.get("midi_b64")
        if midi_b64:
            payload["midi_base64"] = midi_b64
    except Exception:
        # Non-fatal: leave payload as-is
        pass

# ------------------------------------------------------------
# Pydantic schemas
# ------------------------------------------------------------

class HumanizationRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    tempo_bpm: float = Field(default=120.0, ge=30.0, le=320.0)
    style: Optional[str] = Field(default="rock")
    pattern_complexity: float = Field(default=0.7, ge=0.0, le=1.0)

class HumanizationResponse(BaseModel):
    ok: bool
    params: Dict[str, float]
    metadata: Optional[Dict[str, Any]] = None

class HumanizationBatchRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: List[HumanizationRequest] = Field(default_factory=list, min_length=1, max_length=512)

class HumanizationBatchResponse(BaseModel):
    ok: bool
    items: List[HumanizationResponse]
    metadata: Optional[Dict[str, Any]] = None

class PerformanceSpecRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cfg: Dict[str, Any] = Field(default_factory=dict)
    songmap_summary: Dict[str, Any] = Field(default_factory=dict)
    drummer_profile: Dict[str, Any] = Field(default_factory=dict)

class PerformanceSpecResponse(BaseModel):
    ok: bool
    spec: Dict[str, Any]
    metadata: Dict[str, Any]

class SongRoadmapRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cfg: Dict[str, Any] = Field(default_factory=dict)
    songmap_summary: Dict[str, Any] = Field(default_factory=dict)
    drummer_profile: Dict[str, Any] = Field(default_factory=dict)

class SongRoadmapResponse(BaseModel):
    ok: bool
    roadmap: Dict[str, Any]
    metadata: Dict[str, Any]

class SentientTakeRenderResponse(BaseModel):
    ok: bool
    spec: Dict[str, Any]
    drum_track: Optional[Dict[str, Any]] = None
    midi_notes: List[Dict[str, Any]] = Field(default_factory=list)
    plugin_render: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any]

# ------------------------------------------------------------
# Model definition (Torch)
# ------------------------------------------------------------
if TORCH_AVAILABLE and nn is not None:
    class DrumHumanizationModel(nn.Module):
        def __init__(self, input_size: int = 3, hidden_size: int = 64, output_size: int = 9):
            super().__init__()
            self.encoder = nn.Sequential(
                nn.Linear(input_size, hidden_size),
                nn.ReLU(),
                nn.Dropout(0.2),
                nn.Linear(hidden_size, hidden_size * 2),
                nn.ReLU(),
                nn.Dropout(0.2),
            )
            self.predictor = nn.Sequential(
                nn.Linear(hidden_size * 2, hidden_size),
                nn.ReLU(),
                nn.Linear(hidden_size, output_size),
                nn.Sigmoid(),
            )

        def forward(self, x):
            return self.predictor(self.encoder(x))
else:
    DrumHumanizationModel = None  # type: ignore

# ------------------------------------------------------------
# Model path resolution & loading
# ------------------------------------------------------------

def _resolve_active_model_path() -> Path:
    explicit = os.getenv("ACTIVE_MODEL_PATH")
    if explicit:
        return Path(explicit)

    active_json = os.getenv("ACTIVE_MODEL_JSON", "/models/production/active_model.json")
    p = Path(active_json)
    if p.exists():
        data = json.loads(p.read_text(encoding="utf-8"))

        candidates = data.get("candidates")
        if isinstance(candidates, list) and candidates:
            for entry in candidates:
                if not isinstance(entry, str):
                    continue
                entry = entry.strip()
                if not entry:
                    continue
                resolved = Path(entry)
                if not resolved.is_absolute():
                    resolved = (p.parent / resolved).resolve()
                if resolved.exists():
                    return resolved

        model_path = data.get("path")
        if isinstance(model_path, str) and model_path.strip():
            resolved = Path(model_path.strip())
            if not resolved.is_absolute():
                resolved = (p.parent / resolved).resolve()
            return resolved

    return Path("/models/checkpoints/best_model.pth")

def _resolve_onnx_model_path(checkpoint_path: Optional[Path]) -> Optional[Path]:
    explicit = os.getenv("ONNX_MODEL_PATH")
    if explicit:
        p = Path(explicit)
        return p if p.exists() else p

    if checkpoint_path is None:
        return None

    # If checkpoint is foo.pth, try foo.onnx in same directory
    try:
        candidate = checkpoint_path.with_suffix(".onnx")
        if candidate.exists():
            return candidate
    except Exception:
        pass

    # Fallback default
    p = Path("/models/checkpoints/best_model.onnx")
    return p if p.exists() else p

def _load_torch_model(model_path: Path) -> Tuple["DrumHumanizationModel", str]:
    if not TORCH_AVAILABLE or torch is None or DrumHumanizationModel is None:
        raise RuntimeError("Torch not available")
    if not model_path.exists():
        raise FileNotFoundError(str(model_path))

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = DrumHumanizationModel(input_size=3, output_size=len(PARAM_KEYS)).to(device)
    checkpoint = torch.load(model_path, map_location=device)

    state = checkpoint.get("model_state_dict") if isinstance(checkpoint, dict) else None
    if state is None and isinstance(checkpoint, dict):
        state = checkpoint.get("state_dict")
    if state is None and isinstance(checkpoint, dict) and all(isinstance(k, str) for k in checkpoint.keys()):
        state = checkpoint

    if not isinstance(state, dict):
        raise RuntimeError("Unsupported checkpoint format")

    model.load_state_dict(state, strict=False)
    model.eval()
    return model, device

def _load_onnx_session(onnx_path: Path):
    if not ONNX_AVAILABLE or ort is None:
        raise RuntimeError("onnxruntime not available")
    # Providers: prefer CUDA when available, else CPU
    providers = None
    try:
        providers = ort.get_available_providers()
    except Exception:
        providers = None

    # Choose provider order conservatively
    preferred = []
    if providers and "CUDAExecutionProvider" in providers:
        preferred.append("CUDAExecutionProvider")
    preferred.append("CPUExecutionProvider")

    sess = ort.InferenceSession(onnx_path.as_posix(), providers=preferred)
    return sess

def _export_onnx_from_torch(model: "DrumHumanizationModel", device: str, onnx_path: Path) -> None:
    if not TORCH_AVAILABLE or torch is None:
        raise RuntimeError("Torch not available for ONNX export")
    if not ONNX_EXPORT_AVAILABLE:
        raise RuntimeError("onnx package not available for export")

    onnx_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = onnx_path.with_suffix(onnx_path.suffix + ".tmp")

    # Export on CPU for portability/stability; keep inference model on its original device.
    orig_device = "cpu"
    try:
        try:
            orig_device = str(next(model.parameters()).device)
        except Exception:
            orig_device = "cpu"

        model_cpu = model.to("cpu")
        model_cpu.eval()
        dummy = torch.zeros((1, 3), dtype=torch.float32, device="cpu")

        torch.onnx.export(
            model_cpu,
            dummy,
            tmp_path.as_posix(),
            input_names=["x"],
            output_names=["y"],
            opset_version=17,
            do_constant_folding=True,
            dynamic_axes={"x": {0: "batch"}, "y": {0: "batch"}},
        )

        if not tmp_path.exists():
            raise RuntimeError(f"ONNX export did not produce file: {tmp_path}")

        tmp_path.replace(onnx_path)
        if not onnx_path.exists():
            raise RuntimeError(f"ONNX export failed to finalize file: {onnx_path}")
    finally:
        try:
            if orig_device and orig_device != "cpu":
                model.to(orig_device)
            elif device and device != "cpu":
                model.to(device)
        except Exception:
            pass

# ------------------------------------------------------------
# Input normalization
# ------------------------------------------------------------

def _normalize_inputs(tempo_bpm: float, style_int: float, complexity: float) -> np.ndarray:
    tempo = float(np.clip(tempo_bpm, 30.0, 320.0))
    tempo_n = (np.log(tempo) - np.log(30.0)) / (np.log(320.0) - np.log(30.0))
    tempo_n = float(np.clip(tempo_n, 0.0, 1.0))

    style_max = max(_STYLE_TO_INT.values()) if _STYLE_TO_INT else 1
    style_n = float(np.clip(float(style_int) / float(max(style_max, 1)), 0.0, 1.0))

    comp_n = float(np.clip(complexity, 0.0, 1.0))
    return np.array([[tempo_n, style_n, comp_n]], dtype=np.float32)

def _normalize_batch(reqs: List[HumanizationRequest]) -> np.ndarray:
    xs = []
    for r in reqs:
        xs.append(_normalize_inputs(r.tempo_bpm, float(_style_to_int(r.style)), r.pattern_complexity)[0])
    return np.stack(xs, axis=0).astype(np.float32)

# ------------------------------------------------------------
# Cache (Step 3: caching)
# ------------------------------------------------------------

def _bin(v: float, bin_size: float) -> float:
    if bin_size <= 0:
        return float(v)
    return float(round(v / bin_size) * bin_size)

def _cache_key(req: HumanizationRequest) -> Tuple[float, int, float]:
    return (_bin(float(req.tempo_bpm), CACHE_TEMPO_BIN), int(_style_to_int(req.style)), _bin(float(req.pattern_complexity), CACHE_COMPLEXITY_BIN))

@lru_cache(maxsize=8192)
def _cached_infer(tempo_bpm: float, style_int: int, complexity: float) -> Dict[str, float]:
    # This function will be called only when CACHE_ENABLED is true.
    # It routes to backend inference for a single item.
    req = HumanizationRequest(tempo_bpm=tempo_bpm, style=str(style_int), pattern_complexity=complexity)  # style stored as int string here
    # We'll bypass style mapping and pass style_int directly by injecting via helper below.
    return _infer_single_with_styleint(tempo_bpm, style_int, complexity)

# ------------------------------------------------------------
# FastAPI app / global state
# ------------------------------------------------------------

app = FastAPI(title="DrumTracKAI Humanization Service", version=API_VERSION)

# CORS for internet frontend access (Netlify/Vercel)
_cors_env = str(os.getenv("CORS_ALLOW_ORIGINS", "")).strip()
_cors_origins = [o.strip() for o in _cors_env.replace(";", ",").split(",") if o.strip()]
_allow_all = not _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all else _cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(calibration_router)
if assimilation_profiles_router is not None:
    app.include_router(assimilation_profiles_router)
app.include_router(assimilation_generation_router)

_CALIBRATION_ARTIFACT_DIR = (_repo_root() / "artifacts" / "calibration").resolve()
_CALIBRATION_ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
app.mount(
    "/static/calibration_artifacts",
    StaticFiles(directory=str(_CALIBRATION_ARTIFACT_DIR), check_dir=False),
    name="calibration_artifacts",
)

TORCH_MODEL = None
TORCH_DEVICE = "cpu"
TORCH_MODEL_PATH: Optional[Path] = None

ONNX_SESSION = None
ONNX_MODEL_PATH: Optional[Path] = None

MODEL_LOAD_ERROR: Optional[str] = None
ACTIVE_BACKEND: str = "fallback"


def _app_env() -> str:
    return str(os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development"))).strip().lower()


def _strict_model_runtime_required() -> bool:
    if str(os.getenv("LLM_STRICT_READINESS", "")).strip().lower() in {"1", "true", "yes", "on"}:
        return True
    return _app_env() in {"staging", "production", "prod", "live"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _checkpoint_status() -> Dict[str, Any]:
    active: Optional[Path] = None
    try:
        active = _resolve_active_model_path()
    except Exception:
        active = None

    onnx: Optional[Path] = None
    try:
        onnx = _resolve_onnx_model_path(active)
    except Exception:
        onnx = None

    active_exists = bool(active and active.exists())
    onnx_exists = bool(onnx and onnx.exists())
    expected_model_sha256 = str(os.getenv("GENERATION_MODEL_SHA256", "")).strip().lower() or None
    model_version = str(os.getenv("DRUMTRACKAI_MODEL_VERSION", "")).strip() or None
    actual_model_sha256: Optional[str] = None
    model_sha256_verified: Optional[bool] = None
    if onnx_exists and onnx is not None:
        try:
            actual_model_sha256 = _sha256_file(onnx)
            if expected_model_sha256:
                model_sha256_verified = hmac.compare_digest(actual_model_sha256, expected_model_sha256)
        except Exception:
            actual_model_sha256 = None
            model_sha256_verified = False if expected_model_sha256 else None

    return {
        "active_model_path": str(active) if active else None,
        "active_model_exists": active_exists,
        "active_model_size": int(active.stat().st_size) if active_exists and active is not None else None,
        "onnx_model_path": str(onnx) if onnx else None,
        "onnx_model_exists": onnx_exists,
        "onnx_model_size": int(onnx.stat().st_size) if onnx_exists and onnx is not None else None,
        "expected_model_sha256": expected_model_sha256,
        "actual_model_sha256": actual_model_sha256,
        "model_sha256_verified": model_sha256_verified,
        "model_version": model_version,
    }


def _backend_ready() -> bool:
    if ACTIVE_BACKEND == "torch":
        return TORCH_MODEL is not None
    if ACTIVE_BACKEND == "onnx":
        return ONNX_SESSION is not None
    return False


def _readiness_snapshot() -> Dict[str, Any]:
    strict_required = _strict_model_runtime_required()
    checkpoint = _checkpoint_status()

    backend_ready = _backend_ready()
    reason = None

    if strict_required:
        backend_ready = True
        if ACTIVE_BACKEND != "onnx":
            backend_ready = False
            reason = "Strict readiness requires ACTIVE_BACKEND=onnx"
        elif ONNX_SESSION is None:
            backend_ready = False
            reason = "ONNX session is not loaded"
        elif not bool(checkpoint.get("onnx_model_exists")):
            backend_ready = False
            reason = "ONNX model path does not exist"
        else:
            expected_sha = checkpoint.get("expected_model_sha256")
            if expected_sha and not bool(checkpoint.get("model_sha256_verified")):
                backend_ready = False
                reason = "ONNX model SHA-256 does not match GENERATION_MODEL_SHA256"

        if backend_ready and MODEL_LOAD_ERROR:
            err = str(MODEL_LOAD_ERROR)
            if not err.startswith("Torch load failed"):
                backend_ready = False
                reason = MODEL_LOAD_ERROR

        ready = bool(backend_ready)
        if not ready and reason is None:
            reason = MODEL_LOAD_ERROR or "No canonical inference backend is active"
    else:
        ready = True

    return {
        "ready": bool(ready),
        "strict_required": strict_required,
        "app_env": _app_env(),
        "backend": ACTIVE_BACKEND,
        "backend_ready": backend_ready,
        "model_version": checkpoint.get("model_version"),
        "model_sha256_verified": checkpoint.get("model_sha256_verified"),
        "model_load_error": MODEL_LOAD_ERROR,
        "checkpoint": checkpoint,
        "reason": reason,
    }


def _enforce_ready_for_inference() -> None:
    snapshot = _readiness_snapshot()
    if snapshot["strict_required"] and not snapshot["backend_ready"]:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "Generation backend not ready",
                "backend": snapshot["backend"],
                "reason": snapshot["reason"],
                "checkpoint": snapshot["checkpoint"],
            },
        )

# ------------------------------------------------------------
# Backend selection & load (Step 2: ONNX optional inference)
# ------------------------------------------------------------

def _select_backend() -> str:
    if INFERENCE_BACKEND in {"torch", "onnx"}:
        return INFERENCE_BACKEND
    # auto
    if ONNX_AVAILABLE:
        return "onnx"
    if TORCH_AVAILABLE:
        return "torch"
    return "fallback"

def _startup_load():
    global TORCH_MODEL, TORCH_DEVICE, TORCH_MODEL_PATH, ONNX_SESSION, ONNX_MODEL_PATH, MODEL_LOAD_ERROR, ACTIVE_BACKEND

    backend = _select_backend()

    # Try ONNX if selected
    checkpoint_path = None
    try:
        checkpoint_path = _resolve_active_model_path()
    except Exception:
        checkpoint_path = None

    if backend == "onnx":
        try:
            ONNX_MODEL_PATH = _resolve_onnx_model_path(checkpoint_path)
            if ONNX_MODEL_PATH is None:
                raise RuntimeError("ONNX path not resolved")
            if not ONNX_MODEL_PATH.exists() and TORCH_AVAILABLE and torch is not None:
                # Generate ONNX from torch checkpoint when missing.
                if checkpoint_path is None:
                    checkpoint_path = _resolve_active_model_path()
                tmp_model, tmp_device = _load_torch_model(checkpoint_path)
                _export_onnx_from_torch(tmp_model, tmp_device, ONNX_MODEL_PATH)
            if not ONNX_MODEL_PATH.exists():
                raise FileNotFoundError(str(ONNX_MODEL_PATH))
            ONNX_SESSION = _load_onnx_session(ONNX_MODEL_PATH)
            ACTIVE_BACKEND = "onnx"
            log_event("startup", backend="onnx", onnx_model_path=str(ONNX_MODEL_PATH))
            return
        except Exception as e:
            MODEL_LOAD_ERROR = f"ONNX load failed: {e}"
            ONNX_SESSION = None
            log_event("startup", backend="onnx_failed", error=MODEL_LOAD_ERROR)
            # fall through to torch if auto; if explicitly onnx, remain fallback
            if INFERENCE_BACKEND == "onnx":
                ACTIVE_BACKEND = "fallback"
                log_event("startup", backend="fallback", error=MODEL_LOAD_ERROR)
                return
            backend = "torch"

    if backend == "torch":
        try:
            if checkpoint_path is None:
                checkpoint_path = _resolve_active_model_path()
            TORCH_MODEL_PATH = checkpoint_path
            TORCH_MODEL, TORCH_DEVICE = _load_torch_model(TORCH_MODEL_PATH)
            ACTIVE_BACKEND = "torch"

            # Best-effort: export/load ONNX sidecar so ONNX is available too.
            try:
                ONNX_MODEL_PATH = _resolve_onnx_model_path(TORCH_MODEL_PATH)
                if ONNX_MODEL_PATH is not None and not ONNX_MODEL_PATH.exists():
                    _export_onnx_from_torch(TORCH_MODEL, TORCH_DEVICE, ONNX_MODEL_PATH)
                if ONNX_MODEL_PATH is not None and ONNX_MODEL_PATH.exists():
                    ONNX_SESSION = _load_onnx_session(ONNX_MODEL_PATH)
            except Exception as e:
                MODEL_LOAD_ERROR = f"ONNX load failed: {e}"

            log_event("startup", backend="torch", device=TORCH_DEVICE, checkpoint=str(TORCH_MODEL_PATH))
            return
        except Exception as e:
            MODEL_LOAD_ERROR = f"Torch load failed: {e}"
            TORCH_MODEL = None
            TORCH_DEVICE = "cpu"
            if INFERENCE_BACKEND == "torch":
                ACTIVE_BACKEND = "fallback"
                log_event("startup", backend="fallback", error=MODEL_LOAD_ERROR)
                return
            ACTIVE_BACKEND = "fallback"
            log_event("startup", backend="fallback", error=MODEL_LOAD_ERROR)
            return

    ACTIVE_BACKEND = "fallback"
    log_event("startup", backend="fallback")

_startup_load()

# ------------------------------------------------------------
# Middleware: request latency logs (structured)
# ------------------------------------------------------------

@app.middleware("http")
async def latency_middleware(request: Request, call_next):
    t0 = time.perf_counter()
    try:
        resp = await call_next(request)
        return resp
    finally:
        dt_ms = (time.perf_counter() - t0) * 1000.0
        log_event(
            "request",
            method=request.method,
            path=request.url.path,
            status=getattr(locals().get("resp", None), "status_code", None),
            latency_ms=round(dt_ms, 3),
            backend=ACTIVE_BACKEND,
        )

# ------------------------------------------------------------
# Inference utilities
# ------------------------------------------------------------

def _fallback_response(metadata: Optional[Dict[str, Any]] = None) -> HumanizationResponse:
    return HumanizationResponse(ok=True, params=dict(DEFAULT_HUMANIZATION_PARAMS), metadata=metadata)

def _infer_single_with_styleint(tempo_bpm: float, style_int: int, complexity: float) -> Dict[str, float]:
    """
    Low-level inference for cache (style passed as int already).
    Returns params dict only.
    """
    if ACTIVE_BACKEND == "torch" and TORCH_MODEL is not None and TORCH_AVAILABLE and torch is not None:
        x = _normalize_inputs(tempo_bpm, float(style_int), complexity)
        with torch.no_grad():
            xt = torch.from_numpy(x).to(TORCH_DEVICE)
            y = TORCH_MODEL(xt).detach().cpu().numpy()[0]
        return {k: float(y[i]) for i, k in enumerate(PARAM_KEYS)}

    if ACTIVE_BACKEND == "onnx" and ONNX_SESSION is not None and ONNX_AVAILABLE and ort is not None:
        x = _normalize_inputs(tempo_bpm, float(style_int), complexity)
        y = ONNX_SESSION.run(None, {"x": x.astype(np.float32)})[0][0]
        return {k: float(y[i]) for i, k in enumerate(PARAM_KEYS)}

    return dict(DEFAULT_HUMANIZATION_PARAMS)

def _infer_many(reqs: List[HumanizationRequest]) -> List[HumanizationResponse]:
    """
    Batch inference (Step 1: batch endpoint).
    Uses caching per-item when enabled, and uses backend batch execution when available.
    """
    if not reqs:
        return []

    _enforce_ready_for_inference()

    # If caching is enabled, resolve from cache first, and collect misses
    outputs: List[Optional[HumanizationResponse]] = [None] * len(reqs)
    misses_idx: List[int] = []
    misses: List[HumanizationRequest] = []

    if CACHE_ENABLED:
        for i, r in enumerate(reqs):
            tempo_b = _bin(float(r.tempo_bpm), CACHE_TEMPO_BIN)
            style_i = int(_style_to_int(r.style))
            comp_b = _bin(float(r.pattern_complexity), CACHE_COMPLEXITY_BIN)
            try:
                params = _cached_infer(tempo_b, style_i, comp_b)
                outputs[i] = HumanizationResponse(ok=True, params=dict(params), metadata={"backend": ACTIVE_BACKEND, "cached": True})
            except Exception:
                misses_idx.append(i)
                misses.append(r)
    else:
        misses_idx = list(range(len(reqs)))
        misses = reqs

    # If no misses, return all cached
    if not misses:
        return [o if o is not None else _fallback_response(metadata={"backend": ACTIVE_BACKEND}) for o in outputs]

    # Backend batch inference for misses
    start = time.perf_counter()
    if ACTIVE_BACKEND == "torch" and TORCH_MODEL is not None and TORCH_AVAILABLE and torch is not None:
        x = _normalize_batch(misses)
        with torch.no_grad():
            xt = torch.from_numpy(x).to(TORCH_DEVICE)
            yb = TORCH_MODEL(xt).detach().cpu().numpy()  # (B, 9)
        for j, outrow in enumerate(yb):
            params = {k: float(outrow[i]) for i, k in enumerate(PARAM_KEYS)}
            idx = misses_idx[j]
            outputs[idx] = HumanizationResponse(ok=True, params=params, metadata={"backend": "torch", "cached": False})
    elif ACTIVE_BACKEND == "onnx" and ONNX_SESSION is not None and ONNX_AVAILABLE and ort is not None:
        x = _normalize_batch(misses).astype(np.float32)
        yb = ONNX_SESSION.run(None, {"x": x})[0]  # (B, 9)
        for j, outrow in enumerate(yb):
            params = {k: float(outrow[i]) for i, k in enumerate(PARAM_KEYS)}
            idx = misses_idx[j]
            outputs[idx] = HumanizationResponse(ok=True, params=params, metadata={"backend": "onnx", "cached": False})
    else:
        for idx in misses_idx:
            outputs[idx] = _fallback_response(metadata={"backend": "fallback", "cached": False})

    dt_ms = (time.perf_counter() - start) * 1000.0
    log_event("humanize_batch", n=len(reqs), misses=len(misses), latency_ms=round(dt_ms, 3), backend=ACTIVE_BACKEND, cache=CACHE_ENABLED)

    return [o if o is not None else _fallback_response(metadata={"backend": ACTIVE_BACKEND}) for o in outputs]

def _infer_one(req: HumanizationRequest) -> HumanizationResponse:
    _enforce_ready_for_inference()

    if CACHE_ENABLED:
        tempo_b, style_i, comp_b = _cache_key(req)
        try:
            params = _cached_infer(tempo_b, style_i, comp_b)
            return HumanizationResponse(
                ok=True,
                params=dict(params),
                metadata={
                    "backend": ACTIVE_BACKEND,
                    "cached": True,
                },
            )
        except HTTPException:
            raise
        except Exception:
            pass
    # No cache or cache miss => use batch path for uniformity
    return _infer_many([req])[0]

# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok", "version": API_VERSION}


_DRY_PROBE_JOBS: Dict[str, Dict[str, Any]] = {}
_DRY_PROBE_JOB_TTL_SECONDS = 10 * 60


def _normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "none" else text


def _normalize_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default

    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("integer value cannot be empty")
        if text.lower() == "none":
            return default
        value = text

    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid integer value: {value!r}") from exc


@lru_cache(maxsize=1)
def _central_db_service() -> Any:
    if CentralDatabaseService is None:
        raise RuntimeError("CentralDatabaseService is unavailable; dry-probe render cannot run")

    service = (
        CentralDatabaseService.get_instance()  # type: ignore[attr-defined]
        if hasattr(CentralDatabaseService, "get_instance")
        else CentralDatabaseService()
    )

    if hasattr(service, "initialize"):
        db_path = os.getenv("DRY_PROBE_DB_PATH")
        try:
            ok = service.initialize(db_path=db_path) if db_path else service.initialize()
        except Exception as exc:
            logger.exception("dry_probe: database initialization failed: %s", exc)
            raise RuntimeError(f"Failed to initialize central database service: {exc}") from exc
        else:
            if not ok:
                raise RuntimeError("Failed to initialize central database service")

    return service


@lru_cache(maxsize=1)
def _calibration_renderer() -> Any:
    if CalibrationRenderService is None or RenderRequest is None:
        raise RuntimeError("CalibrationRenderService is unavailable; dry-probe render cannot run")

    artifact_root_env = os.getenv("DRY_PROBE_ARTIFACT_ROOT")
    base_root = Path(artifact_root_env).expanduser() if artifact_root_env else _CALIBRATION_ARTIFACT_DIR
    base_root = Path(base_root).resolve()
    base_root.mkdir(parents=True, exist_ok=True)
    return CalibrationRenderService(_central_db_service(), artifact_root=base_root)


def _prune_dry_probe_jobs(now: float) -> None:
    expired = [job_id for job_id, job in _DRY_PROBE_JOBS.items() if now - job.get("created_at", now) > _DRY_PROBE_JOB_TTL_SECONDS]
    for job_id in expired:
        try:
            _DRY_PROBE_JOBS.pop(job_id, None)
        except Exception:
            continue


def _execute_dry_probe(payload: Dict[str, Any]) -> Dict[str, Any]:
    if generate_candidate_run is None:
        raise RuntimeError("Candidate generator unavailable; dry-probe render cannot run")

    now = time.time()

    def _prepare_dry_probe_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        drummer = _normalize_text(payload.get("drummer")).lower()
        base_groove = _normalize_text(payload.get("base_groove"))
        target_drummer = _normalize_text(payload.get("target_drummer")).lower() or drummer

        if not base_groove:
            raise HTTPException(status_code=400, detail="base_groove must be provided")
        if not drummer:
            raise HTTPException(status_code=400, detail="drummer must be provided")

        try:
            seed = _normalize_int(payload.get("seed"), default=0)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="seed must be an integer") from exc

        try:
            repeats = _normalize_int(payload.get("repeats"), default=1)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="repeats must be an integer") from exc

        sample_pack_version = _normalize_text(payload.get("sample_pack_version")) or None

        return {
            "drummer": drummer,
            "base_groove": base_groove,
            "target_drummer": target_drummer,
            "seed": seed,
            "repeats": repeats,
            "sample_pack_version": sample_pack_version,
        }

    payload = _prepare_dry_probe_payload(payload)

    base_groove_id = payload["base_groove"]
    drummer_slug = payload["target_drummer"]
    seed = payload["seed"]
    repeats = payload["repeats"]
    sample_pack_version = payload.get("sample_pack_version")

    db = _central_db_service()

    try:
        candidate = generate_candidate_run(
            db=db,
            base_groove_id=base_groove_id,
            target_drummer_slug=drummer_slug,
            drummer_slug=drummer_slug,
            seed=seed,
            repeats=repeats,
        )
    except FileNotFoundError as exc:
        logger.warning("dry_probe: base groove missing: %s", exc)
        raise HTTPException(status_code=400, detail=f"base_groove_not_found: {exc}") from exc
    except ValueError as exc:
        logger.warning("dry_probe: invalid request for drummer '%s': %s", drummer_slug, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("dry_probe: candidate generation failed for %s", drummer_slug)
        raise HTTPException(status_code=500, detail=f"candidate_generation_failed: {exc}") from exc

    run_id = f"dry_probe_{uuid.uuid4().hex[:12]}"

    started_at = datetime.utcnow()
    run_metadata = dict(candidate.metadata or {})
    run_metadata.update(
        {
            "source": "dry_probe",
            "sample_pack_version": sample_pack_version,
            "base_groove_id": base_groove_id,
            "seed": seed,
            "repeats": repeats,
        }
    )
    run_metrics = {
        "tempo_bpm": candidate.tempo_bpm,
        "bars": candidate.bars,
        "note_count": candidate.note_count,
    }

    try:
        logged_run_id = db.log_calibration_run(
            drummer_slug=drummer_slug,
            outcome="dry_probe_preview",
            started_at=started_at,
            completed_at=started_at,
            note_count=candidate.note_count,
            metadata=run_metadata,
            metrics=run_metrics,
            run_id=run_id,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("dry_probe: failed to log calibration run for %s", run_id)
        raise HTTPException(status_code=500, detail=f"calibration_run_log_failed: {exc}") from exc

    if not logged_run_id:
        logger.error("dry_probe: calibration run logging returned no run_id for %s", run_id)
        raise HTTPException(status_code=500, detail="calibration_run_log_failed")

    try:
        db.upsert_calibration_run_events(
            run_id=run_id,
            drummer_slug=drummer_slug,
            event_stream=candidate.event_stream,
            tempo_bpm=candidate.tempo_bpm,
            time_signature=candidate.time_signature,
            bars=candidate.bars,
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("dry_probe: failed to persist run events for %s", run_id)
        raise HTTPException(status_code=500, detail=f"persist_failed: {exc}") from exc

    renderer = _calibration_renderer()

    render_recipe = {
        "source": "dry_probe",
        "drummer": drummer_slug,
        "base_groove_id": base_groove_id,
        "seed": seed,
        "repeats": repeats,
        "sample_pack_version": sample_pack_version,
        "metadata": candidate.metadata,
    }
    if candidate.performance_spec:
        render_recipe["performance_spec"] = candidate.performance_spec

    try:
        result = renderer.render_run(
            RenderRequest(
                run_id=run_id,
                render_profile_id="dry_probe_preview_v1",
                sample_pack_version=str(sample_pack_version or "default"),
                kit_id=str(candidate.kit_id or "default_kit"),
                seed=seed,
                render_recipe=render_recipe,
            )
        )
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("dry_probe: render failed for run %s", run_id)
        raise HTTPException(status_code=500, detail=f"render_failed: {exc}") from exc

    artifacts: List[Dict[str, Any]] = list(result.get("artifacts") or [])
    preview_url = next((art.get("url") for art in artifacts if art.get("url")), None)
    download_url = preview_url or next((art.get("storage_uri") for art in artifacts if art.get("storage_uri")), None)

    job = {
        "ok": True,
        "job_id": result.get("job_id"),
        "state": "completed",
        "created_at": now,
        "payload": {
            "drummer": drummer_slug,
            "base_groove": base_groove_id,
            "seed": seed,
            "repeats": repeats,
            "sample_pack_version": sample_pack_version,
        },
        "run_id": run_id,
        "preview_url": preview_url,
        "download_url": download_url,
        "artifacts": artifacts,
        "metadata": {
            "note_count": candidate.note_count,
            "tempo_bpm": candidate.tempo_bpm,
            "time_signature": candidate.time_signature,
            "bars": candidate.bars,
            "kit_id": candidate.kit_id,
            "sections": candidate.sections,
            "rollup_available": bool(candidate.rollup),
            "performance_spec": candidate.performance_spec,
        },
    }

    job_id = str(job.get("job_id") or f"dry-probe-{uuid.uuid4().hex[:12]}")
    job["job_id"] = job_id
    _DRY_PROBE_JOBS[job_id] = job
    return job


@app.post("/api/render/dry-probe")
def create_dry_probe_job(req: Dict[str, Any]):
    payload = {
        "drummer": req.get("drummer"),
        "base_groove": req.get("base_groove"),
        "seed": req.get("seed", 0),
        "repeats": req.get("repeats", 1),
        "sample_pack_version": req.get("sample_pack_version"),
    }
    return _execute_dry_probe(payload)


@app.get("/api/render/dry-probe/{job_id}")
def get_dry_probe_job(job_id: str):
    job = _DRY_PROBE_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/api/sentient-profiles/{drummer_id}")
def api_get_sentient_profile(drummer_id: str):
    if callable(build_sentient_profile_response):
        return build_sentient_profile_response(drummer_id)
    return {"ok": True, "drummer_id": drummer_id, "profile": None, "found": False}

@app.post("/v1/humanization_params", response_model=HumanizationResponse)
def humanization_params(req: HumanizationRequest):
    try:
        return _infer_one(req)
    except Exception as e:
        return _fallback_response(metadata={"reason": "inference_error", "error": str(e), "backend": ACTIVE_BACKEND})

@app.post("/v1/humanization_params_batch", response_model=HumanizationBatchResponse)
def humanization_params_batch(req: HumanizationBatchRequest):
    try:
        items = _infer_many(req.items)
        return HumanizationBatchResponse(ok=True, items=items, metadata={"backend": ACTIVE_BACKEND, "cache_enabled": CACHE_ENABLED})
    except Exception as e:
        # Return all fallback to avoid UI failure
        items = [_fallback_response(metadata={"reason": "batch_error", "error": str(e), "backend": ACTIVE_BACKEND}) for _ in req.items]
        return HumanizationBatchResponse(ok=True, items=items, metadata={"backend": ACTIVE_BACKEND, "error": str(e)})

# ------------------------------------------------------------
# /v1/performance_spec (unchanged core logic; uses humanization endpoint)
# ------------------------------------------------------------

@app.post("/v1/performance_spec", response_model=PerformanceSpecResponse)
def performance_spec(req: PerformanceSpecRequest):
    _enforce_ready_for_inference()

    cfg = req.cfg or {}
    songmap_summary = req.songmap_summary or {}
    drummer_profile = req.drummer_profile or {}

    if (not isinstance(drummer_profile, dict) or not drummer_profile) and callable(build_sentient_profile_response):
        drummer_id = None
        try:
            drummer_id = (cfg or {}).get("drummer")
        except Exception:
            drummer_id = None
        drummer_id = str(drummer_id or "").strip()
        if drummer_id:
            try:
                resp = build_sentient_profile_response(drummer_id)
                prof = (resp or {}).get("profile")
                if isinstance(prof, dict) and prof:
                    drummer_profile = prof
            except Exception:
                pass

    groove_controls = cfg.get("grooveControls")
    if not isinstance(groove_controls, dict):
        groove_controls = {}

    brain_config = cfg.get("brainConfig")
    if not isinstance(brain_config, dict):
        brain_config = {}

    def _brain_element_value(element_id: str) -> Optional[float]:
        elements = brain_config.get("elementSettings")
        if not isinstance(elements, list):
            return None
        for entry in elements:
            if not isinstance(entry, dict):
                continue
            if entry.get("elementId") != element_id:
                continue
            if entry.get("disabled") is True:
                return None
            try:
                return float(entry.get("value"))
            except Exception:
                return None
        return None

    tempos = cfg.get("tempos") or [cfg.get("tempo", 120.0)]
    if not isinstance(tempos, list) or not tempos:
        tempos = [120.0]

    avg_tempo = float(sum(float(t) for t in tempos) / max(len(tempos), 1))
    style = cfg.get("style")

    humanize_amount = float(cfg.get("humanizeAmount", 0.5))
    swing_amount_cfg = float(cfg.get("swingAmount", 0.0))
    ghost_amount_cfg = float(cfg.get("ghostNoteAmount", 0.2))
    intensity = float(cfg.get("intensity", 0.6))
    variation = float(cfg.get("variation", 0.5))

    try:
        p3242 = drummer_profile.get("phase32_42_features") if isinstance(drummer_profile, dict) else None
        p3742 = (p3242 or {}).get("phase37_42") if isinstance(p3242, dict) else None
        if isinstance(p3742, dict):
            personality = p3742.get("drummer_personality_profile")
            micro = p3742.get("microtiming_profile")

            if isinstance(personality, dict):
                try:
                    aggressiveness = float(personality.get("aggressiveness", 0.5))
                    restraint = float(personality.get("restraint", 0.5))
                    chaos = float(personality.get("chaos", 0.3))
                    ghost_style = float(personality.get("ghostStyle", 0.3))
                    intensity = max(0.0, min(1.0, intensity * (0.85 + 0.35 * aggressiveness)))
                    variation = max(0.0, min(1.0, variation * (0.85 + 0.35 * chaos)))
                    ghost_amount_cfg = max(0.0, min(1.0, ghost_amount_cfg * (0.75 + 0.65 * ghost_style)))
                    humanize_amount = max(0.0, min(1.0, humanize_amount * (0.85 + 0.25 * (1.0 - restraint))))
                except Exception:
                    pass

            if isinstance(micro, dict):
                g = micro.get("global")
                if isinstance(g, dict) and g.get("std_ms") is not None:
                    try:
                        std_ms = float(g.get("std_ms"))
                        loosen = max(0.0, min(1.0, std_ms / 18.0))
                        humanize_amount = max(0.0, min(1.0, humanize_amount * (0.80 + 0.60 * loosen)))
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        gc_swing = float(groove_controls.get("swing", 0.0) or 0.0)
        swing_amount_cfg = max(0.0, min(1.0, swing_amount_cfg + gc_swing))
    except Exception:
        pass

    try:
        gc_ghost = float(groove_controls.get("ghost_note_density", 0.0) or 0.0)
        ghost_amount_cfg = max(0.0, min(1.0, ghost_amount_cfg + (gc_ghost - 0.3) * 0.5))
    except Exception:
        pass

    try:
        gc_humanize = float(groove_controls.get("humanize", 0.0) or 0.0)
        humanize_amount = max(0.0, min(1.0, humanize_amount + (gc_humanize - 0.5) * 0.5))
    except Exception:
        pass

    try:
        gc_fill = float(groove_controls.get("fill_density", 0.0) or 0.0)
        variation = max(0.0, min(1.0, variation + (gc_fill - 0.4) * 0.25))
    except Exception:
        pass

    try:
        gc_dyn = float(groove_controls.get("dynamic_range", 0.0) or 0.0)
        intensity = max(0.0, min(1.0, intensity + (gc_dyn - 0.5) * 0.25))
    except Exception:
        pass

    pattern_complexity = float(cfg.get("complexity", 0.7))

    params_resp = humanization_params(HumanizationRequest(
        tempo_bpm=avg_tempo,
        style=style,
        pattern_complexity=pattern_complexity,
    ))
    params = params_resp.params

    timing_tightness = drummer_profile.get("timing_tightness")
    if timing_tightness is None:
        timing_tightness = drummer_profile.get("timing_precision")
    try:
        timing_tightness = float(timing_tightness) if timing_tightness is not None else 0.8
    except Exception:
        timing_tightness = 0.8
    timing_tightness = max(0.0, min(1.0, timing_tightness))

    timing_override = _brain_element_value("timing")
    if timing_override is not None:
        timing_tightness = max(0.0, min(1.0, timing_tightness + (timing_override - 0.5) * 0.8))

    ghost_pref = drummer_profile.get("ghost_note_frequency")
    if ghost_pref is None:
        ghost_pref = drummer_profile.get("ghost_frequency")
    try:
        ghost_pref = float(ghost_pref) if ghost_pref is not None else 0.5
    except Exception:
        ghost_pref = 0.5
    ghost_pref = max(0.0, min(1.0, ghost_pref))

    preferred_feel = drummer_profile.get("preferred_feel") or drummer_profile.get("feel")
    preferred_feel = str(preferred_feel or "").strip().lower()

    signature_techniques = drummer_profile.get("signature_techniques")
    if not isinstance(signature_techniques, list):
        signature_techniques = []
    signature_text = " ".join(str(t).lower() for t in signature_techniques)

    swing_from_model = float(params.get("swing_factor", 0.0))
    swing_amount = max(0.0, min(1.0, (swing_amount_cfg + swing_from_model) * 0.5))

    timing_var = float(params.get("timing_variance", 0.0))
    timing_drift = float(params.get("timing_drift", 0.0))
    groove_consistency = float(params.get("groove_consistency", 0.8))

    max_ms = 10.0 * max(0.0, min(1.0, humanize_amount))
    var_ms = max(0.0, min(max_ms, timing_var * max_ms))
    drift_ms = max(-max_ms, min(max_ms, (timing_drift - 0.5) * 2.0 * (max_ms * 0.6)))
    tightness = max(0.0, min(1.0, groove_consistency))
    var_ms *= (0.4 + 0.6 * (1.0 - tightness))
    var_ms *= (0.4 + 0.6 * (1.0 - timing_tightness))

    laid_back = 0.0
    feel_map = {"rock": "straight", "funk": "laid_back", "jazz": "swing", "shuffle": "shuffle", "blues": "shuffle"}
    global_feel = feel_map.get(str(style or "").lower(), "straight")
    if preferred_feel in {"straight", "swing", "shuffle", "laid_back", "pushed"}:
        global_feel = preferred_feel
    if swing_amount > 0.55:
        global_feel = "swing"
    if global_feel == "laid_back":
        laid_back = 0.2
    if global_feel == "pushed":
        laid_back = -0.2

    if "laid" in signature_text or "dilla" in signature_text or "behind" in signature_text:
        laid_back = max(laid_back, 0.28)
        if global_feel == "straight":
            global_feel = "laid_back"
    if "pocket" in signature_text:
        var_ms *= 0.85
    if "tight" in signature_text or "precision" in signature_text:
        var_ms *= 0.7

    brain_mode = str(brain_config.get("mode") or "").strip().lower()
    if brain_mode == "easy":
        var_ms *= 0.85
    elif brain_mode == "pro":
        var_ms *= 1.05

    def _subdivision_offsets(scale: float) -> list:
        out = []
        for i in range(16):
            base = float(np.sin((i + 1) * 1.7)) * var_ms * scale
            if (i % 2) == 1:
                base += swing_amount * 8.0
            base += drift_ms
            out.append(float(max(-max_ms, min(max_ms, base))))
        return out

    base_velocity = int(60 + intensity * 50)
    base_velocity = max(1, min(127, base_velocity))
    model_ghost_pref = float(params.get("ghost_note_frequency", 0.15))
    ghost_density = float(max(0.0, min(1.0, ghost_amount_cfg * (0.6 * ghost_pref + 0.4 * model_ghost_pref))))

    def _profile(inst: str, timing_scale: float, vel_bias: int, accent_boost: int, ghost_reduction: float, rr: int):
        return {
            "instrumentId": inst,
            "microTiming": {"subdivisionOffsetsMs": _subdivision_offsets(timing_scale), "swingAmount": float(swing_amount), "laidBackAmount": float(laid_back)},
            "velocityProfile": {
                "base": int(max(1, min(127, base_velocity + vel_bias))),
                "accentBoost": int(max(0, min(40, accent_boost))),
                "ghostReduction": float(max(0.0, min(1.0, ghost_reduction))),
                "randomRange": int(max(0, min(20, rr))),
                "phraseShape": "swell" if variation > 0.6 else "flat",
            },
            "ghostDensity": float(ghost_density if inst.startswith("snare") else ghost_density * 0.4),
            "flamProbability": float(0.1 if humanize_amount > 0.6 and inst.startswith("snare") else 0.0),
            "dragProbability": float(0.05 if humanize_amount > 0.7 and inst.startswith("snare") else 0.0),
        }

    section_id = str(cfg.get("sectionId", "section"))
    start_measure = int(cfg.get("startMeasure", 1))
    end_measure = int(cfg.get("endMeasure", max(start_measure, start_measure)))

    sections = songmap_summary.get("sections")
    if not isinstance(sections, list) or not sections:
        sections = [{"label": section_id, "startBar": start_measure, "endBar": end_measure, "energy": 0.5}]

    section_profile_map = {}
    if callable(build_section_profile_map):
        try:
            section_profile_map = build_section_profile_map(sections, drummer_profile)
        except Exception:
            section_profile_map = {}

    phrases = []
    for idx, sec in enumerate(sections):
        if not isinstance(sec, dict):
            continue
        label = sec.get("label") or sec.get("type") or f"section_{idx}"

        bar_start = sec.get("startBar") if sec.get("startBar") is not None else sec.get("start")
        bar_end = sec.get("endBar") if sec.get("endBar") is not None else sec.get("end")

        try:
            bar_start = int(bar_start)
        except Exception:
            bar_start = start_measure
        try:
            bar_end = int(bar_end)
        except Exception:
            bar_end = end_measure
        if bar_end < bar_start:
            bar_end = bar_start

        energy = sec.get("energy")
        try:
            energy = float(energy) if energy is not None else 0.5
        except Exception:
            energy = 0.5
        energy = max(0.0, min(1.0, energy))

        section_profile = None
        if callable(resolve_section_profile):
            try:
                section_profile = section_profile_map.get(f"{idx}:{str(label).strip().lower()}") or resolve_section_profile(sec, drummer_profile)
            except Exception:
                section_profile = None
        if not isinstance(section_profile, dict):
            section_profile = drummer_profile

        local_feel = global_feel
        if callable(derive_time_feel):
            try:
                local_feel = derive_time_feel(section_profile, global_feel)
            except Exception:
                local_feel = global_feel

        local_transition_bias = {}
        if callable(derive_transition_bias):
            try:
                local_transition_bias = derive_transition_bias(section_profile)
            except Exception:
                local_transition_bias = {}

        local_orchestration_bias = {}
        if callable(derive_orchestration_bias):
            try:
                local_orchestration_bias = derive_orchestration_bias(section_profile)
            except Exception:
                local_orchestration_bias = {}

        retrieval_hints = None
        if callable(derive_section_asset_scoring):
            try:
                retrieval_hints = derive_section_asset_scoring(
                    section_profile,
                    section_type=str(label),
                    local_time_feel=str(local_feel),
                    timekeeper=str(local_orchestration_bias.get("preferred_timekeeper") or "hats"),
                    energy=float(energy),
                    fill_aggression=float((local_transition_bias or {}).get("fill_probability_bias", 0.0)),
                    ghost_target=float(ghost_density),
                    syncopation_target=float(variation),
                )
            except Exception:
                retrieval_hints = None

        section_humanize_amount = max(0.0, min(1.0, humanize_amount * (0.75 + 0.5 * float((local_transition_bias or {}).get("humanize_bias", 0.5)))))
        energy_intensity = max(0.0, min(1.0, intensity * (0.75 + 0.5 * (energy - 0.5))))
        energy_vel = int(60 + energy_intensity * 50)
        energy_vel = max(1, min(127, energy_vel))

        local_base_velocity = energy_vel
        local_random = int(max(0.0, min(1.0, section_humanize_amount)) * 10)
        if "chorus" in str(label).lower() or "drop" in str(label).lower():
            local_random = min(20, int(local_random * 1.15))
        if "intro" in str(label).lower():
            local_random = int(local_random * 0.85)

        def _profile_local(inst: str, timing_scale: float, vel_bias: int, accent_boost: int, ghost_reduction: float, rr: int):
            profile = _profile(inst, timing_scale, vel_bias, accent_boost, ghost_reduction, rr)
            profile["velocityProfile"]["base"] = int(max(1, min(127, local_base_velocity + vel_bias)))
            profile["velocityProfile"]["randomRange"] = int(max(0, min(20, local_random)))
            return profile

        profiles = [
            _profile_local("kick", 0.5, 10, int(energy_intensity * 20), 0.7, int(humanize_amount * 8)),
            _profile_local("snare_center", 1.0, 0, int(15 + energy_intensity * 25), 0.4, int(humanize_amount * 10)),
            _profile_local("hihat_closed", 0.8, -10, int(8 + energy_intensity * 12), 0.6, int(humanize_amount * 6)),
            _profile_local("ride_bow", 0.7, -5, int(10 + energy_intensity * 15), 0.5, int(humanize_amount * 7)),
        ]

        section_phrase_selection = sec.get("phraseSelection") if isinstance(sec, dict) else None
        if not isinstance(section_phrase_selection, dict) and callable(select_phrase_families):
            try:
                sec_orch = sec.get("orchestration") if isinstance(sec.get("orchestration"), dict) else {}
                sec_fill = (
                    (sec.get("transitions") or {}).get("fillOut")
                    if isinstance(sec.get("transitions"), dict)
                    else {}
                )
                sec_timing = sec.get("timing") if isinstance(sec.get("timing"), dict) else {}
                section_phrase_selection = select_phrase_families(
                    section_type=str(sec.get("sectionType") or label or "section"),
                    energy=float(energy),
                    variation=float(variation),
                    timekeeper=str(sec_orch.get("timekeeper") or "hats"),
                    fill_family=str(sec_fill.get("family") or "auto"),
                    fill_enabled=bool((sec_fill.get("enabled") is True) if isinstance(sec_fill, dict) else True),
                    time_feel=str(sec_timing.get("timeFeel") or preferred_feel or "straight"),
                    drummer_profile=drummer_profile,
                )
            except Exception:
                section_phrase_selection = None
        if not isinstance(section_phrase_selection, dict):
            section_phrase_selection = {}

        groove_family = str(section_phrase_selection.get("grooveFamily") or "")
        fill_family = str(section_phrase_selection.get("fillFamily") or "")
        selected_shape = (
            choose_phrase_shape_from_family(groove_family, fill_family, "swell" if variation > 0.6 else "flat")
            if callable(choose_phrase_shape_from_family)
            else ("swell" if variation > 0.6 else "flat")
        )

        sentient_timing = drummer_profile.get("instrument_timing_profiles") or drummer_profile.get("timing_profiles")
        sentient_dynamics = drummer_profile.get("instrument_dynamic_profiles") or drummer_profile.get("dynamic_profiles")
        sentient_transitions = drummer_profile.get("transition_model")
        if build_sentient_instrument_profile and (sentient_timing or sentient_dynamics or sentient_transitions):
            sentient_profiles = []
            sentient_ids = ["kick", "snare_center", "hihat_closed", "ride_bow"]
            for inst_id in sentient_ids:
                try:
                    sentient_profiles.append(
                        build_sentient_instrument_profile(
                            instrument_id=inst_id,
                            section_label=label,
                            local_base_velocity=local_base_velocity,
                            humanize_amount=humanize_amount,
                            swing_amount=swing_amount,
                            laid_back=laid_back,
                            global_var_ms=var_ms,
                            ghost_density=ghost_density,
                            drummer_profile=drummer_profile,
                            energy_intensity=energy_intensity,
                            variation=variation,
                        )
                    )
                except Exception:
                    sentient_profiles = []
                    break
            if sentient_profiles:
                profiles = sentient_profiles

        # Apply phrase-family-selected shape override to all instrument profiles.
        try:
            for p in profiles:
                if isinstance(p, dict) and isinstance(p.get("velocityProfile"), dict):
                    p["velocityProfile"]["phraseShape"] = selected_shape
        except Exception:
            pass

        section_phrase_assets = sec.get("phraseAssets") if isinstance(sec, dict) else None
        if not isinstance(section_phrase_assets, dict) and callable(retrieve_phrase_assets):
            try:
                sec_orch = sec.get("orchestration") if isinstance(sec.get("orchestration"), dict) else {}
                section_phrase_assets = retrieve_phrase_assets(
                    phrase_selection=section_phrase_selection,
                    section_type=str(sec.get("sectionType") or label or "section"),
                    style_group=str(cfg.get("style") or songmap_summary.get("styleGroup") or "rock"),
                    timekeeper=str(sec_orch.get("timekeeper") or "hats"),
                    bars=int(bar_end - bar_start + 1),
                    energy=float(energy),
                    groove_catalog=None,
                )
            except Exception:
                section_phrase_assets = None

        phrase_assets = section_phrase_assets if isinstance(section_phrase_assets, dict) else {}
        phrase_selection = section_phrase_selection if isinstance(section_phrase_selection, dict) else {}
        phrase_event_pattern = None
        if callable(build_phrase_event_pattern):
            try:
                phrase_event_pattern = build_phrase_event_pattern(
                    phrase_assets=phrase_assets,
                    phrase_selection=phrase_selection,
                    bars=max(1, bar_end - bar_start + 1),
                )
            except Exception:
                phrase_event_pattern = None

        phrase_meta = {
            "sectionSentientOverride": bool(callable(has_sentient_identity) and has_sentient_identity(section_profile)) if isinstance(section_profile, dict) else False,
            "sectionTimeFeel": str(local_feel),
            "sectionHumanizeAmount": float(section_humanize_amount),
        }
        if isinstance(local_transition_bias, dict):
            phrase_meta["transitionBias"] = local_transition_bias
        if isinstance(local_orchestration_bias, dict):
            phrase_meta["orchestrationBias"] = local_orchestration_bias
        if isinstance(retrieval_hints, dict):
            phrase_meta["retrievalHints"] = retrieval_hints

        phrase_payload = {
            "phraseId": f"{section_id}_local_service_{idx}",
            "barStart": bar_start,
            "barEnd": bar_end,
            "profiles": profiles,
            "sectionMeta": phrase_meta,
        }
        if phrase_selection:
            phrase_payload["phraseSelection"] = phrase_selection
        if phrase_assets:
            phrase_payload["phraseAssets"] = phrase_assets
        if isinstance(phrase_event_pattern, dict):
            phrase_payload["phraseEventPattern"] = phrase_event_pattern
        if isinstance(retrieval_hints, dict):
            phrase_payload["retrievalHints"] = retrieval_hints

        phrases.append(phrase_payload)

    spec = {"styleId": style, "globalFeel": global_feel, "quantizationBase": "16th", "phrases": phrases}

    if callable(build_dcsm_payload_from_sentient_spec):
        try:
            has_patterns = any(
                isinstance(p, dict)
                and isinstance(p.get("phraseEventPattern"), dict)
                and isinstance((p.get("phraseEventPattern") or {}).get("events"), list)
                for p in phrases
            )
            if has_patterns:
                spec["dcsmRenderPayload"] = build_dcsm_payload_from_sentient_spec(
                    spec=spec,
                    cfg=req.cfg or {},
                    style_id=str(style or spec.get("styleId") or "rock"),
                    resolution_ppq=int(cfg.get("resolutionPpq") or 960),
                )
        except Exception:
            pass

    return PerformanceSpecResponse(
        ok=True,
        spec=spec,
        metadata={
            "version": API_VERSION,
            "backend": ACTIVE_BACKEND,
            "cache_enabled": CACHE_ENABLED,
            "torch_device": TORCH_DEVICE,
            "model_path": str(TORCH_MODEL_PATH) if TORCH_MODEL_PATH else None,
            "onnx_path": str(ONNX_MODEL_PATH) if ONNX_MODEL_PATH else None,
            "songmap_sections": len(songmap_summary.get("sections") or []),
            "drummer_profile_keys": len(drummer_profile.keys()),
        },
    )


# ------------------------------------------------------------
# /v1/song_roadmap (arrangement director layer)
# ------------------------------------------------------------


@app.post("/v1/song_roadmap", response_model=SongRoadmapResponse)
def song_roadmap(req: SongRoadmapRequest):
    cfg = req.cfg or {}
    drummer_profile = req.drummer_profile or {}
    song_sections = cfg.get("songSections")
    if not isinstance(song_sections, list):
        song_sections = []

    style_group = cfg.get("styleGroup") or cfg.get("style_group") or cfg.get("style") or "rock"
    style_group = str(style_group or "rock").strip().lower()

    tempos = cfg.get("tempos") or [cfg.get("tempo", 120.0)]
    if not isinstance(tempos, list) or not tempos:
        tempos = [120.0]
    try:
        avg_tempo = float(sum(float(t) for t in tempos) / max(len(tempos), 1))
    except Exception:
        avg_tempo = 120.0

    intensity = float(cfg.get("intensity", 0.65))
    variation = float(cfg.get("variation", 0.55))
    humanize_amount = float(cfg.get("humanizeAmount", 0.7))
    ghost_amount = float(cfg.get("ghostNoteAmount", 0.5))
    swing_amount = float(cfg.get("swingAmount", 0.0))
    chorus_ride_pref = float(cfg.get("chorusRidePreference", 0.0) or 0.0)

    preferred_feel = str(drummer_profile.get("preferred_feel") or drummer_profile.get("feel") or "").strip().lower()
    time_feel = preferred_feel if preferred_feel in {"straight", "swing", "shuffle", "laid_back", "pushed"} else "straight"
    if swing_amount >= 0.6 and time_feel == "straight":
        time_feel = "swing"

    def _section_key(name: str) -> str:
        raw = str(name or "").strip().lower()
        raw = "_".join(raw.split())
        raw = "".join(ch for ch in raw if (ch.isalnum() or ch == "_"))
        return raw.strip("_") or "section"

    def _energy_for_type(section_type: str) -> float:
        st = _section_key(section_type)
        base_map = {
            "intro": 0.45,
            "verse": 0.6,
            "pre": 0.72,
            "prechorus": 0.72,
            "chorus": 0.88,
            "bridge": 0.75,
            "solo": 0.78,
            "breakdown": 0.55,
            "outro": 0.5,
            "ending": 0.5,
        }
        base = base_map.get(st, 0.62)
        return max(0.0, min(1.0, (base * 0.7 + float(intensity) * 0.3)))

    global_fill_policy = {
        "defaultLength": "last_bar" if variation >= 0.55 else "last_2_beats",
        "frequency": "all_transitions" if variation >= 0.6 else "section_transitions",
        "transitionFills": True,
        "repetitionFills": bool(variation >= 0.7),
    }

    if build_song_roadmap_section_overrides and isinstance(drummer_profile, dict):
        try:
            global_override = build_song_roadmap_section_overrides(
                section_type="global",
                energy=max(0.0, min(1.0, intensity)),
                variation=max(0.0, min(1.0, variation)),
                swing_amount=max(0.0, min(1.0, swing_amount)),
                time_feel=time_feel,
                drummer_profile=drummer_profile,
                current_timekeeper="hats",
                current_fill_enabled=True,
            )
            gp = global_override.get("fillPolicy") if isinstance(global_override, dict) else None
            if isinstance(gp, dict):
                global_fill_policy.update(gp)
        except Exception:
            pass

    section_profile_map = {}
    if callable(build_section_profile_map):
        try:
            section_profile_map = build_section_profile_map(song_sections, drummer_profile)
        except Exception:
            section_profile_map = {}

    roadmap_sections: List[Dict[str, Any]] = []
    for idx, sec in enumerate(song_sections):
        if not isinstance(sec, dict):
            continue
        name = sec.get("name") or sec.get("label") or "section"
        bars = int(sec.get("bars") or 1)
        st = _section_key(name)
        energy = _energy_for_type(st)

        timekeeper = "hats"
        if st == "chorus" and (chorus_ride_pref >= 0.25 or energy >= 0.8):
            timekeeper = "ride"
        if st in {"bridge", "solo"} and energy >= 0.72:
            timekeeper = "mixed"

        fill_out_enabled = True
        if st == "intro":
            fill_out_enabled = False
        if st in {"outro", "ending"}:
            fill_out_enabled = True

        section_profile = None
        if callable(resolve_section_profile):
            try:
                section_profile = section_profile_map.get(f"{idx}:{st}") or resolve_section_profile(sec, drummer_profile)
            except Exception:
                section_profile = None
        if not isinstance(section_profile, dict):
            section_profile = drummer_profile

        local_time_feel = time_feel
        if callable(derive_time_feel):
            try:
                local_time_feel = derive_time_feel(section_profile, time_feel)
            except Exception:
                local_time_feel = time_feel

        local_transition_bias = {}
        if callable(derive_transition_bias):
            try:
                local_transition_bias = derive_transition_bias(section_profile)
            except Exception:
                local_transition_bias = {}

        local_orchestration_bias = {}
        if callable(derive_orchestration_bias):
            try:
                local_orchestration_bias = derive_orchestration_bias(section_profile)
            except Exception:
                local_orchestration_bias = {}

        retrieval_hints = None
        if callable(derive_section_asset_scoring):
            try:
                local_fill_aggression = max(0.0, min(1.0, 0.35 + energy * 0.55 + 0.25 * float((local_transition_bias or {}).get("fill_probability_bias", 0.0))))
                local_ghost_target = max(0.0, min(1.0, ghost_amount * 0.8))
                local_syncopation = max(0.0, min(1.0, 0.25 + variation * 0.5))
                retrieval_hints = derive_section_asset_scoring(
                    section_profile,
                    section_type=str(st),
                    local_time_feel=str(local_time_feel),
                    timekeeper=str(local_orchestration_bias.get("preferred_timekeeper") or "hats"),
                    energy=float(energy),
                    fill_aggression=float(local_fill_aggression),
                    ghost_target=float(local_ghost_target),
                    syncopation_target=float(local_syncopation),
                )
            except Exception:
                retrieval_hints = None

        section_payload = {
            "sectionIndex": idx,
            "sectionType": st,
            "bars": bars,
            "energy": energy,
            "orchestration": {
                "timekeeper": timekeeper,
                "hatOpenBias": max(0.0, min(1.0, 0.15 + energy * 0.35 + swing_amount * 0.15)),
                "rideBellProbability": 0.1 if timekeeper in {"ride", "mixed"} else 0.02,
                "crashDownbeatProbability": 0.65 if st in {"chorus", "bridge", "outro"} else 0.35,
            },
            "grooveIntent": {
                "kickDensityTarget": max(0.0, min(1.0, 0.35 + energy * 0.45)),
                "snareGhostTarget": max(0.0, min(1.0, ghost_amount * 0.8)),
                "cymbalDensityTarget": max(0.0, min(1.0, 0.45 + energy * 0.4)),
                "syncopation": max(0.0, min(1.0, 0.25 + variation * 0.5)),
            },
            "transitions": {
                "fillOut": {
                    "enabled": bool(fill_out_enabled),
                    "length": global_fill_policy["defaultLength"],
                    "family": "auto",
                    "aggression": max(0.0, min(1.0, 0.35 + energy * 0.55)),
                },
                "pickupIntoNext": {
                    "enabled": bool(energy >= 0.72 and variation >= 0.55),
                    "type": "snare_pickup",
                },
            },
            "timing": {
                "timeFeel": local_time_feel,
                "shuffleMode": "swing_8th" if local_time_feel in {"swing", "shuffle"} else "straight",
                "humanizeAmount": max(0.0, min(1.0, humanize_amount)),
            },
        }
        if isinstance(retrieval_hints, dict):
            section_payload["retrievalHints"] = retrieval_hints

        if build_song_roadmap_section_overrides and isinstance(drummer_profile, dict):
            try:
                sentient = build_song_roadmap_section_overrides(
                    section_type=st,
                    energy=energy,
                    variation=variation,
                    swing_amount=swing_amount,
                    time_feel=time_feel,
                    drummer_profile=drummer_profile,
                    current_timekeeper=timekeeper,
                    current_fill_enabled=fill_out_enabled,
                )
                if isinstance(sentient, dict):
                    for top_key in ("orchestration", "grooveIntent", "transitions", "timing"):
                        if isinstance(sentient.get(top_key), dict):
                            section_payload.setdefault(top_key, {}).update(sentient[top_key])
                    if isinstance(sentient.get("globalHints"), dict):
                        section_payload["globalHints"] = sentient["globalHints"]
            except Exception:
                pass

        if callable(select_phrase_families):
            try:
                sec_fill = section_payload.get("transitions", {}).get("fillOut", {}) if isinstance(section_payload.get("transitions"), dict) else {}
                sec_timing = section_payload.get("timing") if isinstance(section_payload.get("timing"), dict) else {}
                section_payload["phraseSelection"] = select_phrase_families(
                    section_type=st,
                    energy=float(energy),
                    variation=float(variation),
                    timekeeper=str(section_payload.get("orchestration", {}).get("timekeeper") or "hats"),
                    fill_family=str((sec_fill or {}).get("family") or "auto"),
                    fill_enabled=bool((sec_fill or {}).get("enabled", True)),
                    time_feel=str((sec_timing or {}).get("timeFeel") or time_feel),
                    drummer_profile=drummer_profile,
                )
            except Exception:
                pass

        if callable(retrieve_phrase_assets) and isinstance(section_payload.get("phraseSelection"), dict):
            try:
                section_payload["phraseAssets"] = retrieve_phrase_assets(
                    phrase_selection=section_payload.get("phraseSelection") or {},
                    section_type=st,
                    style_group=style_group,
                    timekeeper=str(section_payload.get("orchestration", {}).get("timekeeper") or "hats"),
                    bars=int(bars),
                    energy=float(energy),
                    groove_catalog=None,
                )
            except Exception:
                pass

        roadmap_sections.append(section_payload)

    roadmap = {
        "version": API_VERSION,
        "styleGroup": style_group,
        "global": {
            "avgTempo": avg_tempo,
            "timeFeel": time_feel,
            "defaultSwing": max(0.0, min(1.0, swing_amount)),
            "crashPolicy": {"onSectionStart": True, "intensityThreshold": 0.55},
            "fillPolicy": global_fill_policy,
            "fillCrashPolicy": {"startCrashProb": 0.55, "endCrashProb": 0.35},
        },
        "sections": roadmap_sections,
    }

    return SongRoadmapResponse(
        ok=True,
        roadmap=roadmap,
        metadata={
            "backend": ACTIVE_BACKEND,
            "sections": len(roadmap_sections),
        },
    )


@app.post("/api/generate-drums")
@app.post("/v1/generate-drums")
async def generate_drums_default_route(request: Request):
    raw = await request.json()
    normalized = None
    if callable(normalize_generate_drums_payload):
        normalized = normalize_generate_drums_payload(raw or {})
    if not isinstance(normalized, dict):
        normalized = {
            "cfg": dict(raw or {}),
            "songmap_summary": {},
            "drummer_profile": {},
        }

    req = PerformanceSpecRequest(**normalized)
    sentient_requested = False
    if callable(has_sentient_profile):
        try:
            sentient_requested = bool(has_sentient_profile(raw or {}) or has_sentient_profile(normalized))
        except Exception:
            sentient_requested = False

    if sentient_requested:
        routed = render_sentient_take(req)
        if isinstance(routed, dict):
            payload = dict(routed)
        else:
            payload = routed.model_dump() if hasattr(routed, "model_dump") else routed.dict()
        metadata = dict(payload.get("metadata") or {})
        metadata.update({
            "default_route": "/api/generate-drums",
            "preferred_endpoint": "/v1/render_sentient_take",
            "sentient_routed": True,
        })
        payload["metadata"] = metadata
        # Ensure MIDI base64 for frontend importer
        try:
            _ensure_midi_base64_on_payload(payload, req.cfg or {})
        except Exception:
            pass
        return payload

    perf_resp = performance_spec(req)
    payload = {
        "ok": bool(perf_resp.ok),
        "spec": dict(perf_resp.spec or {}),
        "metadata": dict(perf_resp.metadata or {}),
    }

    bundle = None
    if callable(build_sentient_take_bundle):
        try:
            bundle = build_sentient_take_bundle(spec=payload["spec"], cfg=req.cfg or {})
        except Exception as exc:  # pragma: no cover
            bundle = {
                "available": False,
                "source": "error",
                "reason": str(exc),
                "drum_track": None,
                "midi_notes": [],
                "plugin_render": None,
            }

    if isinstance(bundle, dict):
        if isinstance(bundle.get("drum_track"), dict):
            payload["drum_track"] = bundle.get("drum_track")
        if isinstance(bundle.get("midi_notes"), list):
            payload["midi_notes"] = bundle.get("midi_notes")
        if isinstance(bundle.get("plugin_render"), dict):
            payload["plugin_render"] = bundle.get("plugin_render")

    payload["metadata"].update({
        "default_route": "/api/generate-drums",
        "preferred_endpoint": "/v1/performance_spec",
        "sentient_routed": False,
    })
    # Ensure MIDI base64 is available in response
    try:
        _ensure_midi_base64_on_payload(payload, req.cfg or {})
    except Exception:
        pass
    return payload


# ------------------------------------------------------------
# UI compatibility routes
# ------------------------------------------------------------

@app.get("/api/drummers")
def api_list_drummers():
    by_id: Dict[str, Dict[str, Any]] = {}
    out: List[Dict[str, Any]] = []

    # Prefer metadata from admin DB when available
    for row in _list_db_drummers():
        sid = str((row or {}).get("id") or "").strip()
        if not sid:
            continue
        d = {
            "id": sid,
            "display_name": str(row.get("display_name") or _title_from_slug(sid)),
            "style": row.get("style"),
            "genre_tags": list(row.get("genre_tags") or []),
            "icon": "🥁",
        }
        # Include profileType when available for frontend normalization
        if row.get("profileType"):
            d["profileType"] = row.get("profileType")
        by_id[sid] = d
        out.append(d)

    # Add any packaged profiles that aren't in the DB list
    for slug in _scan_profile_slugs():
        sid = str(slug or "").strip()
        if not sid or sid in by_id:
            continue
        out.append({
            "id": sid,
            "display_name": _title_from_slug(sid),
            "style": None,
            "genre_tags": [],
            "icon": "🥁",
        })

    # Ensure a deterministic order
    out.sort(key=lambda d: str(d.get("display_name") or d.get("id") or "").lower())

    return {"ok": True, "drummers": out, "count": len(out)}


@app.get("/api/drummer-presets")
def api_list_drummer_presets(profileType: Optional[str] = None):
    # Presets are optional; return an empty list to satisfy the UI without erroring.
    return {"ok": True, "items": []}


@app.get("/api/styles")
def api_list_styles():
    items = _list_styles()
    return {"ok": True, "items": items, "count": len(items)}


@app.get("/api/drummer-styles")
def api_list_drummer_styles(admin: Optional[bool] = None):
    items = _list_styles(include_admin=bool(admin) if admin is not None else False)
    return {"ok": True, "items": items, "count": len(items)}


@app.get("/api/grooves/style-groups")
def api_grooves_style_groups(sources: Optional[str] = None, limit: int = 200, admin: Optional[bool] = None):
    want = {s.strip().lower() for s in str(sources or "").split(",") if s.strip()}
    if not want:
        want = {"dtk_standard"}  # default legacy bucket

    items: List[str] = []
    seen: Set[str] = set()

    # DTK/Admin DB categories
    if "dtk_standard" in want or "dtk" in want or "db" in want:
        for s in _list_styles(include_admin=bool(admin) if admin is not None else False):
            k = str(s or "").strip().lower()
            if k and k not in seen:
                seen.add(k)
                items.append(k)

    # EGMD style groups from groove catalog
    if "egmd" in want:
        cat = _get_groove_catalog()
        try:
            groups = cat.list_style_groups(sources=["egmd"], limit=max(1, int(limit or 200))) if cat is not None else []
        except Exception:
            groups = []
        if not groups:
            # Fallback to training JSONL if catalog unavailable or empty
            try:
                groups = _egmd_style_groups_from_training(limit=max(1, int(limit or 200)))
            except Exception:
                groups = []
        for g in groups or []:
            k = str(g or "").strip().lower()
            if k and k not in seen:
                seen.add(k)
                items.append(k)

    if isinstance(limit, int) and limit > 0:
        items = items[:limit]
    return {"ok": True, "items": items}


@app.get("/api/admin/drummer-categories")
def api_admin_drummer_categories(adminOnly: Optional[bool] = None):
    p = _resolve_admin_db_path_for_listing()
    items: List[Dict[str, Any]] = []
    if not p:
        return {"ok": True, "items": items}
    try:
        conn = sqlite3.connect(str(p))
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {str(r[0]) for r in cur.fetchall() or []}

        if "drummer_category_mappings" not in tables:
            try:
                conn.close()
            except Exception:
                pass
            return {"ok": True, "items": []}

        # Determine if admin flag exists on mappings
        admin_flag_exists = False
        try:
            cur.execute("PRAGMA table_info(drummer_category_mappings)")
            cols = [str(c[1]).strip().lower() for c in (cur.fetchall() or [])]
            admin_flag_exists = "is_admin_only" in cols
        except Exception:
            admin_flag_exists = False

        # Load mappings
        try:
            if admin_flag_exists:
                cur.execute(
                    """
                    SELECT category_id, COALESCE(display_name, ''), COALESCE(is_admin_only,0)
                    FROM drummer_category_mappings
                    ORDER BY lower(COALESCE(display_name, category_id))
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT category_id, COALESCE(display_name, ''), 0 as is_admin_only
                    FROM drummer_category_mappings
                    ORDER BY lower(COALESCE(display_name, category_id))
                    """
                )
            rows = cur.fetchall() or []
        except Exception:
            rows = []

        by_cat: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            cid = str(r[0] or "").strip()
            dname = str(r[1] or "").strip()
            is_admin = bool(int(r[2] or 0)) if admin_flag_exists else False
            if not cid:
                continue
            by_cat[cid] = {
                "category_id": cid,
                "display_name": dname or cid,
                "is_admin_only": is_admin,
                "drummers": [],
            }

        # Load assignments (optional)
        if "drummer_category_assignments" in tables:
            try:
                cur.execute("SELECT drummer_id, category_id FROM drummer_category_assignments")
                for a in cur.fetchall() or []:
                    did = str(a[0] or "").strip()
                    cid = str(a[1] or "").strip()
                    if did and cid and cid in by_cat:
                        arr = by_cat[cid].setdefault("drummers", [])
                        if did not in arr:
                            arr.append(did)
            except Exception:
                pass

        # Optionally filter only admin-only items
        only_flag = bool(adminOnly) if adminOnly is not None else False
        out_items = []
        for obj in by_cat.values():
            if only_flag and not bool(obj.get("is_admin_only")):
                continue
            out_items.append(obj)

        try:
            conn.close()
        except Exception:
            pass
        return {"ok": True, "items": out_items, "count": len(out_items)}
    except Exception:
        return {"ok": True, "items": []}


@app.get("/api/grooves/search")
def api_grooves_search(
    request: Request,
):
    # Groove search via GrooveCatalog with optional complexity sorting and EGMD fallback.
    try:
        qp = request.query_params
        q = str(qp.get("q") or "").strip()
        tags_raw = str(qp.get("tags") or "").strip()
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []
        sources_raw = str(qp.get("sources") or "").strip().lower()
        sources = [s.strip().lower() for s in sources_raw.split(",") if s.strip()] if sources_raw else []
        style_group = str(qp.get("style_group") or "").strip().lower() or None
        sort = str(qp.get("sort") or "").strip().lower()
        try:
            limit = max(1, int(qp.get("limit") or 24))
        except Exception:
            limit = 24

        cat = _get_groove_catalog()
        items: List[Dict[str, Any]] = []
        if cat is not None:
            cards = cat.search(
                query=q or None,
                tags=tags or None,
                sources=sources or None,
                style_group=style_group,
                limit=max(limit * 3, 50),
            )
            if sort in {"complexity_asc", "complexity_desc"}:
                rev = sort.endswith("_desc")
                def _key(c):
                    v = getattr(c, "complexity_score", None)
                    return (v is None, float(v) if v is not None else 0.0)
                cards = sorted(cards, key=_key, reverse=rev)
            for c in cards[:limit]:
                d = c.to_dict() if hasattr(c, "to_dict") else dict(c)
                items.append(d)
        if (not items) and ("egmd" in sources or not sources):
            items = _egmd_phrases_from_training(style_group, limit=limit)
        return {"ok": True, "items": items}
    except Exception:
        return {"ok": True, "items": []}


@app.get("/api/grooves/{groove_id}")
def api_grooves_get(groove_id: str):
    gid = str(groove_id or "").strip()
    if not gid:
        raise HTTPException(status_code=400, detail="groove_id required")
    # No catalog configured in this build; acknowledge route yet not found.
    raise HTTPException(status_code=404, detail="groove not found")


@app.get("/api/grooves/{groove_id}/audition")
def api_grooves_audition(groove_id: str):
    # Provide a valid shape so UI doesn't error. No audition items by default.
    return {"ok": True, "items": []}


@app.get("/api/grooves/{groove_id}/audio")
def api_grooves_audio(groove_id: str):
    # No audio available by default; return 404 to indicate absence.
    raise HTTPException(status_code=404, detail="audio not available for groove")


# Legacy groove endpoints (compatibility with older frontends)
@app.post("/api/groove/analyze")
def api_groove_analyze(payload: Dict[str, Any]):
    return {"ok": True, "items": []}


@app.get("/api/groove/goals")
def api_groove_goals():
    return {"ok": True, "items": []}


@app.post("/api/groove/apply-patch")
def api_groove_apply_patch(payload: Dict[str, Any]):
    return {"ok": True}


# ------------------------------------------------------------
# Health and status
# ------------------------------------------------------------

@app.get("/healthz")
def healthz():
    snapshot = _readiness_snapshot()
    return {
        "ok": True,
        "version": API_VERSION,
        "backend": snapshot["backend"],
        "ready": snapshot["ready"],
        "strict_required": snapshot["strict_required"],
        "app_env": snapshot["app_env"],
        "backend_ready": snapshot["backend_ready"],
        "model_version": snapshot["model_version"],
        "model_sha256_verified": snapshot["model_sha256_verified"],
        "model_error": snapshot["model_load_error"],
        "checkpoint": snapshot["checkpoint"],
        "reason": snapshot["reason"],
    }


@app.get("/readyz")
def readyz():
    snapshot = _readiness_snapshot()
    if not snapshot["ready"]:
        raise HTTPException(status_code=503, detail=snapshot)
    return {"ok": True, **snapshot}


@app.get("/api/llm/status")
def api_llm_status():
    return {
        "ok": True,
        "version": API_VERSION,
        "backend": ACTIVE_BACKEND,
        "torch_available": TORCH_AVAILABLE,
        "onnx_available": ONNX_AVAILABLE,
        "cache_enabled": CACHE_ENABLED,
    }


# ------------------------------------------------------------
# Plugin MIDI rendering (stub)
# ------------------------------------------------------------

@app.post("/api/render-plugin-midi")
def api_render_plugin_midi(payload: Dict[str, Any]):
    plugin = str((payload or {}).get("plugin") or "jamstix")
    # Minimal shaped response; actual MIDI rendering is optional in this build.
    return {
        "plugin": plugin,
        "midi_base64": "",
        "ticks_per_beat": 480,
    }


# ------------------------------------------------------------
# File upload suite (basic local handling)
# ------------------------------------------------------------

def _uploads_dir() -> Path:
    p = _repo_root() / "uploads"
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


@app.post("/files/upload-url")
def files_upload_url(key: str):
    # Return a dummy URL; client will fall back to direct POST on failure.
    return {"url": "http://127.0.0.1:1/presigned"}


@app.post("/files/upload")
async def files_upload(file: UploadFile = File(...), key: Optional[str] = Form(None)):
    filename = str(getattr(file, "filename", "upload.bin") or "upload.bin")
    safe_name = filename.replace("\\", "/").split("/")[-1]
    rel_key = str(key or f"uploads/{safe_name}")
    if not rel_key.lower().startswith("uploads/"):
        rel_key = f"uploads/{rel_key}"
    out_path = _repo_root() / rel_key
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    with open(out_path, "wb") as f:
        f.write(data)
    return {"ok": True, "key": rel_key, "httpUrl": f"/{rel_key}"}


@app.get("/files/waveform")
def files_waveform(key: str, max_points: Optional[int] = None):
    # Minimal stub without DSP; client tolerates empty peaks.
    return {"sr": 0, "peaks": []}


@app.get("/files/download-url")
def files_download_url(key: str):
    rel = str(key or "").lstrip("/")
    if not rel.lower().startswith("uploads/"):
        rel = f"uploads/{rel}"
    return {"url": f"/{rel}"}


# Legacy upload endpoints used by some flows
@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    # Delegate to /files/upload for consistency
    res = await files_upload(file=file)
    return {"success": bool(res.get("ok")), "file_id": res.get("key"), "message": "Uploaded", "key": res.get("key")}


@app.post("/api/upload/init")
def api_upload_init():
    # Client will proceed to /api/upload or /files/upload after init.
    return {"ok": True}


@app.post("/api/upload/complete")
def api_upload_complete():
    return {"ok": True}


# ------------------------------------------------------------
# EGMD compatibility (phrase/style listings)
# ------------------------------------------------------------

@app.get("/api/egmd/phrases")
def api_egmd_phrases(style_group: Optional[str] = None, meter: Optional[str] = None, tempo_bpm: Optional[str] = None, limit: int = 50):
    cat = _get_groove_catalog()
    sg = str(style_group or "").strip().lower()
    try:
        items: List[Dict[str, Any]] = []
        cards = []
        if cat is not None:
            try:
                cards = cat.search(
                    query=None,
                    tags=None,
                    sources=["egmd"],
                    style_group=sg or None,
                    limit=max(1, int(limit or 50)),
                )
            except Exception:
                cards = []
        for c in cards or []:
            d = c.to_dict() if hasattr(c, "to_dict") else dict(c)
            pid = d.get("phrase_id")
            if pid is None:
                # Prefer explicit EGMD phrase id fields if present
                pid = d.get("egmd_phrase_id") or d.get("phraseId")
            try:
                pid_int = int(pid) if pid is not None and str(pid).strip() != "" else None
            except Exception:
                pid_int = None
            items.append(
                {
                    "phrase_id": pid_int,
                    "midi_path": d.get("midi_path"),
                    "audio_path": d.get("audio_path"),
                    "style_group": d.get("style_group"),
                    "title": d.get("title"),
                    "source": d.get("source"),
                    "bars": d.get("bars"),
                    "tempo_bpm": d.get("tempo_bpm"),
                }
            )
        # Fallback to training JSONL when catalog missing/empty
        if not items:
            items = _egmd_phrases_from_training(sg or None, limit=max(1, int(limit or 50)))
        return {"ok": True, "items": items}
    except Exception:
        return {"ok": True, "items": []}


@app.get("/api/egmd/style-groups")
def api_egmd_style_groups(limit: int = 200):
    cat = _get_groove_catalog()
    try:
        groups: List[str] = []
        if cat is not None:
            try:
                groups = cat.list_style_groups(sources=["egmd"], limit=max(1, int(limit or 200)))
            except Exception:
                groups = []
        if not groups:
            groups = _egmd_style_groups_from_training(limit=max(1, int(limit or 200)))
        return {"ok": True, "items": groups}
    except Exception:
        return {"ok": True, "items": []}


# Additional groove summary stub
@app.get("/api/grooves/complexity-summary")
def api_grooves_complexity_summary(sources: Optional[str] = None, style_group: Optional[str] = None):
    return {"ok": True, "items": []}


# ------------------------------------------------------------
# Kits API (used by kit selector panes)
# ------------------------------------------------------------

@app.get("/api/kits")
def api_list_kits():
    return {"ok": True, "items": []}


@app.get("/api/kits/{kit_id}/manifest")
def api_get_kit_manifest(kit_id: str):
    kid = str(kit_id or "").strip()
    if not kid:
        raise HTTPException(status_code=400, detail="kit_id required")
    # Minimal manifest structure
    return {
        "ok": True,
        "kit_id": kid,
        "version": "1.0",
        "instruments": [],
        "map": {},
    }


# ------------------------------------------------------------
# Audio analysis and MIDI generation compatibility
# ------------------------------------------------------------

@app.get("/analyze/tempo")
def api_analyze_tempo(key: str, start: Optional[float] = None, end: Optional[float] = None):
    # Return a neutral tempo if unknown
    return {"bpm": 120.0, "tempoCurve": []}


@app.post("/analyze/tempo_sections")
def api_analyze_tempo_sections(payload: Dict[str, Any]):
    # Return empty per-section results
    secs = (payload or {}).get("sections") or []
    results = [
        {"start": float(s.get("start", 0.0)), "end": float(s.get("end", 0.0)), "tempo": None, "candidates": [], "confidence": 0.0}
        for s in secs
        if isinstance(s, dict)
    ]
    return {"results": results}


@app.post("/align/sections")
async def api_align_sections(key: str, request: Request):
    # Echo back sections with no modification
    try:
        sections = await request.json()
        if not isinstance(sections, list):
            sections = []
    except Exception:
        sections = []
    return {"tempo": None, "sections": sections or []}


@app.post("/generate/midi64")
def api_generate_midi64(payload: Dict[str, Any]):
    # Return an empty base64 MIDI placeholder
    return {"base64": ""}


@app.post("/generate/midi_sections")
def api_generate_midi_sections(payload: Dict[str, Any]):
    return {"filename": "drums.mid", "base64": ""}
# ------------------------------------------------------------
# /v1/render_sentient_take endpoint
# ------------------------------------------------------------

@app.post("/v1/render_sentient_take", response_model=SentientTakeRenderResponse)
def render_sentient_take(req: PerformanceSpecRequest):
    perf_resp = performance_spec(req)
    spec = dict(perf_resp.spec or {})
    bundle = None
    if callable(build_sentient_take_bundle):
        try:
            bundle = build_sentient_take_bundle(spec=spec, cfg=req.cfg or {})
        except Exception as exc:  # pragma: no cover - additive resilience
            bundle = {
                "available": False,
                "source": "error",
                "reason": str(exc),
                "drum_track": None,
                "midi_notes": [],
                "plugin_render": None,
            }
    if not isinstance(bundle, dict):
        bundle = {
            "available": False,
            "source": "helper_unavailable",
            "reason": "render_take_sentient_missing",
            "drum_track": None,
            "midi_notes": [],
            "plugin_render": None,
        }

    metadata = dict(perf_resp.metadata or {})
    metadata.update(
        {
            "rendered_take_available": bool(bundle.get("available")),
            "render_source": bundle.get("source"),
            "plugin_target": (req.cfg or {}).get("pluginTarget") or (req.cfg or {}).get("plugin") or None,
        }
    )

    return SentientTakeRenderResponse(
        ok=True,
        spec=spec,
        drum_track=bundle.get("drum_track"),
        midi_notes=bundle.get("midi_notes") or [],
        plugin_render=bundle.get("plugin_render"),
        metadata=metadata,
    )

# ------------------------------------------------------------
# Debug explain endpoint
# ------------------------------------------------------------

@app.post("/v1/debug/explain")
def debug_explain(req: PerformanceSpecRequest):
    cfg = req.cfg or {}
    tempos = cfg.get("tempos") or [cfg.get("tempo", 120.0)]
    if not isinstance(tempos, list) or not tempos:
        tempos = [120.0]
    avg_tempo = float(sum(float(t) for t in tempos) / max(len(tempos), 1))
    style = cfg.get("style")
    complexity = float(cfg.get("complexity", 0.7))

    hreq = HumanizationRequest(tempo_bpm=avg_tempo, style=style, pattern_complexity=complexity)
    norm = _normalize_inputs(hreq.tempo_bpm, float(_style_to_int(hreq.style)), hreq.pattern_complexity)[0].tolist()
    hresp = humanization_params(hreq)

    return {
        "ok": True,
        "backend": ACTIVE_BACKEND,
        "cache_enabled": CACHE_ENABLED,
        "inputs": hreq.model_dump(),
        "normalized": {"tempo": norm[0], "style": norm[1], "complexity": norm[2]},
        "humanization": hresp.model_dump(),
        "torch_available": TORCH_AVAILABLE,
        "onnx_available": ONNX_AVAILABLE,
        "torch_loaded": TORCH_MODEL is not None,
        "onnx_loaded": ONNX_SESSION is not None,
        "device": TORCH_DEVICE,
    }
