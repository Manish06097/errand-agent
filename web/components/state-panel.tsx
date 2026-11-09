"use client";

import type { ConversationOut } from "@/lib/types";

// The agent's state snapshot (GET /api/conversations/{id}.state): phase, brief, counters.
export function StatePanel({ conversation }: { conversation: ConversationOut | undefined }) {
  const state = conversation?.state as Record<string, unknown> | null | undefined;
  if (!state) return <p className="text-xs text-neutral-500">No state yet.</p>;
  const errand = state["errand"] as Record<string, unknown> | null | undefined;
  const brief = (errand?.["brief"] ?? {}) as Record<string, unknown>;
  const missing = (errand?.["missing"] ?? []) as string[];
  return (
    <div data-testid="state-panel" className="flex flex-col gap-3 text-xs">
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-neutral-500">outcome</dt>
        <dd>{String(state["outcome"] ?? "none")}</dd>
        <dt className="text-neutral-500">next</dt>
        <dd>{((state["next"] as string[] | undefined) ?? []).join(", ") || "none"}</dd>
        <dt className="text-neutral-500">parked</dt>
        <dd>{state["interrupted"] ? "waiting for approval" : "no"}</dd>
        {errand && (
          <>
            <dt className="text-neutral-500">phase</dt>
            <dd>{String(errand["phase"] ?? "")}</dd>
            <dt className="text-neutral-500">missing</dt>
            <dd>{missing.join(", ") || "nothing"}</dd>
          </>
        )}
      </dl>
      {errand && (
        <div>
          <p className="mb-1 font-medium">Brief</p>
          <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-neutral-50 p-2 dark:bg-neutral-800/60">
            {JSON.stringify(brief, null, 2)}
          </pre>
        </div>
      )}
      <div>
        <p className="mb-1 font-medium">Counters</p>
        <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-neutral-50 p-2 dark:bg-neutral-800/60">
          {JSON.stringify(state["counters"], null, 2)}
        </pre>
      </div>
    </div>
  );
}
