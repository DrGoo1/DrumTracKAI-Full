import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { Session } from "@supabase/supabase-js";
import {
  CandidateRatings,
  CalibrationArtifact,
  CalibrationReviewerItem,
  ReviewChoice,
  ReviewerDrummer,
  ReviewerIdentity,
  ReviewerNextState,
  fetchCalibrationHealth,
  fetchNextReviewerState,
  fetchReviewerDrummers,
  fetchReviewerIdentity,
  submitReviewerItem,
} from "../api/calibrationV2";
import { supabase, supabaseConfigurationError } from "../lib/supabaseClient";

const ratingLabels: Array<[keyof CandidateRatings, string]> = [
  ["stylistic_authenticity", "Stylistic authenticity"],
  ["groove_feel", "Groove and pocket"],
  ["dynamics", "Dynamic touch"],
  ["phrasing", "Phrasing"],
  ["kit_balance", "Kit balance"],
  ["fill_behavior", "Fill behavior"],
  ["human_realism", "Human realism"],
  ["overall_usefulness", "Overall usefulness"],
];

const newRatings = (): CandidateRatings => ({
  stylistic_authenticity: 5,
  groove_feel: 5,
  dynamics: 5,
  phrasing: 5,
  kit_balance: 5,
  fill_behavior: 5,
  human_realism: 5,
  overall_usefulness: 5,
});

const newChoiceState = (): Record<"preferred" | "closer" | "feel" | "musical", ReviewChoice> => ({
  preferred: "tie",
  closer: "tie",
  feel: "tie",
  musical: "tie",
});

function makeIdempotencyKey(itemId: string): string {
  const randomPart = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return `calibration-review:${itemId}:${randomPart}`;
}

function firstPlayableArtifact(artifacts: CalibrationArtifact[]): CalibrationArtifact | null {
  return artifacts.find((artifact) => Boolean(artifact.url)) || null;
}

interface TrackedAudioProps {
  label: "neutral" | "A" | "B";
  artifact: CalibrationArtifact | null;
  onListeningDelta: (label: "neutral" | "A" | "B", deltaMs: number) => void;
  onPlay: (label: "neutral" | "A" | "B") => void;
}

function TrackedAudio({ label, artifact, onListeningDelta, onPlay }: TrackedAudioProps) {
  const lastTick = useRef<number | null>(null);
  const timer = useRef<number | null>(null);

  const stopTimer = useCallback(() => {
    if (timer.current !== null) {
      window.clearInterval(timer.current);
      timer.current = null;
    }
    lastTick.current = null;
  }, []);

  useEffect(() => stopTimer, [stopTimer]);

  if (!artifact) {
    return <div className="text-sm text-amber-300">Audio is not ready yet.</div>;
  }

  return (
    <audio
      className="w-full"
      controls
      preload="metadata"
      src={artifact.url}
      onPlay={() => {
        onPlay(label);
        stopTimer();
        lastTick.current = performance.now();
        timer.current = window.setInterval(() => {
          const now = performance.now();
          const previous = lastTick.current ?? now;
          lastTick.current = now;
          onListeningDelta(label, Math.max(0, Math.round(now - previous)));
        }, 250);
      }}
      onPause={stopTimer}
      onEnded={stopTimer}
      onError={stopTimer}
    />
  );
}

