"use client";

import { useEffect, useState } from "react";

type Health =
  | { state: "loading" }
  | { state: "ok"; body: unknown }
  | { state: "error"; message: string };

// Fetched from the browser through the Next rewrite, so a green result proves the
// whole browser -> Next -> FastAPI path, not just that the API process is up.
export function ApiHealth() {
  const [health, setHealth] = useState<Health>({ state: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/health", { cache: "no-store", signal: controller.signal })
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        setHealth({ state: "ok", body: await res.json() });
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setHealth({ state: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => controller.abort();
  }, []);

  const badge =
    health.state === "ok"
      ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900/40 dark:text-emerald-200"
      : health.state === "error"
        ? "bg-red-100 text-red-900 dark:bg-red-900/40 dark:text-red-200"
        : "bg-neutral-100 text-neutral-700 dark:bg-neutral-800 dark:text-neutral-300";

  return (
    <section
      aria-live="polite"
      className="rounded-lg border border-neutral-200 p-4 dark:border-neutral-800"
    >
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-sm font-medium">API health</h2>
        <span data-testid="api-health-state" className={`rounded px-2 py-0.5 text-xs font-medium ${badge}`}>
          {health.state}
        </span>
      </div>
      <pre className="mt-3 overflow-x-auto text-xs text-neutral-700 dark:text-neutral-300">
        {health.state === "ok"
          ? JSON.stringify(health.body, null, 2)
          : health.state === "error"
            ? `GET /api/health failed: ${health.message}`
            : "GET /api/health ..."}
      </pre>
    </section>
  );
}
