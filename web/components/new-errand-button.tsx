"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { Button } from "./ui/button";

export function NewErrandButton() {
  const router = useRouter();
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);
  return (
    <Button
      variant="primary"
      data-testid="new-errand"
      disabled={busy}
      onClick={async () => {
        setBusy(true);
        try {
          const result = await api.createConversation();
          if (result.ok) {
            void qc.invalidateQueries({ queryKey: ["conversations"] });
            router.push(`/c/${result.body.conversation_id}`);
          }
        } finally {
          setBusy(false);
        }
      }}
    >
      <Plus className="h-4 w-4" aria-hidden /> New errand
    </Button>
  );
}