function ChoiceRow({
  label,
  value,
  onChange,
}: {
  label: string;
  value: ReviewChoice;
  onChange: (choice: ReviewChoice) => void;
}) {
  const choices: ReviewChoice[] = ["A", "B", "tie", "neither"];
  return (
    <fieldset className="rounded-lg border border-slate-700 p-3">
      <legend className="px-1 text-sm font-medium text-slate-200">{label}</legend>
      <div className="flex flex-wrap gap-2">
        {choices.map((choice) => (
          <label
            key={choice}
            className={`cursor-pointer rounded-md border px-3 py-2 text-sm ${
              value === choice
                ? "border-cyan-400 bg-cyan-950 text-cyan-100"
                : "border-slate-700 bg-slate-900 text-slate-300"
            }`}
          >
            <input
              type="radio"
              className="sr-only"
              checked={value === choice}
              onChange={() => onChange(choice)}
            />
            {choice === "tie" ? "Tie" : choice === "neither" ? "Neither" : `Performance ${choice}`}
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function RatingsPanel({
  candidate,
  ratings,
  onChange,
  disabled,
}: {
  candidate: "A" | "B";
  ratings: CandidateRatings;
  onChange: (next: CandidateRatings) => void;
  disabled: boolean;
}) {
  return (
    <section className="rounded-xl border border-slate-700 bg-slate-900/70 p-4">
      <h3 className="mb-4 text-lg font-semibold text-white">Performance {candidate}</h3>
      <div className="space-y-4">
        {ratingLabels.map(([key, label]) => (
          <label key={key} className="block text-sm text-slate-200">
            <div className="mb-1 flex justify-between gap-4">
              <span>{label}</span>
              <span className="font-semibold text-cyan-300">{ratings[key]}</span>
            </div>
            <input
              className="w-full"
              type="range"
              min={1}
              max={10}
              step={1}
              disabled={disabled}
              value={ratings[key]}
              onChange={(event) => onChange({ ...ratings, [key]: Number(event.target.value) })}
            />
          </label>
        ))}
      </div>
    </section>
  );
}

export default function CalibrationReviewer() {
  const [session, setSession] = useState<Session | null>(null);
  const [serviceState, setServiceState] = useState<
    "checking" | "ready" | "disabled" | "unavailable"
  >("checking");
  const [serviceMessage, setServiceMessage] = useState(
    "Checking calibration service readiness…",
  );
  const [email, setEmail] = useState("");
  const [authMessage, setAuthMessage] = useState("");
  const [identity, setIdentity] = useState<ReviewerIdentity | null>(null);
  const [drummers, setDrummers] = useState<ReviewerDrummer[]>([]);
  const [selectedDrummer, setSelectedDrummer] = useState("");
  const [item, setItem] = useState<CalibrationReviewerItem | null>(null);
  const [nextState, setNextState] = useState<ReviewerNextState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [choices, setChoices] = useState(newChoiceState);
  const [ratingsA, setRatingsA] = useState<CandidateRatings>(newRatings);
  const [ratingsB, setRatingsB] = useState<CandidateRatings>(newRatings);
  const [confidence, setConfidence] = useState(3);
  const [technicalIssue, setTechnicalIssue] = useState(false);
  const [cannotJudge, setCannotJudge] = useState(false);
  const [comment, setComment] = useState("");
  const [listeningMs, setListeningMs] = useState<Record<"neutral" | "A" | "B", number>>({
    neutral: 0,
    A: 0,
    B: 0,
  });
  const [playCounts, setPlayCounts] = useState<Record<"neutral" | "A" | "B", number>>({
    neutral: 0,
    A: 0,
    B: 0,
  });
  const [idempotencyKey, setIdempotencyKey] = useState("");
  const pollTimer = useRef<number | null>(null);

  const resetReview = useCallback((nextItem: CalibrationReviewerItem | null) => {
    setItem(nextItem);
    setChoices(newChoiceState());
    setRatingsA(newRatings());
    setRatingsB(newRatings());
    setConfidence(3);
    setTechnicalIssue(false);
    setCannotJudge(false);
    setComment("");
    setListeningMs({ neutral: 0, A: 0, B: 0 });
    setPlayCounts({ neutral: 0, A: 0, B: 0 });
    setIdempotencyKey(nextItem ? makeIdempotencyKey(nextItem.item_id) : "");
  }, []);

  const clearPoll = useCallback(() => {
    if (pollTimer.current !== null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  }, []);

  const applyNextState = useCallback((state: ReviewerNextState) => {
    setNextState(state);
    resetReview(state.item);
  }, [resetReview]);

  useEffect(() => clearPoll, [clearPoll]);

  useEffect(() => {
    let active = true;
    fetchCalibrationHealth()
      .then((health) => {
        if (!active) return;
        if (!health.enabled) {
          setServiceState("disabled");
          setServiceMessage(
            "Calibration is temporarily paused while new listening trials are prepared.",
          );
          return;
        }
        setServiceState("ready");
        setServiceMessage("Secure calibration service is available.");
      })
      .catch(() => {
        if (!active) return;
        setServiceState("unavailable");
        setServiceMessage(
          "Calibration is temporarily unavailable. No review data has been lost.",
        );
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!supabase) return undefined;
    let active = true;
    supabase.auth.getSession().then(({ data }) => {
      if (active) setSession(data.session);
    });
    const { data } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession);
      if (!nextSession) {
        clearPoll();
        setIdentity(null);
        setDrummers([]);
        setSelectedDrummer("");
        setNextState(null);
        resetReview(null);
      }
    });
    return () => {
      active = false;
      data.subscription.unsubscribe();
    };
  }, [clearPoll, resetReview]);

  const loadForDrummer = useCallback(async (
    currentSession: Session,
    drummerSlug: string,
    quiet = false,
  ) => {
    if (!drummerSlug) {
      applyNextState({
        item: null,
        status: "none",
        trial_id: null,
        message: "No assimilated drummer model is selected.",
        retry_after_seconds: 0,
      });
      return;
    }
    if (!quiet) setBusy(true);
    setError("");
    try {
      const state = await fetchNextReviewerState(currentSession, drummerSlug);
      applyNextState(state);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : String(requestError));
      setNextState(null);
      resetReview(null);
    } finally {
      if (!quiet) setBusy(false);
    }
  }, [applyNextState, resetReview]);

  const loadReviewerContext = useCallback(async (currentSession: Session) => {
    setBusy(true);
    setError("");
    try {
      const [me, availableDrummers] = await Promise.all([
        fetchReviewerIdentity(currentSession),
        fetchReviewerDrummers(currentSession),
      ]);
      setIdentity(me);
      setDrummers(availableDrummers);
      const initialSlug = availableDrummers[0]?.drummer_slug || "";
      setSelectedDrummer(initialSlug);
      if (initialSlug) {
        const state = await fetchNextReviewerState(currentSession, initialSlug);
        applyNextState(state);
      } else {
        applyNextState({
          item: null,
          status: "none",
          trial_id: null,
          message: "No assimilation-ready drummer models are currently available.",
          retry_after_seconds: 0,
        });
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : String(requestError));
    } finally {
      setBusy(false);
    }
  }, [applyNextState]);

  useEffect(() => {
    if (session) void loadReviewerContext(session);
  }, [session, loadReviewerContext]);

  useEffect(() => {
    clearPoll();
    if (!session || !selectedDrummer || item) return undefined;
    if (!nextState || !["preparing", "queued"].includes(nextState.status)) return undefined;
    const delayMs = Math.max(2, nextState.retry_after_seconds || 5) * 1000;
    pollTimer.current = window.setTimeout(() => {
      void loadForDrummer(session, selectedDrummer, true);
    }, delayMs);
    return clearPoll;
  }, [clearPoll, item, loadForDrummer, nextState, selectedDrummer, session]);

  const loadNext = useCallback(async () => {
    if (!session) return;
    setSuccess("");
    await loadForDrummer(session, selectedDrummer, false);
  }, [loadForDrummer, selectedDrummer, session]);

  const selectedModel = useMemo(
    () => drummers.find((drummer) => drummer.drummer_slug === selectedDrummer) || null,
    [drummers, selectedDrummer],
  );

  const minimumMs = (item?.rubric.minimum_listening_seconds_per_candidate || 10) * 1000;
  const normalReviewReady = Boolean(
    item
      && playCounts.A > 0
      && playCounts.B > 0
      && listeningMs.A >= minimumMs
      && listeningMs.B >= minimumMs,
  );
  const canSubmit = Boolean(item && !busy && (technicalIssue || cannotJudge || normalReviewReady));

  const artifacts = useMemo(() => ({
    neutral: firstPlayableArtifact(item?.lanes.neutral || []),
    A: firstPlayableArtifact(item?.lanes.A || []),
    B: firstPlayableArtifact(item?.lanes.B || []),
  }), [item]);

  async function requestMagicLink(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setAuthMessage("");
    if (!supabase || serviceState !== "ready") return;
    const redirectTo = `${window.location.origin}/calibration`;
    const { error: signInError } = await supabase.auth.signInWithOtp({
      email: email.trim(),
      options: { emailRedirectTo: redirectTo },
    });
    if (signInError) {
      setError(signInError.message);
      return;
    }
    setAuthMessage("Check your email for the secure sign-in link.");
  }

  async function submitReview() {
    if (!session || !item || !canSubmit) return;
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await submitReviewerItem(
        session,
        item.item_id,
        {
          preferred_candidate: choices.preferred,
          closer_to_target: choices.closer,
          better_feel: choices.feel,
          more_musical: choices.musical,
          confidence,
          technical_issue: technicalIssue,
          cannot_judge: cannotJudge,
          comment: comment.trim() || undefined,
          listening_ms: listeningMs.A + listeningMs.B,
          candidate_a_listening_ms: listeningMs.A,
          candidate_b_listening_ms: listeningMs.B,
          candidate_a_play_count: playCounts.A,
          candidate_b_play_count: playCounts.B,
          ratings_a: technicalIssue || cannotJudge ? undefined : ratingsA,
          ratings_b: technicalIssue || cannotJudge ? undefined : ratingsB,
        },
        idempotencyKey,
      );
      setSuccess("Review saved. Loading the next blinded comparison.");
      const state = await fetchNextReviewerState(session, selectedDrummer || undefined);
      applyNextState(state);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : String(requestError));
    } finally {
      setBusy(false);
    }
  }

  if (supabaseConfigurationError) {
    return (
      <main className="mx-auto max-w-3xl p-6 text-slate-100">
        <h1 className="text-2xl font-semibold">Calibration reviewer portal</h1>
        <p className="mt-4 rounded-lg border border-red-700 bg-red-950/60 p-4 text-red-100">
          {supabaseConfigurationError}
        </p>
      </main>
    );
  }

  if (!session) {
    return (
      <main className="mx-auto max-w-xl p-6 text-slate-100">
        <h1 className="text-3xl font-semibold">DrumTracKAI Expert Calibration</h1>
        <p className="mt-3 text-slate-300">
          This invitation-only portal collects blinded evaluations from experienced drummers.
          Your comparisons improve feel, dynamics, phrasing, and musical realism without
          revealing which candidate uses the current model.
        </p>
        <div
          className={`mt-6 rounded-lg border px-4 py-3 text-sm ${
            serviceState === "ready"
              ? "border-emerald-700 bg-emerald-950/40 text-emerald-200"
              : serviceState === "checking"
                ? "border-slate-700 bg-slate-900 text-slate-300"
                : "border-amber-700 bg-amber-950/40 text-amber-200"
          }`}
          role="status"
        >
          {serviceMessage}
        </div>
        <form className="mt-5 rounded-xl border border-slate-700 bg-slate-900 p-5" onSubmit={requestMagicLink}>
          <label className="block text-sm font-medium text-slate-200">
            Reviewer email
            <input
              className="mt-2 w-full rounded-md border border-slate-600 bg-slate-950 px-3 py-2 text-white"
              type="email"
              required
              autoComplete="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
            />
          </label>
          <button
            className="mt-4 rounded-md bg-cyan-600 px-4 py-2 font-semibold text-white enabled:hover:bg-cyan-500 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
            type="submit"
            disabled={serviceState !== "ready"}
          >
            {serviceState === "checking" ? "Checking service…" : "Send secure sign-in link"}
          </button>
          {authMessage && <p className="mt-3 text-sm text-emerald-300">{authMessage}</p>}
          {error && <p className="mt-3 text-sm text-red-300">{error}</p>}
        </form>
      </main>
    );
  }

  return (
    <main className="mx-auto max-w-6xl p-4 text-slate-100 md:p-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold">Expert drummer calibration</h1>
          <p className="mt-1 text-sm text-slate-400">
            Signed in as {identity?.display_name || session.user.email || "reviewer"}. Candidate identities are blinded.
          </p>
        </div>
        <button
          className="rounded-md border border-slate-600 px-3 py-2 text-sm text-slate-300 hover:bg-slate-800"
          onClick={() => void supabase?.auth.signOut()}
          type="button"
        >
          Sign out
        </button>
      </div>

      <section className="mt-6 rounded-xl border border-slate-700 bg-slate-900/60 p-4">
        <div className="flex flex-wrap items-end gap-3">
          <label className="min-w-64 flex-1 text-sm text-slate-200">
            Assimilated drummer model
            <select
              className="mt-1 w-full rounded-md border border-slate-600 bg-slate-950 px-3 py-2 text-white"
              value={selectedDrummer}
              disabled={busy || drummers.length === 0}
              onChange={(event) => {
                const slug = event.target.value;
                clearPoll();
                setSelectedDrummer(slug);
                setNextState(null);
                resetReview(null);
                if (session && slug) void loadForDrummer(session, slug, false);
              }}
            >
              {drummers.length === 0 && <option value="">No models available</option>}
              {drummers.map((drummer) => (
                <option key={drummer.drummer_slug} value={drummer.drummer_slug}>
                  {drummer.display_name} · {drummer.source_song_count} songs · {drummer.assimilation_score}% assimilated
                  {drummer.ready_trial_count > 0 ? ` · ${drummer.ready_trial_count} ready` : ""}
                  {drummer.queued_trial_count > 0 ? ` · ${drummer.queued_trial_count} preparing` : ""}
                </option>
              ))}
            </select>
          </label>
          <button
            className="rounded-md bg-slate-700 px-4 py-2 text-sm font-semibold hover:bg-slate-600 disabled:opacity-50"
            disabled={busy || !selectedDrummer}
            onClick={() => void loadNext()}
            type="button"
          >
            Load or prepare comparison
          </button>
        </div>
        {selectedModel && (
          <div className="mt-3 flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-400">
            <span>{selectedModel.source_song_count} analyzed signature songs</span>
            <span>{selectedModel.assimilation_score}% assimilation score</span>
            {selectedModel.rollup_version && <span>Profile {selectedModel.rollup_version}</span>}
            <span>
              {selectedModel.ready_trial_count} ready · {selectedModel.queued_trial_count} preparing
            </span>
          </div>
        )}
      </section>

      {error && <p className="mt-4 rounded-lg border border-red-700 bg-red-950/60 p-3 text-red-100">{error}</p>}
      {success && <p className="mt-4 rounded-lg border border-emerald-700 bg-emerald-950/60 p-3 text-emerald-100">{success}</p>}

      {!item ? (
        <section className="mt-6 rounded-xl border border-slate-700 bg-slate-900 p-8 text-center text-slate-300">
          {busy ? (
            "Checking the assimilated drummer model…"
          ) : ["preparing", "queued"].includes(nextState?.status || "") ? (
            <div>
              <p className="font-medium text-cyan-200">Preparing a new blinded comparison…</p>
              <p className="mt-2 text-sm text-slate-400">
                DrumTracKAI is applying the personality extracted from the drummer&apos;s signature-song analyses and rendering neutral, A, and B through the same playback chain.
              </p>
              {nextState?.trial_id && (
                <p className="mt-2 text-xs text-slate-500">Trial {nextState.trial_id}</p>
              )}
            </div>
          ) : (
            nextState?.message || "No rendered comparisons are currently assigned."
          )}
        </section>
      ) : (
        <>
          <section className="mt-6 rounded-xl border border-slate-700 bg-slate-900 p-5">
            <h2 className="text-xl font-semibold">Target: {item.target_drummer_display_name}</h2>
            <p className="mt-2 text-sm text-slate-300">
              Listen to the neutral groove for context, then compare Performance A and Performance B. The labels are randomized.
            </p>
            <div className="mt-5 grid gap-4 lg:grid-cols-3">
              {(["neutral", "A", "B"] as const).map((label) => (
                <div key={label} className="rounded-lg border border-slate-700 bg-slate-950 p-4">
                  <h3 className="mb-3 font-semibold text-white">
                    {label === "neutral" ? "Neutral pattern" : `Performance ${label}`}
                  </h3>
                  <TrackedAudio
                    label={label}
                    artifact={artifacts[label]}
                    onListeningDelta={(lane, delta) => setListeningMs((current) => ({
                      ...current,
                      [lane]: current[lane] + delta,
                    }))}
                    onPlay={(lane) => setPlayCounts((current) => ({
                      ...current,
                      [lane]: current[lane] + 1,
                    }))}
                  />
                  <p className="mt-2 text-xs text-slate-400">
                    Heard {Math.floor(listeningMs[label] / 1000)}s · played {playCounts[label]} time(s)
                  </p>
                </div>
              ))}
            </div>
            <p className="mt-3 text-xs text-slate-400">
              Normal submissions require at least {Math.ceil(minimumMs / 1000)} seconds of listening to both A and B.
            </p>
          </section>

          <section className="mt-6 grid gap-4 md:grid-cols-2">
            <RatingsPanel candidate="A" ratings={ratingsA} onChange={setRatingsA} disabled={technicalIssue || cannotJudge} />
            <RatingsPanel candidate="B" ratings={ratingsB} onChange={setRatingsB} disabled={technicalIssue || cannotJudge} />
          </section>

          <section className="mt-6 space-y-4 rounded-xl border border-slate-700 bg-slate-900 p-5">
            <h2 className="text-xl font-semibold">Comparison</h2>
            <ChoiceRow label="Which performance do you prefer overall?" value={choices.preferred} onChange={(preferred) => setChoices({ ...choices, preferred })} />
            <ChoiceRow label={`Which is closer to ${item.target_drummer_display_name}?`} value={choices.closer} onChange={(closer) => setChoices({ ...choices, closer })} />
            <ChoiceRow label="Which has the better feel and pocket?" value={choices.feel} onChange={(feel) => setChoices({ ...choices, feel })} />
            <ChoiceRow label="Which is more musical?" value={choices.musical} onChange={(musical) => setChoices({ ...choices, musical })} />

            <label className="block text-sm text-slate-200">
              Confidence: <span className="font-semibold text-cyan-300">{confidence}/5</span>
              <input className="mt-2 w-full" type="range" min={1} max={5} value={confidence} onChange={(event) => setConfidence(Number(event.target.value))} />
            </label>

            <div className="flex flex-wrap gap-4 text-sm text-slate-200">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={technicalIssue} onChange={(event) => setTechnicalIssue(event.target.checked)} />
                Technical audio problem
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={cannotJudge} onChange={(event) => setCannotJudge(event.target.checked)} />
                Cannot make a reliable judgment
              </label>
            </div>

            <label className="block text-sm text-slate-200">
              Optional comments
              <textarea
                className="mt-2 min-h-24 w-full rounded-md border border-slate-600 bg-slate-950 px-3 py-2 text-white"
                maxLength={4000}
                value={comment}
                onChange={(event) => setComment(event.target.value)}
              />
            </label>

            <button
              className="rounded-md bg-cyan-600 px-5 py-3 font-semibold text-white hover:bg-cyan-500 disabled:cursor-not-allowed disabled:opacity-40"
              disabled={!canSubmit}
              onClick={() => void submitReview()}
              type="button"
            >
              {busy ? "Saving…" : "Submit blinded review"}
            </button>
          </section>
        </>
      )}
    </main>
  );
}
