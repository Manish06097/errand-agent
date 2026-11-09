"use client";

import { useEffect, useRef } from "react";
import type { CardRecord, TraceRecord } from "./types";

// One EventSource per run against GET /api/runs/{id}/events. The browser reconnects by
// itself and sends Last-Event-ID, so the server replays what was missed. `token` events
// are never replayed: after a reconnect the transcript fetch is the truth for text, so
// `onReconnected` asks the caller to refetch it.
export type RunStreamHandlers = {
  onRecord: (record: TraceRecord, event: string) => void;
  onCard: (record: CardRecord, event: "card" | "card.update") => void;
  onToken: (text: string) => void;
  onFinished: (record: TraceRecord) => void;
  onReconnected: () => void;
};

const NAMED = ["run.started", "trace", "card", "card.update", "run.finished"] as const;

export function useRunStream(runId: string | null, handlers: RunStreamHandlers): void {
  const latest = useRef(handlers);
  useEffect(() => {
    latest.current = handlers;
  });

  useEffect(() => {
    if (!runId) return;
    const source = new EventSource(`/api/runs/${runId}/events`);
    let dropped = false;
    let done = false;

    for (const name of NAMED) {
      source.addEventListener(name, (raw) => {
        const data = JSON.parse((raw as MessageEvent<string>).data) as TraceRecord;
        if (name === "card" || name === "card.update") {
          latest.current.onCard(data as CardRecord, name);
        }
        latest.current.onRecord(data, name);
        if (name === "run.finished") {
          // The server closes the stream after run.finished; EventSource would reopen it.
          done = true;
          source.close();
          latest.current.onFinished(data);
        }
      });
    }
    source.addEventListener("token", (raw) => {
      const data = JSON.parse((raw as MessageEvent<string>).data) as { text: string };
      latest.current.onToken(data.text);
    });
    source.onerror = () => {
      if (!done) dropped = true;
    };
    source.onopen = () => {
      if (dropped) {
        dropped = false;
        latest.current.onReconnected();
      }
    };
    return () => {
      done = true;
      source.close();
    };
  }, [runId]);
}
