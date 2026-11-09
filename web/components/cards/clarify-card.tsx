"use client";

import { useChatContext } from "@/components/chat-context";
import { Button } from "@/components/ui/button";
import type { ClarifyCard as Clarify } from "@/lib/types";
import { StatusLine } from "./status-line";

export function ClarifyCard({ body, live }: { body: Clarify; live: boolean }) {
  const chat = useChatContext();
  return (
    <div data-testid="msg-clarify">
      <p className="whitespace-pre-wrap break-words">{body.question}</p>
      {(body.options ?? []).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {(body.options ?? []).map((option) => (
            <Button
              key={option}
              size="sm"
              disabled={!live}
              data-testid="quick-reply"
              onClick={() => chat.send(option)}
            >
              {option}
            </Button>
          ))}
        </div>
      )}
      <StatusLine text={body.status_line} />
    </div>
  );
}
