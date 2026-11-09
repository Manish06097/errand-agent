"use client";

import { useEffect, useRef } from "react";
import { agentClock } from "@/lib/agent-clock";
import { api } from "@/lib/api";
import type { MessageOut } from "@/lib/types";
import { cn } from "@/lib/utils";
import { ActionCard } from "./cards/action-card";
import { CardReminder } from "./cards/card-reminder";
import { ClarifyCard } from "./cards/clarify-card";
import { OutcomeCard } from "./cards/outcome-card";
import { ProactiveLine } from "./cards/proactive-line";
import { OutcomeNotice, VerifyingCard } from "./cards/outcome-notice";
import { ProviderComparisonCard } from "./cards/provider-comparison-card";
import { LatestMessage } from "./cards/status-line";
import { TextMessage } from "./cards/text-message";
import { useChatContext } from "./chat-context";

// Each card acks its render once, when it is actually on screen: at least half visible
// in a visible tab. The stamp is the agent clock at that moment, not the time the
// request goes out. A re-render, a remount or a second observer never acks twice.
const acked = new Set<string>();

function RenderAck({ messageId, children }: { messageId: string; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node || acked.has(messageId)) return;
    const observer = new IntersectionObserver(
      (entries) => {
        const seen = entries.some((e) => e.isIntersecting);
        if (!seen || document.visibilityState !== "visible" || acked.has(messageId)) return;
        if (!agentClock.synced) return;
        acked.add(messageId);
        observer.disconnect();
        void api.rendered(messageId, agentClock.iso());
      },
      { threshold: 0.5 },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [messageId]);
  return <div ref={ref}>{children}</div>;
}

function Body({ message, live }: { message: MessageOut; live: boolean }) {
  const body = message.body;
  switch (body.type) {
    case "text":
      return <TextMessage body={body} />;
    case "clarify":
      return <ClarifyCard body={body} live={live} />;
    case "shortlist":
      return <ProviderComparisonCard body={body} live={live} />;
    case "action":
      return <ActionCard body={body} approval={message.approval} live />;
    case "reminder":
      return <CardReminder body={body} approval={message.approval} live />;
    case "outcome":
      return <OutcomeCard body={body} />;
    case "proactive":
      return <ProactiveLine body={body} />;
    case "notice":
      return body.outcome === "PENDING_VERIFICATION" ? (
        <VerifyingCard body={body} live={live} />
      ) : (
        <OutcomeNotice body={body} live={live} verifying={false} />
      );
    default:
      return null;
  }
}

export function MessageList({ messages }: { messages: MessageOut[] }) {
  const chat = useChatContext();
  const bottom = useRef<HTMLDivElement>(null);
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");

  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [messages.length, chat.liveText, chat.queued.length, chat.activity.length]);

  return (
    <ol className="flex flex-col gap-3" data-testid="messages">
      {messages.map((message) => {
        const mine = message.role === "user";
        const isCard = message.body.type !== "text";
        const content = mine ? (
          <p className="whitespace-pre-wrap break-words">
            {message.body.type === "text" ? message.body.text : ""}
          </p>
        ) : (
          <LatestMessage.Provider value={lastAssistant?.message_id === message.message_id}>
            <Body
              message={message}
              live={!chat.busy && lastAssistant?.message_id === message.message_id}
            />
          </LatestMessage.Provider>
        );
        return (
          <li
            key={message.message_id}
            data-testid={mine ? "user-message" : "assistant-message"}
            data-message-id={message.message_id}
            className={cn(
              "max-w-[92%] rounded-2xl px-4 py-3 text-sm sm:max-w-[85%]",
              mine
                ? "self-end bg-indigo-600 text-white"
                : "self-start border border-neutral-200 bg-white dark:border-neutral-800 dark:bg-neutral-900",
            )}
          >
            {!mine && isCard ? <RenderAck messageId={message.message_id}>{content}</RenderAck> : content}
          </li>
        );
      })}
      {chat.queued.map((q) => (
        <li
          key={q.client_msg_id}
          data-testid="queued-message"
          className="max-w-[92%] self-end rounded-2xl border border-dashed border-indigo-400 px-4 py-3 text-sm text-indigo-900 dark:text-indigo-200"
        >
          <p className="whitespace-pre-wrap break-words">{q.text}</p>
          <p className="mt-1 text-xs opacity-70">queued, sends when the agent finishes</p>
        </li>
      ))}
      {chat.activity.length > 0 && (
        <li data-testid="activity" className="self-start text-xs text-neutral-600 dark:text-neutral-400">
          <ul className="flex flex-col gap-0.5">
            {chat.activity.map((line) => (
              <li key={line.id} data-testid="activity-line">
                {line.text}
              </li>
            ))}
          </ul>
        </li>
      )}
      {chat.liveText && (
        <li
          data-testid="live-text"
          className="max-w-[85%] self-start rounded-2xl border border-neutral-200 px-4 py-3 text-sm dark:border-neutral-800"
        >
          <p className="whitespace-pre-wrap break-words">{chat.liveText}</p>
        </li>
      )}
      {chat.busy && !chat.liveText && (
        <li data-testid="working" className="self-start text-xs text-neutral-500">
          Working…
        </li>
      )}
      <div ref={bottom} />
    </ol>
  );
}
