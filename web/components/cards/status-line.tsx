// The code-written status line under a message (10.8), rendered exactly as sent.
export function StatusLine({ text }: { text: string | null | undefined }) {
  if (!text) return null;
  return (
    <p data-testid="status-line" className="mt-2 border-t border-neutral-200 pt-2 text-xs text-neutral-600 dark:border-neutral-700 dark:text-neutral-400">
      {text}
    </p>
  );
}
