"use client";

import Link from "next/link";
import type { ProactiveLine as Line } from "@/lib/types";

// One line the agent wrote into this conversation from another run (a provider cancel, a
// rebooking). It carries no approval and asks nothing, so it is plain text and a link.
export function ProactiveLine({ body }: { body: Line }) {
  return (
    <p data-testid="msg-proactive" className="break-words">
      {body.text}{" "}
      <Link
        href={`/c/${body.followup_conversation_id}`}
        data-testid="open-followup"
        className="font-medium text-indigo-700 underline dark:text-indigo-300"
      >
        Open follow-up
      </Link>
    </p>
  );
}
