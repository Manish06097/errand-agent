import { BadgeCheck } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { OutcomeCard as Outcome } from "@/lib/types";
import { StatusLine } from "./status-line";

// "verified" shows only when the server says so, and the server says so only for
// *_VERIFIED outcomes. The client does not infer it from the outcome name.
export function OutcomeCard({ body }: { body: Outcome }) {
  return (
    <div data-testid="msg-outcome" data-outcome={body.outcome}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-semibold">{body.title}</h3>
        {body.verified && (
          <Badge data-testid="verified-badge" tone="good" className="gap-1">
            <BadgeCheck className="h-3 w-3" aria-hidden /> verified
          </Badge>
        )}
      </div>
      <p className="mt-1 whitespace-pre-wrap break-words">{body.text}</p>
      {(body.lines ?? []).length > 0 && (
        <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
          {(body.lines ?? []).map((line) => (
            <div key={line.label} className="contents">
              <dt className="text-neutral-600 dark:text-neutral-400">{line.label}</dt>
              <dd className="break-words">{line.value}</dd>
            </div>
          ))}
        </dl>
      )}
      <StatusLine text={body.status_line} />
    </div>
  );
}
