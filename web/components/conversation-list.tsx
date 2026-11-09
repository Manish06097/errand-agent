"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { NewErrandButton } from "./new-errand-button";

export function ConversationList({ currentId }: { currentId?: string }) {
  const list = useQuery({ queryKey: ["conversations"], queryFn: api.listConversations });
  return (
    <nav aria-label="Conversations" className="flex flex-col gap-2" data-testid="conversation-list">
      <NewErrandButton />
      {list.isError && <p className="text-xs text-red-700">Could not load conversations.</p>}
      <ul className="flex flex-col gap-1">
        {(list.data ?? []).map((c) => (
          <li key={c.conversation_id}>
            <Link
              href={`/c/${c.conversation_id}`}
              data-testid="conversation-link"
              className={cn(
                "block rounded-md px-2 py-1.5 text-sm hover:bg-neutral-100 dark:hover:bg-neutral-800",
                c.conversation_id === currentId && "bg-neutral-100 dark:bg-neutral-800",
              )}
            >
              <span className="block truncate">{c.title ?? c.preview ?? "New errand"}</span>
              <span className="block truncate text-xs text-neutral-500">{c.conversation_id}</span>
            </Link>
          </li>
        ))}
      </ul>
      {list.data?.length === 0 && <p className="text-xs text-neutral-500">No errands yet.</p>}
    </nav>
  );
}
