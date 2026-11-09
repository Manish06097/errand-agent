"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { agentClock } from "./agent-clock";
import { api } from "./api";
import { newId } from "./ids";
import { useRunStream } from "./sse";
import { activityLine } from "./trace";
import type { ConversationOut, MessageOut, TraceRecord, VisibleCard } from "./types";

// A message the user sent while a run was active. It keeps the compose stamp and the
// card it was looking at from the moment of Send, and is posted after run.finished.
export type Queued = {
  client_msg_id: string;
  text: string;
  client_composed_at: string;
  visible_card: VisibleCard | null;
};

export type DecisionResult =
  | { kind: "accepted" }
  | { kind: "already_approved" }
  | { kind: "details_changed" }
  | { kind: "refused"; error: string }
  | { kind: "busy" };

export type Activity = { id: string; text: string };

const RETRY_MS = 200;
const RETRY_LIMIT = 40;

/** The approval card on screen at Send time: the latest live action card or reminder. */
export function visibleCardOf(conv: ConversationOut | undefined): VisibleCard | null {
  if (!conv) return null;
  for (let i = conv.messages.length - 1; i >= 0; i -= 1) {
    const m: MessageOut | undefined = conv.messages[i];
    if (!m || m.role !== "assistant") continue;
    const body = m.body;
    if ((body.type === "action" || body.type === "reminder") && m.approval?.status === "pending") {
      return { approval_id: body.approval_id, fingerprint: body.fingerprint, message_id: m.message_id };
    }
  }
  return null;
}

export function useChat(conversationId: string) {
  const qc = useQueryClient();
  const conv = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => api.conversation(conversationId),
    refetchOnWindowFocus: false,
  });
  const convRef = useRef<ConversationOut | undefined>(undefined);
  useEffect(() => {
    convRef.current = conv.data;
  });

  const [startedRun, setStartedRun] = useState<string | null>(null);
  const [finished, setFinished] = useState<ReadonlySet<string>>(new Set());
  const [submitting, setSubmitting] = useState(false);
  const [queued, setQueued] = useState<Queued[]>([]);
  const [liveText, setLiveText] = useState("");
  const [activity, setActivity] = useState<Activity[]>([]);
  const [records, setRecords] = useState<Record<string, TraceRecord[]>>({});
  const [notice, setNotice] = useState<string | null>(null);

  const serverActive = conv.data?.active_run_id ?? null;
  const candidate = startedRun ?? serverActive;
  const streamRunId = candidate && !finished.has(candidate) ? candidate : null;
  const busy = streamRunId !== null || submitting;

  const refetch = useCallback(
    () => qc.invalidateQueries({ queryKey: ["conversation", conversationId] }),
    [qc, conversationId],
  );

  useRunStream(streamRunId, {
    onRecord: (record, event) => {
      setRecords((prev) => {
        const have = prev[record.run_id] ?? [];
        if (have.some((r) => r.seq === record.seq)) return prev;
        return { ...prev, [record.run_id]: [...have, record] };
      });
      if (event === "trace") {
        const line = activityLine(record);
        if (line) setActivity((prev) => (prev.some((a) => a.id === line.id) ? prev : [...prev, line]));
      }
    },
    onCard: () => {
      void refetch();
    },
    onToken: (text) => setLiveText((prev) => prev + text),
    onFinished: (record) => {
      setFinished((prev) => new Set(prev).add(record.run_id));
      setStartedRun(null);
      setLiveText("");
      setActivity([]);
      void refetch();
      void qc.invalidateQueries({ queryKey: ["trace", record.run_id] });
      void qc.invalidateQueries({ queryKey: ["conversations"] });
    },
    onReconnected: () => {
      // Tokens are not replayed; the transcript is the truth for text.
      setLiveText("");
      void refetch();
    },
  });

  const startRun = useCallback((runId: string) => {
    setFinished((prev) => {
      if (!prev.has(runId)) return prev;
      const next = new Set(prev);
      next.delete(runId);
      return next;
    });
    setStartedRun(runId);
  }, []);

  const submit = useCallback(
    async (message: Queued): Promise<void> => {
      setSubmitting(true);
      setNotice(null);
      try {
        for (let attempt = 0; attempt < RETRY_LIMIT; attempt += 1) {
          const result = await api.sendMessage(conversationId, {
            client_msg_id: message.client_msg_id,
            text: message.text,
            client_composed_at: message.client_composed_at,
            visible_card: message.visible_card,
          });
          if (result.status === 200 || result.status === 202) {
            if (result.body.run_id) startRun(result.body.run_id);
            void refetch();
            return;
          }
          if (result.status === 409 && result.body.error === "run_in_progress") {
            // The previous run's stream ended a moment before the server released it.
            await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
            continue;
          }
          setNotice(`Message not sent (${result.body.error ?? result.status}).`);
          return;
        }
        setNotice("Message not sent: a run is still in progress.");
      } finally {
        setSubmitting(false);
      }
    },
    [conversationId, refetch, startRun],
  );

  // Queued messages go out one at a time, after the run in front of them has finished.
  useEffect(() => {
    if (busy || queued.length === 0) return;
    const [head, ...rest] = queued;
    if (!head) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- drains a queue once the run ends
    setQueued(rest);
    void submit(head);
  }, [busy, queued, submit]);

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      // Captured now, at Send: the agent clock and the card the user is looking at.
      const message: Queued = {
        client_msg_id: newId("cm"),
        text: trimmed,
        client_composed_at: agentClock.iso(),
        visible_card: visibleCardOf(convRef.current),
      };
      if (busy) setQueued((prev) => [...prev, message]);
      else void submit(message);
    },
    [busy, submit],
  );

  const decide = useCallback(
    async (
      approvalId: string,
      fingerprint: string,
      decision: "approve" | "decline",
    ): Promise<DecisionResult> => {
      const result = await api.decide(approvalId, {
        decision,
        fingerprint,
        client_action_id: newId("ca"),
      });
      if (result.status === 202 || (result.status === 200 && result.body.status !== "already_approved")) {
        if (result.body.run_id) startRun(result.body.run_id);
        void refetch();
        return { kind: "accepted" };
      }
      if (result.status === 200) {
        void refetch();
        return { kind: "already_approved" };
      }
      void refetch();
      const error = result.body.error ?? String(result.status);
      if (error === "details_changed") return { kind: "details_changed" };
      // A card a newer one replaced answers no_longer_valid; to the user the details changed.
      if (error === "no_longer_valid" && ["superseded", "invalidated"].includes(result.body.status ?? "")) {
        return { kind: "details_changed" };
      }
      if (error === "run_in_progress") return { kind: "busy" };
      return { kind: "refused", error };
    },
    [refetch, startRun],
  );

  const action = useCallback(
    async (kind: "select_slot" | "refresh_hold", payload: Record<string, string>) => {
      const result = await api.action(conversationId, {
        client_action_id: newId("ca"),
        kind,
        payload,
      });
      if (result.status === 202 && result.body.run_id) startRun(result.body.run_id);
      else if (result.status === 409 && result.body.error === "run_in_progress") {
        setNotice("A run is in progress; try again in a moment.");
      }
      void refetch();
      return result.status;
    },
    [conversationId, refetch, startRun],
  );

  return {
    conv,
    busy,
    streamRunId,
    queued,
    liveText,
    activity,
    records,
    notice,
    send,
    decide,
    action,
    refetch,
  };
}

export type Chat = ReturnType<typeof useChat>;
