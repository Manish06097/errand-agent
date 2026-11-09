"use client";

import Link from "next/link";
import { useState } from "react";
import { useChatContext } from "@/components/chat-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { keyLabel, scopeLabel } from "@/lib/memory";
import type { BodyOf } from "@/lib/types";
import { useMemoryActions } from "@/lib/use-memory-actions";
import type { components } from "@/lib/api-types.gen";

type Resolved = components["schemas"]["ResolvedMemory"];

function Note({ text }: { text: string | null }) {
  if (!text) return null;
  return (
    <p role="status" data-testid="memory-note" className="mt-1 text-xs text-amber-800 dark:text-amber-300">
      {text}
    </p>
  );
}

function useConversationId(): string {
  return useChatContext().conv.data?.conversation_id ?? "";
}

/** "Saved: home area Indiranagar" with Undo; the value shows only while the row is active. */
export function MemorySavedChip({ body }: { body: BodyOf<"memory_saved"> }) {
  const actions = useMemoryActions();
  const row = body.memory;
  const status = row?.status ?? "active";
  return (
    <div data-testid="memory-saved" data-memory-id={body.memory_id} data-memory-status={status}>
      <div className="flex flex-wrap items-center gap-2">
        {status === "active" && row ? (
          <>
            <span>
              Saved: <strong>{keyLabel(row.key).toLowerCase()}</strong>
              {row.label ? ` ${row.label}` : ""}
            </span>
            <Button
              size="sm"
              data-testid="memory-undo"
              disabled={actions.busy}
              onClick={() => void actions.undo(body.memory_id)}
            >
              Undo
            </Button>
          </>
        ) : (
          <>
            <span>Saved: {row ? keyLabel(row.key).toLowerCase() : "a preference"}</span>
            <Badge data-testid="memory-state" tone={status === "forgotten" ? "bad" : "neutral"}>
              {stateWord(status)}
            </Badge>
          </>
        )}
      </div>
      <Note text={actions.note} />
    </div>
  );
}

function stateWord(status: string): string {
  switch (status) {
    case "forgotten":
      return "Forgotten";
    case "rejected":
      return "Undone";
    case "superseded":
      return "Changed since";
    case "pending_confirmation":
      return "Waiting for you";
    default:
      return status.replaceAll("_", " ");
  }
}

/** "You usually book before noon. Remember that?" with Keep and Dismiss. */
export function MemorySuggestionChip({ body }: { body: BodyOf<"memory_suggestion"> }) {
  const actions = useMemoryActions();
  const row = body.memory;
  const status = row?.status ?? "pending_confirmation";
  const pending = status === "pending_confirmation";
  return (
    <div data-testid="memory-suggestion" data-memory-id={body.memory_id} data-memory-status={status}>
      <p>
        {row ? `I noticed a pattern: ${keyLabel(row.key).toLowerCase()}${row.label ? ` ${row.label}` : ""}. ` : ""}
        {pending ? "Remember that?" : ""}
      </p>
      {pending ? (
        <div className="mt-2 flex gap-2">
          <Button
            variant="primary"
            size="sm"
            data-testid="memory-keep"
            disabled={actions.busy}
            onClick={() => void actions.keep(body.memory_id)}
          >
            Keep
          </Button>
          <Button
            size="sm"
            data-testid="memory-dismiss"
            disabled={actions.busy}
            onClick={() => void actions.dismiss(body.memory_id)}
          >
            Dismiss
          </Button>
        </div>
      ) : (
        <Badge data-testid="memory-state" tone={status === "active" ? "good" : "neutral"}>
          {status === "active" ? "Kept" : status === "rejected" ? "Dismissed" : stateWord(status)}
        </Badge>
      )}
      <Note text={actions.note} />
    </div>
  );
}

/** "Remember this for next time?" The chip holds no value; the click re-reads the words. */
export function MemoryOfferChip({
  body,
  messageId,
}: {
  body: BodyOf<"memory_offer">;
  messageId: string;
}) {
  const actions = useMemoryActions();
  const conversationId = useConversationId();
  const [done, setDone] = useState(false);
  return (
    <div data-testid="memory-offer">
      <p>
        Remember this for next time? <span className="text-neutral-600 dark:text-neutral-400">
          ({keyLabel(body.key).toLowerCase()}, {scopeLabel(body.scope)})
        </span>
      </p>
      {done ? (
        <Badge tone="good">Saved</Badge>
      ) : (
        <div className="mt-2 flex gap-2">
          <Button
            variant="primary"
            size="sm"
            data-testid="memory-save"
            disabled={actions.busy}
            onClick={async () => {
              const ok = await actions.chat(conversationId, "save_memory", {
                chip_message_id: messageId,
              });
              if (ok) setDone(true);
            }}
          >
            Remember
          </Button>
        </div>
      )}
      <Note text={actions.note} />
    </div>
  );
}

