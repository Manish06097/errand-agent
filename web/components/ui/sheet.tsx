"use client";

import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

// A side or bottom sheet on Radix Dialog (shadcn/ui's Sheet, trimmed).
export function Sheet({
  open,
  onOpenChange,
  title,
  side,
  children,
  testId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  side: "left" | "right" | "bottom";
  children: ReactNode;
  testId?: string;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content
          data-testid={testId}
          aria-describedby={undefined}
          className={cn(
            "fixed z-50 flex flex-col gap-2 bg-white p-4 shadow-xl dark:bg-neutral-900",
            side === "bottom" && "inset-x-0 bottom-0 max-h-[85vh] rounded-t-xl",
            side === "left" && "inset-y-0 left-0 w-[85vw] max-w-xs",
            side === "right" && "inset-y-0 right-0 w-[90vw] max-w-sm",
          )}
        >
          <div className="flex items-center justify-between">
            <Dialog.Title className="text-sm font-semibold">{title}</Dialog.Title>
            <Dialog.Close
              aria-label="Close"
              className="rounded p-1 text-neutral-500 hover:bg-neutral-100 dark:hover:bg-neutral-800"
            >
              <X className="h-4 w-4" />
            </Dialog.Close>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
