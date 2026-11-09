"use client";

import { useEffect, useState } from "react";
import { agentClock } from "./agent-clock";

/** The agent clock, re-read every `intervalMs`; null until the first tick after mount. */
export function useAgentNow(intervalMs = 1000): number | null {
  const [now, setNow] = useState<number | null>(null);
  useEffect(() => {
    const read = () => setNow(agentClock.synced ? agentClock.nowMs() : null);
    const first = setTimeout(read, 0);
    const timer = setInterval(read, intervalMs);
    return () => {
      clearTimeout(first);
      clearInterval(timer);
    };
  }, [intervalMs]);
  return now;
}
