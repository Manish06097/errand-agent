"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import {
  dayLabel,
  inputFromValue,
  keyLabel,
  scopeLabel,
  valueFromInput,
  type MemoryRow,
} from "@/lib/memory";
import { useMemoryActions } from "@/lib/use-memory-actions";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { ConfirmDialog } from "./ui/confirm-dialog";

function Evidence({ row }: { row: MemoryRow }) {
  const ev = row.evidence;
  if (!ev) return null;
  if (ev.type === "user_msg" && ev.quote) {
    return (
      <p className="text-xs text-neutral-600 dark:text-neutral-400">
        You said: &ldquo;{ev.quote}&rdquo;
      </p>
    );
  }
  if (ev.type === "rule") {
    return (
      <p className="text-xs text-neutral-600 dark:text-neutral-400">
        From your bookings {(ev.booking_ids ?? []).join(", ")}
      </p>
    );
  }
  return (
    <p className="text-xs text-neutral-600 dark:text-neutral-400">You set this on this page or in chat.</p>
  );
}

function PreferenceRow({ row }: { row: MemoryRow }) {
  const actions = useMemoryActions();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(inputFromValue(row.value));
  const [confirm, setConfirm] = useState(false);
  return (
    <li
      data-testid="memory-row"
      data-memory-id={row.memory_id}
      data-key={row.key}
      data-status={row.status}
      className="flex flex-col gap-1 rounded-lg border border-neutral-200 p-3 dark:border-neutral-800"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-sm font-medium">
            {keyLabel(row.key)} <span className="font-normal text-neutral-500">({scopeLabel(row.scope)})</span>
          </p>
          {editing ? (
            <form
              className="mt-1 flex gap-2"
              onSubmit={async (e) => {
                e.preventDefault();
                const ok = await actions.correct(row.memory_id, valueFromInput(row.value, text));
                if (ok) setEditing(false);
              }}
            >
              <input
                data-testid="memory-edit-input"
                aria-label={`New value for ${keyLabel(row.key)}`}
                value={text}
                onChange={(e) => setText(e.target.value)}
                className="h-8 min-w-0 rounded-md border border-neutral-300 px-2 text-sm dark:border-neutral-700 dark:bg-neutral-900"
              />
              <Button size="sm" variant="primary" type="submit" data-testid="memory-edit-save" disabled={actions.busy}>
                Save
              </Button>
              <Button size="sm" type="button" onClick={() => setEditing(false)}>
                Cancel
              </Button>
            </form>
          ) : (
            <p data-testid="memory-value" className="break-words text-sm first-letter:uppercase">
              {row.label}
            </p>
          )}
        </div>
        <Badge tone={row.kind === "explicit" ? "info" : "neutral"} data-testid="memory-source">
          {row.source}
        </Badge>
      </div>
      <Evidence row={row} />
      {row.last_used_at !== null && (
        <p className="text-xs text-neutral-500">Last used {dayLabel(row.last_used_at)}</p>
      )}
      {!editing && (
        <div className="mt-1 flex gap-2">
          <Button size="sm" data-testid="memory-edit" onClick={() => setEditing(true)}>
            Edit
          </Button>
          <Button size="sm" variant="danger" data-testid="memory-forget" onClick={() => setConfirm(true)}>
            Forget
          </Button>
        </div>
      )}
      {actions.note && (
        <p role="status" className="text-xs text-amber-800 dark:text-amber-300">
          {actions.note}
        </p>
      )}
      <ConfirmDialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Forget ${keyLabel(row.key).toLowerCase()}?`}
        confirmLabel="Forget it"
        busy={actions.busy}
        testId="forget-confirm"
        onConfirm={async () => {
          await actions.forget(row.memory_id);
          setConfirm(false);
        }}
      >
        It is removed from everything I keep, including your earlier messages that mention it and
        any older values. Bookings already made stay.
      </ConfirmDialog>
    </li>
  );
}

function SuggestionRow({ row }: { row: MemoryRow }) {
  const actions = useMemoryActions();
  return (
    <li
      data-testid="memory-suggestion-row"
      data-memory-id={row.memory_id}
      data-key={row.key}
      className="flex flex-col gap-1 rounded-lg border border-dashed border-neutral-300 p-3 dark:border-neutral-700"
    >
      <p className="text-sm font-medium">
        {keyLabel(row.key)} <span className="font-normal text-neutral-500">({scopeLabel(row.scope)})</span>
      </p>
      <p className="text-sm">{row.label}</p>
      <Evidence row={row} />
      <div className="mt-1 flex gap-2">
        <Button size="sm" variant="primary" data-testid="memory-keep" disabled={actions.busy} onClick={() => void actions.keep(row.memory_id)}>
          Keep
        </Button>
        <Button size="sm" data-testid="memory-dismiss" disabled={actions.busy} onClick={() => void actions.dismiss(row.memory_id)}>
          Dismiss
        </Button>
      </div>
      {actions.note && <p role="status" className="text-xs text-amber-800 dark:text-amber-300">{actions.note}</p>}
    </li>
  );
}

function HistoryRow({ row }: { row: MemoryRow }) {
  // A forgotten row has no value to show; the others keep theirs.
  const forgotten = row.status === "forgotten";
  const word =
    row.status === "forgotten"
      ? `Forgotten on ${dayLabel(row.forgotten_at)}`
      : row.status === "superseded"
        ? "Replaced by a newer value"
        : "Dismissed or undone";
  return (
    <li
      data-testid="memory-history-row"
      data-memory-id={row.memory_id}
      data-key={row.key}
      data-status={row.status}
      className="flex flex-wrap items-center justify-between gap-2 text-sm"
    >
      <span>
        {keyLabel(row.key)} ({scopeLabel(row.scope)})
        {!forgotten && row.label ? `: ${row.label}` : ""}
      </span>
      <span className="text-xs text-neutral-500">{word}</span>
    </li>
  );
}

export function MemoryPage() {
  const memory = useQuery({ queryKey: ["memory"], queryFn: api.memory.list });
  const data = memory.data;
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-8" data-testid="memory-page">
      <header className="flex items-center justify-between gap-2">
        <h1 className="text-xl font-semibold tracking-tight">What I remember about you</h1>
        <Link href="/" className="text-sm underline">
          Back to errands
        </Link>
      </header>
      {memory.isError && <p role="alert" className="text-sm text-red-700">Could not load your memory.</p>}
      <section aria-labelledby="prefs">
        <h2 id="prefs" className="mb-2 text-sm font-semibold">
          Your preferences
        </h2>
        {data && data.preferences.length === 0 && (
          <p className="text-sm text-neutral-500" data-testid="memory-empty">
            Nothing saved. Say &ldquo;from now on I prefer ...&rdquo; in a chat and it shows up here.
          </p>
        )}
        <ul className="flex flex-col gap-2" data-testid="memory-preferences">
          {(data?.preferences ?? []).map((row) => (
            <PreferenceRow key={row.memory_id} row={row} />
          ))}
        </ul>
      </section>
      {(data?.suggestions.length ?? 0) > 0 && (
        <section aria-labelledby="sugg">
          <h2 id="sugg" className="mb-2 text-sm font-semibold">
            Suggestions
          </h2>
          <ul className="flex flex-col gap-2" data-testid="memory-suggestions">
            {(data?.suggestions ?? []).map((row) => (
              <SuggestionRow key={row.memory_id} row={row} />
            ))}
          </ul>
        </section>
      )}
      {(data?.history.length ?? 0) > 0 && (
        <section aria-labelledby="hist">
          <h2 id="hist" className="mb-2 text-sm font-semibold">
            History
          </h2>
          <ul className="flex flex-col gap-1.5" data-testid="memory-history">
            {(data?.history ?? []).map((row) => (
              <HistoryRow key={row.memory_id} row={row} />
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
