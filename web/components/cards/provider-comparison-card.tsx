"use client";

import { Star, TriangleAlert } from "lucide-react";
import { useState } from "react";
import { useChatContext } from "@/components/chat-context";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import type { CandidateOption, ShortlistCard } from "@/lib/types";
import { StatusLine } from "./status-line";

const TOP = 3;

const BADGE_TEXT: Record<string, string> = {
  verified: "Verified",
  unverified_reviews: "Reviews unverified",
  cheapest: "Cheapest",
  top_rated: "Top rated",
};

function Candidate({ candidate, live }: { candidate: CandidateOption; live: boolean }) {
  const chat = useChatContext();
  const [pending, setPending] = useState<string | null>(null);
  const flagged = candidate.injection_flags > 0 || candidate.notice;
  return (
    <li
      data-testid="candidate"
      className="rounded-lg border border-neutral-200 p-3 dark:border-neutral-700"
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium">{candidate.name}</span>
        <span className="inline-flex items-center gap-0.5 text-xs text-neutral-600 dark:text-neutral-400">
          <Star className="h-3 w-3" aria-hidden /> {candidate.rating.toFixed(1)}
        </span>
        <span className="text-sm">₹{candidate.price_inr.toLocaleString("en-IN")}</span>
        {candidate.verified && <Badge tone="good">Verified</Badge>}
        {(candidate.badges ?? [])
          .filter((b) => b !== "verified")
          .map((b) => (
            <Badge key={b} tone={b === "unverified_reviews" ? "warn" : "info"}>
              {BADGE_TEXT[b] ?? b}
            </Badge>
          ))}
      </div>
      {flagged && (
        <p
          data-testid="injection-notice"
          className="mt-1 flex items-start gap-1 text-xs text-amber-800 dark:text-amber-300"
        >
          <TriangleAlert className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
          <span>{candidate.notice}</span>
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        {candidate.slots.map((slot) => (
          <Button
            key={slot.ref}
            size="sm"
            data-testid="slot-button"
            disabled={!live || pending !== null}
            onClick={async () => {
              setPending(slot.ref);
              try {
                await chat.action("select_slot", { slot_ref: slot.ref });
              } finally {
                setPending(null);
              }
            }}
          >
            {slot.label}
          </Button>
        ))}
      </div>
    </li>
  );
}

export function ProviderComparisonCard({ body, live }: { body: ShortlistCard; live: boolean }) {
  const [all, setAll] = useState(false);
  const shown = all ? body.candidates : body.candidates.slice(0, TOP);
  const more = body.candidates.length - TOP;
  return (
    <div data-testid="msg-shortlist">
      {body.recommendation && <p className="mb-2 whitespace-pre-wrap">{body.recommendation}</p>}
      <p className="mb-2 text-xs text-neutral-600 dark:text-neutral-400">{body.window_label}</p>
      <ul className="flex flex-col gap-2">
        {shown.map((candidate) => (
          <Candidate key={candidate.provider_id} candidate={candidate} live={live} />
        ))}
      </ul>
      {more > 0 && !all && (
        <Button size="sm" variant="ghost" className="mt-2" onClick={() => setAll(true)}>
          Show more ({more})
        </Button>
      )}
      {body.excluded_count > 0 && (
        <p data-testid="excluded-row" className="mt-2 text-xs text-neutral-600 dark:text-neutral-400">
          {body.excluded_count} excluded
        </p>
      )}
      <StatusLine text={body.status_line} />
    </div>
  );
}
