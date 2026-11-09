// Pure helpers over trace records: the tree for the trace panel, the filter chips, and
// the activity lines in the chat. Activity lines use only code-owned fields (tool name,
// outcome, latency); never model text. A record type we do not know produces no line.
import type { TraceRecord } from "./types";

export type FilterKey = "model" | "tools" | "approval" | "memory" | "context";

export const FILTERS: { key: FilterKey; label: string }[] = [
  { key: "model", label: "Model" },
  { key: "tools", label: "Tools" },
  { key: "approval", label: "Approval" },
  { key: "memory", label: "Memory" },
  { key: "context", label: "Context" },
];

export function categoryOf(type: string): FilterKey | null {
  if (type === "model.call" || type === "reader.call") return "model";
  if (type === "tool.call" || type === "status.check" || type.startsWith("intent.")) return "tools";
  if (type.startsWith("approval.")) return "approval";
  if (type.startsWith("memory.")) return "memory";
  if (type === "context.built") return "context";
  return null;
}

export type TraceRow = {
  record: TraceRecord;
  depth: number;
};

function field<T>(record: TraceRecord, key: string): T | undefined {
  return (record as Record<string, unknown>)[key] as T | undefined;
}

export function num(record: TraceRecord, key: string): number | null {
  const value = field<unknown>(record, key);
  return typeof value === "number" ? value : null;
}

export function str(record: TraceRecord, key: string): string | null {
  const value = field<unknown>(record, key);
  return typeof value === "string" ? value : null;
}

export function obj(record: TraceRecord, key: string): Record<string, unknown> | null {
  const value = field<unknown>(record, key);
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** Rows in seq order, each with its span's depth from parent_span_id. */
export function buildRows(records: TraceRecord[]): TraceRow[] {
  const parent = new Map<string, string | null>();
  for (const r of records) {
    if (!parent.has(r.span_id)) parent.set(r.span_id, r.parent_span_id ?? null);
  }
  const depthOf = (span: string): number => {
    let depth = 0;
    let cur = parent.get(span) ?? null;
    const seen = new Set<string>();
    while (cur && !seen.has(cur) && depth < 64) {
      seen.add(cur);
      depth += 1;
      cur = parent.get(cur) ?? null;
    }
    return depth;
  };
  const sorted = [...records].sort((a, b) => a.seq - b.seq);
  return sorted.map((record) => ({ record, depth: depthOf(record.span_id) }));
}

export type Totals = { tokensIn: number; tokensOut: number; costUsd: number; modelCalls: number };

export function totalsOf(records: TraceRecord[]): Totals {
  const totals: Totals = { tokensIn: 0, tokensOut: 0, costUsd: 0, modelCalls: 0 };
  for (const r of records) {
    if (r.type !== "model.call" && r.type !== "reader.call") continue;
    const usage = obj(r, "usage");
    if (usage) {
      totals.tokensIn +=
        Number(usage["input_uncached"] ?? 0) +
        Number(usage["cache_read"] ?? 0) +
        Number(usage["cache_write_5m"] ?? 0) +
        Number(usage["cache_write_1h"] ?? 0);
      totals.tokensOut += Number(usage["output"] ?? 0);
    }
    totals.costUsd += num(r, "cost_usd") ?? 0;
    if (r.type === "model.call") totals.modelCalls += 1;
  }
  return totals;
}

export function rowLabel(r: TraceRecord): string {
  switch (r.type) {
    case "node.start":
      return `node ${str(r, "node") ?? ""}`.trim();
    case "node.end":
      return `node end ${str(r, "node") ?? ""}`.trim();
    case "model.call":
      return `model ${str(r, "model") ?? ""}`.trim();
    case "reader.call":
      return `reader ${str(r, "provider_id") ?? ""}`.trim();
    case "tool.call":
      return `tool ${str(r, "tool") ?? ""}`.trim();
    default:
      return r.type;
  }
}

export function rowDetail(r: TraceRecord): string {
  const parts: string[] = [];
  if (r.type === "tool.call") parts.push(str(r, "error") ? `error ${str(r, "error")}` : "ok");
  if (r.type === "model.call") {
    const stop = str(r, "stop_reason");
    if (stop) parts.push(stop);
  }
  if (r.type.startsWith("approval.")) {
    const approval = str(r, "approval_id");
    if (approval) parts.push(approval);
  }
  const latency = num(r, "latency_ms");
  if (latency !== null) parts.push(`${Math.round(latency)} ms`);
  return parts.join(" · ");
}

const TOOL_LABELS: Record<string, string> = {
  search_providers: "Searched providers",
  get_provider: "Looked up a provider",
  hold_slot: "Held a slot",
  list_bookings: "Checked your bookings",
  get_booking_status: "Checked the booking status",
  book: "Sent the booking",
};

/** One activity line for a streamed record, or null (most records and every unknown type). */
export function activityLine(r: TraceRecord): { id: string; text: string } | null {
  if (r.type === "tool.call") {
    const tool = str(r, "tool");
    if (!tool) return null;
    const label = TOOL_LABELS[tool] ?? tool;
    const error = str(r, "error");
    const latency = num(r, "latency_ms");
    const timing = latency === null ? "" : ` · ${Math.round(latency)} ms`;
    return { id: `a${r.seq}`, text: `${label} · ${error ? `error ${error}` : "ok"}${timing}` };
  }
  if (r.type === "model.call") {
    const latency = num(r, "latency_ms");
    const timing = latency === null ? "" : ` · ${(latency / 1000).toFixed(1)} s`;
    return { id: `a${r.seq}`, text: `Planner call${timing}` };
  }
  return null;
}

export function moneyUsd(value: number): string {
  return `$${value.toFixed(value < 0.01 ? 5 : 4)}`;
}
