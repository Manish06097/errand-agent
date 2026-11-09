import type { TextBody } from "@/lib/types";
import { StatusLine } from "./status-line";

// Model text appears only here (15.2), as plain text: no markdown, no HTML.
export function TextMessage({ body }: { body: TextBody }) {
  return (
    <div data-testid="msg-text">
      <p className="whitespace-pre-wrap break-words">{body.text}</p>
      <StatusLine text={body.status_line} />
    </div>
  );
}
