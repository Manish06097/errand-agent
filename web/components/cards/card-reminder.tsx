"use client";

import { useState } from "react";
import { useChatContext } from "@/components/chat-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { ApprovalStatus, CardReminder as Reminder } from "@/lib/types";
import { statusTone } from "./action-card";
import { StatusLine } from "./status-line";

export function CardReminder({
  body,
  approval,
  live,
}: {
  body: Reminder;
  approval: ApprovalStatus | null | undefined;
  live: boolean;
}) {
  const chat = useChatContext();
  const [phase, setPhase] = useState<"idle" | "submitting" | "done">("idle");
  const [note, setNote] = useState<string | null>(null);
  const status = approval?.status ?? "pending";
  const pending = status === "pending";

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

  return (
    <div data-testid="msg-reminder" data-approval-status={status}>
      <div className="flex items-start justify-between gap-2">
        <p className="whitespace-pre-wrap break-words">{body.text}</p>
        <Badge tone={statusTone(status)}>{status}</Badge>
      </div>
      {pending && !body.typed_grant_ok && (
        <p data-testid="reminder-hint" className="mt-1 text-xs text-neutral-600 dark:text-neutral-400">
          {body.hint}
        </p>
      )}
      {pending && live && (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button
            variant="primary"
            data-testid="approve-button"
            disabled={phase !== "idle"}
            onClick={() => decide("approve")}
          >
            {body.button_label}
          </Button>
          <Button
            data-testid="decline-button"
            disabled={phase !== "idle"}
            onClick={() => decide("decline")}
          >
            {body.decline_label}
          </Button>
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