/** "Update your saved home area? [Update] [Just this time]". */
export function MemoryUpdateChip({
  body,
  messageId,
}: {
  body: BodyOf<"memory_update">;
  messageId: string;
}) {
  const actions = useMemoryActions();
  const conversationId = useConversationId();
  const [done, setDone] = useState<"updated" | "once" | null>(null);
  const stale = body.memory && body.memory.status !== "active";
  return (
    <div data-testid="memory-update" data-memory-id={body.memory_id}>
      <p>Update your saved {keyLabel(body.key).toLowerCase()}?</p>
      {done !== null ? (
        <Badge tone="good">{done === "updated" ? "Updated" : "Just this time"}</Badge>
      ) : stale ? (
        <Badge tone="neutral">{stateWord(body.memory?.status ?? "")}</Badge>
      ) : (
        <div className="mt-2 flex gap-2">
          <Button
            variant="primary"
            size="sm"
            data-testid="memory-update-yes"
            disabled={actions.busy}
            onClick={async () => {
              const ok = await actions.chat(conversationId, "update_memory", {
                chip_message_id: messageId,
              });
              if (ok) setDone("updated");
            }}
          >
            Update
          </Button>
          <Button
            size="sm"
            data-testid="memory-update-once"
            disabled={actions.busy}
            onClick={async () => {
              const ok = await actions.chat(conversationId, "update_memory", {
                chip_message_id: messageId,
                choice: "just_this_time",
              });
              if (ok) setDone("once");
            }}
          >
            Just this time
          </Button>
        </div>
      )}
      <Note text={actions.note} />
    </div>
  );
}

/** What propose_forget renders. The click is the forget; the model never does it. */
export function MemoryForgetChip({ body }: { body: BodyOf<"memory_forget"> }) {
  const actions = useMemoryActions();
  const [open, setOpen] = useState(false);
  const [kept, setKept] = useState(false);
  const conversationId = useConversationId();
  const row = body.memory;
  const status = row?.status ?? "active";
  const name = row ? keyLabel(row.key).toLowerCase() : "this preference";
  return (
    <div data-testid="memory-forget" data-memory-id={body.memory_id} data-memory-status={status}>
      <p>
        Forget {name}
        {row?.label ? ` (${row.label})` : ""}?
      </p>
      {status === "forgotten" ? (
        <Badge data-testid="memory-state" tone="bad">
          Forgotten
        </Badge>
      ) : kept || status !== "active" ? (
        <Badge tone="neutral">{kept ? "Kept" : stateWord(status)}</Badge>
      ) : (
        <div className="mt-2 flex gap-2">
          <Button
            variant="danger"
            size="sm"
            data-testid="memory-forget-yes"
            disabled={actions.busy}
            onClick={() => setOpen(true)}
          >
            Forget
          </Button>
          <Button size="sm" data-testid="memory-forget-keep" onClick={() => setKept(true)}>
            Keep
          </Button>
        </div>
      )}
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Forget ${name}?`}
        confirmLabel="Forget it"
        busy={actions.busy}
        testId="forget-confirm"
        onConfirm={async () => {
          await actions.chat(conversationId, "confirm_forget", { memory_id: body.memory_id });
          setOpen(false);
        }}
      >
        It is removed from everything I keep, including your earlier messages that mention it.
        Bookings already made stay.
      </ConfirmDialog>
      <Note text={actions.note} />
    </div>
  );
}

function ListRow({ row, pending }: { row: Resolved; pending: boolean }) {
  const actions = useMemoryActions();
  const [open, setOpen] = useState(false);
  return (
    <li
      data-testid="memory-list-row"
      data-memory-id={row.memory_id}
      className="flex flex-wrap items-center justify-between gap-2"
    >
      <span>
        <span className="text-neutral-600 dark:text-neutral-400">
          {keyLabel(row.key)} ({scopeLabel(row.scope)}):{" "}
        </span>
        {row.label}
      </span>
      <span className="flex gap-1">
        {pending ? (
          <>
            <Button
              size="sm"
              variant="primary"
              data-testid="memory-keep"
              disabled={actions.busy}
              onClick={() => void actions.keep(row.memory_id)}
            >
              Keep
            </Button>
            <Button
              size="sm"
              data-testid="memory-dismiss"
              disabled={actions.busy}
              onClick={() => void actions.dismiss(row.memory_id)}
            >
              Dismiss
            </Button>
          </>
        ) : (
          <Button
            size="sm"
            variant="danger"
            data-testid="memory-forget-yes"
            disabled={actions.busy}
            onClick={() => setOpen(true)}
          >
            Forget
          </Button>
        )}
      </span>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Forget ${keyLabel(row.key).toLowerCase()}?`}
        confirmLabel="Forget it"
        busy={actions.busy}
        testId="forget-confirm"
        onConfirm={async () => {
          await actions.forget(row.memory_id);
          setOpen(false);
        }}
      >
        It is removed from everything I keep. Bookings already made stay.
      </ConfirmDialog>
    </li>
  );
}

/** What list_memories renders. Its contents never reach the model. */
export function MemoryListCard({ body }: { body: BodyOf<"memory_list"> }) {
  const preferences = body.preferences ?? [];
  const suggestions = body.suggestions ?? [];
  return (
    <div data-testid="memory-list">
      <h3 className="font-semibold">What I remember</h3>
      {preferences.length === 0 && suggestions.length === 0 && (
        <p className="mt-1 text-sm text-neutral-600 dark:text-neutral-400">Nothing saved right now.</p>
      )}
      {preferences.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1.5 text-sm">
          {preferences.map((row) => (
            <ListRow key={row.memory_id} row={row} pending={false} />
          ))}
        </ul>
      )}
      {suggestions.length > 0 && (
        <>
          <p className="mt-3 text-xs font-medium uppercase text-neutral-500">Suggestions</p>
          <ul className="mt-1 flex flex-col gap-1.5 text-sm">
            {suggestions.map((row) => (
              <ListRow key={row.memory_id} row={row} pending />
            ))}
          </ul>
        </>
      )}
      <Link href="/memory" className="mt-3 inline-block text-xs underline" data-testid="open-memory-page">
        Manage on the memory page
      </Link>
    </div>
  );
}
