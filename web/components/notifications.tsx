"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type { NotificationOut } from "@/lib/types";
import { Button } from "./ui/button";
import { Sheet } from "./ui/sheet";

// The bell reads GET /api/notifications. GET /api/users/me/events is a nudge: each
// notification or proactive line it announces refetches the bell and the conversation
// list, and the browser's own reconnect sends Last-Event-ID so a missed one is replayed.
// A slow poll covers a stream that never connected.
const POLL_MS = 5000;

export function useNotifications() {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["notifications"],
    queryFn: api.notifications,
    refetchInterval: POLL_MS,
    refetchOnWindowFocus: true,
  });
  // A new bell row means the agent wrote into some conversation (the origin chat's line):
  // whichever way the news arrived, the transcripts and the list are refetched.
  const count = query.data?.notifications.length ?? 0;
  useEffect(() => {
    if (count === 0) return;
    void qc.invalidateQueries({ queryKey: ["conversations"] });
    void qc.invalidateQueries({ queryKey: ["conversation"] });
  }, [count, qc]);
  useEffect(() => {
    const source = new EventSource("/api/users/me/events");
    const refresh = () => {
      void qc.invalidateQueries({ queryKey: ["notifications"] });
      void qc.invalidateQueries({ queryKey: ["conversations"] });
      void qc.invalidateQueries({ queryKey: ["conversation"] });
    };
    source.addEventListener("notification", refresh);
    source.addEventListener("message", refresh);
    return () => source.close();
  }, [qc]);
  return query;
}

/** Urgent, not quiet, unread: a toast for a moment and a banner that stays. */
export function NotificationHost() {
  const { data } = useNotifications();
  const qc = useQueryClient();
  const seen = useRef<Set<string> | null>(null);
  const [toast, setToast] = useState<NotificationOut | null>(null);
  const loud = (data?.notifications ?? []).filter((n) => n.urgency === "urgent" && !n.quiet && !n.read);

  useEffect(() => {
    if (!data) return;
    if (seen.current === null) {
      // What was already there when the page opened is not news: the banner shows it.
      seen.current = new Set(data.notifications.map((n) => n.notification_id));
      return;
    }
    const fresh = loud.find((n) => !seen.current?.has(n.notification_id));
    for (const n of data.notifications) seen.current.add(n.notification_id);
    if (fresh) {
      setToast(fresh);
      const timer = setTimeout(() => setToast(null), 6000);
      return () => clearTimeout(timer);
    }
  }, [data, loud]);

  const top = loud[0];
  return (
    <>
      {top && (
        <div
          role="alert"
          data-testid="urgent-banner"
          className="flex items-center justify-between gap-3 bg-amber-100 px-4 py-2 text-sm text-amber-950 dark:bg-amber-900 dark:text-amber-50"
        >
          <span className="min-w-0 truncate">{top.title}</span>
          <Link
            href={`/c/${top.conversation_id}`}
            data-testid="banner-open"
            className="shrink-0 font-medium underline"
            onClick={() => {
              void api.markRead(top.notification_id).then(() =>
                qc.invalidateQueries({ queryKey: ["notifications"] }),
              );
            }}
          >
            Open
          </Link>
        </div>
      )}
      {toast && (
        <div
          role="status"
          data-testid="toast"
          className="fixed bottom-4 right-4 z-50 flex max-w-xs items-start gap-2 rounded-lg border border-neutral-300 bg-white p-3 text-sm shadow-lg dark:border-neutral-700 dark:bg-neutral-900"
        >
          <span>{toast.title}</span>
          <button type="button" aria-label="Dismiss" onClick={() => setToast(null)}>
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
      )}
    </>
  );
}

export function NotificationBell() {
  const { data } = useNotifications();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const unread = data?.unread ?? 0;
  const rows = [...(data?.notifications ?? [])].reverse();

  return (
    <>
      <Button
        variant="ghost"
        size="icon"
        aria-label={unread ? `Notifications, ${unread} unread` : "Notifications"}
        data-testid="bell"
        className="relative"
        onClick={() => setOpen(true)}
      >
        <Bell className="h-4 w-4" aria-hidden />
        {unread > 0 && (
          <span
            data-testid="bell-count"
            className="absolute -right-1 -top-1 rounded-full bg-red-600 px-1.5 text-[10px] font-semibold leading-4 text-white"
          >
            {unread}
          </span>
        )}
      </Button>
      <Sheet open={open} onOpenChange={setOpen} title="Notifications" side="left" testId="bell-sheet">
        <ul className="flex flex-col gap-2 text-sm" data-testid="bell-list">
          {rows.length === 0 && <li className="text-neutral-500">Nothing yet.</li>}
          {rows.map((n) => (
            <li key={n.notification_id}>
              <Link
                href={`/c/${n.conversation_id}`}
                data-testid="bell-item"
                data-unread={n.read ? "false" : "true"}
                className="block rounded-md border border-neutral-200 px-3 py-2 hover:bg-neutral-50 dark:border-neutral-800 dark:hover:bg-neutral-800"
                onClick={() => {
                  setOpen(false);
                  void api.markRead(n.notification_id).then(() =>
                    qc.invalidateQueries({ queryKey: ["notifications"] }),
                  );
                }}
              >
                <span className={n.read ? "" : "font-semibold"}>{n.title}</span>
              </Link>
            </li>
          ))}
        </ul>
      </Sheet>
    </>
  );
}
