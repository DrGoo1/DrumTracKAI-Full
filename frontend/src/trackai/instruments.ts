export type TrackAIInstrumentId = "drums" | "bass";

export interface TrackAIRatingDimension {
  key: string;
  label: string;
  description: string;
}

export interface TrackAIInstrumentSpec {
  instrumentId: TrackAIInstrumentId;
  productId: "drumtrackai" | "basstrackai";
  displayName: string;
  subjectLabel: string;
  ratings: TrackAIRatingDimension[];
  executionAuthorized: false;
}

const common: TrackAIRatingDimension[] = [
  { key: "stylistic_authenticity", label: "Stylistic authenticity", description: "Matches intended style and technique profile" },
  { key: "groove_feel", label: "Groove and pocket", description: "Timing feels musical and intentional" },
  { key: "dynamics", label: "Dynamic touch", description: "Dynamics fit the musical context" },
  { key: "phrasing", label: "Phrasing", description: "Phrase shape supports the section" },
  { key: "human_realism", label: "Human realism", description: "Avoids mechanical or implausible behavior" },
  { key: "overall_usefulness", label: "Overall usefulness", description: "Useful in a production" },
];

export const trackAIInstruments: Record<TrackAIInstrumentId, TrackAIInstrumentSpec> = {
  drums: {
    instrumentId: "drums", productId: "drumtrackai", displayName: "DrumTracKAI", subjectLabel: "Drummer / technique profile",
    ratings: [...common.slice(0, 4),
      { key: "kit_balance", label: "Kit balance", description: "Natural distribution of energy across the kit" },
      { key: "fill_behavior", label: "Fill behavior", description: "Idiomatic and structurally appropriate fills" },
      ...common.slice(4)], executionAuthorized: false,
  },
  bass: {
    instrumentId: "bass", productId: "basstrackai", displayName: "BassTracKAI", subjectLabel: "Bassist / technique profile",
    ratings: [...common.slice(0, 4),
      { key: "kick_lock", label: "Kick-lock relationship", description: "Intentional interaction with kick and pocket" },
      { key: "note_length", label: "Note-length behavior", description: "Convincing sustain, muting, rests, and releases" },
      { key: "harmonic_accuracy", label: "Harmonic accuracy", description: "Harmony-aware notes, approaches, and extensions" },
      { key: "articulation", label: "Bass articulation", description: "Idiomatic ghosts, mutes, slides, attacks, and transitions" },
      ...common.slice(4)], executionAuthorized: false,
  },
};
