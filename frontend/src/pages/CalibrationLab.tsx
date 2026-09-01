import React, { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import axios from 'axios';
import { resolveApiBaseNormalized } from '../utils/apiBase';
import {
  Activity,
  ArrowRight,
  ClipboardList,
  Gauge,
  Headphones,
  RefreshCcw,
  Save,
  Sparkles,
  Users,
} from 'lucide-react';

type CompletionStatus = 'ready' | 'refine' | 'needs_tuning' | 'unknown';
type TabId = 'adjustments' | 'metrics' | 'feedback' | 'listening';

interface CompletionStatusInfo {
  status: CompletionStatus;
  completion_ratio: number | null;
}

interface DrummerListItem {
  slug: string;
  displayName: string;
  headline?: string;
  completionStatus: CompletionStatusInfo;
  assimilationStatus?: {
    status?: string;
    ready_for_calibration?: boolean;
    missing_steps?: string[];
    counts?: Record<string, number>;
    metrics?: Record<string, number>;
  };
  latestRunAt?: string | null;
  metricsWithin?: number;
  metricsCompared?: number;
}

const formatClockTime = (value: number | null): string => {
  if (!value) return '—';
  return new Date(value).toLocaleTimeString();
};

const formatTimeSeconds = (value: number): string => {
  if (!Number.isFinite(value) || value < 0) return '0:00';
  const whole = Math.floor(value);
  const minutes = Math.floor(whole / 60);
  const seconds = whole % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
};

const hasPlayableArtifacts = (item: EvaluationItemPayload | null): boolean => {
  const artifactMap = item?.artifact_map;
  if (!artifactMap) return false;

  const laneHasPlayableArtifact = (lane: 'A' | 'B'): boolean => {
    const entries = artifactMap[lane] || [];
    return entries.some((artifact) => Boolean(resolveArtifactSource(artifact)));
  };

  return laneHasPlayableArtifact('A') && laneHasPlayableArtifact('B');
};

const AudioPreviewPlayer: React.FC<{ src: string; title: string }> = ({ src, title }) => {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [isPlaying, setIsPlaying] = useState(false);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [loopEnabled, setLoopEnabled] = useState(false);
  const [waveformBins, setWaveformBins] = useState<number[]>([]);
  const [waveformError, setWaveformError] = useState<string | null>(null);
  const [playbackError, setPlaybackError] = useState<string | null>(null);

  const playbackProgress = duration > 0 ? Math.min(1, Math.max(0, currentTime / duration)) : 0;

  useEffect(() => {
    let cancelled = false;
    const loadWaveform = async () => {
      setWaveformError(null);
      setWaveformBins([]);
      try {
        const response = await fetch(src);
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const audioBufferRaw = await response.arrayBuffer();
        const WindowWithWebkitAudio = window as Window & {
          webkitAudioContext?: typeof AudioContext;
        };
        const AudioContextCtor = window.AudioContext || WindowWithWebkitAudio.webkitAudioContext;
        if (!AudioContextCtor) {
          throw new Error('AudioContext unavailable');
        }
        const audioContext = new AudioContextCtor();
        try {
          const decoded = await audioContext.decodeAudioData(audioBufferRaw.slice(0));
          if (cancelled) return;
          const channel = decoded.getChannelData(0);
          if (!channel.length) return;

          const bins = 96;
          const blockSize = Math.max(1, Math.floor(channel.length / bins));
          const next: number[] = [];
          for (let i = 0; i < bins; i += 1) {
            const start = i * blockSize;
            const end = Math.min(channel.length, start + blockSize);
            let peak = 0;
            for (let j = start; j < end; j += 1) {
              const v = Math.abs(channel[j] || 0);
              if (v > peak) peak = v;
            }
            next.push(peak);
          }
          setWaveformBins(next);
        } finally {
          void audioContext.close();
        }
      } catch {
        if (!cancelled) {
          setWaveformError('Waveform unavailable for this track.');
        }
      }
    };

    void loadWaveform();
    return () => {
      cancelled = true;
    };
  }, [src]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.pause();
    audio.currentTime = 0;
    audio.load();
    setIsPlaying(false);
    setCurrentTime(0);
    setDuration(0);
    setPlaybackError(null);

    const onPlay = () => {
      setIsPlaying(true);
      setPlaybackError(null);
    };
    const onPause = () => {
      setIsPlaying(false);
    };
    const onError = () => {
      const mediaError = audio.error;
      const code = mediaError?.code;
      const detail = code ? ` (media error ${code})` : '';
      setPlaybackError(`Playback failed for this track${detail}.`);
      setIsPlaying(false);
    };

    const onLoadedMetadata = () => {
      setDuration(Number.isFinite(audio.duration) ? audio.duration : 0);
    };
    const onTimeUpdate = () => {
      setCurrentTime(audio.currentTime || 0);
    };
    const onEnded = () => {
      if (!audio.loop) {
        setIsPlaying(false);
      }
    };

    audio.addEventListener('play', onPlay);
    audio.addEventListener('pause', onPause);
    audio.addEventListener('error', onError);
    audio.addEventListener('loadedmetadata', onLoadedMetadata);
    audio.addEventListener('timeupdate', onTimeUpdate);
    audio.addEventListener('ended', onEnded);
    return () => {
      audio.removeEventListener('play', onPlay);
      audio.removeEventListener('pause', onPause);
      audio.removeEventListener('error', onError);
      audio.removeEventListener('loadedmetadata', onLoadedMetadata);
      audio.removeEventListener('timeupdate', onTimeUpdate);
      audio.removeEventListener('ended', onEnded);
    };
  }, [src]);

  const togglePlayback = async () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      try {
        setPlaybackError(null);
        await audio.play();
        setIsPlaying(true);
      } catch {
        setPlaybackError('Unable to start playback. Try again or open track in a new tab.');
        setIsPlaying(false);
      }
    } else {
      audio.pause();
      setIsPlaying(false);
    }
  };

  const nudge = (deltaSeconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const max = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : Math.max(currentTime, 0);
    const next = Math.min(max, Math.max(0, (audio.currentTime || 0) + deltaSeconds));
    audio.currentTime = next;
    setCurrentTime(next);
  };

  const seekTo = (seconds: number) => {
    const audio = audioRef.current;
    if (!audio) return;
    const max = Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : Math.max(duration, 0);
    const next = Math.min(max, Math.max(0, seconds));
    audio.currentTime = next;
    setCurrentTime(next);
  };

  const handleSeek = (event: React.ChangeEvent<HTMLInputElement>) => {
    const audio = audioRef.current;
    if (!audio) return;
    const next = Number(event.target.value);
    audio.currentTime = next;
    setCurrentTime(next);
  };

  const toggleLoop = () => {
    const audio = audioRef.current;
    if (!audio) return;
    const next = !loopEnabled;
    audio.loop = next;
    setLoopEnabled(next);
  };

  return (
    <div className="rounded-xl border border-purple-500/30 bg-purple-950/50 p-3">
      <audio ref={audioRef} src={src} preload="metadata" className="hidden" />
      <p className="mb-2 text-[11px] text-purple-100/70">{title}</p>
      <div className="mb-3 rounded-lg border border-purple-500/25 bg-purple-950/40 p-2">
        <div className="relative h-20 overflow-hidden rounded bg-black/30">
          <div className="absolute inset-0 flex items-end gap-[2px] px-1 py-1">
            {waveformBins.length > 0
              ? waveformBins.map((bin, index) => {
                  const minHeight = 8;
                  const px = minHeight + Math.round(bin * 54);
                  return (
                    <span
                      key={`${title}-${index}`}
                      style={{ height: `${px}px` }}
                      className="w-full rounded-sm bg-emerald-300/80"
                    />
                  );
                })
              : Array.from({ length: 64 }).map((_, index) => (
                  <span key={`${title}-placeholder-${index}`} className="h-3 w-full rounded-sm bg-purple-300/25" />
                ))}
          </div>
          <div
            className="absolute top-0 bottom-0 w-[2px] bg-amber-300 shadow-[0_0_8px_rgba(252,211,77,0.85)]"
            style={{ left: `${playbackProgress * 100}%` }}
          />
          <button
            type="button"
            onClick={(event) => {
              const rect = event.currentTarget.getBoundingClientRect();
              const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
              seekTo((duration || 0) * ratio);
            }}
            className="absolute inset-0"
            aria-label="Seek within track waveform"
          />
        </div>
        {waveformError && <p className="mt-1 text-[10px] text-purple-200/70">{waveformError}</p>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => seekTo(0)}
          className="rounded-md border border-purple-500/40 px-2 py-1 text-[11px] text-purple-100"
        >
          |&lt;
        </button>
        <button
          type="button"
          onClick={() => nudge(-10)}
          className="rounded-md border border-purple-500/40 px-2 py-1 text-[11px] text-purple-100"
        >
          -10s
        </button>
        <button
          type="button"
          onClick={togglePlayback}
          className="rounded-md border border-emerald-400/50 bg-emerald-500/20 px-3 py-1 text-[11px] font-semibold text-emerald-100"
        >
          {isPlaying ? 'Pause' : 'Play'}
        </button>
        <button
          type="button"
          onClick={() => nudge(10)}
          className="rounded-md border border-purple-500/40 px-2 py-1 text-[11px] text-purple-100"
        >
          +10s
        </button>
        <button
          type="button"
          onClick={() => seekTo(duration || 0)}
          className="rounded-md border border-purple-500/40 px-2 py-1 text-[11px] text-purple-100"
        >
          &gt;|
        </button>
        <button
          type="button"
          onClick={toggleLoop}
          className={`rounded-md border px-2 py-1 text-[11px] ${
            loopEnabled
              ? 'border-amber-400/60 bg-amber-500/20 text-amber-100'
              : 'border-purple-500/40 text-purple-100'
          }`}
        >
          Loop {loopEnabled ? 'On' : 'Off'}
        </button>
      </div>
      {playbackError && (
        <p className="mt-2 text-[10px] text-rose-200">
          {playbackError}{' '}
          <a href={src} target="_blank" rel="noreferrer" className="underline decoration-dotted underline-offset-2">
            Open track
          </a>
        </p>
      )}
      <div className="mt-2">
        <input
          type="range"
          min={0}
          max={Math.max(duration, 0.001)}
          step={0.01}
          value={Math.min(currentTime, Math.max(duration, 0.001))}
          onChange={handleSeek}
          className="w-full accent-emerald-400"
        />
        <div className="mt-1 flex items-center justify-between text-[10px] text-purple-100/60">
          <span>{formatTimeSeconds(currentTime)}</span>
          <span>{formatTimeSeconds(duration)}</span>
        </div>
      </div>
    </div>
  );
};

const AdjustmentKnob: React.FC<{
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  help?: string;
  onChange: (next: number) => void;
}> = ({ label, value, min, max, step, help, onChange }) => {
  const boundedValue = Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : min;
  const span = Math.max(max - min, Number.EPSILON);
  const ratio = (boundedValue - min) / span;
  const angle = -135 + ratio * 270;
  const radians = (angle * Math.PI) / 180;
  const x2 = 50 + 28 * Math.cos(radians);
  const y2 = 50 + 28 * Math.sin(radians);

  const nudge = (delta: number) => {
    const next = Math.min(max, Math.max(min, boundedValue + delta));
    onChange(Number(next.toFixed(4)));
  };

  return (
    <div className="rounded-2xl border border-amber-400/25 bg-amber-500/5 p-3">
      <p className="text-[11px] font-semibold uppercase tracking-[0.25em] text-amber-100/90">{label}</p>
      <div className="mt-2 flex items-center gap-3">
        <button
          type="button"
          onClick={() => nudge(-step)}
          className="h-7 w-7 rounded-full border border-amber-300/40 text-xs text-amber-100"
          aria-label={`Decrease ${label}`}
        >
          −
        </button>
        <div className="relative h-20 w-20">
          <svg viewBox="0 0 100 100" className="h-20 w-20">
            <circle cx="50" cy="50" r="34" className="fill-purple-950/90 stroke-purple-500/30" strokeWidth="7" />
            <circle
              cx="50"
              cy="50"
              r="34"
              className="fill-none stroke-amber-300/60"
              strokeWidth="6"
              strokeDasharray={`${ratio * 214} 214`}
              transform="rotate(-135 50 50)"
            />
            <line x1="50" y1="50" x2={x2} y2={y2} className="stroke-amber-200" strokeWidth="5" strokeLinecap="round" />
          </svg>
          <input
            type="range"
            min={min}
            max={max}
            step={step}
            value={boundedValue}
            onChange={(event) => onChange(Number(event.target.value))}
            className="absolute inset-0 cursor-pointer opacity-0"
            aria-label={label}
          />
        </div>
        <button
          type="button"
          onClick={() => nudge(step)}
          className="h-7 w-7 rounded-full border border-amber-300/40 text-xs text-amber-100"
          aria-label={`Increase ${label}`}
        >
          +
        </button>
      </div>
      <div className="mt-2 text-xs text-amber-200 font-mono">{boundedValue.toFixed(2)}</div>
      {help && <p className="mt-1 text-[11px] text-purple-100/70">{help}</p>}
    </div>
  );
};

