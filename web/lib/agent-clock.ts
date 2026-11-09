// The agent clock (architecture 5.7, 8.7). `client_composed_at` and a render ack's
// `client_ts` are measured on it: the last X-Agent-Clock (or transcript `clock`) reading
// plus the time elapsed on performance.now() since it arrived. Never Date.now(): on
// `make dev` the agent clock is about 52,416 s off the wall clock, so a Date.now() stamp
// is far in the past and never gets a typed grant (and one in the future is refused).

export type ClockMode = "sim" | "system_offset";

export type ClockReading = { mode: ClockMode; nowMs: number; offsetMs: number };

const JITTER_MS = 1000;

export const AGENT_CLOCK_HEADER = "X-Agent-Clock";

export function parseClockHeader(value: string | null): ClockReading | null {
  if (!value) return null;
  const parts = value.split(";").map((p) => p.trim());
  const mode = parts[0];
  if (mode !== "sim" && mode !== "system_offset") return null;
  const fields: Record<string, number> = {};
  for (const part of parts.slice(1)) {
    const [key, raw] = part.split("=");
    if (key && raw !== undefined && raw !== "" && Number.isFinite(Number(raw))) {
      fields[key] = Number(raw);
    }
  }
  const nowMs = fields["now_ms"];
  if (nowMs === undefined) return null;
  return { mode, nowMs, offsetMs: fields["offset_ms"] ?? 0 };
}

export class AgentClock {
  private reading: ClockReading | null = null;
  private receivedAt = 0;
  private last = 0;

  /** Record a reading, taken the moment its response arrived. */
  sync(reading: ClockReading, perfNow: number = performance.now()): void {
    // Jitter between two responses must not run the clock backwards, but a real jump
    // back (a scenario reload moves the world's clock) is the new truth, not a glitch.
    if (reading.nowMs < this.last - JITTER_MS) this.last = 0;
    this.reading = reading;
    this.receivedAt = perfNow;
  }

  get synced(): boolean {
    return this.reading !== null;
  }

  /** The agent clock now, in epoch ms. Never moves backwards between calls. */
  nowMs(perfNow: number = performance.now()): number {
    if (!this.reading) throw new Error("the agent clock has not been synced yet");
    // A simulated clock only moves when the server moves it.
    const elapsed = this.reading.mode === "sim" ? 0 : Math.max(0, perfNow - this.receivedAt);
    const now = Math.max(this.last, Math.floor(this.reading.nowMs + elapsed));
    this.last = now;
    return now;
  }

  /** ISO 8601 with an offset, for client_composed_at and the render ack. */
  iso(perfNow?: number): string {
    return new Date(this.nowMs(perfNow)).toISOString();
  }
}

export const agentClock = new AgentClock();
