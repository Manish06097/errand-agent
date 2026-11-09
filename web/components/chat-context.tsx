"use client";

import { createContext, useContext } from "react";
import type { Chat } from "@/lib/use-chat";

const ChatContext = createContext<Chat | null>(null);

export const ChatProvider = ChatContext.Provider;

export function useChatContext(): Chat {
  const chat = useContext(ChatContext);
  if (!chat) throw new Error("useChatContext outside a ChatProvider");
  return chat;
}