interface AdjustmentMetadata {
  field_help?: Record<string, string>;
}

interface CalibrationRun {
  id: string;
  started_at: string;
  completed_at?: string;
  outcome: 'success' | 'failure' | 'pending';
  note_count?: number;
  fills_per_minute?: number | null;
  delta_summary?: string;
  error_message?: string | null;
}

interface FeedbackEntry {
  id: string;
  submitted_at: string;
  author: string;
  rating: number;
  comment: string;
}

interface DrummerDetailPayload {
  slug: string;
  displayName: string;
  adjustments: Record<string, any>;
  rollupTargets: Record<string, any>;
  metrics?: Record<string, any>;
  metadata?: AdjustmentMetadata;
  assimilationStatus?: {
    status?: string;
    ready_for_calibration?: boolean;
    missing_steps?: string[];
    counts?: Record<string, number>;
    metrics?: Record<string, number>;
  };
  runHistory?: CalibrationRun[];
  feedbackSamples?: FeedbackEntry[];
  completionStatus?: CompletionStatusInfo;
}

interface CalibrationHealth {
  status: 'ok' | 'degraded';
  db_path?: string | null;
  db_exists: boolean;
  calibration_tables: Record<string, boolean>;
  notes?: string[];
}

interface AudioArtifactPayload {
  artifact_id: string;
  run_id?: string | null;
  artifact_type: string;
  storage_uri: string;
  public_url?: string | null;
  duration_sec?: number | null;
  loudness_lufs?: number | null;
  render_recipe?: Record<string, unknown> | null;
}

interface EvaluationItemPayload {
  item_id: string;
  session_id: string;
  target_drummer_slug: string;
  base_groove_id: string;
  baseline_label?: string | null;
  baseline_reference_audio_url?: string | null;
  reference_artifact_id?: string | null;
  baseline_run_id?: string | null;
  candidate_a_run_id?: string | null;
  candidate_b_run_id?: string | null;
  eval_mode: 'single' | 'AB' | 'ABX';
  ab_mapping: Record<string, string | null>;
  artifact_map: Record<string, AudioArtifactPayload[]>;
}

interface GenerateCandidatesResponse {
  status: string;
  run_ids: string[];
  session_id?: string | null;
  item_id?: string | null;
  baseline_run_id?: string | null;
  reference_artifact_id?: string | null;
  baseline_reference_available?: boolean;
  baseline_missing_reason?: string | null;
  artifact_wait_enforced?: boolean;
  artifact_wait_timeout_sec?: number;
}

interface ListeningLaneProgress {
  lane: string;
  run_id?: string | null;
  ready: boolean;
  artifact_count: number;
  artifact_types: string[];
  strict_reference_ok?: boolean;
  not_required?: boolean;
  run_outcome?: string | null;
  run_started_at?: string | null;
  run_completed_at?: string | null;
  run_age_seconds?: number | null;
  stalled_in_queue?: boolean;
  reason?: string | null;
}

interface ListeningItemProgressPayload {
  item_id: string;
  all_ready: boolean;
  missing_lanes: string[];
  lanes: ListeningLaneProgress[];
  queue_stall_hint_seconds?: number;
}

interface GenerateRunResponse {
  status: string;
  run_id?: string;
}

type JudgmentChoice = '' | 'A' | 'B' | 'tie';

interface PairwiseJudgmentForm {
  preferred_candidate: JudgmentChoice;
  closer_to_target: JudgmentChoice;
  better_feel: JudgmentChoice;
  more_musical: JudgmentChoice;
  confidence: number;
}

interface DrummerDetail extends DrummerDetailPayload {
  originalAdjustments: Record<string, any>;
}

const STATUS_LABEL: Record<CompletionStatus, string> = {
  ready: 'Ready',
  refine: 'Needs Review',
  needs_tuning: 'Needs Tuning',
  unknown: 'Unknown',
};

const STATUS_FILTERS: Array<{ id: 'all' | CompletionStatus; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'ready', label: 'Ready' },
  { id: 'refine', label: 'Needs Review' },
  { id: 'needs_tuning', label: 'Needs Tuning' },
];

const CONTROL_BOUNDS: Record<string, { min: number; max: number; step: number }> = {
  timing_scale: { min: 0.6, max: 1.6, step: 0.01 },
  velocity_std_scale: { min: 0.6, max: 1.6, step: 0.01 },
  fill_factor: { min: 0, max: 2.5, step: 0.01 },
  fill_fpm_cap: { min: 0, max: 12, step: 0.05 },
  snare_share_scale: { min: 0.2, max: 2.5, step: 0.01 },
  hihat_share_scale: { min: 0.2, max: 1.6, step: 0.01 },
  kick_share_scale: { min: 0.2, max: 1.6, step: 0.01 },
  tom_share_scale: { min: 0.2, max: 1.6, step: 0.01 },
  ride_share_scale: { min: 0.2, max: 1.6, step: 0.01 },
  cymbal_share_scale: { min: 0.2, max: 1.6, step: 0.01 },
  hihat_share_floor: { min: 0, max: 0.6, step: 0.01 },
  max_fills_per_bar_scale: { min: 0.5, max: 2.5, step: 0.05 },
  fill_velocity_scale: { min: 0.4, max: 1.4, step: 0.01 },
};

const NUMBER_FIELDS = new Set(Object.keys(CONTROL_BOUNDS));

const FIELD_GROUPS: Array<{ title: string; keys: string[]; blurb: string }> = [
  {
    title: 'Microtiming & Feel',
    keys: ['timing_scale', 'velocity_std_scale'],
    blurb: "Loosen or tighten pocket and adjust dynamic spread to match the drummer's touch.",
  },
  {
    title: 'Fill Density & Flow',
    keys: ['fill_factor', 'fill_fpm_cap', 'max_fills_per_bar_scale', 'fill_velocity_scale'],
    blurb: 'Control how often fills appear, how clustered they get, and how aggressive they land.',
  },
  {
    title: 'Kit Balance',
    keys: [
      'snare_share_scale',
      'hihat_share_scale',
      'kick_share_scale',
      'tom_share_scale',
      'ride_share_scale',
      'cymbal_share_scale',
      'hihat_share_floor',
    ],
    blurb: 'Reallocate note share across the kit to preserve each drummer fingerprint.',
  },
];

const FEEL_KNOB_KEYS = new Set(['timing_scale', 'velocity_std_scale', 'fill_factor', 'fill_velocity_scale']);

const API_BASE = resolveApiBaseNormalized();
const API_ROOT = `${API_BASE}/calibration`.replace(/([^:]\/)\/+?/g, '$1');
const api = axios.create({ baseURL: API_ROOT, timeout: 15000 });
const CALIBRATION_STATIC_PREFIX = '/static/calibration_artifacts';
const LISTENING_QUEUE_TIMEOUT_MS = 180000;
const LISTENING_ITEM_TIMEOUT_MS = 45000;
const LISTENING_ITEM_READY_RETRIES = 8;
const LISTENING_ITEM_READY_DELAY_MS = 1500;
const LISTENING_ARTIFACT_READY_RETRIES = 16;
const LISTENING_ARTIFACT_READY_DELAY_MS = 2000;
const DRUMMER_DETAIL_TIMEOUT_MS = 45000;
const DEFAULT_TEST_DRUMMER_KEYWORD = 'bonham';
const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

const pickDefaultDrummerSlug = (items: DrummerListItem[]): string | null => {
  if (!items.length) return null;
  const bonham = items.find((item) => {
    const slug = String(item.slug || '').toLowerCase();
    const name = String(item.displayName || '').toLowerCase();
    return slug.includes(DEFAULT_TEST_DRUMMER_KEYWORD) || name.includes('john bonham');
  });
  return (bonham || items[0]).slug;
};

