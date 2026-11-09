"use client";

import * as Dialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";
import { Button } from "./button";

// A small modal that asks once before something that cannot be undone.
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  children,
  confirmLabel,
  onConfirm,
  busy,
  testId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  busy?: boolean;
  testId?: string;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/40" />
        <Dialog.Content
          data-testid={testId}
          aria-describedby={undefined}
          className="fixed left-1/2 top-1/2 z-50 w-[92vw] max-w-sm -translate-x-1/2 -translate-y-1/2 rounded-xl bg-white p-4 shadow-xl dark:bg-neutral-900"
        >
          <Dialog.Title className="text-sm font-semibold">{title}</Dialog.Title>
          <div className="mt-2 text-sm text-neutral-700 dark:text-neutral-300">{children}</div>
          <div className="mt-4 flex justify-end gap-2">
            <Dialog.Close asChild>
              <Button data-testid="confirm-cancel">Cancel</Button>
            </Dialog.Close>
            <Button variant="danger" data-testid="confirm-ok" disabled={busy} onClick={onConfirm}>
              {confirmLabel}
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
