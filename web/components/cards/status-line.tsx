"use client";

import { createContext, useContext } from "react";

// Whether the message being rendered is the last assistant message. The status line is
// the code-written line under the last message of a run (10.8), true when it was written;
// under an older message it would contradict what happened since ("Nothing booked yet"
// above a verified booking), so only the latest message shows it.
export const LatestMessage = createContext(true);

// The server's line, rendered exactly as sent.
export function StatusLine({ text }: { text: string | null | undefined }) {
  const latest = useContext(LatestMessage);
  if (!text || !latest) return null;
  return (
    <p data-testid="status-line" className="mt-2 border-t border-neutral-200 pt-2 text-xs text-neutral-600 dark:border-neutral-700 dark:text-neutral-400">
      {text}
    </p>
  );
}