const ensureAbsoluteArtifactUrl = (value: string): string => {
  if (!value) return value;
  if (/^https?:\/\//i.test(value)) return value;
  const normalized = value.startsWith('/') ? value : `/${value}`;
  const apiBase = resolveApiBaseNormalized();
  const backendBase = (apiBase && apiBase.trim()) || 'http://localhost:8000';
  return `${backendBase.replace(/\/$/, '')}${normalized}`;
};

const resolveArtifactSource = (artifact: AudioArtifactPayload): string | null => {
  const candidates = [artifact.public_url, artifact.storage_uri].filter((value): value is string => Boolean(value && value.trim()));

  for (const rawCandidate of candidates) {
    const trimmed = rawCandidate.trim();
    if (!trimmed) continue;
    if (/^https?:\/\//i.test(trimmed)) return trimmed;
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(trimmed)) continue;
    if (trimmed.startsWith(CALIBRATION_STATIC_PREFIX)) return ensureAbsoluteArtifactUrl(trimmed);

    const normalized = trimmed.replace(/\\/g, '/').replace(/^\.?\//, '');
    const marker = 'artifacts/calibration/';
    const markerIndex = normalized.toLowerCase().indexOf(marker);
    if (markerIndex !== -1) {
      const relative = normalized.slice(markerIndex + marker.length).replace(/^\/+/, '');
      return ensureAbsoluteArtifactUrl(`${CALIBRATION_STATIC_PREFIX}/${relative}`);
    }

    return ensureAbsoluteArtifactUrl(`/${normalized}`);
  }

  return null;
};

const formatPercent = (value?: number | null) => {
  if (value == null) return '—';
  return `${Math.round(value * 100)}%`;
};

const formatDate = (value?: string | null) => (value ? new Date(value).toLocaleString() : 'No runs yet');

const sanitizeNumber = (key: string, raw: string) => {
  const parsed = Number(raw);
  if (Number.isNaN(parsed)) return null;
  const bounds = CONTROL_BOUNDS[key];
  if (!bounds) return parsed;
  return Math.min(bounds.max, Math.max(bounds.min, parsed));
};

const diffKeys = (baseline: Record<string, any>, next: Record<string, any>) => {
  const list: string[] = [];
  Object.keys(next).forEach((key) => {
    if (JSON.stringify(baseline[key]) !== JSON.stringify(next[key])) {
      list.push(key);
    }
  });
  return list;
};

const slugToTitle = (value: string) =>
  value
    .split('_')
    .map((segment) => segment.charAt(0).toUpperCase() + segment.slice(1))
    .join(' ');

const CompletionBadge: React.FC<{ status: CompletionStatus; ratio?: number | null }> = ({ status, ratio }) => (
  <span
    className="inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-semibold uppercase tracking-wide"
  >
    <span className="h-2 w-2 rounded-full bg-current" />
    {STATUS_LABEL[status]}
    {ratio != null && <span className="text-white/70">{formatPercent(ratio)}</span>}
  </span>
);

const labelize = (key: string) =>
  key
    .replace(/_/g, ' ')
    .replace(/\b([a-z])/g, (match) => match.toUpperCase())
    .replace('Fpm', 'FPM');

const formatShare = (value?: number | null) => {
  if (value == null) return '—';
  return `${(value * 100).toFixed(1)}%`;
};

const describeRunOutcome = (run: CalibrationRun) => {
  if (run.outcome === 'pending') return 'Processing';
  if (run.outcome === 'failure') return 'Failed';
  return 'Successful';
};

const RUN_BADGE_STYLE: Record<CalibrationRun['outcome'], string> = {
  success: 'bg-emerald-500/15 text-emerald-200 border border-emerald-500/30',
  failure: 'bg-rose-500/15 text-rose-200 border border-rose-500/30',
  pending: 'bg-amber-500/15 text-amber-200 border border-amber-500/30',
};

const asShareMap = (shares: unknown): Record<string, number> => {
  if (!shares) return {};
  if (typeof shares === 'string') {
    try {
      const parsed = JSON.parse(shares);
      return typeof parsed === 'object' && parsed ? parsed : {};
    } catch (error) {
      return {};
    }
  }
  if (typeof shares === 'object') {
    return shares as Record<string, number>;
  }
  return {};
};

const shouldRetryRequest = (error: unknown): boolean => {
  if (!axios.isAxiosError(error)) return false;
  const statusCode = error.response?.status;
  if (!statusCode) return true;
  return statusCode >= 500 || statusCode === 429;
};

const sleepWithBackoff = async (attempt: number) => {
  await sleep(1200 * attempt);
};

const formatStructuredApiDetail = (detail: any): string | null => {
  if (!detail || typeof detail !== 'object') return null;
  const stage = typeof detail.stage === 'string' ? detail.stage.trim() : '';
  const message = typeof detail.message === 'string' ? detail.message.trim() : '';
  if (!stage && !message) return null;

  let suffix = '';
  if (stage === 'assimilation_status' && detail.assimilationStatus && typeof detail.assimilationStatus === 'object') {
    const missing = Array.isArray(detail.assimilationStatus.missing_steps) ? detail.assimilationStatus.missing_steps : [];
    if (missing.length > 0) {
      suffix = ` Missing steps: ${missing.map(slugToTitle).join(', ')}.`;
    }
  }

  if (stage && message) return `${message} [${stage}]${suffix}`;
  return `${message || stage}${suffix}`;
};

const extractApiErrorMessage = (error: unknown, fallback: string): string => {
  if (axios.isAxiosError(error)) {
    const axiosError = error as any;
    const code = String(axiosError?.code || '').trim();
    const method = String(axiosError?.config?.method || 'request').toUpperCase();
    const requestPath = String(axiosError?.config?.url || '').replace(/^\//, '');
    const requestBase = String(axiosError?.config?.baseURL || API_ROOT || API_BASE || '').replace(/\/$/, '');
    const requestUrl = requestPath ? `${requestBase}/${requestPath}` : requestBase || 'backend API';
    const origin = typeof window !== 'undefined' && window.location?.origin ? window.location.origin : 'unknown origin';
    if (code === 'ECONNABORTED') {
      return `Request timed out contacting ${requestUrl} from ${origin} (${method}${code ? `, ${code}` : ''}).`;
    }
    const statusCode = axiosError?.response?.status;
    const detail = axiosError?.response?.data?.detail;
    if (typeof detail === 'string' && detail.trim()) {
      return statusCode ? `${detail} (HTTP ${statusCode})` : detail;
    }
    if (detail && typeof detail === 'object') {
      const structured = formatStructuredApiDetail(detail);
      if (structured) {
        return statusCode ? `${structured} (HTTP ${statusCode})` : structured;
      }
    }
    const message = axiosError?.message;
    if (typeof message === 'string' && message.trim()) {
      if (/network error/i.test(message)) {
        return `Network error contacting ${requestUrl} from ${origin} (${method}${code ? `, ${code}` : ''}). Check backend health and CORS preflight.`;
      }
      return statusCode ? `${message} (HTTP ${statusCode})` : message;
    }
  }
  return fallback;
};

const extractItemIdFromGenerateError = (error: unknown): string | null => {
  if (!axios.isAxiosError(error)) return null;
  const data = error.response?.data as
    | { item_id?: string | null; detail?: { item_id?: string | null } | string }
    | undefined;
  const directItemId = (data?.item_id || '').trim();
  if (directItemId) return directItemId;
  const detail = data?.detail;
  if (detail && typeof detail === 'object' && 'item_id' in detail) {
    const nestedItemId = String((detail as { item_id?: string | null }).item_id || '').trim();
    if (nestedItemId) return nestedItemId;
  }
  return null;
};

const CalibrationLab: React.FC = () => {
  const [drummers, setDrummers] = useState<DrummerListItem[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [filter, setFilter] = useState<'all' | CompletionStatus>('all');
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [detail, setDetail] = useState<DrummerDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [pendingAdjustments, setPendingAdjustments] = useState<Record<string, any> | null>(null);
  const [tab, setTab] = useState<TabId>('adjustments');
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [textDrafts, setTextDrafts] = useState<Record<string, string>>({});
  const [textErrors, setTextErrors] = useState<Record<string, string>>({});
  const [feedbackForm, setFeedbackForm] = useState<{ rating: number; comment: string }>({ rating: 4, comment: '' });
  const [feedbackSubmitting, setFeedbackSubmitting] = useState(false);
  const [health, setHealth] = useState<CalibrationHealth | null>(null);
  const [currentItem, setCurrentItem] = useState<EvaluationItemPayload | null>(null);
  const [itemLoading, setItemLoading] = useState(false);
  const [itemError, setItemError] = useState<string | null>(null);
  const [pendingListeningItemId, setPendingListeningItemId] = useState<string | null>(null);
  const [listeningBusy, setListeningBusy] = useState(false);
  const [reviewerId, setReviewerId] = useState('calibration_auto');
  const [baseGrooveId, setBaseGrooveId] = useState('base_groove');
  const [pairwiseSubmitting, setPairwiseSubmitting] = useState(false);
  const [pairwiseMessage, setPairwiseMessage] = useState<string | null>(null);
  const [laneProgress, setLaneProgress] = useState<ListeningLaneProgress[]>([]);
  const [laneProgressUpdatedAt, setLaneProgressUpdatedAt] = useState<number | null>(null);
  const [listeningProgress, setListeningProgress] = useState<{ active: boolean; label: string; value: number | null }>({
    active: false,
    label: '',
    value: null,
  });
  const artifactPollStateRef = useRef<{ itemId: string | null; attempts: number }>({ itemId: null, attempts: 0 });
  const artifactPollBusyRef = useRef(false);
  const [artifactPollInfo, setArtifactPollInfo] = useState<{ active: boolean; attempts: number; lastCheckedAt: number | null }>(
    {
      active: false,
      attempts: 0,
      lastCheckedAt: null,
    }
  );
  const [pairwiseForm, setPairwiseForm] = useState<PairwiseJudgmentForm>({
    preferred_candidate: '',
    closer_to_target: '',
    better_feel: '',
    more_musical: '',
    confidence: 3,
  });
  const autoQueuedSlugsRef = useRef<Set<string>>(new Set());
  const autoQueueInFlightRef = useRef<Set<string>>(new Set());

  const [debugInfo, setDebugInfo] = useState<{
    apiBase: string;
    drummersUrl: string;
    status?: number;
    contentType?: string | null;
    bodyStart?: string;
    isArray?: boolean;
    length?: number;
    error?: string;
  } | null>(null);

  const loadDrummers = useCallback(async () => {
    setListLoading(true);
    setListError(null);
    try {
      let response: { data: DrummerListItem[] | { value?: DrummerListItem[]; drummers?: DrummerListItem[] } } | null = null;
      let lastError: any = null;
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        try {
          response = await api.get<DrummerListItem[] | { value?: DrummerListItem[]; drummers?: DrummerListItem[] }>('drummers', {
            timeout: 45000,
          });
          break;
        } catch (error: any) {
          lastError = error;
          if (attempt < 3) {
            await sleep(1200 * attempt);
          }
        }
      }
      if (!response) {
        throw lastError ?? new Error('Network Error');
      }
      const raw: any = response.data;
      const items: DrummerListItem[] = Array.isArray(raw)
        ? raw
        : Array.isArray(raw?.value)
        ? raw.value
        : Array.isArray(raw?.drummers)
        ? raw.drummers
        : [];
      setDrummers(items);
      if (items.length && !selectedSlug) {
        const preferred = pickDefaultDrummerSlug(items);
        if (preferred) {
          setSelectedSlug(preferred);
        }
      }
    } catch (error: any) {
      const statusCode = error?.response?.status;
      const detail = error?.response?.data?.detail ?? error?.message ?? 'Network Error';
      if (statusCode) {
        setListError(`Unable to load drummer roster (HTTP ${statusCode}): ${String(detail)}`);
      } else {
        setListError(`Unable to load drummer roster: ${String(detail)}. If the backend just woke up, retry in a few seconds.`);
      }
    } finally {
      setListLoading(false);
    }
  }, [selectedSlug]);

  const loadHealth = useCallback(async () => {
    try {
      const response = await api.get<CalibrationHealth>('health');
      setHealth(response.data);
    } catch (error) {
      setHealth({
        status: 'degraded',
        db_exists: false,
        db_path: null,
        calibration_tables: {},
        notes: ['health_unavailable'],
      });
    }
  }, []);

  const fetchListeningProgress = useCallback(async (itemId: string, options?: { silent?: boolean }) => {
    const normalized = (itemId || '').trim();
    if (!normalized) {
      setLaneProgress([]);
      setLaneProgressUpdatedAt(null);
      return;
    }

    const silent = Boolean(options?.silent);
    try {
      const response = await api.get<ListeningItemProgressPayload>(`evaluation-items/${normalized}/progress`, {
        timeout: LISTENING_ITEM_TIMEOUT_MS,
      });
      const lanes = Array.isArray(response.data?.lanes) ? response.data.lanes : [];
      setLaneProgress(lanes);
      setLaneProgressUpdatedAt(Date.now());

      if (silent) {
        return;
      }

      const pending = lanes.filter((lane) => !lane.ready).map((lane) => lane.lane);
      if (pending.length > 0) {
        setListeningProgress((prev) => ({
          ...prev,
          active: true,
          label: `Rendering lanes: ${pending.join(', ')}`,
          value: null,
        }));
      }
    } catch {
      if (!silent) {
        setLaneProgress([]);
      }
    }
  }, []);

  const loadDetail = useCallback(
    async (slug: string | null, options?: { preserveStatusMessage?: boolean }) => {
      if (!slug) {
        setDetail(null);
        setPendingAdjustments(null);
        return;
      }
      setDetailLoading(true);
      setDetailError(null);
      if (!options?.preserveStatusMessage) {
        setStatusMessage(null);
      }
      try {
        let response: { data: DrummerDetailPayload } | null = null;
        let lastError: unknown = null;
        for (let attempt = 1; attempt <= 3; attempt += 1) {
          try {
            response = await api.get<DrummerDetailPayload>(`drummers/${slug}`, {
              timeout: DRUMMER_DETAIL_TIMEOUT_MS,
            });
            break;
          } catch (error) {
            lastError = error;
            if (attempt < 3 && shouldRetryRequest(error)) {
              await sleepWithBackoff(attempt);
              continue;
            }
            throw error;
          }
        }
        if (!response) {
          throw lastError ?? new Error('Failed to load calibration detail.');
        }
        const payload = response.data;
        const merged: DrummerDetail = {
          ...payload,
          originalAdjustments: { ...payload.adjustments },
        };
        setDetail(merged);
        setPendingAdjustments({ ...payload.adjustments });
        const newDrafts: Record<string, string> = {};
        Object.entries(payload.adjustments).forEach(([key, value]) => {
          if (!NUMBER_FIELDS.has(key)) {
            newDrafts[key] = typeof value === 'string' ? value : JSON.stringify(value, null, 2);
          }
        });
        setTextDrafts(newDrafts);
        setTextErrors({});
      } catch (error) {
        const baseMessage = extractApiErrorMessage(error, 'Failed to load calibration detail.');
        const statusCode = axios.isAxiosError(error) ? error.response?.status : undefined;
        if (statusCode && statusCode >= 500) {
          setDetailError(`${baseMessage} (endpoint: /calibration/drummers/${slug})`);
        } else if (!statusCode) {
          setDetailError(`${baseMessage} (endpoint: /calibration/drummers/${slug})`);
        } else {
          setDetailError(baseMessage);
        }
      } finally {
        setDetailLoading(false);
      }
    },
    []
  );

  const fetchItem = useCallback(async (itemId: string, options?: { silent?: boolean }) => {
    const normalized = (itemId || '').trim();
    const endpoint = `/calibration/evaluation-items/${normalized}`;
    if (!normalized) {
      setCurrentItem(null);
      return;
    }
    const silent = Boolean(options?.silent);
    if (!silent) {
      setItemLoading(true);
    }
    setItemError(null);
    try {
      let response: { data: EvaluationItemPayload } | null = null;
      let lastError: unknown = null;
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        try {
          response = await api.get<EvaluationItemPayload>(`evaluation-items/${normalized}`, {
            timeout: LISTENING_ITEM_TIMEOUT_MS,
          });
          break;
        } catch (error) {
          lastError = error;
          if (attempt < 3 && shouldRetryRequest(error)) {
            await sleepWithBackoff(attempt);
            continue;
          }
          throw error;
        }
      }
      if (!response) {
        throw lastError ?? new Error('Unable to load listening item.');
      }
      setCurrentItem(response.data);
    } catch (error) {
      const baseMessage = extractApiErrorMessage(error, 'Unable to load listening item.');
      const statusCode = axios.isAxiosError(error) ? error.response?.status : undefined;
      if (statusCode && statusCode >= 500) {
        setItemError(`${baseMessage} (endpoint: ${endpoint})`);
      } else if (!statusCode) {
        setItemError(`${baseMessage} (endpoint: ${endpoint})`);
      } else {
        setItemError(baseMessage);
      }
    } finally {
      if (!silent) {
        setItemLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    loadDrummers();
  }, [loadDrummers]);

  useEffect(() => {
    loadHealth();
  }, [loadHealth]);

  useEffect(() => {
    loadDetail(selectedSlug);
  }, [selectedSlug, loadDetail]);

  useEffect(() => {
    if (process.env.NODE_ENV !== 'development') {
      return;
    }
    const liveBase = resolveApiBaseNormalized();
    const url = `${liveBase.replace(/\/$/, '')}/calibration/drummers`;
    fetch(url, { mode: 'cors', cache: 'no-store' })
      .then(async (r) => {
        const text = await r.text();
        let parsed: any;
        let parseErr: string | undefined;
        try {
          parsed = JSON.parse(text);
        } catch (e: any) {
          parseErr = e?.message ? String(e.message) : 'parse_error';
        }
        setDebugInfo({
          apiBase: liveBase,
          drummersUrl: url,
          status: r.status,
          contentType: r.headers.get('content-type'),
          bodyStart: text.slice(0, 240),
          isArray: Array.isArray(parsed),
          length: Array.isArray(parsed) ? parsed.length : undefined,
          error: parseErr,
        });
      })
      .catch((e) => {
        setDebugInfo({ apiBase: liveBase, drummersUrl: url, error: String(e) });
      });
  }, []);

  const filteredDrummers = useMemo(() => {
    if (filter === 'all') return drummers;
    return drummers.filter((entry) => entry.completionStatus.status === filter);
  }, [drummers, filter]);

  const sortedDrummers = useMemo(() => {
    const pool = filteredDrummers.length ? filteredDrummers : drummers;
    return [...pool].sort((a, b) => a.displayName.localeCompare(b.displayName));
  }, [filteredDrummers, drummers]);

  const changedKeys = useMemo(() => {
    if (!detail || !pendingAdjustments) return [];
    return diffKeys(detail.originalAdjustments, pendingAdjustments);
  }, [detail, pendingAdjustments]);

  const hasPendingChanges = changedKeys.length > 0;

  const completion = detail?.completionStatus ?? detail?.metrics?.completion_status;
  const assimilation = detail?.assimilationStatus;
  const missingSteps = assimilation?.missing_steps ?? [];
  const assimilationReady = Boolean(assimilation?.ready_for_calibration);
  const latestRun = detail?.runHistory?.[0] ?? null;
  const readinessHint = missingSteps.length
    ? `Assimilation not ready (${missingSteps.map(slugToTitle).join(', ')}).`
    : 'Assimilation not ready for calibration yet.';

  const handleSelectDrummer = (slug: string) => {
    setSelectedSlug(slug);
    setTab('adjustments');
    setCurrentItem(null);
    setPendingListeningItemId(null);
    setLaneProgress([]);
    setLaneProgressUpdatedAt(null);
    setItemError(null);
    setPairwiseMessage(null);
    setListeningProgress({ active: false, label: '', value: null });
  };

  const handleNumberChange = (key: string, raw: string) => {
    if (!pendingAdjustments) return;
    const value = sanitizeNumber(key, raw);
    if (value == null) return;
    setPendingAdjustments((prev) => (prev ? { ...prev, [key]: value } : prev));
  };

  const handleTextDraftChange = (key: string, value: string) => {
    setTextDrafts((prev) => ({ ...prev, [key]: value }));
  };

  const handleTextDraftCommit = (key: string) => {
    const draft = textDrafts[key];
    try {
      const parsed = JSON.parse(draft);
      setPendingAdjustments((prev) => (prev ? { ...prev, [key]: parsed } : prev));
      setTextErrors((prev) => {
        const next = { ...prev };
        delete next[key];
        return next;
      });
    } catch (error) {
      setTextErrors((prev) => ({ ...prev, [key]: 'Invalid JSON. Ensure objects and arrays are well-formed.' }));
    }
  };

  const handleQueueListeningItem = useCallback(async (): Promise<boolean> => {
    if (!selectedSlug) return false;
    let queuedSuccessfully = false;
    setListeningBusy(true);
    setItemError(null);
    setPairwiseMessage(null);
    setPendingListeningItemId(null);
    setLaneProgress([]);
    setLaneProgressUpdatedAt(null);
    setListeningProgress({ active: true, label: 'Queueing listening item...', value: 0.05 });
    if (!assimilationReady) {
      setPairwiseMessage(`${readinessHint} Trying server-side queue anyway to verify latest readiness.`);
    }
    try {
      const requestPayload = {
        base_groove_id: baseGrooveId,
        target_drummer_slug: selectedSlug,
        candidate_count: 2,
        include_baseline: true,
        strict_reference_baseline: false,
        wait_for_all_artifacts: false,
        artifact_wait_timeout_sec: 30,
        artifact_poll_interval_ms: 1500,
        reviewer_id: reviewerId || `calibration_auto_${selectedSlug}`,
      };

      let response: { data: GenerateCandidatesResponse } | null = null;
      let lastError: unknown = null;
      for (let attempt = 1; attempt <= 6; attempt += 1) {
        setListeningProgress({
          active: true,
          label: `Queueing listening item (${attempt}/6)...`,
          value: 0.05 + (attempt / 6) * 0.25,
        });
        try {
          response = await api.post<GenerateCandidatesResponse>('generate-candidates', requestPayload, {
            timeout: LISTENING_QUEUE_TIMEOUT_MS,
          });
          break;
        } catch (error) {
          lastError = error;
          const recoveredItemId = extractItemIdFromGenerateError(error);
          if (recoveredItemId) {
            response = {
              data: {
                status: 'queued',
                run_ids: [],
                item_id: recoveredItemId,
              },
            };
            setListeningProgress({
              active: true,
              label: 'Queue request timed out, but item was created. Tracking render progress...',
              value: 0.3,
            });
            break;
          }
          if (attempt < 6 && shouldRetryRequest(error)) {
            setListeningProgress((prev) => ({
              ...prev,
              active: true,
              label: `Queue attempt ${attempt} failed. Retrying...`,
            }));
            await sleepWithBackoff(attempt);
            continue;
          }
          throw error;
        }
      }
      if (!response) {
        throw lastError ?? new Error('Unable to queue listening item.');
      }

      const nextItemId = response.data.item_id;
      if (nextItemId) {
        queuedSuccessfully = true;
        setPendingListeningItemId(nextItemId);
        let hydratedItem: EvaluationItemPayload | null = null;
        const totalHydrationAttempts = LISTENING_ITEM_READY_RETRIES + LISTENING_ARTIFACT_READY_RETRIES;
        for (let attempt = 1; attempt <= totalHydrationAttempts; attempt += 1) {
          setListeningProgress({
            active: true,
            label: `Preparing listening drum tracks (${attempt}/${totalHydrationAttempts})...`,
            value: 0.3 + (attempt / totalHydrationAttempts) * 0.6,
          });
          try {
            const itemResponse = await api.get<EvaluationItemPayload>(`evaluation-items/${nextItemId}`, {
              timeout: LISTENING_ITEM_TIMEOUT_MS,
            });
            hydratedItem = itemResponse.data;
            await fetchListeningProgress(nextItemId, { silent: true });
            if (hasPlayableArtifacts(hydratedItem)) {
              break;
            }

            if (attempt < totalHydrationAttempts) {
              await sleep(LISTENING_ARTIFACT_READY_DELAY_MS);
              continue;
            }

            break;
          } catch (error) {
            const statusCode = axios.isAxiosError(error) ? error.response?.status : undefined;
            const isLastAttempt = attempt === totalHydrationAttempts;
            if (statusCode === 404 && !isLastAttempt) {
              await sleep(LISTENING_ITEM_READY_DELAY_MS);
              continue;
            }
            if (!isLastAttempt && shouldRetryRequest(error)) {
              await sleepWithBackoff(attempt);
              continue;
            }
            throw error;
          }
        }

        if (hydratedItem) {
          setCurrentItem(hydratedItem);
          setPendingListeningItemId(null);
          const hasAudio = hasPlayableArtifacts(hydratedItem);
          setItemError(hasAudio ? null : 'Listening item is ready, but drum tracks are still rendering. Try Refresh Detail in a few seconds.');
          setListeningProgress({ active: false, label: '', value: null });
          setPairwiseMessage(
            hasAudio
              ? 'Listening item queued. Review baseline vs A/B and submit judgment.'
              : 'Listening item queued. Drum track render is still in progress.'
          );
        } else {
          setCurrentItem((prev) => prev);
          setItemError('Listening item queued, but drum tracks are still preparing. Retry in a few seconds.');
          setPairwiseMessage('Listening item queued. Drum tracks are still processing in the background.');
          setListeningProgress({ active: true, label: 'Rendering drum tracks...', value: null });
        }
      } else {
        setPairwiseMessage('Candidates queued, but no evaluation item was created.');
        setPendingListeningItemId(null);
        setListeningProgress({ active: false, label: '', value: null });
      }
    } catch (error) {
      const baseMessage = extractApiErrorMessage(error, 'Unable to queue listening item. Check backend logs and retry.');
      const statusCode = axios.isAxiosError(error) ? error.response?.status : undefined;
      if (statusCode && statusCode >= 500) {
        setItemError(`${baseMessage} (endpoint: /calibration/generate-candidates)`);
      } else if (!statusCode) {
        setItemError(`${baseMessage} (endpoint: /calibration/generate-candidates)`);
      } else {
        setItemError(baseMessage);
      }
      setPendingListeningItemId(null);
      setListeningProgress({ active: false, label: '', value: null });
      return false;
    } finally {
      await Promise.allSettled([loadDetail(selectedSlug, { preserveStatusMessage: true }), loadDrummers()]);
      setListeningBusy(false);
    }
    return queuedSuccessfully;
  }, [selectedSlug, assimilationReady, readinessHint, baseGrooveId, reviewerId, loadDetail, loadDrummers, fetchListeningProgress]);

  useEffect(() => {
    if (!selectedSlug || detailLoading || listeningBusy || !detail) return;
    if (detail.slug !== selectedSlug) return;
    if (!assimilationReady) return;
    if (pendingListeningItemId) return;
    if (currentItem?.target_drummer_slug === selectedSlug) return;
    if (autoQueuedSlugsRef.current.has(selectedSlug)) return;
    if (autoQueueInFlightRef.current.has(selectedSlug)) return;

    autoQueueInFlightRef.current.add(selectedSlug);
    setPairwiseMessage('Auto-loading listening drum tracks for selected drummer...');
    void (async () => {
      const succeeded = await handleQueueListeningItem();
      autoQueueInFlightRef.current.delete(selectedSlug);
      if (succeeded) {
        autoQueuedSlugsRef.current.add(selectedSlug);
      } else {
        setPairwiseMessage('Auto-load could not queue drum tracks yet. Queue Listening Item to try again.');
      }
    })();
  }, [selectedSlug, detail, detailLoading, listeningBusy, assimilationReady, pendingListeningItemId, currentItem, handleQueueListeningItem]);

  const handlePairwiseSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!currentItem) return;
    if (!pairwiseForm.preferred_candidate || !pairwiseForm.closer_to_target || !pairwiseForm.better_feel || !pairwiseForm.more_musical) {
      setPairwiseMessage('Select all judgment choices before submitting.');
      return;
    }
    setPairwiseSubmitting(true);
    setPairwiseMessage(null);
    try {
      await api.post(`evaluation-items/${currentItem.item_id}/judgment`, {
        preferred_candidate: pairwiseForm.preferred_candidate,
        closer_to_target: pairwiseForm.closer_to_target,
        better_feel: pairwiseForm.better_feel,
        more_musical: pairwiseForm.more_musical,
        confidence: pairwiseForm.confidence,
      });
      setPairwiseMessage('Judgment saved.');
    } catch (error) {
      setPairwiseMessage('Unable to save judgment.');
    } finally {
      setPairwiseSubmitting(false);
    }
  };

  const handleSave = async () => {
    if (!pendingAdjustments || !selectedSlug) return;
    setSaving(true);
    setStatusMessage(null);
    try {
      await api.post(`drummers/${selectedSlug}/adjustments`, {
        adjustments: pendingAdjustments,
      });
      setStatusMessage('Adjustments saved. Regenerate to validate the new feel.');
      await Promise.all([loadDetail(selectedSlug, { preserveStatusMessage: true }), loadDrummers()]);
    } catch (error) {
      setStatusMessage('Failed to save adjustments. Review your changes and retry.');
    } finally {
      setSaving(false);
    }
  };

  const handleGenerate = async () => {
    if (!selectedSlug) return;
    if (!assimilationReady) {
      setStatusMessage(`${readinessHint} Trying server-side launch anyway to verify latest readiness.`);
    }
    setTab('metrics');
    setGenerating(true);
    setStatusMessage('Launching calibration run...');
    try {
      let response: { data: GenerateRunResponse } | null = null;
      let lastError: unknown = null;
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        setStatusMessage(`Launching calibration run (attempt ${attempt}/3)...`);
        try {
          response = await api.post<GenerateRunResponse>(`drummers/${selectedSlug}/generate`, undefined, { timeout: 15000 });
          break;
        } catch (error) {
          lastError = error;
          if (attempt < 3 && shouldRetryRequest(error)) {
            setStatusMessage(`Launch attempt ${attempt} failed; retrying...`);
            await sleepWithBackoff(attempt);
            continue;
          }
          throw error;
        }
      }
      if (!response) {
        throw lastError ?? new Error('Failed to trigger generation.');
      }

      const runId = (response.data?.run_id || '').trim();

      setStatusMessage('Generation queued. Waiting for backend completion...');
      await Promise.all([loadDetail(selectedSlug, { preserveStatusMessage: true }), loadDrummers()]);

      if (runId) {
        let terminalRun: CalibrationRun | null = null;
        for (let attempt = 0; attempt < 12; attempt += 1) {
          await sleep(2000);
          let detailResponse: { data: DrummerDetailPayload };
          try {
            detailResponse = await api.get<DrummerDetailPayload>(`drummers/${selectedSlug}`);
          } catch (pollError) {
            if (attempt < 11 && shouldRetryRequest(pollError)) {
              continue;
            }
            throw pollError;
          }
          const runs = detailResponse.data.runHistory ?? [];
          const candidate = runs.find((run) => run.id === runId);
          if (candidate && candidate.outcome !== 'pending') {
            terminalRun = candidate;
            break;
          }
        }

        if (terminalRun?.outcome === 'failure') {
          setStatusMessage(terminalRun.error_message || 'Calibration run failed. Check backend logs and retry.');
        } else if (terminalRun?.outcome === 'success') {
          setStatusMessage('Calibration run completed successfully. For baseline/A-B audio, click "Queue Listening Item".');
        } else {
          setStatusMessage('Generation queued. Still processing in background; use Refresh Detail in a few seconds. For baseline/A-B audio, click "Queue Listening Item".');
        }
      } else {
        setStatusMessage('Generation queued. Refresh for updated metrics once the run completes. For baseline/A-B audio, click "Queue Listening Item".');
      }

      await Promise.all([loadDetail(selectedSlug, { preserveStatusMessage: true }), loadDrummers()]);
    } catch (error) {
      setStatusMessage(extractApiErrorMessage(error, 'Failed to trigger generation. Check backend logs for details.'));
    } finally {
      setGenerating(false);
    }
  };

  const handleFeedbackSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedSlug) return;
    if (!feedbackForm.comment.trim()) {
      setStatusMessage('Add a comment before submitting feedback.');
      return;
    }
    setFeedbackSubmitting(true);
    setStatusMessage(null);
    try {
      await api.post('feedback', {
        drummer: selectedSlug,
        rating: feedbackForm.rating,
        comment: feedbackForm.comment.trim(),
      });
      setFeedbackForm({ rating: 4, comment: '' });
      setStatusMessage('Feedback submitted. Thanks for the perspective!');
      await loadDetail(selectedSlug);
    } catch (error) {
      setStatusMessage('Unable to send feedback. Please try again.');
    } finally {
      setFeedbackSubmitting(false);
    }
  };

  const rollupShares = useMemo(() => asShareMap(detail?.rollupTargets?.instrument_shares), [detail]);
  const actualShares = useMemo(() => asShareMap(detail?.metrics?.instrument_category_shares), [detail]);
  const hasCurrentPlayableArtifacts = useMemo(() => hasPlayableArtifacts(currentItem), [currentItem]);
  const activeListeningItemId = currentItem?.item_id ?? pendingListeningItemId;
  const listeningProgressValue = useMemo(() => {
    if (listeningProgress.value != null) {
      return Math.min(1, Math.max(0, listeningProgress.value));
    }
    if (currentItem && !hasCurrentPlayableArtifacts) {
      return Math.min(0.95, 0.4 + artifactPollInfo.attempts * 0.03);
    }
    return 0;
  }, [listeningProgress.value, currentItem, hasCurrentPlayableArtifacts, artifactPollInfo.attempts]);
  const effectiveListeningLabel = useMemo(() => {
    const stalled = laneProgress.filter((lane) => !lane.ready && lane.stalled_in_queue).map((lane) => lane.lane);
    if (stalled.length > 0) {
      return `Render queue appears stalled: ${stalled.join(', ')}. Check backend worker logs.`;
    }

    const queued = laneProgress.filter((lane) => !lane.ready && String(lane.run_outcome || '').toLowerCase() === 'queued').map((lane) => lane.lane);
    if (queued.length > 0) {
      return `Rendering lanes: ${queued.join(', ')} (queued)`;
    }

    return listeningProgress.label || 'Rendering drum tracks...';
  }, [laneProgress, listeningProgress.label]);
  const showListeningProgress =
    listeningProgress.active || itemLoading || Boolean(pendingListeningItemId) || Boolean(currentItem && !hasCurrentPlayableArtifacts);

  useEffect(() => {
    const currentItemId = activeListeningItemId ?? null;
    if (artifactPollStateRef.current.itemId !== currentItemId) {
      artifactPollStateRef.current = { itemId: currentItemId, attempts: 0 };
      setArtifactPollInfo({ active: false, attempts: 0, lastCheckedAt: null });
    }

    if (!currentItemId || (hasCurrentPlayableArtifacts && !pendingListeningItemId) || listeningBusy) {
      setArtifactPollInfo((prev) => (prev.active ? { ...prev, active: false } : prev));
      return;
    }

    setArtifactPollInfo((prev) => ({ ...prev, active: true }));

    const timerId = window.setInterval(async () => {
      if (artifactPollBusyRef.current) {
        return;
      }
      if (artifactPollStateRef.current.itemId !== currentItemId) {
        return;
      }
      artifactPollStateRef.current.attempts += 1;
      setArtifactPollInfo({
        active: true,
        attempts: artifactPollStateRef.current.attempts,
        lastCheckedAt: Date.now(),
      });
      artifactPollBusyRef.current = true;
      try {
        await fetchItem(currentItemId, { silent: true });
        await fetchListeningProgress(currentItemId, { silent: true });
      } finally {
        artifactPollBusyRef.current = false;
      }
    }, LISTENING_ARTIFACT_READY_DELAY_MS);

    return () => {
      window.clearInterval(timerId);
      setArtifactPollInfo((prev) => (prev.active ? { ...prev, active: false } : prev));
    };
  }, [activeListeningItemId, pendingListeningItemId, hasCurrentPlayableArtifacts, listeningBusy, fetchItem, fetchListeningProgress]);

  useEffect(() => {
    if (hasCurrentPlayableArtifacts) {
      setPendingListeningItemId(null);
      setListeningProgress((prev) => (prev.active ? { active: false, label: '', value: null } : prev));
    }
  }, [hasCurrentPlayableArtifacts]);

  const metricsRows = useMemo(
    () => [
      {
        key: 'velocity_mean',
        label: 'Velocity Mean',
        actual: detail?.metrics?.velocity_mean,
        target: detail?.rollupTargets?.velocity_mean,
      },
      {
        key: 'velocity_std',
        label: 'Velocity Std',
        actual: detail?.metrics?.velocity_std,
        target: detail?.rollupTargets?.velocity_std,
      },
      {
        key: 'micro_timing_std',
        label: 'Micro Timing Std (ms)',
        actual: detail?.metrics?.micro_timing_std,
        target: detail?.rollupTargets?.timing_std_ms,
      },
      {
        key: 'fills_per_minute',
        label: 'Fills per Minute',
        actual: detail?.metrics?.fills_per_minute,
        target: detail?.rollupTargets?.fills_per_min,
      },
      {
        key: 'ghost_ratio',
        label: 'Ghost Ratio',
        actual: detail?.metrics?.ghost_ratio,
        target: detail?.rollupTargets?.ghost_ratio,
      },
      {
        key: 'fill_ratio',
        label: 'Fill Ratio',
        actual: detail?.metrics?.fill_ratio,
        target: detail?.rollupTargets?.fill_ratio,
      },
    ],
    [detail]
  );

  const artifactGroups = useMemo(() => {
    const map = currentItem?.artifact_map || {};
    const baselineEntries = [...(map.baseline || [])];
    const baselineFallback = currentItem?.baseline_reference_audio_url?.trim();
    if (baselineEntries.length === 0 && baselineFallback) {
      baselineEntries.push({
        artifact_id: `baseline-ref-${currentItem?.item_id || 'unknown'}`,
        run_id: currentItem?.baseline_run_id || null,
        artifact_type: 'reference_song',
        storage_uri: baselineFallback,
        public_url: baselineFallback,
        render_recipe: { generated: 'baseline_reference_fallback' },
      });
    }
    const baselineLabel = currentItem?.baseline_label ? `Baseline Drum Track · ${currentItem.baseline_label}` : 'Baseline Drum Track';
    const groups: Array<{ key: string; label: string; entries: AudioArtifactPayload[] }> = [
      { key: 'baseline', label: baselineLabel, entries: baselineEntries },
      { key: 'A', label: 'A Drum Track', entries: map.A || [] },
      { key: 'B', label: 'B Drum Track', entries: map.B || [] },
    ];

    Object.entries(map).forEach(([label, entries]) => {
      if (label === 'baseline' || label === 'A' || label === 'B') return;
      groups.push({ key: label, label: `${label} Drum Track`, entries: entries || [] });
    });

    return groups;
  }, [currentItem]);

  const sourceAnalysisId = useMemo(() => {
    if (!currentItem?.artifact_map) return null;
    const candidateArtifact = currentItem.artifact_map.A?.[0] || currentItem.artifact_map.B?.[0];
    const recipe = candidateArtifact?.render_recipe as Record<string, unknown> | undefined;
    const sourceAnalysis = recipe?.source_analysis_id;
    if (typeof sourceAnalysis === 'string' && sourceAnalysis.trim()) {
      return sourceAnalysis.trim();
    }
    return null;
  }, [currentItem]);

  const sourceAnalysisUrl = useMemo(() => {
    if (!sourceAnalysisId) return null;
    const apiBase = resolveApiBaseNormalized() || 'http://localhost:8000';
    return `${apiBase.replace(/\/$/, '')}/calibration/analysis/${encodeURIComponent(sourceAnalysisId)}`;
  }, [sourceAnalysisId]);

  return (
    <div className="min-h-screen bg-[#09031a] text-white">
      <header className="relative overflow-hidden border-b border-purple-500/20 bg-gradient-to-br from-purple-950/80 via-purple-900/40 to-amber-900/20">
        <div className="absolute inset-0 opacity-40">
          <div className="absolute -top-24 -left-24 h-64 w-64 rounded-full bg-purple-700 blur-3xl" />
          <div className="absolute bottom-0 right-0 h-72 w-72 rounded-full bg-amber-500 blur-[120px]" />
        </div>
        <div className="relative mx-auto flex max-w-6xl flex-col gap-6 px-6 py-14 md:flex-row md:items-center md:justify-between">
          <div className="max-w-3xl">
            <h1 className="text-5xl font-black leading-tight md:text-7xl">Drummer Calibration Lab</h1>
            <p className="mt-4 text-2xl font-bold leading-tight md:text-4xl">
              <span className="text-amber-300">Human feel</span> with <span className="text-amber-300">AI precision</span>
            </p>
            <p className="mt-4 text-base text-purple-100/70 md:text-lg">
              Review the drum tracks created with assimilated data to the original baseline track and tune the
              assimilation knobs to indicate how the assimilation needs to be modified to improve the model.
            </p>
            <div className="mt-6 flex flex-wrap gap-3 text-xs text-purple-100/70">
              <span className="inline-flex items-center gap-2 rounded-full border border-purple-500/40 bg-purple-500/10 px-3 py-1">
                <Sparkles className="h-3 w-3 text-amber-300" />
                Collaborative calibration ready
              </span>
              <span className="inline-flex items-center gap-2 rounded-full border border-purple-500/40 bg-purple-500/10 px-3 py-1">
                <Gauge className="h-3 w-3 text-amber-200" />
                Metric tolerance target ±10%
              </span>
              <span className="inline-flex max-w-full items-center gap-2 rounded-full border border-amber-400/40 bg-amber-500/10 px-3 py-1 text-amber-100">
                <Activity className="h-3 w-3 text-amber-300" />
                <span className="font-semibold">API</span>
                <span className="max-w-[340px] truncate font-mono text-[11px]">{API_BASE}</span>
              </span>
            </div>
          </div>
          <div className="flex flex-col gap-3 text-sm text-purple-100/80">
            <div className="rounded-2xl border border-purple-500/40 bg-purple-500/10 p-5 shadow-2xl shadow-purple-950/30">
              <p className="text-xs uppercase tracking-[0.4em] text-purple-200/70">Workflow Snapshot</p>
              <ul className="mt-3 space-y-2 text-sm">
                <li className="flex items-center gap-2">
                  <Sparkles className="h-4 w-4 text-amber-300" /> Adjust feel & kit balance
                </li>
                <li className="flex items-center gap-2">
                  <RefreshCcw className="h-4 w-4 text-purple-200" /> Trigger new calibration run
                </li>
                <li className="flex items-center gap-2">
                  <Users className="h-4 w-4 text-purple-200" /> Capture pro drummer feedback
                </li>
              </ul>
            </div>
            {statusMessage && (
              <div className="rounded-xl border border-purple-400/40 bg-purple-500/10 p-4 text-xs text-purple-100">
                {statusMessage}
              </div>
            )}
            {health && (
              <div className="rounded-xl border border-purple-400/40 bg-purple-500/10 p-4 text-xs text-purple-100">
                <div className="flex items-center justify-between gap-3">
                  <span className="font-semibold uppercase tracking-[0.2em]">Backend Health</span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase ${
                      health.status === 'ok' ? 'bg-emerald-500/25 text-emerald-100' : 'bg-rose-500/25 text-rose-100'
                    }`}
                  >
                    {health.status}
                  </span>
                </div>
                <p className="mt-2 break-all text-[11px] text-purple-100/80">DB: {health.db_path || 'unknown'}</p>
              </div>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-6xl gap-10 px-6 py-12 lg:grid-cols-[320px,1fr]">
        <aside className="space-y-8">
          <section className="rounded-3xl border border-purple-500/30 bg-purple-900/20 p-6">
            <div className="flex items-center gap-3 text-sm font-medium uppercase tracking-[0.3em] text-purple-200">
              <Activity className="h-4 w-4 text-amber-300" /> Status Filters
            </div>
            <div className="mt-4 flex flex-wrap gap-2">
              {STATUS_FILTERS.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  onClick={() => setFilter(option.id)}
                  className="rounded-full px-3.5 py-1.5 text-xs font-semibold transition"
                >
                  {option.label}
                </button>
              ))}
            </div>
          </section>

          {debugInfo && (
            <div className="rounded-2xl border border-amber-500/30 bg-amber-500/10 p-3 text-[11px] text-amber-100/90">
              <div>API Base: <span className="break-all">{debugInfo.apiBase}</span></div>
              <div>URL: <span className="break-all">{debugInfo.drummersUrl}</span></div>
              {typeof debugInfo.status !== 'undefined' && (
                <div>Status: {debugInfo.status} · {debugInfo.contentType || 'no content-type'}</div>
              )}
              {typeof debugInfo.isArray !== 'undefined' && (
                <div>Detected Array: {String(debugInfo.isArray)} {typeof debugInfo.length !== 'undefined' ? `· length ${debugInfo.length}` : ''}</div>
              )}
              {debugInfo.error && <div className="text-rose-200">Error: {debugInfo.error}</div>}
              {debugInfo.bodyStart && (
                <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap break-all text-[10px] text-amber-200/80">{debugInfo.bodyStart}</pre>
              )}
            </div>
          )}

          <section className="space-y-4">
            {listLoading ? (
              <div className="rounded-3xl border border-purple-500/20 bg-purple-900/10 p-6 text-sm text-purple-100/70">
                Loading roster…
              </div>
            ) : listError ? (
              <div className="rounded-3xl border border-rose-500/30 bg-rose-500/10 p-6 text-sm text-rose-100/90">
                {listError}
              </div>
            ) : sortedDrummers.length === 0 ? (
              <div className="rounded-3xl border border-purple-500/30 bg-purple-900/10 p-6 text-sm text-purple-100/70">
                No drummers match the current filter.
              </div>
            ) : (
              sortedDrummers.map((entry) => (
                <button
                  key={entry.slug}
                  type="button"
                  onClick={() => handleSelectDrummer(entry.slug)}
                  className="w-full rounded-3xl border px-5 py-4 text-left transition hover:-translate-y-1 hover:border-amber-300/60 hover:shadow-lg hover:shadow-purple-900/30"
                >
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-sm font-semibold uppercase tracking-[0.3em] text-purple-200/90">
                        {entry.displayName}
                      </p>
                      <p className="mt-1 text-xs text-purple-100/70">
                        {entry.headline || 'Assimilation profile alignment'}
                      </p>
                    </div>
                    <CompletionBadge
                      status={entry.completionStatus.status}
                      ratio={entry.completionStatus.completion_ratio}
                    />
                  </div>
                  <div className="mt-4 flex flex-wrap gap-3 text-[11px] text-purple-100/60">
                    <span className="inline-flex items-center gap-1 rounded-full border border-purple-500/30 bg-purple-500/10 px-2 py-0.5">
                      <Activity className="h-3 w-3 text-amber-300" />
                      {entry.assimilationStatus?.ready_for_calibration ? 'Ready for calibration' : 'Needs processing'}
                    </span>
                    <span className="inline-flex items-center gap-1 rounded-full border border-purple-500/30 bg-purple-500/10 px-2 py-0.5">
                      <Gauge className="h-3 w-3 text-amber-300" />
                      {entry.metricsWithin ?? 0}/{entry.metricsCompared ?? 0} metrics in tolerance
                    </span>
                    <span className="inline-flex items-center gap-1 rounded-full border border-purple-500/30 bg-purple-500/10 px-2 py-0.5">
                      <RefreshCcw className="h-3 w-3" />
                      {formatDate(entry.latestRunAt)}
                    </span>
                  </div>
                </button>
              ))
            )}
          </section>

          <section className="rounded-3xl border border-purple-500/30 bg-purple-900/20 p-6">
            <div className="flex items-center gap-2 text-xs uppercase tracking-[0.4em] text-purple-200/80">
              <ClipboardList className="h-4 w-4 text-amber-300" /> Contributor Guide
            </div>
            <p className="mt-4 text-sm text-purple-100/70">
              Calibrators, follow this flow for meaningful tweaks:
            </p>
            <ol className="mt-4 space-y-3 text-sm text-purple-100/80">
              <li>
                <span className="font-semibold text-amber-200">1.</span> Skim the status grid and choose the drummer with
                the lowest completion ratio.
              </li>
              <li>
                <span className="font-semibold text-amber-200">2.</span> In <em>Adjustments</em>, move one control family at a
                time. Start with kit balance, then feel, then fills.
              </li>
              <li>
                <span className="font-semibold text-amber-200">3.</span> Hover the info labels to understand the musical impact. Log why you changed each control in the notes box below the sliders.
              </li>
              <li>
                <span className="font-semibold text-amber-200">4.</span> Launch a calibration run. Wait for metrics to land,
                then compare target vs actual in the <em>Metrics</em> tab.
              </li>
              <li>
                <span className="font-semibold text-amber-200">5.</span> Share qualitative feedback in the <em>Feedback</em> tab—focus on feel, fills, and balance observations.
              </li>
            </ol>
            <p className="mt-4 text-xs italic text-purple-100/60">
              Producers: once a profile holds =80% metrics within tolerance and external feedback is positive, flip the
              completion status to “ready” in the backend.
            </p>
          </section>
        </aside>

        <section className="space-y-8">
          <div className="rounded-3xl border border-purple-500/30 bg-purple-900/20 p-6">
            {detailLoading ? (
              <div className="text-sm text-purple-100/70">Loading calibration detail…</div>
            ) : detailError ? (
              <div className="text-sm text-rose-100/80">{detailError}</div>
            ) : !detail ? (
              <div className="text-sm text-purple-100/70">Select a drummer to begin calibration.</div>
            ) : (
              <div className="space-y-6">
                <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
                  <div>
                    <p className="text-xs uppercase tracking-[0.5em] text-purple-200/70">Current Drummer</p>
                    <div className="mt-2 flex items-center gap-3">
                      <h2 className="text-2xl font-semibold text-white">{detail.displayName}</h2>
                      {completion && (
                        <CompletionBadge status={completion.status} ratio={completion.completion_ratio} />
                      )}
                    </div>
                    <p className="mt-2 text-xs text-purple-100/70">
                      Last run: {formatDate(detail?.runHistory?.[0]?.started_at)} • Note count:{' '}
                      {detail?.metrics?.note_count ?? '—'}
                    </p>
                    <p className="mt-2 text-xs text-purple-100/70">
                      Assimilation: {assimilation?.ready_for_calibration ? 'Ready for calibration' : 'Needs processing'}
                    </p>
                    <p className="mt-1 text-xs text-purple-100/70">
                      Latest run: {latestRun ? `${describeRunOutcome(latestRun)} (${formatDate(latestRun.started_at)})` : 'No runs yet'}
                    </p>
                    {!assimilation?.ready_for_calibration && missingSteps.length > 0 && (
                      <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-amber-100">
                        {missingSteps.map((step) => (
                          <span key={step} className="rounded-full border border-amber-300/40 bg-amber-500/15 px-2 py-0.5">
                            {slugToTitle(step)}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                  <div className="flex flex-wrap gap-3">
                    <button
                      type="button"
                      onClick={handleSave}
                      disabled={!hasPendingChanges || saving}
                      className="inline-flex items-center gap-2 rounded-full px-4 py-2 text-sm font-semibold transition"
                    >
                      <Save className="h-4 w-4" /> {saving ? 'Saving…' : 'Save Adjustments'}
                    </button>
                    <button
                      type="button"
                      onClick={handleGenerate}
                      disabled={generating}
                      className="inline-flex items-center gap-2 rounded-full border border-amber-400/60 bg-amber-500/20 px-4 py-2 text-sm font-semibold text-amber-100 transition hover:bg-amber-500/30"
                    >
                      <RefreshCcw className="h-4 w-4" />
                      {generating ? 'Launching…' : assimilationReady ? 'Run Metrics Calibration' : 'Run Blocked'}
                    </button>
                    <button
                      type="button"
                      onClick={handleQueueListeningItem}
                      disabled={listeningBusy || !selectedSlug}
                      className="inline-flex items-center gap-2 rounded-full border border-emerald-400/60 bg-emerald-500/20 px-4 py-2 text-sm font-semibold text-emerald-100 transition hover:bg-emerald-500/30"
                    >
                      <Headphones className="h-4 w-4" />
                      {listeningBusy ? 'Queuing…' : assimilationReady ? 'Queue Listening Item' : 'Listening Blocked'}
                    </button>
                    <button
                      type="button"
                      onClick={() => loadDetail(selectedSlug)}
                      className="inline-flex items-center gap-2 rounded-full border border-purple-500/30 bg-purple-900/30 px-4 py-2 text-sm text-purple-100"
                    >
                      <ArrowRight className="h-4 w-4" /> Refresh Detail
                    </button>
                  </div>
                  <p className="text-[11px] text-purple-100/70">
                    Tip: <span className="font-semibold text-purple-100">Run Metrics Calibration</span> updates metrics/run history. Use
                    <span className="font-semibold text-emerald-200"> Queue Listening Item</span> to generate baseline, A, and B drum tracks.
                  </p>
                  {statusMessage && (
                    <div className="rounded-xl border border-purple-400/40 bg-purple-500/10 p-3 text-xs text-purple-100">
                      {statusMessage}
                    </div>
                  )}
                </div>

                {hasPendingChanges && (
                  <div className="rounded-2xl border border-amber-400/40 bg-amber-500/10 p-4 text-xs text-amber-100">
                    <p className="font-semibold uppercase tracking-[0.3em]">Pending Changes</p>
                    <ul className="mt-2 grid gap-2 md:grid-cols-2">
                      {changedKeys.map((key) => (
                        <li key={key} className="rounded-xl bg-purple-900/30 p-3">
                          <p className="text-[11px] uppercase tracking-[0.3em] text-purple-200/80">{labelize(key)}</p>
                          <p className="mt-1 text-xs text-purple-100/80">
                            from <span className="font-mono text-purple-200">{JSON.stringify(detail.originalAdjustments[key])}</span>
                            <br />to{' '}
                            <span className="font-mono text-amber-200">{JSON.stringify(pendingAdjustments?.[key])}</span>
                          </p>
                        </li>
                      ))}
                    </ul>
                  </div>
                )}

                <div className="grid gap-6">
                  <div className="order-2 space-y-4 lg:max-h-[58vh] lg:overflow-y-auto lg:pr-1">
                    <div className="rounded-2xl border border-purple-500/20 bg-purple-900/20 p-4">
                      <p className="text-[11px] font-semibold uppercase tracking-[0.28em] text-purple-200/80">Calibration Controls</p>
                      <div className="mt-3 grid gap-3">
                        <label className="text-xs text-emerald-100">
                          Reviewer ID
                          <input
                            type="text"
                            value={reviewerId}
                            onChange={(event) => setReviewerId(event.target.value)}
                            className="mt-2 w-full rounded-lg border border-emerald-500/30 bg-emerald-950/40 px-3 py-2 text-xs text-emerald-50"
                          />
                        </label>
                        <label className="text-xs text-emerald-100">
                          Base Groove ID
                          <input
                            type="text"
                            value={baseGrooveId}
                            onChange={(event) => setBaseGrooveId(event.target.value)}
                            className="mt-2 w-full rounded-lg border border-emerald-500/30 bg-emerald-950/40 px-3 py-2 text-xs text-emerald-50"
                          />
                        </label>
                        <div className="flex flex-wrap gap-2 pt-1">
                          {(['adjustments', 'metrics', 'feedback'] as TabId[]).map((id) => (
                            <button
                              key={id}
                              type="button"
                              onClick={() => setTab(id)}
                              className="rounded-full px-3 py-1.5 text-[11px] font-semibold transition"
                            >
                              {labelize(id)}
                            </button>
                          ))}
                        </div>
                      </div>
                    </div>

                {tab === 'adjustments' && pendingAdjustments && (
                  <div className="space-y-6">
                    {FIELD_GROUPS.map((group) => (
                      <div key={group.title} className="rounded-3xl border border-purple-500/20 bg-purple-900/10 p-5">
                        <div className="flex flex-col gap-2 sm:flex-row sm:items-baseline sm:justify-between">
                          <div>
                            <p className="text-sm font-semibold text-white">{group.title}</p>
                            <p className="text-xs text-purple-100/70">{group.blurb}</p>
                          </div>
                        </div>
                        <div className="mt-4 space-y-4">
                          <div className="grid gap-3 sm:grid-cols-2">
                            {group.keys
                              .filter((key) => FEEL_KNOB_KEYS.has(key) && Boolean(CONTROL_BOUNDS[key]))
                              .map((key) => {
                                const meta = CONTROL_BOUNDS[key];
                                const value = Number(pendingAdjustments[key]);
                                const help = detail.metadata?.field_help?.[key];
                                return (
                                  <AdjustmentKnob
                                    key={key}
                                    label={labelize(key)}
                                    value={Number.isFinite(value) ? value : meta.min}
                                    min={meta.min}
                                    max={meta.max}
                                    step={meta.step}
                                    help={help}
                                    onChange={(next) => handleNumberChange(key, String(next))}
                                  />
                                );
                              })}
                          </div>

                          <div className="grid gap-3">
                            {group.keys
                              .filter((key) => !FEEL_KNOB_KEYS.has(key) || !CONTROL_BOUNDS[key])
                              .map((key) => {
                                const value = pendingAdjustments[key];
                                const meta = CONTROL_BOUNDS[key];
                                const help = detail.metadata?.field_help?.[key];
                                if (meta) {
                                  return (
                                    <div key={key} className="rounded-2xl border border-purple-500/20 bg-purple-900/20 p-3">
                                      <div className="flex items-center justify-between text-xs text-purple-100/70">
                                        <span className="font-semibold text-white">{labelize(key)}</span>
                                        <span className="font-mono text-amber-200">{Number(value).toFixed(2)}</span>
                                      </div>
                                      <input
                                        type="range"
                                        min={meta.min}
                                        max={meta.max}
                                        step={meta.step}
                                        value={Number(value)}
                                        onChange={(event) => handleNumberChange(key, event.target.value)}
                                        className="mt-2 w-full accent-amber-400"
                                      />
                                      <div className="mt-2 flex flex-col gap-2 text-xs text-purple-100/70">
                                        <input
                                          type="number"
                                          value={Number(value)}
                                          min={meta.min}
                                          max={meta.max}
                                          step={meta.step}
                                          onChange={(event) => handleNumberChange(key, event.target.value)}
                                          className="w-24 rounded-lg border border-purple-500/30 bg-purple-950/60 px-2 py-1 text-xs"
                                        />
                                        {help && <span className="text-[11px] text-purple-200/70">{help}</span>}
                                      </div>
                                    </div>
                                  );
                                }
                                const draftValue = textDrafts[key] ?? '';
                                return (
                                  <div key={key} className="space-y-2 rounded-2xl border border-purple-500/20 bg-purple-900/20 p-3">
                                    <div className="text-xs font-semibold uppercase tracking-[0.3em] text-purple-200">
                                      {labelize(key)}
                                    </div>
                                    <textarea
                                      value={draftValue}
                                      onChange={(event) => handleTextDraftChange(key, event.target.value)}
                                      onBlur={() => handleTextDraftCommit(key)}
                                      rows={4}
                                      className="w-full resize-none rounded-lg border border-purple-500/30 bg-purple-950/60 px-3 py-2 text-xs font-mono text-purple-100"
                                    />
                                    {help && <div className="text-[11px] text-purple-200/70">{help}</div>}
                                    {textErrors[key] && <div className="text-[11px] text-rose-200/80">{textErrors[key]}</div>}
                                  </div>
                                );
                              })}
                          </div>
                        </div>
                      </div>
                    ))}

                    <div className="rounded-3xl border border-purple-500/20 bg-purple-900/10 p-5 text-xs text-purple-100/70">
                      <p className="font-semibold uppercase tracking-[0.3em] text-purple-200">Calibration Notes</p>
                      <p className="mt-2">
                        Before saving, bullet why you changed each control in your personal notebook or shared doc. Producers
                        use that context to approve merges and keep profiles consistent across songs.
                      </p>
                    </div>
                  </div>
                )}

                {tab === 'metrics' && (
                  <div className="space-y-6">
                    <div className="rounded-3xl border border-purple-500/20 bg-purple-900/10 p-5">
                      <p className="text-sm font-semibold text-white">Core Metric Alignment</p>
                      <div className="mt-3 overflow-hidden rounded-2xl border border-purple-500/20">
                        <table className="min-w-full text-left text-xs text-purple-100">
                          <thead className="bg-purple-900/60 text-[11px] uppercase tracking-[0.3em] text-purple-200/80">
                            <tr>
                              <th className="px-4 py-3">Metric</th>
                              <th className="px-4 py-3">Generated</th>
                              <th className="px-4 py-3">Target</th>
                              <th className="px-4 py-3">Diff</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-purple-500/20">
                            {metricsRows.map((row) => {
                              const actual = row.actual ?? null;
                              const target = row.target ?? null;
                              const diff = actual != null && target != null && target !== 0 ? (actual - target) / target : null;
                              return (
                                <tr key={row.key}>
                                  <td className="px-4 py-3 font-medium text-white">{row.label}</td>
                                  <td className="px-4 py-3">{actual != null ? actual.toFixed(3) : '—'}</td>
                                  <td className="px-4 py-3">{target != null ? target.toFixed(3) : '—'}</td>
                                  <td className="px-4 py-3">
                                    {diff != null ? formatPercent(diff) : '—'}
                                  </td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    </div>

                    <div className="grid gap-6 lg:grid-cols-2">
                      <div className="rounded-3xl border border-purple-500/20 bg-purple-900/10 p-5">
                        <p className="text-sm font-semibold text-white">Rollup Share Targets</p>
                        <ul className="mt-3 space-y-2 text-xs text-purple-100/70">
                          {Object.entries(rollupShares).map(([key, value]) => (
                            <li key={key} className="flex items-center justify-between rounded-2xl bg-purple-900/20 px-3 py-2">
                              <span className="font-semibold text-purple-100">{labelize(key)}</span>
                              <span className="font-mono text-amber-200">{formatShare(value)}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                      <div className="rounded-3xl border border-purple-500/20 bg-purple-900/10 p-5">
                        <p className="text-sm font-semibold text-white">Generated Share Actuals</p>
                        <ul className="mt-3 space-y-2 text-xs text-purple-100/70">
                          {Object.entries(actualShares).map(([key, value]) => (
                            <li key={key} className="flex items-center justify-between rounded-2xl bg-purple-900/20 px-3 py-2">
                              <span className="font-semibold text-purple-100">{labelize(key)}</span>
                              <span className="font-m??? text-amber-200">{formatShare(value)}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    </div>

                    <div className="rounded-3xl border border-purple-500/20 bg-purple-900/10 p-5">
                      <p className="text-sm font-semibold text-white">Run History</p>
                      <div className="mt-3 space-y-3">
                        {detail.runHistory?.length ? (
                          detail.runHistory.map((run) => (
                            <div
                              key={run.id}
                              className="flex flex-col gap-2 rounded-2xl border border-purple-500/20 bg-purple-900/20 p-4 md:flex-row md:items-center md:justify-between"
                            >
                              <div className="space-y-1 text-xs text-purple-100/80">
                                <div className="flex items-center gap-2">
                                  <span className={`rounded-full px-2 py-0.5 text-[11px] ${RUN_BADGE_STYLE[run.outcome]}`}>
                                    {describeRunOutcome(run)}
                                  </span>
                                  <span>{formatDate(run.started_at)}</span>
                                </div>
                                <div>
                                  <span className="font-semibold text-purple-100">Notes:</span>{' '}
                                  {run.error_message || run.delta_summary || 'Not provided'}
                                </div>
                                <div className="flex flex-wrap gap-3 text-purple-100/70">
                                  <span>Notes: {run.note_count ?? '—'}</span>
                                  <span>FPM: {run.fills_per_minute?.toFixed(2) ?? '—'}</span>
                                </div>
                              </div>
                            </div>
                          ))
                        ) : (
                          <p className="text-xs text-purple-100/60">No calibration runs logged yet. Trigger one to populate history.</p>
                        )}
                      </div>
                    </div>
                  </div>
                )}

                {tab === 'feedback' && (
                  <div className="grid gap-6 lg:grid-cols-[1.2fr,1fr]">
                    <div className="space-y-4 rounded-3xl border border-purple-500/20 bg-purple-900/10 p-5">
                      <p className="text-sm font-semibold text-white">Team Feedback Log</p>
                      <div className="space-y-3 text-xs text-purple-100/80">
                        {detail.feedbackSamples?.length ? (
                          detail.feedbackSamples.map((entry) => (
                            <div key={entry.id} className="rounded-2xl border border-purple-500/20 bg-purple-900/20 p-4">
                              <div className="flex items-center justify-between text-[11px] uppercase tracking-[0.3em] text-purple-200/70">
                                <span>{entry.author}</span>
                                <span>{formatDate(entry.submitted_at)}</span>
                              </div>
                              <div className="mt-2 flex items-center gap-2 text-xs text-amber-200">
                                <Sparkles className="h-4 w-4" /> Rating: {entry.rating}/5
                              </div>
                              <p className="mt-2 text-sm text-purple-100/80">{entry.comment}</p>
                            </div>
                          ))
                        ) : (
                          <p>No feedback captured yet. Invite drummers to weigh in and log their notes here.</p>
                        )}
                      </div>
                    </div>

                    <form
                      onSubmit={handleFeedbackSubmit}
                      className="rounded-3xl border border-purple-500/20 bg-purple-900/10 p-5"
                    >
                      <p className="text-sm font-semibold text-white">Add Your Perspective</p>
                      <p className="mt-1 text-xs text-purple-100/70">
                        Focus on human feel: timing looseness, fill phrasing, cymbal articulation, and kit balance.
                      </p>
                      <label className="mt-4 block text-xs font-semibold text-purple-100">
                        Rating (1 = needs overhaul, 5 = stage ready)
                        <input
                          type="range"
                          min={1}
                          max={5}
                          step={1}
                          value={feedbackForm.rating}
                          onChange={(event) => setFeedbackForm((prev) => ({ ...prev, rating: Number(event.target.value) }))}
                          className="mt-2 w-full accent-amber-400"
                        />
                        <span className="mt-1 block font-mono text-amber-200">{feedbackForm.rating}/5</span>
                      </label>
                      <label className="mt-4 block text-xs font-semibold text-purple-100">
                        Comment
                        <textarea
                          value={feedbackForm.comment}
                          onChange={(event) => setFeedbackForm((prev) => ({ ...prev, comment: event.target.value }))}
                          rows={6}
                          placeholder="Describe what grooves, what feels robotic, and suggested tweaks (e.g., raise hat floor, lower fill cap)."
                          className="mt-2 w-full resize-none rounded-lg border border-purple-500/30 bg-purple-950/60 px-3 py-2 text-xs text-purple-100"
                        />
                      </label>
                      <button
                        type="submit"
                        disabled={feedbackSubmitting}
                        className="mt-4 inline-flex items-center gap-2 rounded-full border border-purple-500/40 bg-gradient-to-r from-purple-500 to-amber-500 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-purple-900/40 transition hover:shadow-amber-500/30"
                      >
                        <Users className="h-4 w-4" /> {feedbackSubmitting ? 'Sending…' : 'Submit Feedback'}
                      </button>
                    </form>
                  </div>
                )}

                  </div>

                  <div className="order-1 space-y-5 rounded-2xl border border-emerald-500/30 bg-emerald-950/30 p-5">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <p className="text-xs font-semibold uppercase tracking-[0.3em] text-emerald-100">Listening Workspace</p>
                      <button
                        type="button"
                        onClick={handleQueueListeningItem}
                        disabled={listeningBusy || !selectedSlug}
                        className="inline-flex items-center gap-2 rounded-full border border-emerald-400/60 bg-emerald-500/20 px-4 py-2 text-xs font-semibold text-emerald-100"
                      >
                        <Headphones className="h-4 w-4" /> {listeningBusy ? 'Queuing…' : 'Queue Listening Item'}
                      </button>
                    </div>
                    {itemError && <p className="rounded-xl bg-rose-500/20 px-3 py-2 text-xs text-rose-200">{itemError}</p>}
                    {pairwiseMessage && <p className="rounded-xl bg-emerald-500/20 px-3 py-2 text-xs text-emerald-200">{pairwiseMessage}</p>}
                    {showListeningProgress && (
                      <div className="rounded-xl border border-amber-400/30 bg-amber-500/10 px-3 py-2">
                        <div className="mb-1 flex items-center justify-between text-[11px] text-amber-100">
                          <span>{effectiveListeningLabel}</span>
                          <span>{Math.round(listeningProgressValue * 100)}%</span>
                        </div>
                        <div className="h-2 w-full overflow-hidden rounded-full bg-purple-950/70">
                          <div
                            className="h-full rounded-full bg-gradient-to-r from-amber-300 to-emerald-300 transition-[width] duration-500"
                            style={{ width: `${Math.max(6, Math.round(listeningProgressValue * 100))}%` }}
                          />
                        </div>
                      </div>
                    )}
                    {currentItem && artifactPollInfo.attempts > 0 && !hasCurrentPlayableArtifacts && (
                      <p className="rounded-xl bg-amber-500/15 px-3 py-2 text-xs text-amber-100">
                        Auto-refresh {artifactPollInfo.active ? 'active' : 'idle'} · checks {artifactPollInfo.attempts} · last checked{' '}
                        {formatClockTime(artifactPollInfo.lastCheckedAt)}
                      </p>
                    )}
                    {laneProgress.length > 0 && (
                      <div className="rounded-xl border border-purple-500/25 bg-purple-900/20 p-3">
                        <div className="mb-2 flex items-center justify-between text-[11px] uppercase tracking-[0.2em] text-purple-200/80">
                          <span>Lane Render Status</span>
                          <span>Updated {formatClockTime(laneProgressUpdatedAt)}</span>
                        </div>
                        <div className="grid gap-2 md:grid-cols-3">
                          {laneProgress.map((lane) => (
                            <div
                              key={`lane-progress-${lane.lane}`}
                              className={`rounded-lg border px-2 py-2 text-[11px] ${
                                lane.ready
                                  ? 'border-emerald-400/40 bg-emerald-500/10 text-emerald-100'
                                  : 'border-amber-400/40 bg-amber-500/10 text-amber-100'
                              }`}
                            >
                              <p className="font-semibold">{lane.lane}</p>
                              <p className="mt-1">
                                {lane.not_required
                                  ? 'Not required'
                                  : lane.ready
                                  ? 'Ready'
                                  : lane.stalled_in_queue
                                  ? 'Queued (stalled)'
                                  : String(lane.run_outcome || '').toLowerCase() === 'queued'
                                  ? 'Queued'
                                  : 'Rendering'}
                              </p>
                              <p className="mt-1 text-[10px] opacity-80">Artifacts: {lane.artifact_count}</p>
                              {!lane.ready && typeof lane.run_age_seconds === 'number' && lane.run_age_seconds >= 0 && (
                                <p className="mt-1 text-[10px] opacity-80">Queued for {formatTimeSeconds(lane.run_age_seconds)}</p>
                              )}
                              {lane.lane === 'baseline' && lane.strict_reference_ok === false && !lane.not_required && (
                                <p className="mt-1 text-[10px] text-rose-200">Baseline source not strict-reference ready</p>
                              )}
                              {lane.reason && (
                                <p className="mt-1 text-[10px] opacity-80">{lane.reason}</p>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {currentItem && (
                      <>
                        <div className="rounded-2xl border border-purple-500/20 bg-purple-900/20 p-4 text-xs text-purple-100/80">
                          <p>Session: <span className="font-mono">{currentItem.session_id}</span></p>
                          <p className="mt-1">Item: <span className="font-mono">{currentItem.item_id}</span></p>
                          <p className="mt-1">Base groove: {currentItem.base_groove_id}</p>
                          {currentItem.baseline_label && <p className="mt-1">Baseline song: {currentItem.baseline_label}</p>}
                          {sourceAnalysisId && (
                            <p className="mt-1">
                              A/B generated from baseline analysis:{' '}
                              {sourceAnalysisUrl ? (
                                <a
                                  href={sourceAnalysisUrl}
                                  target="_blank"
                                  rel="noreferrer"
                                  className="font-mono text-purple-200 underline decoration-dotted underline-offset-2 hover:text-white"
                                >
                                  {sourceAnalysisId}
                                </a>
                              ) : (
                                <span className="font-mono">{sourceAnalysisId}</span>
                              )}
                            </p>
                          )}
                        </div>

                        <div className="grid gap-4">
                          {artifactGroups.map(({ key, label, entries }) => (
                            <div key={label} className="rounded-2xl border border-purple-500/20 bg-purple-900/20 p-3">
                              <p className="text-[11px] uppercase tracking-[0.3em] text-purple-200/80">{label}</p>
                              {sourceAnalysisId && (key === 'A' || key === 'B') && (
                                <p className="mt-1 text-[10px] text-purple-100/60">
                                  Source analysis:{' '}
                                  {sourceAnalysisUrl ? (
                                    <a
                                      href={sourceAnalysisUrl}
                                      target="_blank"
                                      rel="noreferrer"
                                      className="font-mono text-purple-200 underline decoration-dotted underline-offset-2 hover:text-white"
                                    >
                                      {sourceAnalysisId}
                                    </a>
                                  ) : (
                                    <span className="font-mono">{sourceAnalysisId}</span>
                                  )}
                                </p>
                              )}
                              <div className="mt-2 space-y-3">
                                {entries.map((artifact) => {
                                  const src = resolveArtifactSource(artifact);
                                  return (
                                    <div key={artifact.artifact_id} className="space-y-2">
                                      {src ? (
                                        <AudioPreviewPlayer src={src} title={artifact.artifact_type || 'drum track'} />
                                      ) : (
                                        <div className="rounded-xl border border-rose-400/40 bg-rose-500/10 p-2 text-[11px] text-rose-200">
                                          Unable to resolve drum track source.
                                        </div>
                                      )}
                                      <p className="text-[11px] text-purple-100/60">{artifact.artifact_id}</p>
                                    </div>
                                  );
                                })}
                                {entries.length === 0 && <p className="text-[11px] text-purple-100/60">No drum tracks yet.</p>}
                              </div>
                            </div>
                          ))}
                        </div>

                        <form onSubmit={handlePairwiseSubmit} className="rounded-2xl border border-purple-500/20 bg-purple-900/20 p-4">
                          <p className="text-sm font-semibold text-white">Pairwise Judgment</p>
                          <div className="mt-3 grid gap-3 md:grid-cols-2">
                            {([
                              ['preferred_candidate', 'Preferred candidate'],
                              ['closer_to_target', 'Closer to target drummer'],
                              ['better_feel', 'Better feel'],
                              ['more_musical', 'More musical'],
                            ] as Array<[keyof PairwiseJudgmentForm, string]>).map(([key, label]) => (
                              <label key={key} className="text-xs text-purple-100">
                                {label}
                                <select
                                  value={String(pairwiseForm[key] || '')}
                                  onChange={(event) =>
                                    setPairwiseForm((prev) => ({ ...prev, [key]: event.target.value as JudgmentChoice }))
                                  }
                                  className="mt-2 w-full rounded-lg border border-purple-500/30 bg-purple-950/60 px-3 py-2 text-xs text-purple-100"
                                >
                                  <option value="">Select…</option>
                                  <option value="A">A</option>
                                  <option value="B">B</option>
                                  <option value="tie">Tie</option>
                                </select>
                              </label>
                            ))}
                          </div>
                          <label className="mt-3 block text-xs text-purple-100">
                            Confidence (1-5)
                            <input
                              type="range"
                              min={1}
                              max={5}
                              step={1}
                              value={pairwiseForm.confidence}
                              onChange={(event) =>
                                setPairwiseForm((prev) => ({ ...prev, confidence: Number(event.target.value) }))
                              }
                              className="mt-2 w-full accent-amber-400"
                            />
                          </label>
                          <button
                            type="submit"
                            disabled={pairwiseSubmitting}
                            className="mt-4 inline-flex items-center gap-2 rounded-full border border-purple-500/40 bg-gradient-to-r from-purple-500 to-amber-500 px-4 py-2 text-sm font-semibold text-white"
                          >
                            <Users className="h-4 w-4" /> {pairwiseSubmitting ? 'Submitting…' : 'Submit Judgment'}
                          </button>
                        </form>
                      </>
                    )}
                  </div>
                </div>
                </div>
            )}
          </div>
        </section>
      </main>
    </div>
  );
};

export default CalibrationLab;
