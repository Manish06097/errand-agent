"use client";

import { useChatContext } from "@/components/chat-context";
import { Button } from "@/components/ui/button";
import type { OutcomeNotice as Notice } from "@/lib/types";
import { StatusLine } from "./status-line";

// A code-written outcome message. Buttons come only from the actions the server sent:
// refresh_hold and check_now go through the actions route; retry, narrow_search,
// raise_budget, options and cancel are the user's own quick reply (the server's label sent
// as a message); answer and shorten send nothing, they hand the composer to the user
// (nobody types "Answer"). A failed reschedule's `alternatives` are select_slot buttons.
export function OutcomeNotice({
  body,
  live,
  verifying,
}: {
  body: Notice;
  live: boolean;
  verifying: boolean;
}) {
  const chat = useChatContext();
  return (
    <div data-testid={verifying ? "msg-verifying" : "msg-notice"} data-outcome={body.outcome}>
      <p className="whitespace-pre-wrap break-words">{body.text}</p>
      {(body.actions ?? []).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {(body.actions ?? []).map((action) => (
            <Button
              key={`${action.kind}:${action.label}`}
              size="sm"
              disabled={!live}
              data-testid={`notice-action-${action.kind}`}
              onClick={() => {
                if (action.kind === "refresh_hold" && action.approval_id) {
                  void chat.action("refresh_hold", { approval_id: action.approval_id });
                } else if (action.kind === "check_now") {
                  void chat.action("check_now", {});
                } else if (action.kind === "answer" || action.kind === "shorten") {
                  chat.editLast(action.kind === "shorten");
                } else {
                  chat.send(action.label);
                }
              }}
            >
              {action.label}
            </Button>
          ))}
        </div>
      )}
      {(body.alternatives ?? []).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2" data-testid="notice-alternatives">
          {(body.alternatives ?? []).map((slot) => (
            <Button
              key={slot.ref}
              size="sm"
              disabled={!live}
              data-testid={`alternative-${slot.ref}`}
              onClick={() => void chat.action("select_slot", { slot_ref: slot.ref })}
            >
              {slot.label}
            </Button>
          ))}
        </div>
      )}
      <StatusLine text={body.status_line} />
    </div>
  );
}

// PENDING_VERIFICATION: the booking may exist; the card says so in the server's words and
// offers only the actions the server sends ("Check now" while the provider's answer can
// still change).
export function VerifyingCard({ body, live }: { body: Notice; live: boolean }) {
  return <OutcomeNotice body={body} live={live} verifying />;
}
