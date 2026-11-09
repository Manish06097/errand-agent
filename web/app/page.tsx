import Link from "next/link";
import { ConversationList } from "@/components/conversation-list";
import { NotificationBell } from "@/components/notifications";
import { ApiHealth } from "./api-health";

export default function Home() {
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-10">
      <header className="flex flex-col gap-1">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">Errand agent</h1>
          <div className="flex items-center gap-3">
            <Link href="/memory" className="text-sm underline" data-testid="memory-link">
              Memory
            </Link>
            <NotificationBell />
          </div>
        </div>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          Arrange a home-service errand. The agent asks before it books anything.
        </p>
      </header>
      <ConversationList />
      <ApiHealth />
    </main>
  );
}
