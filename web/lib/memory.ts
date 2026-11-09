// The /api/memory routes answer plain JSON (the row view of backend memory/service.py),
// not a response model, so the row shape is written once here. Chat chips use the
// generated ResolvedMemory type instead.
export type MemoryStatus =
  | "active"
  | "pending_confirmation"
  | "superseded"
  | "rejected"
  | "forgotten";

export type MemoryRow = {
  memory_id: string;
  key: string;
  scope: string;
  value: unknown;
  label: string | null;
  kind: "explicit" | "inferred";
  status: MemoryStatus;
  source: string;
  evidence: {
    type: string;
    message_id?: string;
    quote?: string | null;
    cue?: string | null;
    rule?: string;
    booking_ids?: string[];
  } | null;
  supersedes: string | null;
  superseded_by: string | null;
  created_at: number | null;
  confirmed_at: number | null;
  forgotten_at: number | null;
  last_used_at: number | null;
};

export type MemoryGroups = {
  preferences: MemoryRow[];
  suggestions: MemoryRow[];
  history: MemoryRow[];
};

const KEY_WORDS: Record<string, string> = {
  home_area: "Home area",
  earliest_time: "Earliest time",
  latest_time: "Latest time",
  budget_ceiling: "Budget ceiling",
  preferred_provider: "Preferred provider",
  avoid_provider: "Avoided provider",
  avoid_days: "Days to avoid",
  language: "Language",
  access_note: "Access note",
  contact_window: "Contact window",
  usual_time_of_day: "Usual time of day",
};

export function keyLabel(key: string): string {
  return KEY_WORDS[key] ?? key.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}

export function scopeLabel(scope: string): string {
  return scope === "global" ? "any errand" : scope.replace("service:", "").replaceAll("_", " ");
}

/** Turn the text of an edit box back into the value type the row already has. */
export function valueFromInput(current: unknown, text: string): unknown {
  if (typeof current === "number") return Number(text.replace(/[^\d.]/g, ""));
  if (Array.isArray(current)) {
    return text
      .split(",")
      .map((part) => part.trim())
      .filter(Boolean);
  }
  return text.trim();
}

export function inputFromValue(value: unknown): string {
  if (Array.isArray(value)) return value.join(", ");
  return value === null || value === undefined ? "" : String(value);
}

/** "Forgotten on 5 Nov" from an epoch-ms stamp on the agent's clock. */
export function dayLabel(ms: number | null): string {
  if (ms === null) return "";
  return new Date(ms).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}
