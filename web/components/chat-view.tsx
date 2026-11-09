"use client";

import { useQuery } from "@tanstack/react-query";
import { Activity, Menu } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { api } from "@/lib/api";
import { useChat } from "@/lib/use-chat";
import { ChatProvider } from "./chat-context";
import { Composer } from "./composer";
import { ConversationList } from "./conversation-list";
import { DevDrawer } from "./dev-drawer";
import { NotificationBell } from "./notifications";
import { MessageList } from "./message-list";
import { StatePanel } from "./state-panel";
import { TracePanel } from "./trace-panel";
import { Button } from "./ui/button";
import { Sheet } from "./ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "./ui/tabs";

function Inspector({
  chat,
  langfuseUrl,
}: {
  chat: ReturnType<typeof useChat>;
  langfuseUrl: string | null;
}) {
  const runs = [...(chat.conv.data?.runs ?? [])];
  if (chat.streamRunId && !runs.some((r) => r.run_id === chat.streamRunId)) {
    runs.push({ run_id: chat.streamRunId, status: "running", outcome: null, trigger: null });
  }
  return (
    <Tabs defaultValue="trace" className="flex min-h-0 flex-1 flex-col">
      <TabsList>
        <TabsTrigger value="trace" data-testid="tab-trace">
          Trace
        </TabsTrigger>
        <TabsTrigger value="state" data-testid="tab-state">
          State
        </TabsTrigger>
      </TabsList>
      <TabsContent value="trace" className="min-h-0 flex-1 overflow-y-auto py-2">
        <TracePanel
          runs={runs}
          live={chat.records}
          streamRunId={chat.streamRunId}
          langfuseUrl={langfuseUrl}
        />
      </TabsContent>
      <TabsContent value="state" className="min-h-0 flex-1 overflow-y-auto py-2">
        <StatePanel conversation={chat.conv.data} />
      </TabsContent>
    </Tabs>
  );
}

export function ChatView({ conversationId }: { conversationId: string }) {
  const chat = useChat(conversationId);
  const [sidebar, setSidebar] = useState(false);
  const [inspector, setInspector] = useState(false);
  const meta = useQuery({ queryKey: ["meta"], queryFn: api.meta });
  const health = useQuery({ queryKey: ["health"], queryFn: api.health });
  const langfuse = health.data?.langfuse;
  // The link exists only when the sink is on; the dashboard's own address is the sink's.
  const langfuseUrl =
    langfuse?.state === "on" && langfuse.host ? langfuse.host : null;
  const conversation = chat.conv.data;

  return (
    <ChatProvider value={chat}>
      <div className="flex h-dvh overflow-hidden">
        <aside className="hidden w-64 shrink-0 flex-col gap-2 overflow-y-auto border-r border-neutral-200 p-3 md:flex dark:border-neutral-800">
          <ConversationList currentId={conversationId} />
        </aside>
        <Sheet open={sidebar} onOpenChange={setSidebar} title="Conversations" side="left" testId="sidebar-drawer">
          <ConversationList currentId={conversationId} />
        </Sheet>

        <main className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center justify-between gap-2 border-b border-neutral-200 px-3 py-2 dark:border-neutral-800">
            <Button
              variant="ghost"
              size="icon"
              className="md:hidden"
              aria-label="Conversations"
              data-testid="open-sidebar"
              onClick={() => setSidebar(true)}
            >
              <Menu className="h-4 w-4" aria-hidden />
            </Button>
            <h1 className="min-w-0 flex-1 truncate text-sm font-semibold">Errand {conversationId}</h1>
            <Link href="/memory" className="text-sm underline" data-testid="memory-link">
              Memory
            </Link>
            <NotificationBell />
            {meta.data?.dev && <DevDrawer />}
            <Button
              variant="ghost"
              size="icon"
              className="lg:hidden"
              aria-label="Inspector"
              data-testid="open-inspector"
              onClick={() => setInspector(true)}
            >
              <Activity className="h-4 w-4" aria-hidden />
            </Button>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-3 py-4" data-testid="chat-scroll">
            {chat.conv.isError && (
              <p role="alert" className="text-sm text-red-700">
                Could not load this conversation.
              </p>
            )}
            {conversation && conversation.messages.length === 0 && !chat.busy && (
              <p className="text-sm text-neutral-500">
                Say what you need, for example: Book a plumber in Indiranagar for my kitchen tap
                this week, mornings only, under ₹1,500.
              </p>
            )}
            {conversation && <MessageList messages={conversation.messages} />}
            {chat.notice && (
              <p role="alert" data-testid="chat-notice" className="mt-2 text-sm text-amber-800 dark:text-amber-300">
                {chat.notice}
              </p>
            )}
          </div>
          <Composer ready={conversation !== undefined} />
        </main>

        <aside
          className="hidden w-96 shrink-0 flex-col overflow-hidden border-l border-neutral-200 p-3 lg:flex dark:border-neutral-800"
          data-testid="inspector"
        >
          <Inspector chat={chat} langfuseUrl={langfuseUrl} />
        </aside>
        <Sheet open={inspector} onOpenChange={setInspector} title="Inspector" side="bottom" testId="inspector-sheet">
          <div className="flex h-[70vh] flex-col">
            <Inspector chat={chat} langfuseUrl={langfuseUrl} />
          </div>
        </Sheet>
      </div>
    </ChatProvider>
  );
}
