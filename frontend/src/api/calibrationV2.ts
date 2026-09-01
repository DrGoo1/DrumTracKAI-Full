import type { Session } from "@supabase/supabase-js";
import { resolveApiBaseNormalized } from "../utils/apiBase";

const API_BASE = resolveApiBaseNormalized();

export type ReviewChoice = "A" | "B" | "tie" | "neither";

export interface CalibrationHealth {
  status: "ok";
  version: string;
  enabled: boolean;
  internal_reviewers_enabled: boolean;
  external_reviewers_enabled: boolean;
  auto_queue_review_trials: boolean;
}

export interface CalibrationArtifact {
  artifact_id: string;
  artifact_type: string;
  url: string;
  duration_sec?: number | null;
  loudness_lufs?: number | null;
  sample_pack_version?: string | null;
}

export interface CandidateRatings {
  stylistic_authenticity: number;
  groove_feel: number;
  dynamics: number;
  phrasing: number;
  kit_balance: number;
  fill_behavior: number;
  human_realism: number;
  overall_usefulness: number;
}

export interface CalibrationReviewerItem {
  item_id: string;
  session_id: string;
  trial_id: string;
  target_drummer_slug: string;
  target_drummer_display_name: string;
  base_groove_id: string;
  eval_mode: "AB";
  lanes: {
    neutral: CalibrationArtifact[];
    A: CalibrationArtifact[];
    B: CalibrationArtifact[];
  };
  rubric: {
    choices: ReviewChoice[];
    rating_min: number;
    rating_max: number;
    minimum_listening_seconds_per_candidate: number;
  };
}

export interface ReviewerIdentity {
  reviewer_id: string;
  display_name: string;
  expertise_level?: string | null;
  consent_version?: string | null;
  is_active: boolean;
}

export interface ReviewerDrummer {
  drummer_slug: string;
  display_name: string;
  ready_trial_count: number;
  queued_trial_count: number;
  model_ready: boolean;
  can_queue_trial: boolean;
  source_song_count: number;
  assimilation_score: number;
  rollup_version?: string | null;
  blockers: string[];
}

export interface ReviewerNextState {
  item: CalibrationReviewerItem | null;
  status: "ready" | "preparing" | "queued" | "none" | string;
  trial_id?: string | null;
  message?: string | null;
  retry_after_seconds: number;
}

export interface ReviewerSubmission {
  preferred_candidate: ReviewChoice;
  closer_to_target: ReviewChoice;
  better_feel: ReviewChoice;
  more_musical: ReviewChoice;
  confidence: number;
  technical_issue: boolean;
  cannot_judge: boolean;
  comment?: string;
  listening_ms: number;
  candidate_a_listening_ms: number;
  candidate_b_listening_ms: number;
  candidate_a_play_count: number;
  candidate_b_play_count: number;
  ratings_a?: CandidateRatings;
  ratings_b?: CandidateRatings;
}

interface ReviewResult {
  status: string;
  judgment_id: string;
  rating_ids: Record<string, string>;
  trial_id: string;
}

function accessToken(session: Session): string {
  const token = String(session.access_token || "").trim();
  if (!token) throw new Error("Reviewer session has no access token");
  return token;
}

function resolveArtifactUrl(rawUrl: string): string {
  const value = String(rawUrl || "").trim();
  if (!value) return value;
  if (/^(https?:|blob:|data:)/i.test(value)) return value;
  const normalized = value.startsWith("/") ? value : `/${value}`;
  return `${API_BASE.replace(/\/$/, "")}${normalized}`;
}

function normalizeArtifact(artifact: CalibrationArtifact): CalibrationArtifact {
  return {
    ...artifact,
    url: resolveArtifactUrl(artifact.url),
  };
}

function normalizeReviewerItem(item: CalibrationReviewerItem | null): CalibrationReviewerItem | null {
  if (!item) return null;
  return {
    ...item,
    lanes: {
      neutral: (item.lanes?.neutral || []).map(normalizeArtifact),
      A: (item.lanes?.A || []).map(normalizeArtifact),
      B: (item.lanes?.B || []).map(normalizeArtifact),
    },
  };
}

function errorDetail(body: any, statusCode: number): string {
  const detail = body?.detail || body?.message;
  if (typeof detail === "string" && detail.trim()) return detail;
  if (detail && typeof detail === "object") {
    try {
      return JSON.stringify(detail);
    } catch {
      return `Calibration API returned HTTP ${statusCode}`;
    }
  }
  return `Calibration API returned HTTP ${statusCode}`;
}

async function fetchWithTimeout(
  url: string,
  init: RequestInit = {},
  timeoutMs = 15000,
): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new Error("Calibration service did not respond in time");
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

export async function fetchCalibrationHealth(): Promise<CalibrationHealth> {
  const response = await fetchWithTimeout(
    `${API_BASE}/calibration/v2/healthz`,
    { headers: { Accept: "application/json" }, cache: "no-store" },
    7000,
  );
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorDetail(body, response.status));
  return body as CalibrationHealth;
}

async function apiRequest<T>(
  session: Session,
  path: string,
  init: RequestInit = {},
  idempotencyKey?: string,
): Promise<T> {
  const response = await fetchWithTimeout(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      Authorization: `Bearer ${accessToken(session)}`,
      ...(idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}),
      ...(init.headers || {}),
    },
  });

  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(errorDetail(body, response.status));
  }
  return body as T;
}

export async function fetchReviewerIdentity(session: Session): Promise<ReviewerIdentity> {
  return apiRequest<ReviewerIdentity>(session, "/calibration/v2/reviewer/me");
}

export async function fetchReviewerDrummers(session: Session): Promise<ReviewerDrummer[]> {
  const response = await apiRequest<{ items: ReviewerDrummer[] }>(
    session,
    "/calibration/v2/reviewer/drummers",
  );
  return response.items || [];
}

export async function fetchNextReviewerState(
  session: Session,
  targetDrummerSlug?: string,
): Promise<ReviewerNextState> {
  const query = targetDrummerSlug
    ? `?target_drummer_slug=${encodeURIComponent(targetDrummerSlug)}`
    : "";
  const response = await apiRequest<ReviewerNextState>(
    session,
    `/calibration/v2/reviewer/next${query}`,
  );
  return {
    ...response,
    item: normalizeReviewerItem(response.item),
    retry_after_seconds: Math.max(0, Number(response.retry_after_seconds || 0)),
  };
}

export async function fetchNextReviewerItem(
  session: Session,
  targetDrummerSlug?: string,
): Promise<CalibrationReviewerItem | null> {
  return (await fetchNextReviewerState(session, targetDrummerSlug)).item;
}

export async function fetchReviewerItem(
  session: Session,
  itemId: string,
): Promise<CalibrationReviewerItem> {
  const response = await apiRequest<{ item: CalibrationReviewerItem }>(
    session,
    `/calibration/v2/reviewer/items/${encodeURIComponent(itemId)}`,
  );
  const normalized = normalizeReviewerItem(response.item);
  if (!normalized) throw new Error("Calibration item response was empty");
  return normalized;
}

export async function submitReviewerItem(
  session: Session,
  itemId: string,
  payload: ReviewerSubmission,
  idempotencyKey: string,
): Promise<ReviewResult> {
  if (!idempotencyKey.trim()) throw new Error("Idempotency key is required");
  return apiRequest<ReviewResult>(
    session,
    `/calibration/v2/reviewer/items/${encodeURIComponent(itemId)}/review`,
    { method: "POST", body: JSON.stringify(payload) },
    idempotencyKey,
  );
}
