"use client";

import { useState } from "react";
import { useChatContext } from "@/components/chat-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useAgentNow } from "@/lib/use-agent-now";
import type { ActionCard as Action, ApprovalStatus } from "@/lib/types";
import { StatusLine } from "./status-line";

type Phase = "idle" | "submitting" | "done";

export function statusTone(status: string): "good" | "warn" | "bad" | "neutral" {
  if (status === "granted" || status === "consumed") return "good";
  if (status === "pending") return "neutral";
  if (status === "declined") return "neutral";
  return "warn";
}

function remaining(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

// The buttons are UX; the server decides. Every line, label and status line below is
// shown exactly as the server sent it.
export function ActionCard({
  body,
  approval,
  live,
}: {
  body: Action;
  approval: ApprovalStatus | null | undefined;
  live: boolean;
}) {
  const chat = useChatContext();
  const [phase, setPhase] = useState<Phase>("idle");
  const [note, setNote] = useState<string | null>(null);
  const now = useAgentNow();
  const status = approval?.status ?? "pending";
  const pending = status === "pending";
  const holdMs = Date.parse(body.hold_expires_at);
  const left = now === null || Number.isNaN(holdMs) ? null : holdMs - now;
  const holdExpired = body.action === "book" && pending && left !== null && left <= 0;

  async function decide(decision: "approve" | "decline") {
    setPhase("submitting");
    setNote(null);
    const result = await chat.decide(body.approval_id, body.fingerprint, decision);
    if (result.kind === "accepted" || result.kind === "already_approved") {
      setPhase("done");
      return;
    }
    setPhase("idle");
    if (result.kind === "details_changed") setNote("details changed");
    else if (result.kind === "busy") setNote("A run is in progress.");
    else setNote(result.error.replaceAll("_", " "));
  }

  async function refresh() {
    setPhase("submitting");
    await chat.action("refresh_hold", { approval_id: body.approval_id });
    setPhase("idle");
  }

  return (
    <div data-testid="msg-action" data-approval-status={status}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-semibold">{body.title}</h3>
        <Badge data-testid="approval-status" tone={statusTone(status)}>
          {status}
        </Badge>
      </div>
      {body.header && (
        <p data-testid="card-header" className="mt-1 text-sm text-neutral-700 dark:text-neutral-300">
          {body.header}
        </p>
      )}
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        {body.lines.map((line) => (
          <div key={line.label} className="contents">
            <dt className="text-neutral-600 dark:text-neutral-400">{line.label}</dt>
            <dd className="break-words">{line.value}</dd>
          </div>
        ))}
      </dl>
      {body.action === "book" && pending && left !== null && (
        <p data-testid="hold-countdown" className="mt-2 text-xs text-neutral-600 dark:text-neutral-400">
          {holdExpired ? "Hold expired · Refresh hold" : `Hold expires in ${remaining(left)}`}
        </p>
      )}
      {approval?.reason && !pending && (
        <p className="mt-2 text-xs text-neutral-600 dark:text-neutral-400">{approval.reason}</p>
      )}
      {pending && live && (
        <div className="mt-3 flex flex-wrap gap-2">
          {holdExpired ? (
            <Button
              variant="primary"
              data-testid="refresh-hold"
              disabled={phase === "submitting"}
              onClick={refresh}
            >
              Refresh hold
            </Button>
          ) : (
            <Button
              variant="primary"
              data-testid="approve-button"
              disabled={phase !== "idle"}
              onClick={() => decide("approve")}
            >
              {phase === "idle" ? body.button_label : phase === "submitting" ? "Working…" : "Sent"}
            </Button>
          )}
          <Button
            data-testid="decline-button"
            disabled={phase !== "idle"}
            onClick={() => decide("decline")}
          >
            {body.decline_label}
          </Button>
        </div>
      )}
      {pending && live && (body.alternatives ?? []).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2" data-testid="card-alternatives">
          {(body.alternatives ?? []).map((slot) => (
            <Button
              key={slot.ref}
              size="sm"
              data-testid={`alternative-${slot.ref}`}
              disabled={phase !== "idle"}
              onClick={() => void chat.action("select_slot", { slot_ref: slot.ref })}
            >
              {slot.label}
            </Button>
          ))}
        </div>
      )}
      {note && (
        <p role="status" data-testid="card-note" className="mt-2 text-sm text-amber-800 dark:text-amber-300">
          {note}
        </p>
      )}
      <StatusLine text={body.status_line} />
    </div>
  );
}
