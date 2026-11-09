"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";
import { api, type ApiResult } from "./api";
import { newId } from "./ids";

type Failure = { error?: string; detail?: string; current?: { status?: string } };

export type MemoryChatKind = "keep_memory" | "confirm_forget" | "update_memory" | "save_memory";

/**
 * Every memory action (the /memory page, the chips, the list card) goes through here.
 * An API forget publishes no SSE event, so after any action the conversation is read
 * again, and so is the memory list: that is what turns a chip into "Forgotten".
 * A 409 means the row had already moved; the answer says where it is now.
 */
export function useMemoryActions() {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["memory"] }),
      qc.invalidateQueries({ queryKey: ["conversation"] }),
      qc.invalidateQueries({ queryKey: ["trace"] }),
    ]);
  }, [qc]);

  const run = useCallback(
    async (call: (clientActionId: string) => Promise<ApiResult<unknown>>): Promise<boolean> => {
      setBusy(true);
      setNote(null);
      try {
        const result = await call(newId("ma"));
        const body = (result.body ?? {}) as Failure;
        if (!result.ok) {
          setNote(
            result.status === 409
              ? `That already changed${body.current?.status ? ` (${body.current.status.replaceAll("_", " ")})` : ""}.`
              : (body.detail ?? body.error ?? `Not done (${result.status}).`),
          );
        }
        await refresh();
        return result.ok;
      } finally {
        setBusy(false);
      }
    },
    [refresh],
  );

  const chat = useCallback(
    (conversationId: string, kind: MemoryChatKind, payload: Record<string, string>) =>
      run((clientActionId) =>
        api.action(conversationId, { client_action_id: clientActionId, kind, payload }),
      ),
    [run],
  );

  return {
    busy,
    note,
    forget: (id: string) => run((c) => api.memory.forget(id, c)),
    correct: (id: string, value: unknown) => run((c) => api.memory.correct(id, value, c)),
    keep: (id: string) => run((c) => api.memory.keep(id, c)),
    dismiss: (id: string) => run((c) => api.memory.dismiss(id, c)),
    undo: (id: string) => run((c) => api.memory.undo(id, c)),
    chat,
  };
}
