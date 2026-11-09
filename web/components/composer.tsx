"use client";

import { SendHorizontal } from "lucide-react";
import { useEffect, useRef, type FormEvent } from "react";
import { useChatContext } from "./chat-context";
import { Button } from "./ui/button";

// Stays enabled during a run. A message sent mid-run is queued with the stamp and the
// visible card it had at Send (use-chat.ts), and goes out after run.finished.
export function Composer({ ready }: { ready: boolean }) {
  const chat = useChatContext();
  const text = chat.draft;
  const setText = chat.setDraft;
  const input = useRef<HTMLTextAreaElement>(null);

  // A notice action asked for the composer (Answer, Shorten it): focus it, cursor at the end.
  useEffect(() => {
    if (chat.focusTick === 0 || !input.current) return;
    input.current.focus();
    const end = input.current.value.length;
    input.current.setSelectionRange(end, end);
  }, [chat.focusTick]);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!text.trim() || !ready) return;
    chat.send(text);
    setText("");
  }

  return (
    <form onSubmit={submit} className="flex items-end gap-2 border-t border-neutral-200 p-3 dark:border-neutral-800">
      <textarea
        data-testid="composer-input"
        aria-label="Message"
        ref={input}
        rows={1}
        value={text}
        placeholder="What do you need arranged?"
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            submit(e);
          }
        }}
        className="max-h-32 min-h-9 flex-1 resize-none rounded-md border border-neutral-300 bg-white px-3 py-2 text-sm dark:border-neutral-700 dark:bg-neutral-900"
      />
      <Button type="submit" variant="primary" data-testid="send-button" disabled={!ready || !text.trim()}>
        <SendHorizontal className="h-4 w-4" aria-hidden /> Send
      </Button>
    </form>
  );
}
